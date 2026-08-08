"""Pwnagotchi runtime bridge for the Flipper Zero Companion.

The plugin snapshots both UI state and Pwnagotchi's *merged runtime config* into
/run/pwnagotchi-flipper.  The BLE daemon is intentionally separate so Bluetooth
failures cannot block Pwnagotchi's UI/event threads.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

import pwnagotchi
import pwnagotchi.plugins as plugins
import pwnagotchi.ui.faces as faces

STATE_DIR = Path("/run/pwnagotchi-flipper")
STATE_FILE = STATE_DIR / "state.json"
CONFIG_FILE = STATE_DIR / "config.json"
MIN_WRITE_INTERVAL = 0.35


def _clean(value, limit=80):
    text = "" if value is None else str(value)
    text = text.replace("|", "/").replace("\r", " ").replace("\n", " ")
    return " ".join(text.split())[:limit]


def _json_default(value):
    if isinstance(value, set):
        return sorted(value, key=str)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _atomic_json(path: Path, payload):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    os.chmod(path, 0o644)


def _read_temp_c():
    try:
        value = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip())
        return f"{value / 1000.0:.0f}"
    except (OSError, ValueError):
        return "--"


def _ascii_face(raw):
    raw = str(raw or "")
    groups = [
        (("AWAKE", "COOL", "SMART"), "(o_o)"),
        (("HAPPY", "GRATEFUL", "MOTIVATED"), "(^_^)"),
        (("EXCITED", "INTENSE"), "(^o^)"),
        (("SAD", "LONELY"), "(T_T)"),
        (("ANGRY",), "(>_<)"),
        (("SLEEP", "SLEEP2"), "(-_-)"),
        (("BORED",), "(-.-)"),
        (("BROKEN", "DEBUG"), "(x_x)"),
        (("FRIEND",), "(._.)"),
    ]
    for names, ascii_face in groups:
        for name in names:
            if raw == str(getattr(faces, name, object())):
                return ascii_face
    return "(o_o)" if raw else "(-_-)"


class PwnagotchiFlipper(plugins.Plugin):
    __author__ = "adondada"
    __version__ = "0.2.1"
    __license__ = "GPL3"
    __description__ = "Exports Pwnagotchi runtime state/config for the Flipper Zero companion."

    def __init__(self):
        self._lock = threading.Lock()
        self._last_payload = None
        self._last_write = 0.0

    def on_loaded(self):
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            os.chmod(STATE_DIR, 0o755)
            logging.info("[flipper] runtime bridge loaded")
        except OSError as exc:
            logging.error("[flipper] could not create state directory: %s", exc)

    def on_config_changed(self, config):
        """Snapshot the merged config WebCFG itself receives at runtime."""
        try:
            with self._lock:
                _atomic_json(CONFIG_FILE, config)
        except (OSError, TypeError, ValueError) as exc:
            logging.warning("[flipper] runtime config snapshot failed: %s", exc)

    def on_unload(self, ui):
        logging.info("[flipper] runtime bridge unloaded")

    def on_ui_update(self, ui):
        now = time.monotonic()
        if now - self._last_write < MIN_WRITE_INTERVAL:
            return

        try:
            payload = {
                "name": _clean(pwnagotchi.name(), 22),
                "version": _clean(getattr(pwnagotchi, "__version__", ""), 20),
                "mode": _clean(ui.get("mode"), 8),
                "face": _ascii_face(ui.get("face")),
                "channel": _clean(ui.get("channel"), 10),
                "aps": _clean(ui.get("aps"), 18),
                "shakes": _clean(ui.get("shakes"), 18),
                "status": _clean(ui.get("status"), 64),
                "temp": _read_temp_c(),
                "updated": time.time(),
            }
        except Exception as exc:
            logging.debug("[flipper] state snapshot failed: %s", exc)
            return

        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._lock:
            if serialized == self._last_payload:
                self._last_write = now
                return
            try:
                _atomic_json(STATE_FILE, payload)
                self._last_payload = serialized
                self._last_write = now
            except OSError as exc:
                logging.debug("[flipper] state write failed: %s", exc)
