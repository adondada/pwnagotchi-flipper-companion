#!/usr/bin/env python3
"""BLE companion daemon between Pwnagotchi and the Flipper Zero FAP.

v0.2 is schema-driven: the Pi discovers installed plugin files, reads the merged
runtime configuration exported by pwnagotchi_flipper.py, statically inspects
plugin source for metadata/config accesses, and exposes typed options to the FAP.
Nothing about car_mode, gps, memtemp, etc. is hard-coded into the Flipper app.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import tomlkit
from bleak import BleakClient, BleakScanner

SERVICE_UUID = "8fe5b3d5-2e7f-4a98-2a48-7acc60fe0000"
FLIPPER_TX_UUID = "19ed82ae-ed21-4c9d-4145-228e61fe0000"  # Flipper -> Pi
FLIPPER_RX_UUID = "19ed82ae-ed21-4c9d-4145-228e62fe0000"  # Pi -> Flipper

DEFAULT_CONFIG = Path("/etc/pwnagotchi/flipper.toml")
PLUGIN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,63}$")
KEY_PART_RE = re.compile(r"^[A-Za-z0-9_.-]{1,63}$")
SAFE_WIRE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~ /:@[]{}(),+"
MAX_WIRE_TEXT = 150


@dataclass
class SourceOption:
    path: tuple[str, ...]
    required: bool = False
    default: Any = None
    has_default: bool = False


@dataclass
class PluginInfo:
    name: str
    path: Path | None = None
    enabled: bool = False
    version: str = ""
    description: str = ""
    author: str = ""
    source_options: dict[tuple[str, ...], SourceOption] = field(default_factory=dict)


def clean(value: Any, limit: int = 80) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    return " ".join(text.split())[:limit]


def wire(value: Any, limit: int = MAX_WIRE_TEXT, encoded_limit: int = 220) -> str:
    encoded = quote(clean(value, limit), safe=SAFE_WIRE)
    if len(encoded) <= encoded_limit:
        return encoded
    cut = encoded[:encoded_limit]
    # Never cut in the middle of a %HH escape.
    pct = cut.rfind("%")
    if pct >= 0 and len(cut) - pct < 3:
        cut = cut[:pct]
    return cut


def unwire(value: str) -> str:
    return unquote(value)


def load_settings(path: Path) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "bluetooth": {
            "device_address": "",
            "device_name_prefix": "Flipper",
            "scan_timeout": 7.0,
            "reconnect_delay": 5.0,
            "pair": True,
        },
        "state": {
            "state_file": "/run/pwnagotchi-flipper/state.json",
            "runtime_config_file": "/run/pwnagotchi-flipper/config.json",
            "push_interval": 0.7,
        },
        "pwnagotchi": {
            "config_file": "/etc/pwnagotchi/config.toml",
            "service_name": "pwnagotchi",
        },
    }
    if not path.exists():
        return settings
    with path.open("rb") as handle:
        supplied = tomllib.load(handle)
    for section, values in supplied.items():
        if isinstance(values, dict) and section in settings:
            settings[section].update(values)
    return settings


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def flatten(value: Any, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    result: dict[tuple[str, ...], Any] = {}
    if isinstance(value, dict):
        if not value and prefix:
            result[prefix] = {}
        for key, child in value.items():
            result.update(flatten(child, prefix + (str(key),)))
    else:
        result[prefix] = value
    return result


def get_nested(data: Any, path: tuple[str, ...], missing: Any = None) -> Any:
    cur = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return missing
        cur = cur[key]
    return cur


def option_type(value: Any) -> str:
    if isinstance(value, bool):
        return "b"
    if isinstance(value, int) and not isinstance(value, bool):
        return "i"
    if isinstance(value, float):
        return "f"
    if isinstance(value, (list, dict)):
        return "j"
    if value is None:
        return "s"
    return "s"


def value_to_text(value: Any, typ: str | None = None) -> str:
    typ = typ or option_type(value)
    if typ == "b":
        return "true" if bool(value) else "false"
    if typ == "j":
        return json.dumps(value, separators=(",", ":"), ensure_ascii=True)
    return "" if value is None else str(value)


def parse_typed_value(raw: str, typ: str) -> Any:
    if typ == "b":
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if typ == "i":
        return int(raw.strip())
    if typ == "f":
        return float(raw.strip())
    if typ == "j":
        return json.loads(raw)
    return raw


def _literal(node: ast.AST) -> tuple[bool, Any]:
    try:
        return True, ast.literal_eval(node)
    except Exception:
        return False, None


def _self_options_path(node: ast.AST) -> tuple[str, ...] | None:
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id == "self" and node.attr == "options":
            return ()
        return None
    if isinstance(node, ast.Subscript):
        base = _self_options_path(node.value)
        if base is None:
            return None
        ok, key = _literal(node.slice)
        if ok and isinstance(key, str):
            return base + (key,)
    return None


def inspect_plugin_source(path: Path) -> tuple[dict[str, str], dict[tuple[str, ...], SourceOption]]:
    metadata = {"version": "", "description": "", "author": ""}
    options: dict[tuple[str, ...], SourceOption] = {}
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError):
        return metadata, options

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value_node = node.value
            if value_node is None:
                continue
            ok, value = _literal(value_node)
            if not ok or not isinstance(value, (str, int, float)):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    if target.id == "__version__":
                        metadata["version"] = clean(value, 20)
                    elif target.id == "__description__":
                        metadata["description"] = clean(value, 96)
                    elif target.id == "__author__":
                        metadata["author"] = clean(value, 48)

        if isinstance(node, ast.Subscript):
            path_parts = _self_options_path(node)
            if path_parts:
                current = options.get(path_parts, SourceOption(path_parts))
                current.required = True
                options[path_parts] = current

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            base = _self_options_path(node.func.value)
            if base is not None and node.args:
                ok, key = _literal(node.args[0])
                if ok and isinstance(key, str):
                    path_parts = base + (key,)
                    current = options.get(path_parts, SourceOption(path_parts))
                    current.required = False
                    if len(node.args) > 1:
                        default_ok, default = _literal(node.args[1])
                        if default_ok:
                            current.default = default
                            current.has_default = True
                    options[path_parts] = current

    # Remove table-prefix artifacts such as "devices" when source accesses
    # self.options["devices"]["ios-phone"]["mac"].
    paths = list(options)
    for p in paths:
        if any(len(other) > len(p) and other[: len(p)] == p for other in paths):
            if p in options and not options[p].has_default:
                del options[p]
    return metadata, options


class Companion:
    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.state_file = Path(settings["state"]["state_file"])
        self.runtime_config_file = Path(settings["state"]["runtime_config_file"])
        self.pwn_config = Path(settings["pwnagotchi"]["config_file"])
        self.service_name = str(settings["pwnagotchi"]["service_name"])
        self.client: BleakClient | None = None
        self.write_lock = asyncio.Lock()
        self.command_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        self.partial = ""
        self.stop = asyncio.Event()
        self.last_state_payload = ""
        self.restart_required = False

    async def find_flipper(self):
        bt = self.settings["bluetooth"]
        address = clean(bt.get("device_address", ""), 32)
        timeout = float(bt.get("scan_timeout", 7.0))
        if address:
            logging.info("Looking for configured Flipper %s", address)
            return await BleakScanner.find_device_by_address(address, timeout=timeout)

        prefix = str(bt.get("device_name_prefix", "Flipper"))
        logging.info("Scanning for a BLE device named %s*", prefix)
        devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
        for _address, (device, advertisement) in devices.items():
            name = device.name or advertisement.local_name or ""
            if name.startswith(prefix):
                logging.info("Found %s (%s)", name, device.address)
                return device
        return None

    def notification_callback(self, _characteristic, data: bytearray):
        try:
            self.partial += bytes(data).decode("utf-8", errors="ignore")
            while "\n" in self.partial:
                line, self.partial = self.partial.split("\n", 1)
                line = line.rstrip("\r")[:485]
                if line:
                    try:
                        self.command_queue.put_nowait(line)
                    except asyncio.QueueFull:
                        logging.warning("Command queue full; dropping Flipper command")
        except Exception as exc:
            logging.debug("BLE receive error: %s", exc)

    async def send(self, line: str):
        client = self.client
        if not client or not client.is_connected:
            return
        payload = line if line.endswith("\n") else line + "\n"
        raw = payload.encode("utf-8")
        if len(raw) > 486:
            raise ValueError("protocol packet exceeds Flipper serial BLE limit")
        async with self.write_lock:
            await client.write_gatt_char(FLIPPER_RX_UUID, raw, response=True)
        # Give the FAP main loop time to drain its line queue during bulk scans.
        await asyncio.sleep(0.018)

    def read_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def read_runtime_config(self) -> dict[str, Any]:
        runtime: dict[str, Any] = {}
        try:
            data = json.loads(self.runtime_config_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                runtime = data
        except (OSError, json.JSONDecodeError):
            pass

        # Overlay the currently saved TOML on the last runtime snapshot. This is
        # important after a Flipper edit: Pwnagotchi has not restarted yet, so
        # config.json may still contain the previous runtime value.
        try:
            with self.pwn_config.open("rb") as handle:
                saved = tomllib.load(handle)
            if isinstance(saved, dict):
                return deep_merge(runtime, saved) if runtime else saved
        except (OSError, tomllib.TOMLDecodeError):
            pass
        return runtime

    async def send_state(self, force: bool = False):
        state = self.read_state()
        if not state:
            line = "S|name=Pwnagotchi|mode=----|face=(-_-)|ch=-|aps=0|shakes=0|temp=--|status=Waiting for UI bridge"
        else:
            line = (
                "S|name={name}|mode={mode}|face={face}|ch={channel}|aps={aps}|"
                "shakes={shakes}|temp={temp}|status={status}"
            ).format(
                name=clean(state.get("name"), 22),
                mode=clean(state.get("mode"), 8),
                face=clean(state.get("face"), 14),
                channel=clean(state.get("channel"), 10),
                aps=clean(state.get("aps"), 16),
                shakes=clean(state.get("shakes"), 16),
                temp=clean(state.get("temp"), 8),
                status=clean(state.get("status"), 58),
            )
        if force or line != self.last_state_payload:
            await self.send(line)
            self.last_state_payload = line

    def load_pwn_doc(self):
        if not self.pwn_config.exists():
            raise FileNotFoundError(self.pwn_config)
        return tomlkit.parse(self.pwn_config.read_text(encoding="utf-8"))

    def save_pwn_doc(self, doc):
        self.pwn_config.parent.mkdir(parents=True, exist_ok=True)
        backup = self.pwn_config.with_suffix(self.pwn_config.suffix + ".flipper.bak")
        if self.pwn_config.exists():
            shutil.copy2(self.pwn_config, backup)
        tmp = self.pwn_config.with_suffix(self.pwn_config.suffix + ".flipper.tmp")
        tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
        try:
            mode = self.pwn_config.stat().st_mode & 0o777
            os.chmod(tmp, mode)
        except OSError:
            os.chmod(tmp, 0o644)
        os.replace(tmp, self.pwn_config)

    @staticmethod
    def ensure_table(parent, key: str):
        if key not in parent or not hasattr(parent[key], "items"):
            parent[key] = tomlkit.table()
        return parent[key]

    def plugin_paths(self) -> list[Path]:
        result: list[Path] = []
        spec = importlib.util.find_spec("pwnagotchi")
        if spec and spec.origin:
            default_dir = Path(spec.origin).resolve().parent / "plugins" / "default"
            if default_dir.is_dir():
                result.append(default_dir)
        runtime = self.read_runtime_config()
        custom = get_nested(runtime, ("main", "custom_plugins"), "")
        if custom:
            custom_path = Path(str(custom))
            if custom_path.is_dir() and custom_path not in result:
                result.append(custom_path)
        return result

    def discover_plugins(self) -> dict[str, PluginInfo]:
        runtime = self.read_runtime_config()
        configured = get_nested(runtime, ("main", "plugins"), {})
        configured = configured if isinstance(configured, dict) else {}
        result: dict[str, PluginInfo] = {}

        for directory in self.plugin_paths():
            for path in sorted(directory.glob("*.py")):
                name = path.stem
                if not PLUGIN_RE.fullmatch(name):
                    continue
                metadata, src_opts = inspect_plugin_source(path)
                info = result.setdefault(name, PluginInfo(name=name))
                info.path = path
                info.version = metadata["version"]
                info.description = metadata["description"]
                info.author = metadata["author"]
                info.source_options = src_opts

        # Include config-only entries too, but mark them as missing source.
        for name, options in configured.items():
            name = str(name)
            if not PLUGIN_RE.fullmatch(name):
                continue
            info = result.setdefault(name, PluginInfo(name=name))
            if isinstance(options, dict):
                info.enabled = bool(options.get("enabled", False))

        return result

    def plugin_option_records(self, plugin: PluginInfo) -> list[tuple[tuple[str, ...], Any, str, bool]]:
        runtime = self.read_runtime_config()
        options = get_nested(runtime, ("main", "plugins", plugin.name), {})
        options = options if isinstance(options, dict) else {}
        flat = flatten(options)
        flat.pop(("enabled",), None)

        merged: dict[tuple[str, ...], tuple[Any, str, bool]] = {}
        for path, value in flat.items():
            if path and all(KEY_PART_RE.fullmatch(part) for part in path):
                merged[path] = (value, option_type(value), False)

        for path, source_opt in plugin.source_options.items():
            if not path or not all(KEY_PART_RE.fullmatch(part) for part in path):
                continue
            if path in merged:
                value, typ, _ = merged[path]
                merged[path] = (value, typ, source_opt.required)
            elif source_opt.has_default:
                merged[path] = (source_opt.default, option_type(source_opt.default), False)
            else:
                merged[path] = (None, "s", source_opt.required)

        return [(path, *merged[path]) for path in sorted(merged, key=lambda p: ".".join(p).lower())]

    async def send_plugins(self):
        plugins = self.discover_plugins()
        ordered = sorted(plugins.values(), key=lambda p: p.name.lower())
        await self.send(f"PB|count={min(len(ordered), 64)}")
        for info in ordered[:64]:
            options = self.plugin_option_records(info)
            required_missing = sum(1 for _path, value, _typ, req in options if req and value in (None, ""))
            await self.send(
                "P|{name}|{enabled}|{found}|{opts}|{missing}|{version}".format(
                    name=wire(info.name, 63),
                    enabled=1 if info.enabled else 0,
                    found=1 if info.path else 0,
                    opts=len(options),
                    missing=required_missing,
                    version=wire(info.version, 20),
                )
            )
        await self.send(f"PE|count={min(len(ordered), 64)}")

    async def send_plugin_detail(self, name: str):
        plugins = self.discover_plugins()
        info = plugins.get(name)
        if not info:
            raise ValueError("plugin not found")
        options = self.plugin_option_records(info)
        required_missing = sum(1 for _path, value, _typ, req in options if req and value in (None, ""))
        await self.send(
            "OB|{name}|{enabled}|{found}|{count}|{missing}|{version}|{desc}|{source}".format(
                name=wire(info.name, 63),
                enabled=1 if info.enabled else 0,
                found=1 if info.path else 0,
                count=min(len(options), 64),
                missing=required_missing,
                version=wire(info.version, 20),
                desc=wire(info.description or "No description", 88, 110),
                source=wire(str(info.path) if info.path else "config only / source missing", 110, 160),
            )
        )
        for path, value, typ, required in options[:64]:
            text = value_to_text(value, typ)
            await self.send(
                "O|{typ}|{required}|{key}|{value}".format(
                    typ=typ,
                    required=1 if required else 0,
                    key=wire(".".join(path), 100, 170),
                    value=wire(text, 150, 230),
                )
            )
        await self.send(f"OE|count={min(len(options), 64)}")

    async def toggle_plugin(self, name: str):
        if not PLUGIN_RE.fullmatch(name):
            raise ValueError("invalid plugin name")
        discovered = self.discover_plugins()
        if name not in discovered:
            raise ValueError("plugin not found")
        doc = self.load_pwn_doc()
        main = self.ensure_table(doc, "main")
        plugins = self.ensure_table(main, "plugins")
        table = self.ensure_table(plugins, name)
        enabled = bool(table.get("enabled", discovered[name].enabled))
        table["enabled"] = not enabled
        self.save_pwn_doc(doc)
        self.restart_required = True
        await self.send(f"A|{wire(name, 24)} {'enabled' if not enabled else 'disabled'} - restart needed")
        await self.send("R|1")
        await self.send_plugins()
        await self.send_plugin_detail(name)

    async def set_plugin_option(self, plugin_name: str, rel_path: str, typ: str, encoded_value: str):
        if not PLUGIN_RE.fullmatch(plugin_name):
            raise ValueError("invalid plugin name")
        parts = tuple(unwire(rel_path).split("."))
        if not parts or any(not KEY_PART_RE.fullmatch(part) for part in parts) or parts == ("enabled",):
            raise ValueError("invalid option path")
        discovered = self.discover_plugins()
        info = discovered.get(plugin_name)
        if not info:
            raise ValueError("plugin not found")
        valid_paths = {path for path, _v, _t, _r in self.plugin_option_records(info)}
        if parts not in valid_paths:
            raise ValueError("option was not discovered")
        if typ not in {"b", "i", "f", "s", "j"}:
            raise ValueError("unsupported option type")
        value = parse_typed_value(unwire(encoded_value), typ)

        doc = self.load_pwn_doc()
        main = self.ensure_table(doc, "main")
        plugins = self.ensure_table(main, "plugins")
        cursor = self.ensure_table(plugins, plugin_name)
        for part in parts[:-1]:
            cursor = self.ensure_table(cursor, part)
        cursor[parts[-1]] = value
        self.save_pwn_doc(doc)
        self.restart_required = True
        await self.send(f"A|Saved {wire(plugin_name + '.' + '.'.join(parts), 38)}")
        await self.send("R|1")
        await self.send_plugin_detail(plugin_name)

    async def systemctl(self, action: str):
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            action,
            self.service_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode:
            raise RuntimeError(stderr.decode(errors="ignore").strip() or f"systemctl {action} failed")

    async def handle_command(self, line: str):
        logging.debug("RX %s", line)
        parts = line.split("|")
        command = parts[0].upper()
        try:
            if command == "HELLO":
                name = clean(self.read_state().get("name", "Pwnagotchi"), 24)
                await self.send(f"A|Connected to {wire(name, 24)}")
                await self.send(f"R|{1 if self.restart_required else 0}")
            elif command == "GET" and len(parts) >= 2:
                target = parts[1].upper()
                if target == "ALL":
                    await self.send_state(force=True)
                    await self.send(f"R|{1 if self.restart_required else 0}")
                elif target == "PLUGINS":
                    await self.send_plugins()
                elif target == "PLUGIN" and len(parts) == 3:
                    await self.send_plugin_detail(unwire(parts[2]))
                else:
                    raise ValueError("unknown GET target")
            elif command == "SETOPT" and len(parts) == 5:
                await self.set_plugin_option(unwire(parts[1]), parts[2], parts[3], parts[4])
            elif command == "TOGGLE" and len(parts) == 3 and parts[1].upper() == "PLUGIN":
                await self.toggle_plugin(unwire(parts[2]))
            elif command == "ACTION" and len(parts) == 2:
                action = parts[1].lower()
                if action == "restart":
                    await self.send("A|Restarting Pwnagotchi")
                    self.restart_required = False
                    await self.send("R|0")
                    await asyncio.sleep(0.2)
                    await self.systemctl("restart")
                elif action == "reboot":
                    await self.send("A|Rebooting Pi")
                    await asyncio.sleep(0.4)
                    proc = await asyncio.create_subprocess_exec("systemctl", "reboot")
                    await proc.wait()
                else:
                    raise ValueError("unknown system action")
            else:
                raise ValueError("unknown command")
        except Exception as exc:
            logging.warning("Command failed (%s): %s", line, exc)
            await self.send(f"E|{wire(str(exc), 64)}")

    async def command_worker(self):
        while not self.stop.is_set():
            try:
                line = await asyncio.wait_for(self.command_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            await self.handle_command(line)

    async def state_worker(self):
        interval = max(0.25, float(self.settings["state"].get("push_interval", 0.7)))
        while not self.stop.is_set():
            await self.send_state()
            await asyncio.sleep(interval)

    async def run_connection(self, device):
        pair = bool(self.settings["bluetooth"].get("pair", True))
        logging.info("Connecting to %s; pair=%s", device.address, pair)
        async with BleakClient(device, pair=pair, timeout=60.0) as client:
            self.client = client
            self.partial = ""
            self.last_state_payload = ""
            await client.start_notify(FLIPPER_TX_UUID, self.notification_callback)
            logging.info("Connected to Flipper %s", device.address)
            workers = [
                asyncio.create_task(self.command_worker()),
                asyncio.create_task(self.state_worker()),
            ]
            try:
                while client.is_connected and not self.stop.is_set():
                    await asyncio.sleep(0.5)
            finally:
                for task in workers:
                    task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                try:
                    await client.stop_notify(FLIPPER_TX_UUID)
                except Exception:
                    pass
                self.client = None
                logging.info("Flipper disconnected")

    async def run(self):
        delay = max(1.0, float(self.settings["bluetooth"].get("reconnect_delay", 5.0)))
        while not self.stop.is_set():
            try:
                device = await self.find_flipper()
                if not device:
                    logging.info("No matching Flipper found")
                else:
                    await self.run_connection(device)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.warning("BLE cycle failed: %s", exc)
            if not self.stop.is_set():
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass


async def async_main(config_path: Path):
    settings = load_settings(config_path)
    companion = Companion(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, companion.stop.set)
        except NotImplementedError:
            pass
    await companion.run()


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s pwnagotchi-flipper: %(message)s",
    )
    try:
        asyncio.run(async_main(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
