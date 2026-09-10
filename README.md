# Matter HVAC Thermostat (Raspberry Pi)

A custom thermostat that turns a Raspberry Pi and a relay HAT into a full-featured HVAC controller you can operate from your phone — natively, through Apple Home or Google Home, via Matter.

Built to be boring and reliable: no cloud, no app to install, no YAML to fiddle with. It registers itself as a Matter thermostat through [matterbridge](https://github.com/Luligu/matterbridge) and switches your HVAC with real hardware relays that fail safe.

## Why it's special

- **Native phone control via Matter** — works in the iOS/Android Home apps right out of the box. [matterbridge](https://github.com/Luligu/matterbridge) + the `matterbridge-mqtt` plugin act as the Matter bridge; this project handles everything on the Pi side.
- **No configuration files to write** — the thermostat self-registers with the matterbridge-mqtt plugin via a single retained MQTT message. No YAML.
- **Multi-zone sensing** — multiple wireless Govee H5075 BLE temperature sensors are pooled sensibly: it heats the **coldest** room and cools the **hottest** room.
- **Optional outdoor temperature** — configure one extra Govee sensor for outside. It is purely informational: its reading shows on the dashboard, history graph, and is exposed natively to Apple Home / Google Home via Matter (`outdoorTemperature`), but is never averaged with or allowed to influence the room temperatures.
- **Protects your HVAC equipment** — 120-second minimum dwell between state changes (compressor protection), a 60-second startup delay, and normally-open relays so everything is OFF on power loss or reboot.
- **Fails safe on bad data** — if sensor data goes stale, the HVAC is forced off instead of guessing.
- **Setpoint protection** — heat and cool setpoints are kept at least 8 °F apart automatically.
- **Automatic daily schedule** — systemd timers set the thermostat to a day profile (68/76 °F) at 6am and a night profile (67/75 °F) at 11pm.
- **Built-in web interface** — a local Flask dashboard with a 24-hour temperature history graph (including the outdoor temperature when configured) and a color-coded HVAC action bar.
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
                     │   │ (matterbridge   │      │ (Flask WebUI)   │   │                  │
                     │   │  mqtt bridge)   │      │                 │   │                  │
                     │   └──────┬──────────┘      └─────────────────┘   │                  │
                     │          │ MQTT             (port 5000)          │                  │
                     └──────────┼───────────────────────────────────────┘
                                ▼
                     ┌──────────────────────────────────────────────────┐
                     │                  Home server                       │
                     │                                                    │
                     │ ┌──────────────────────┐  ┌──────────────────────┐ │
                     │ │ MQTT Broker          │  │ matterbridge         │ │
                     │ │ (mosquitto)          │  │ + matterbridge-mqtt  │ │
                     │ └──────────────────────┘  │ (Matter bridge)      │ │
                     │                           └──────────────────────┘ │
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

# Configure (sensor MACs, optional outdoor_sensor MAC, mosquitto/matterbridge broker address, setpoints)
#   edit config/defaults.json → this becomes /etc/thermostat/defaults.json

# Install and enable all six services
sudo make install
sudo systemctl daemon-reload
sudo systemctl enable --now thermostat-setup.service \
  thermostat-sensor.service thermostat-control.service \
  thermostat-gpio.service thermostat-mqtt.service \
  thermostat-web.service

# Enable the 6am / 11pm setpoint schedule timers
sudo systemctl enable --now thermostat-schedule-morning.timer \
  thermostat-schedule-night.timer
```

### Using it

- **Web UI** — open `http://<pi-ip>:5000` for the local control panel and 24-hour history graph.
- **Matter (phone apps)** — the thermostat self-registers with the matterbridge-mqtt plugin over MQTT; pair matterbridge in Apple Home / Google Home and the thermostat appears natively. No YAML.

## Local demo mode (try the UI without hardware)

You can run the web dashboard locally with canned sensor data — no sensors, GPIOs, relays, or production install required — to see how the HTML, JavaScript, and Flash-free API feel:

```bash
cd /path/to/repo
PYTHONPATH=src venv/bin/python src/web.py --demo --host 127.0.0.1 --port 5000
```

Then open **http://127.0.0.1:5000**. The demo generates a realistic 24-hour temperature history and HVAC action bar, and keeps feeding new samples so the page stays live. Mode, fan, and setpoint changes made in the UI are reflected in the simulation. Add `--data-dir /tmp/demo-state` to keep the simulated state in a specific directory, or drop `--demo` to run against the real `/run/thermostat` IPC as before.

## For developers

The repository's **[AGENTS.md](AGENTS.md)** is the sole canonical technical
reference — it documents the service internals, IPC file formats, configuration
schema, module map, GPIO pinout, MQTT topics, tuning constants, and packaging
details.

## License

All rights reserved. Custom project.
