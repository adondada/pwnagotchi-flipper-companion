#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run this uninstaller with sudo." >&2
  exit 1
fi

CONFIG="/etc/pwnagotchi/config.toml"
PWN_PYTHON="${PWN_PYTHON:-/home/pi/.pwn/bin/python}"

systemctl disable --now pwnagotchi-flipper.service 2>/dev/null || true
rm -f /etc/systemd/system/pwnagotchi-flipper.service
rm -rf /opt/pwnagotchi-flipper
systemctl daemon-reload

if [[ -x "$PWN_PYTHON" && -f "$CONFIG" ]]; then
  CUSTOM_PATH="$("$PWN_PYTHON" - "$CONFIG" <<'PY'
import sys, tomlkit
from pathlib import Path
doc = tomlkit.parse(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(str(doc.get("main", {}).get("custom_plugins", "/usr/local/share/pwnagotchi/custom-plugins")))
PY
)"
  rm -f "$CUSTOM_PATH/pwnagotchi_flipper.py"
  "$PWN_PYTHON" - "$CONFIG" <<'PY'
import os, sys, tomlkit
from pathlib import Path
path = Path(sys.argv[1])
doc = tomlkit.parse(path.read_text(encoding="utf-8"))
try:
    doc["main"]["plugins"]["pwnagotchi_flipper"]["enabled"] = False
except (KeyError, TypeError):
    pass
tmp = path.with_suffix(path.suffix + ".flipper-uninstall.tmp")
tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
os.chmod(tmp, path.stat().st_mode & 0o777)
os.replace(tmp, path)
PY
  systemctl restart pwnagotchi.service || true
fi

echo "Removed daemon/service/plugin. /etc/pwnagotchi/flipper.toml and config backups were intentionally kept."
