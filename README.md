# Pwnagotchi Companion for Flipper Zero

Wireless **Pwnagotchi display and configuration controller** for Flipper Zero over BLE. No GPIO cable and no Wi-Fi dev board required.

> **Status:** experimental. Back up `/etc/pwnagotchi/config.toml` before changing plugin settings.

## v0.2.1

- Live Pwnagotchi status on the Flipper's 128×64 screen.
- Dynamic plugin discovery. The FAP does not hard-code `car_mode`, `gps`, `memtemp`, or any other plugin.
- Per-plugin configuration browsing and editing.
- Safe static inspection of plugin option requirements using Python `ast`, without importing arbitrary plugin modules.
- BLE TX queueing and a 1.5 s recovery watchdog to prevent a stuck false `BLE busy` state.
- Atomic TOML writes with automatic backup.
- Restart Pwnagotchi and reboot Pi actions with confirmation.

## Architecture

```text
Pwnagotchi UI
    |
    | state/config snapshots
    v
pwnagotchi_flipper.py
    |
    v
flipperd.py  <---- BLE GATT ---->  Pwnagotchi Companion.fap
                                      |
                                      +-- Live dashboard
                                      +-- Dynamic plugins
                                      +-- Config editor
                                      +-- System actions
```

The Raspberry Pi is the BLE central/client. The Flipper runs its serial BLE profile. The bridge plugin only exports Pwnagotchi state/config snapshots; Bluetooth handling lives in a separate systemd daemon so BLE failures do not block the Pwnagotchi UI thread.

## Features

### Live dashboard

- Pwnagotchi name and face
- AUTO/MANU mode
- channel
- AP count
- handshake count
- Pi temperature
- status text

### Dynamic plugin manager

Opening **Plugins** sends `GET|PLUGINS` to the Pi. The daemon then scans:

- Pwnagotchi's built-in plugin directory
- `main.custom_plugins`
- the merged runtime config exported by the bridge plugin
- safe `self.options[...]` / `self.options.get(...)` references found through AST parsing

The Flipper menu is generated from the result. Install a plugin and it appears on the next scan; remove it and it disappears.

Supported option types:

- boolean
- integer
- float
- string
- JSON list/object

Required options inferred from direct `self.options[...]` indexing are marked when missing.

### Config safety

The Flipper cannot submit arbitrary TOML paths. `SETOPT` is accepted only for plugin option paths discovered from runtime config or safe AST inspection.

Before each write, the daemon refreshes:

```text
/etc/pwnagotchi/config.toml.flipper.bak
```

and replaces the config atomically.

Changes may require a Pwnagotchi restart. The Flipper shows `restart *` until **System → Apply + restart** is used.

## Repository layout

```text
pwnagotchi-flipper-companion/
├── flipper/
│   ├── application.fam
│   ├── build.sh
│   └── pwnagotchi_companion.c
├── pwnagotchi/
│   ├── flipperd.py
│   ├── pwnagotchi_flipper.py
│   ├── flipper.toml.example
│   ├── pwnagotchi-flipper.service
│   ├── requirements.txt
│   ├── install.sh
│   └── uninstall.sh
├── protocol/
│   └── PROTOCOL.md
├── RELEASE_NOTES_v0.2.1.md
├── LICENSE
└── README.md
```

## Requirements

### Flipper Zero

- Flipper Zero with microSD
- Bluetooth enabled
- firmware compatible with the uFBT SDK used to build the FAP
- `ufbt` installed on the build computer

### Pwnagotchi

- jayofelony Pwnagotchi
- working BlueZ/Bluetooth on the Pi
- `/etc/pwnagotchi/config.toml`
- Pwnagotchi Python environment, normally `/home/pi/.pwn/bin/python`
- systemd

## Build the Flipper app

```bash
python3 -m pip install --upgrade ufbt
cd flipper
./build.sh
```

The built FAP is placed under `flipper/dist/`. Copy it to the Flipper SD card, for example:

```text
/apps/Bluetooth/pwnagotchi_companion.fap
```

## Install the Pi side

```bash
cd pwnagotchi
chmod +x install.sh
sudo ./install.sh
```

Re-running the installer upgrades the daemon and bridge while preserving an existing `/etc/pwnagotchi/flipper.toml`.

Logs:

```bash
sudo journalctl -u pwnagotchi-flipper -f
```

## BLE configuration

Example `/etc/pwnagotchi/flipper.toml`:

```toml
[bluetooth]
device_address = "AA:BB:CC:DD:EE:FF"
device_name_prefix = "Flipper"
scan_timeout = 7.0
reconnect_delay = 5.0
pair = false

[state]
state_file = "/run/pwnagotchi-flipper/state.json"
runtime_config_file = "/run/pwnagotchi-flipper/config.json"
push_interval = 0.7

[pwnagotchi]
config_file = "/etc/pwnagotchi/config.toml"
service_name = "pwnagotchi"
```

After the first manual pairing, using the fixed BLE address is recommended. If `bluetoothctl` still owns the active connection immediately after pairing, disconnect it once and restart the daemon:

```bash
bluetoothctl disconnect AA:BB:CC:DD:EE:FF
sudo systemctl restart pwnagotchi-flipper
```

## Controls

### Live

```text
OK      menu
BACK    exit
```

### Plugins

```text
UP/DOWN  select plugin
OK       open selected plugin
RIGHT    rescan plugins
BACK     menu
```

Plugin symbols:

```text
[x]  enabled
[ ]  disabled
 ?   config exists but source file was not found
 !   one or more required settings are missing
```

### Plugin details

```text
UP/DOWN     select option
LEFT/RIGHT  toggle boolean or quick-adjust number
OK          toggle boolean / exact-edit other values
BACK        plugin list
```

Strings, exact numbers, and JSON values use Flipper's native TextInput keyboard.

### System

```text
Refresh state
Rescan plugins
Apply + restart
Reboot Pi
```

Restart/reboot require confirmation.

## iPhone tethering

The intended topology is:

```text
iPhone <-- Bluetooth PAN --> Pwnagotchi <-- BLE GATT --> Flipper Zero
```

The companion does not modify the iPhone PAN configuration. The Flipper BLE connection carries only companion state and control traffic.

## Troubleshooting

### `Waiting for Pi...`

```bash
sudo systemctl status pwnagotchi-flipper
sudo journalctl -u pwnagotchi-flipper -f
bluetoothctl info <FLIPPER_ADDRESS>
```

If the Flipper is already `Connected: yes` immediately after manual pairing, disconnect it once and restart the daemon so `flipperd` can become the GATT client.

### Plugins stays on scanning

Watch the daemon while pressing RIGHT in Plugins:

```bash
sudo journalctl -u pwnagotchi-flipper -f
```

Also verify the bridge created:

```bash
ls -l /run/pwnagotchi-flipper/state.json
ls -l /run/pwnagotchi-flipper/config.json
```

## Protocol

See [`protocol/PROTOCOL.md`](protocol/PROTOCOL.md).

## License

GPL-3.0. See [`LICENSE`](LICENSE).
