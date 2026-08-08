# Pwnagotchi Companion BLE protocol

Protocol version: **2**

The Flipper app starts Flipper Zero's serial BLE profile. The Raspberry Pi is the BLE central/client. Messages are newline-terminated and each BLE packet remains below the Flipper serial-profile limit.

Plugin/config fields use percent encoding for `|`, `%`, non-printable bytes, and other characters that would interfere with framing.

## Flipper -> Pi

```text
HELLO|2
GET|ALL
GET|PLUGINS
GET|PLUGIN|car_mode
TOGGLE|PLUGIN|car_mode
SETOPT|car_mode|hop_period|f|0.25
SETOPT|bt-tether|devices.ios-phone.mac|s|AA:BB:CC:DD:EE:FF
ACTION|restart
ACTION|reboot
```

`GET|PLUGINS` is deliberately **not** sent on connection. The Flipper requests a scan only when the Plugins page is opened/refreshed.

`SETOPT` only accepts a plugin and option path that the daemon discovered from the merged runtime config or from safe static analysis of that plugin's `self.options` accesses. It cannot be used as a general arbitrary TOML write primitive.

## Pi -> Flipper

Live state:

```text
S|name=adondada|mode=AUTO|face=(^_^)|ch=6|aps=37|shakes=4|temp=58|status=looking around
```

Plugin scan boundaries:

```text
PB|count=23
...
PE|count=23
```

Each discovered plugin:

```text
P|car_mode|1|1|8|0|6.2.0
```

Fields are:

```text
P|name|enabled|source_found|option_count|required_missing|version
```

Opening a plugin starts a detail block:

```text
OB|car_mode|1|1|8|0|6.2.0|Car mode plugin|/usr/local/share/pwnagotchi/custom-plugins/car_mode.py
O|f|1|hop_period|0.25
O|i|0|min_rssi|-75
O|s|1|some_required_key|
OE|count=8
```

Option types:

- `b` boolean
- `i` integer
- `f` float
- `s` string
- `j` JSON list/object

The second `O` field is `1` when static source inspection indicates the plugin directly indexes that option and therefore expects it to exist. Missing required values are marked on the Flipper.

Config changes set a restart-needed flag:

```text
R|1
```

After `ACTION|restart`:

```text
R|0
```

Acknowledgement/error:

```text
A|Saved car_mode.hop_period
E|option was not discovered
```

## Discovery model

The daemon enumerates `.py` files from Pwnagotchi's built-in plugin directory and the configured `main.custom_plugins` directory. It does not import them. Python `ast` parsing is used to collect metadata (`__version__`, `__description__`, `__author__`) and option access patterns (`self.options[...]` / `self.options.get(...)`). These are merged with the runtime configuration snapshot exported by the companion Pwnagotchi plugin.
