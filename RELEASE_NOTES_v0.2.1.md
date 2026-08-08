# v0.2.1

First public release of **Pwnagotchi Flipper Companion**.

## Highlights

- Wireless Pwnagotchi status display on Flipper Zero over BLE
- Dynamic plugin discovery from the Pwnagotchi instead of hard-coded plugin menus
- Per-plugin configuration browsing and editing
- Plugin metadata / option requirement discovery without importing plugin modules
- Live Pwnagotchi status updates
- BLE TX queueing and timeout recovery to prevent a stuck `BLE busy` state
- Safe config writes with backup support
- System actions for applying configuration and restarting the Pwnagotchi

## Components

- `flipper/` — Flipper Zero FAP source
- `pwnagotchi/` — BLE bridge daemon, Pwnagotchi state plugin, installer and systemd unit
- `protocol/` — Companion BLE protocol documentation

## Notes

- Build the FAP with uFBT using `cd flipper && ./build.sh`.
- Install the Pi side with `cd pwnagotchi && sudo ./install.sh`.
- The Flipper and Pi must be paired over Bluetooth before the daemon can connect.
- The project is experimental. Keep a backup of `/etc/pwnagotchi/config.toml` before changing plugin settings.
