#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run this installer with sudo." >&2
  exit 1
fi

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="/etc/pwnagotchi/config.toml"
COMPANION_CONFIG="/etc/pwnagotchi/flipper.toml"
INSTALL_DIR="/opt/pwnagotchi-flipper"
UNIT_DST="/etc/systemd/system/pwnagotchi-flipper.service"

PWN_PYTHON="${PWN_PYTHON:-/home/pi/.pwn/bin/python}"
if [[ ! -x "$PWN_PYTHON" ]]; then
  echo "Expected Pwnagotchi Python at /home/pi/.pwn/bin/python." >&2
  echo "Set PWN_PYTHON=/path/to/python and run again." >&2
  exit 1
fi
if [[ ! -f "$CONFIG" ]]; then
  echo "Cannot find $CONFIG" >&2
  exit 1
fi

echo "[1/7] Installing Python dependencies into the Pwnagotchi virtual environment..."
"$PWN_PYTHON" -m pip install -r "$HERE/requirements.txt"

echo "[2/7] Finding the active Pwnagotchi custom plugin directory..."
CUSTOM_PATH="$("$PWN_PYTHON" - "$CONFIG" <<'PY'
import sys
import tomlkit
from pathlib import Path
path = Path(sys.argv[1])
doc = tomlkit.parse(path.read_text(encoding="utf-8"))
main = doc.get("main", {})
print(str(main.get("custom_plugins", "/usr/local/share/pwnagotchi/custom-plugins")))
PY
)"
mkdir -p "$CUSTOM_PATH"
cp "$HERE/pwnagotchi_flipper.py" "$CUSTOM_PATH/pwnagotchi_flipper.py"
chmod 0644 "$CUSTOM_PATH/pwnagotchi_flipper.py"

echo "[3/7] Enabling the state-bridge plugin without trampling the rest of config.toml..."
"$PWN_PYTHON" - "$CONFIG" "$CUSTOM_PATH" <<'PY'
import os
import shutil
import sys
from pathlib import Path
import tomlkit

path = Path(sys.argv[1])
custom_path = sys.argv[2]
doc = tomlkit.parse(path.read_text(encoding="utf-8"))
if "main" not in doc or not hasattr(doc["main"], "items"):
    doc["main"] = tomlkit.table()
main = doc["main"]
main["custom_plugins"] = custom_path
if "plugins" not in main or not hasattr(main["plugins"], "items"):
    main["plugins"] = tomlkit.table()
plugins = main["plugins"]
if "pwnagotchi_flipper" not in plugins or not hasattr(plugins["pwnagotchi_flipper"], "items"):
    plugins["pwnagotchi_flipper"] = tomlkit.table()
plugins["pwnagotchi_flipper"]["enabled"] = True
backup = path.with_suffix(path.suffix + ".before-flipper")
shutil.copy2(path, backup)
tmp = path.with_suffix(path.suffix + ".flipper-install.tmp")
tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
os.chmod(tmp, path.stat().st_mode & 0o777)
os.replace(tmp, path)
print(f"Backup: {backup}")
PY

echo "[4/7] Installing BLE daemon..."
mkdir -p "$INSTALL_DIR" /etc/pwnagotchi
cp "$HERE/flipperd.py" "$INSTALL_DIR/flipperd.py"
cp "$HERE/requirements.txt" "$INSTALL_DIR/requirements.txt"
chmod 0755 "$INSTALL_DIR/flipperd.py"
if [[ ! -f "$COMPANION_CONFIG" ]]; then
  cp "$HERE/flipper.toml.example" "$COMPANION_CONFIG"
  chmod 0644 "$COMPANION_CONFIG"
fi

echo "[5/7] Installing systemd service..."
cp "$HERE/pwnagotchi-flipper.service" "$UNIT_DST"
# Keep the repository service readable, while making the installed unit follow a custom venv path.
sed -i "s#^ExecStart=.*#ExecStart=${PWN_PYTHON} ${INSTALL_DIR}/flipperd.py --config ${COMPANION_CONFIG}#" "$UNIT_DST"
systemctl daemon-reload
systemctl enable pwnagotchi-flipper.service >/dev/null

echo "[6/7] Restarting Pwnagotchi so the state bridge loads..."
systemctl restart pwnagotchi.service

echo "[7/7] Starting the Flipper BLE daemon..."
systemctl restart pwnagotchi-flipper.service

cat <<EOF

Installed.
  Plugin:  $CUSTOM_PATH/pwnagotchi_flipper.py
  Daemon:  $INSTALL_DIR/flipperd.py
  Config:  $COMPANION_CONFIG
  Service: pwnagotchi-flipper.service

Next: build/open the Pwnagotchi Companion FAP on the Flipper, then pair the Pi and Flipper once with bluetoothctl as described in README.md.
Logs: journalctl -u pwnagotchi-flipper -f
EOF
