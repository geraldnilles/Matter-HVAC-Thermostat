# Matter HVAC Thermostat (Raspberry Pi)

A custom Matter-compatible HVAC thermostat built on a Raspberry Pi. This project transforms a Raspberry Pi plus a relay HAT into a full-featured thermostat that can be controlled natively from iOS and Android devices via the Matter protocol. Home Assistant acts as the Matter bridge, and MQTT connects the Pi to Home Assistant.

The software is modular ("Unix-like"): six independent `systemd` services handle sensing, decision-making, actuation, and UI, communicating through atomic file-based IPC in a `tmpfs` directory.

## Features

- **Matter integration** — Control from native iOS/Android apps through Home Assistant's Matter bridge (MQTT Climate entity)
- **Multi-zone sensing** — Multiple Govee H5075 BLE temperature sensors, pooled with smart aggregation (heat the coldest room, cool the hottest room)
- **Auto / Heat / Cool / Off modes** with `Auto` fan mode support
- **Hardware safety** — Normally-open relays (all OFF on power loss/reboot), 60-second startup delay, 60-second minimum state dwell time, and a data-failsafe that kills the HVAC if sensor data goes stale
- **Setpoint protection** — Automatic 7°F minimum separation between heat and cool setpoints
- **Home Assistant auto-discovery** — No YAML required; the device registers itself as a Climate entity
- **Built-in WebUI** — Flask-based local interface with REST API and a 24-hour temperature history graph
- **Yocto-friendly packaging** — Standard `make install` with `DESTDIR`/`PREFIX`/`SYSCONFDIR`/`UNITDIR` overrides

## Architecture

```
                     ┌──────────────────────────────────────────────────┐
                     │                    Raspberry Pi                   │
                     │                                                  │
  ┌──────────────┐   │   ┌─────────────────┐      ┌─────────────────┐   │   ┌──────────────────┐
  │ Govee H5075  │───┼──▶│ thermostat-sensor│─────▶│                 │   │   │                  │
  │ BLE sensors  │BLE │   │  (BLE scanner)  │      │                 │   │   │   HVAC relays    │
  └──────────────┘   │   └─────────────────┘      │                 │   │   │  (Fan/Comp/Heat) │
                     │          │ temps           │                 │   │   │                  │
                     │          ▼                 │                 │◀────┼──┘  gpioset (libgpiod)
                     │   ┌─────────────────┐      │  thermostat-     │   │                  │
                     │   │ thermostat-     │      │  control (brain) │   │                  │
                     │   │ control         │─────▶│  + thermostat-   │   │                  │
                     │   └─────────────────┘      │  gpio (muscle)   │   │                  │
                     │          ▲                 │                 │   │                  │
                     │          │ setpoints       │                 │   │                  │
                     │   ┌──────┴──────────┐      │                 │   │                  │
                     │   │ /run/thermostat │      └─────────────────┘   │                  │
                     │   │  (tmpfs IPC)    │            ▲               │                  │
                     │   └──────┬──────────┘            │ state         │                  │
                     │          │ setpoints             │               │                  │
                     │   ┌──────┴──────────┐      ┌─────┴───────────┐   │                  │
                     │   │ thermostat-mqtt │      │ thermostat-web  │   │                  │
                     │   │   (HA bridge)   │      │ (Flask WebUI)   │   │                  │
                     │   └──────┬──────────┘      └─────────────────┘   │                  │
                     │          │ MQTT             (port 5000)          │                  │
                     └──────────┼───────────────────────────────────────┘
                                ▼
                     ┌──────────────────┐        ┌──────────────────┐
                     │  MQTT Broker     │◀──────▶│ Home Assistant   │
                     │  (mosquitto)     │  MQTT  │  (Matter bridge) │
                     └──────────────────┘        └──────────────────┘
                                                        │ Matter
                                                        ▼
                                            ┌──────────────────────┐
                                            │  iOS / Android home  │
                                            │  apps (native Home)  │
                                            └──────────────────────┘
```

### Service overview

All daemons are managed by `systemd` and started in dependency order:

| Service | Role | Startup order |
| --- | --- | --- |
| `thermostat-setup` | One-shot init; seeds `/run/thermostat/` from `/etc/thermostat/defaults.json` | 1 (after filesystems) |
| `thermostat-sensor` | Scans Govee BLE sensors, maintains rolling averages + 24 h history, writes `current_temp`/`min_temp`/`max_temp` | 2 (after setup + bluetooth) |
| `thermostat-control` | The "brain": implements thermostat logic, hysteresis, safety timers; writes `hvac_action` | 3 (after setup + sensor) |
| `thermostat-gpio` | The "muscle": reads `hvac_action`, drives GPIO relays via `gpioset`; 60 s boot delay | 4 (after setup + control) |
| `thermostat-mqtt` | Home Assistant bridge: MQTT Climate entity with Matter-compatible attributes | 5 (after setup + control) |
| `thermostat-web` | Flask WebUI + REST API on `0.0.0.0:5000` | 5 (after setup + control) |

### Inter-process communication

