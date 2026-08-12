# Matter HVAC Thermostat (Raspberry Pi)

A custom thermostat that turns a Raspberry Pi and a relay HAT into a full-featured HVAC controller you can operate from your phone — natively, through Apple Home or Google Home, via Matter.

Built to be boring and reliable: no cloud, no app to install, no YAML to fiddle with. It just shows up in Home Assistant as a climate device, publishes its state over MQTT, and switches your HVAC with real hardware relays that fail safe.

## Why it's special

- **Native phone control via Matter** — works in the iOS/Android Home apps right out of the box. Home Assistant acts as the Matter bridge; this project handles everything on the Pi side.
- **No configuration files to write** — Home Assistant auto-discovery registers the thermostat as a Climate entity. No YAML.
- **Multi-zone sensing** — multiple wireless Govee H5075 BLE temperature sensors are pooled sensibly: it heats the **coldest** room and cools the **hottest** room.
- **Protects your HVAC equipment** — 120-second minimum dwell between state changes (compressor protection), a 60-second startup delay, and normally-open relays so everything is OFF on power loss or reboot.
- **Fails safe on bad data** — if sensor data goes stale, the HVAC is forced off instead of guessing.
- **Setpoint protection** — heat and cool setpoints are kept at least 8 °F apart automatically.
- **Built-in web interface** — a local Flask dashboard with a 24-hour temperature history graph.
- **Yocto-friendly** — standard `make install` with `DESTDIR`/`PREFIX` overrides for cross-packaging.

## How it's built

The software follows a "Unix-like" philosophy: instead of one big program, it is six small `systemd` services with one job each — sensing, decision-making, actuation, and UI — communicating through atomic file-based IPC in a `tmpfs` directory.

```
                     ┌──────────────────────────────────────────────────┐
                     │                    Raspberry Pi                   │
                     │                                                  │
  ┌──────────────┐   │   ┌─────────────────┐      ┌─────────────────┐   │   ┌──────────────────┐
  │ Govee H5075  │───┼──▶│ thermostat-sensor│─────▶│                 │   │   │                  │
  │ BLE sensors  │BLE │   │  (BLE scanner)  │      │                 │   │   │   HVAC relays    │
  └──────────────┘   │   └─────────────────┘      │                 │   │   │  (Fan/Comp/Heat) │
                     │          │ temps           │                 │   │   │                  │
                     │          ▼                 │                 │   │◀────┼──┘  gpioset (libgpiod)
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
                     ┌──────────────────────────────────────────────────┐
                     │       Home Assistant device (separate host)        │
                     │                                                    │
                     │ ┌──────────────────────┐  ┌──────────────────────┐ │
                     │ │ MQTT Broker          │  │ Home Assistant       │ │
                     │ │ (Mosquitto on the    │  │ (Matter bridge)      │ │
                     │ │ HA device)           │  │                      │ │
                     │ └──────────────────────┘  └──────────────────────┘ │
                     │                                                    │
                     │                      │ Matter                      │
                     │                         ▼                          │
                     │          ┌─────────────────────────────┐           │
                     │          │ iOS / Android home apps     │           │
                     │          │ (native Home)               │           │
                     │          └─────────────────────────────┘           │
                     └──────────────────────────────────────────────────┘
```

The daemons start in dependency order — setup → sensor → control → gpio/mqtt/web — and every part is restartable independently.

## Hardware you'll need

- Raspberry Pi (any model with BLE; Pi 3/4/5 recommended)
- Relay HAT/board with 3 channels (Fan, Compressor, Heat) — relays must be **normally open** so the system is OFF by default on power loss
- 1+ [Govee H5075](https://www.govee.com/) BLE temperature/humidity sensors
- A 24 VAC HVAC system (or a bench setup for testing)

## Getting started

```bash
# Dependencies (Debian/Raspbian)
sudo apt install python3 python3-pip libgpiod2
pip3 install -r requirements.txt          # flask, paho-mqtt, bleak

# Configure (sensor MACs, MQTT broker on your Home Assistant device, setpoints)
#   edit config/defaults.json → this becomes /etc/thermostat/defaults.json

# Install and enable all six services
sudo make install
sudo systemctl daemon-reload
sudo systemctl enable --now thermostat-setup.service \
  thermostat-sensor.service thermostat-control.service \
  thermostat-gpio.service thermostat-mqtt.service \
  thermostat-web.service
```

### Using it

- **Web UI** — open `http://<pi-ip>:5000` for the local control panel and 24-hour history graph.
- **Home Assistant** — the thermostat registers itself as a *Thermostat* climate device via MQTT discovery; no YAML needed.
- **Matter (phone apps)** — install the Home Assistant Matter server integration (its MQTT broker/Mosquitto runs on the Home Assistant device, not the Pi), then add the thermostat's climate entity to your Matter bridge. It becomes available in Apple Home / Google Home on iOS and Android.

## For developers

The repository contains a detailed technical reference — service internals, IPC file formats, module map, GPIO pinout, MQTT topics, tuning constants, and packaging details:

➡️ **[AGENTS.md](AGENTS.md)**

The canonical system design is also spec'd in **[spec.md](spec.md)**.

## License

All rights reserved. Custom project.