Services share state via the `tmpfs` filesystem at `/run/thermostat/`. All writers use **atomic writes** (unique PID-based temp file → `fsync` → `os.replace`) so concurrent writers (e.g., MQTT and WebUI setting a setpoint at the same time) can never corrupt state or expose partial reads.

Key IPC files:

| File | Writer(s) | Content |
| --- | --- | --- |
| `current_temp` | sensor | Average temperature of all valid sensors (°F) |
| `min_temp` | sensor | Lowest valid sensor reading (°F) — used for heating |
| `max_temp` | sensor | Highest valid sensor reading (°F) — used for cooling |
| `history.json` | sensor | 24 h ring buffer (1-minute samples, Unix epoch timestamps) |
| `system_mode` | mqtt, web | `off`, `cool`, `heat`, `auto` |
| `fan_mode` | mqtt, web | `auto`, `on` |
| `set_temp_cool` | mqtt, web, control | Cooling setpoint (°F) |
| `set_temp_heat` | mqtt, web, control | Heating setpoint (°F) |
| `hvac_action` | control | `idle`, `heating`, `cooling`, `fan` |

## Hardware

### Requirements

- Raspberry Pi (any model with BLE; Pi 3/4/5 recommended)
- Relay HAT/board with 3 channels (Fan, Compressor, Heat) — relays must be **normally open** so the system defaults to OFF on power loss
- 1× [Govee H5075](https://www.govee.com/) Bluetooth temperature/humidity sensor (wireless, battery powered)
- 24 VAC HVAC system (or a bench setup for testing)

### GPIO pinout

Pins are driven **active-high** via `gpioset` (libgpiod). On service stop, all pins are forced LOW (OFF).

| Component | GPIO (BCM) | Color code |
| --- | ---: | --- |
| Fan | 20 | G (Green) |
| Compressor | 21 | Y (Yellow) |
| Heat | 26 | W (White) |

### Temperature sensors

The system reads [Govee H5075](https://www.govee.com/) BLE sensors (manufacturer ID `0xEC88` / `60552`):

- Only MAC addresses listed in the config allowlist are processed; unknown devices are ignored
- Temperature is decoded from the manufacturer-specific advertisement data (24-bit signed value: `temp°C × 10000 + humidity% × 10`)
- Each sensor keeps a 2-minute rolling average to smooth noise; stale sensors (>2 min) are excluded automatically
- Sensor "failure tolerance": system keeps running as long as ≥1 sensor is valid; if all sensors go stale, HVAC is forced off for safety

## Installation

### 1. Dependencies

The software runs on standard Debian/Raspbian (and Yocto). Python 3.10+ and `libgpiod` (for the `gpioset` command) are required.

```bash
sudo apt install python3 python3-pip libgpiod2
pip3 install -r requirements.txt    # flask, paho-mqtt, bleak
```

### 2. Configure

Edit `config/defaults.json` before installing (or edit `/etc/thermostat/defaults.json` after):

```json
{
  "sensors": {
    "A4:C1:38:00:00:01": "Living Room",
    "A4:C1:38:00:00:02": "Bedroom"
  },
  "system_mode": "off",
  "fan_mode": "auto",
  "set_temp_cool": 76.0,
  "set_temp_heat": 68.0,
  "mqtt": {
    "broker": "homeassistant.lan",
    "port": 1883,
    "username": "",
    "password": ""
  }
}
```

- **`sensors`** — MAC-address allowlist mapped to human-readable room names. Add your Govee H5075 MACs here (discoverable via a BLE scanner such as `hcitool lescan` or the `bleak` examples).
- **`mqtt`** — Broker host, port, and optional credentials. Leave `username`/`password` empty (`""`) for brokers without authentication.
- Temperatures are in **°F**. `set_temp_cool`/`set_temp_heat` are the boot defaults; the control daemon enforces a 7°F minimum gap between them.

### 3. Install

```bash
sudo make install
```

For cross-packaging (e.g., Yocto), override the standard variables:

```bash
make install DESTDIR=$STAGING_DIR PREFIX=/usr SYSCONFDIR=/etc UNITDIR=/lib/systemd/system
```

This installs:

| Component | Destination |
| --- | --- |
| Python daemons | `/usr/share/thermostat/` |
| WebUI template | `/usr/share/thermostat/templates/` |
| Configuration | `/etc/thermostat/defaults.json` |
| systemd units | `/etc/systemd/system/` |

### 4. Enable & start

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now thermostat-setup.service
sudo systemctl enable --now thermostat-sensor.service
sudo systemctl enable --now thermostat-control.service
sudo systemctl enable --now thermostat-gpio.service
sudo systemctl enable --now thermostat-mqtt.service
sudo systemctl enable --now thermostat-web.service
```

All services restart automatically on failure (`Restart=always`), and the GPIO daemon waits an extra 60 s after boot before actuating relays (compressor safety).

## Home Assistant & Matter Integration

The `thermostat-mqtt` daemon registers a **Climate** entity with Home Assistant using MQTT Discovery — no YAML configuration needed. In Home Assistant it appears as a climate device called *Thermostat*.

### Setting up the Matter bridge

1. Install the [Home Assistant Matter (Matter Server) integration](https://www.home-assistant.io/integrations/matter/).
2. Ensure the MQTT broker (e.g., Mosquitto) is running and configured in `defaults.json`.
3. Start the thermostat services; the device publishes retained discovery to `homeassistant/climate/thermostat/config`.
4. In Home Assistant, add the climate entity to your Matter bridge — it becomes available to Apple Home / Google Home and native iOS/Android apps.

### Matter-compatible attributes

The MQTT state topic (`thermostat/state`, published every 5 s) uses Matter-aligned attribute names so Home Assistant maps them cleanly to the Matter thermostat cluster:

| Matter attribute | MQTT JSON key |
| --- | --- |
| `LocalTemperature` | `local_temperature` |
| `SystemMode` | `system_mode` |
| `FanMode` | `fan_mode` |
| `OccupiedCoolingSetpoint` | `occupied_cooling_setpoint` |
| `OccupiedHeatingSetpoint` | `occupied_heating_setpoint` |
| `ThermostatRunningState` | `thermostat_running_state` |

### MQTT topics

| Topic | Direction | Payload |
| --- | --- | --- |
| `thermostat/state` | out | JSON state (Matter attributes above) |
| `thermostat/availability` | out (retained) | `online` / `offline` |
| `homeassistant/climate/thermostat/config` | out (retained) | HA discovery payload |
| `thermostat/mode/set` | in | `off`, `cool`, `heat`, `auto` |
| `thermostat/fan/set` | in | `auto`, `on` |
| `thermostat/cool/set` | in | float (°F) |
| `thermostat/heat/set` | in | float (°F) |

## Thermostat Logic

- **Heating** compares `min_temp` (coldest room) to `set_temp_heat`; **cooling** compares `max_temp` (hottest room) to `set_temp_cool`
- **Hysteresis**: ±0.5°F centered on the setpoint (e.g., heat set to 70°F → ON below 69.5°F, OFF above 70.5°F)
- **Auto mode conflict resolution**: if both heat and cool demand exist simultaneously, the larger deviation from setpoint wins (avoids rapid switching)
- **`fan_mode: on`** runs the fan whenever the system is idle (air circulation), even in `system_mode: off`
- **Minimum dwell time**: 60 seconds between any state transitions — overrides even manual user changes (compressor protection)
- **Setpoint separation**: heat/cool setpoints closer than 7°F are symmetrically expanded to a 7°F gap; effective values are written back to IPC so the UI reflects reality
- **Data failsafe**: if no fresh sensor data exists, all relays are forced OFF

## Web Interface

The thermostat includes a local Flask WebUI bound to `0.0.0.0:5000`:

- **`GET /`** — HTML control panel with live temperature, mode/setpoint controls, and a 24-hour history graph (auto-refreshes every 30 s)
- **`GET /api/state`** — full state as JSON
- **`POST /api/mode`** — `{"mode": "off"|"cool"|"heat"|"auto"}`
- **`POST /api/fan`** — `{"fan": "auto"|"on"}`
- **`POST /api/setpoint`** — `{"type": "cool"|"heat", "value": <float>}`

## Repository Layout

```text
.
├── Makefile                 # Standard install target (DESTDIR/PREFIX/SYSCONFDIR/UNITDIR)
├── README.md                # This file
├── requirements.txt         # Python dependencies (flask, paho-mqtt, bleak)
├── spec.md                  # Detailed system specification
├── icon.svg                 # Thermostat icon
├── config/
│   └── defaults.json        # Default settings + sensor allowlist
├── src/
│   ├── utils.py             # Shared constants, IPC helpers, atomic writes
│   ├── setup.py             # One-shot init: defaults.json → /run/thermostat/
│   ├── sensor.py            # BLE sensor daemon (Govee H5075 decoding, aggregation, history)
│   ├── control.py           # Control daemon (thermostat logic, hysteresis, safety timers)
│   ├── gpio.py              # GPIO daemon (gpioset relay actuation, failsafe)
│   ├── mqtt.py              # MQTT daemon (HA discovery + Matter-compatible state)
│   ├── web.py               # Flask WebUI + REST API daemon
│   └── templates/
│       └── index.html       # WebUI template
└── systemd/
    ├── thermostat-setup.service
    ├── thermostat-sensor.service
    ├── thermostat-control.service
    ├── thermostat-gpio.service
    ├── thermostat-mqtt.service
    └── thermostat-web.service
```

## Development

- The canonical reference is [`spec.md`](spec.md) — it documents the system design, IPC format standards, and daemon behaviors in detail. If you change behavior, update both the code and the spec.
- Run daemons manually for testing: `python3 src/sensor.py`, etc. (they expect the config at `/etc/thermostat/defaults.json` and IPC dir `/run/thermostat/`).
- All IPC files must be written **atomically** (see `utils.atomic_write`); use unique PID-based temp files + `os.replace`.
- Scalar IPC files are UTF-8 text with a single trailing newline; timestamps in `history.json` are Unix epoch seconds.

## License

All rights reserved. Custom project.
