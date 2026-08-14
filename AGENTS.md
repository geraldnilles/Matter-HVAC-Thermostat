# AGENTS.md — Matter HVAC Thermostat (Technical Reference)

Technical guide for developers and coding agents working on this repository. It documents how the system is structured, where each piece of functionality lives, and the invariants that must not be broken.

> **Canonical reference:** [`spec.md`](spec.md) is the authoritative system specification (architecture, IPC format standards, daemon behaviors). If you change behavior, update **both** the code and the spec. This file is the quick-orientation map; `spec.md` is the deep dive.

## What this project is

A Matter-compatible HVAC thermostat running on a Raspberry Pi. A relay HAT provides the actual switching; the software is six independent `systemd` services that communicate via atomic file-based IPC in a `tmpfs` directory (`/run/thermostat/`). Home Assistant bridges MQTT → Matter so the thermostat appears natively in iOS/Android Home apps.

## Runtime architecture

### Service overview & startup order

| Order | Service | Role | Unit |
|---|---|---|---|
| 1 | `thermostat-setup` | One-shot bootstrap; seeds `/run/thermostat/` from `/etc/thermostat/defaults.json` | `systemd/thermostat-setup.service` |
| 2 | `thermostat-sensor` | BLE scan + aggregation; writes temps + 24 h history; waits for time-sync.target | `systemd/thermostat-sensor.service` |
| 3 | `thermostat-control` | The "brain"; thermostat logic, hysteresis, safety timers; writes `hvac_action` | `systemd/thermostat-control.service` |
| 4 | `thermostat-gpio` | The "muscle"; reads `hvac_action`, drives relays via `gpioset`; extra 60 s boot delay (`ExecStartPre`) | `systemd/thermostat-gpio.service` |
| 5 | `thermostat-mqtt` | Home Assistant bridge; MQTT Climate entity with Matter-aligned attributes | `systemd/thermostat-mqtt.service` |
| 5 | `thermostat-web` | Flask WebUI + REST API on `0.0.0.0:5000` | `systemd/thermostat-web.service` |
| 6 | `thermostat-schedule` | Timer-driven setpoint profiles (6am / 11pm) | `systemd/thermostat-schedule-*.service` + `*.timer` |

Dependency ordering: setup → sensor → control → gpio/mqtt/web. All daemons use `Restart=always` (5 s restarts). The GPIO service's `ExecStartPre=/bin/sleep 60` provides compressor-protection boot delay. The MQTT service connects to a remote broker on the Home Assistant device and has no local broker dependency.

### Inter-process communication (IPC)

All shared state lives in `/run/thermostat/` (tmpfs). **Every write must be atomic** — use `utils.atomic_write()` (PID-suffixed temp file → `flush` + `fsync` → `os.replace`). Never write IPC files directly.

| File | Writer(s) | Content |
|---|---|---|
| `current_temp` | sensor | Average °F across all valid sensors |
| `min_temp` | sensor | Coldest valid sensor °F — drives heating |
| `max_temp` | sensor | Hottest valid sensor °F — drives cooling |
| `history.json` | sensor | 24 h ring buffer, 1-min samples, `{"t": epoch, "avg": °F, "sensors": {name: °F}, "set_temp_cool": °F, "set_temp_heat": °F, "hvac_action": string}` (action + setpoints omitted when absent) |
| `system_mode` | mqtt, web | `off` \| `cool` \| `heat` \| `auto` |
| `fan_mode` | mqtt, web | `auto` \| `on` |
| `set_temp_cool` | mqtt, web, control, schedule | Cooling setpoint °F |
| `set_temp_heat` | mqtt, web, control, schedule | Heating setpoint °F |
| `hvac_action` | control | `idle` \| `heating` \| `cooling` \| `fan` |

Format rules (spec §5.1.1): scalar files are UTF-8 text with exactly one trailing newline; structured files are compact JSON (`json.dumps(..., separators=(',', ':'))`).

## Where to look (module map)

| Functionality | Location |
|---|---|
| IPC paths, atomic write/read helpers, scalar+JSON readers/writers | `src/utils.py` — **read this first** |
| Boot-time seeding of IPC from `defaults.json`; one-shot service | `src/setup.py` |
| Govee H5075 BLE decoding, allowlist, rolling averages, aggregation, 24 h history, failure→failsafe | `src/sensor.py` |
| Hysteresis, mode logic, auto conflict resolution, 8 °F setpoint gap, 120 s dwell, 60 s startup delay, data failsafe | `src/control.py` |
| Relay actuation (`gpioset`, libgpiod v1/v2 auto-detect), pin mapping, shutdown failsafe to OFF | `src/gpio.py` |
| HA MQTT discovery payload, state publication, command subscription | `src/mqtt.py` |
| Flask WebUI + REST API endpoints, history graph | `src/web.py`, `src/templates/index.html` |
| Hardware-free canned-data simulator for the WebUI (local testing) | `src/demo.py` |
| Timer-driven setpoint profile helper (one-shot, snap + atomic write) | `src/schedule.py` |
| Default config: sensor MAC allowlist, initial modes/setpoints, MQTT broker | `config/defaults.json` |
| systemd units and ordering (services + timers) | `systemd/*.service`, `systemd/*.timer` |
| Install/packaging rules (`DESTDIR`/`PREFIX`/`SYSCONFDIR`/`UNITDIR`) | `Makefile` |
| Python deps: `flask`, `paho-mqtt`, `bleak` | `requirements.txt` |
| Full system design doc | `spec.md` |

## Hardware interface (spec §2)

- **Relays:** 3 channels (Fan, Compressor, Heat), **normally open** → all OFF on power loss/reboot. Driven **active-high** via the `gpioset` CLI (libgpiod).
- **GPIO (BCM numbering):** Fan = 20 (G), Compressor = 21 (Y), Heat = 26 (W). Chip: `gpiochip0`.
- **Relay state map:**

  | `hvac_action` | Fan | Compressor | Heat |
  |---|---|---|---|
  | `heating` | 1 | 0 | 1 |
  | `cooling` | 1 | 1 | 0 |
  | `fan` | 1 | 0 | 0 |
  | `idle` | 0 | 0 | 0 |

- On daemon shutdown (SIGTERM/SIGINT) or error, **all pins are forced LOW** via `set_all_low()`.
- **Govee H5075 decoding** (`src/sensor.py`, `_decode_govee_temp`): manufacturer ID `0xEC88` (60552); bytes 1–3 are a signed 24-bit value where `raw = temp°C × 10000 + humidity% × 10`. Humidity is stripped (divide by 1000), sign bit is `0x800000`, result divided by 10 to get °C, converted to °F. Only MACs in the allowlist (normalize with `.upper()`) are processed.

## Tuning constants (be careful changing these)

All defined as module-level constants in the daemons. They are safety- or comfort-critical:

| Constant | Value | Where |
|---|---|---|
| `SENSOR_TIMEOUT` | 120 s | stale-sensor threshold |
| `ROLLING_WINDOW` | 120 s | per-sensor rolling average |
| `HISTORY_INTERVAL` | 60 s | history sampling rate |
| `HISTORY_MAX_ENTRIES` | 1440 | 24 h at 1-min |
| `CLOCK_JUMP_THRESHOLD` | 10.0 s | wall-vs-mono drift treated as an NTP clock step |
| `SCANNER_WATCHDOG_TIMEOUT` | 45.0 s | restart BleakScanner if no advertisements for this long |
| `HYSTERESIS` | 0.5 °F | ±0.5 °F around setpoint (1 °F swing) |
| `MIN_SETPOINT_GAP` | 8.0 °F | heat/cool separation (auto-expands symmetrically around midpoint) |
| `MIN_DWELL_TIME` | 120 s | minimum time between state transitions — overrides even manual commands (compressor protection) |
| `STARTUP_DELAY` | 60 s | control daemon startup safety delay |
| `POLL_INTERVAL` | 1.0 s | control/gpio loop cadence |
| MQTT `POLL_INTERVAL` | 5.0 s | state topic publish cadence |
| sensor main loop sleep | 5 s | scan/aggregation cadence |
| `SET_TEMP_COOL` / `SET_TEMP_HEAT` defaults | 74.0 / 70.0 °F | fallbacks in control/mqtt/web when IPC file missing |

## Thermostat logic invariants (`src/control.py`)

- **Heating** compares `min_temp` (coldest room) vs `set_temp_heat`; **cooling** compares `max_temp` vs `set_temp_cool`.
- Hysteresis: ON below `set − 0.5`, OFF above `set + 0.5`. Active state uses "continue until satisfied" logic; inactive state uses demand detection.
- **Auto mode conflict:** if both heat and cool demand, the larger deviation from setpoint wins; an already-active action takes priority.
- **`fan_mode: on`** → `hvac_action = fan` whenever the system would otherwise be idle, even in `system_mode: off`.
- **Data failsafe:** missing `min_temp`/`max_temp` → force `idle`.
- Setpoint separation is re-derived from local IPC values on every cycle; adjusted setpoints are **written back** to IPC so the UI/MQTT show effective values. Setpoint separation expands symmetrically around a midpoint **snapped to the nearest whole degree** (see `utils.round_degree()`); all setpoint writes (MQTT, WebUI) are snapped to the nearest whole degree to prevent fractional artifacts, and control snaps its IPC inputs on every cycle so stale fractional values self-heal.
- The 120 s dwell timer blocks *all* state changes (checked before any write) — it is not bypassed by user commands.
- **Post-cycle fan purge:** When heating or cooling satisfies demand, the daemon transitions to `hvac_action = fan` for `MIN_DWELL_TIME` (120 s) before returning to `idle` (if `fan_mode: auto`), extracting residual thermal energy from the HVAC unit.

## MQTT interface (`src/mqtt.py`)

Topics:

| Topic | Direction | Payload |
|---|---|---|
| `thermostat/state` | out (5 s) | JSON state |
| `thermostat/availability` | out, retained | `online` / `offline` |
| `homeassistant/climate/thermostat/config` | out, retained | HA discovery payload |
| `thermostat/mode/set` | in | `off` \| `cool` \| `heat` \| `auto` |
| `thermostat/fan/set` | in | `auto` \| `on` |
| `thermostat/cool/set` | in | float °F |
| `thermostat/heat/set` | in | float °F |

State payload uses **Matter-aligned attribute names** so HA maps them to the Matter thermostat cluster:

| Matter attribute | MQTT JSON key |
|---|---|
| `LocalTemperature` | `local_temperature` |
| `SystemMode` | `system_mode` |
| `FanMode` | `fan_mode` |
| `OccupiedCoolingSetpoint` | `occupied_cooling_setpoint` |
| `OccupiedHeatingSetpoint` | `occupied_heating_setpoint` |
| `ThermostatRunningState` | `thermostat_running_state` |

MQTT broker config comes from the `mqtt` section of `defaults.json` (broker, port, username/password); falls back to `homeassistant.lan:1883`. On connect: publishes availability, discovery, initial state, subscribes commands. On shutdown: publishes `offline` retained.

> **Do NOT add `fan_mode_*` keys back to the HA discovery payload.** They make Home Assistant set the `FAN_MODE` supported feature, which causes `home-assistant-matter-hub` to classify the device as a Matter **Room Air Conditioner** (`0x0072`) instead of a plain **Thermostat** (`0x002A`). The `fan_mode` value is still published in `thermostat/state`, and `thermostat/fan/set` remains subscribed, so fan control continues working outside of HA discovery. See `spec.md` §4.4.

## WebUI REST API (`src/web.py`)

- `GET /` — HTML dashboard (auto-refresh 30 s) with live temp, mode/setpoint controls, 24 h history graph (room lines + bold average + dashed heat/cool setpoint lines) plus a horizontal HVAC action bar below it (color-coded heating/cooling/fan/idle segments on the same time scale) and a "Current Room Temperatures" table at the bottom (each room's live temp from the latest `history.json` `sensors` map); setpoint controls use a single +/- button pair that adjusts heat and cool setpoints simultaneously
- `GET /api/state` — full state as JSON (includes `history`)
- `POST /api/mode` — `{"mode": "off"|"cool"|"heat"|"auto"}`
- `POST /api/fan` — `{"fan": "auto"|"on"}`
- `POST /api/setpoint` — `{"type": "cool"|"heat", "value": <float>}` (single-setpoint override; retained for compatibility)
- `POST /api/setpoints` — `{"delta": <float>}` — adjusts both setpoints together (used by WebUI +/- buttons)

### Local demo mode (no hardware)

The WebUI can run in a fully local, hardware-free demo mode that feeds it canned sensor data. `src/demo.py` generates a realistic per-room temperature model, a populated 24 h history ring buffer, and a hysteresis/dwell-aware `hvac_action`, then keeps generating samples so the page auto-refresh feels live. Mode/fan/setpoint changes made in the UI are written to the demo data directory and picked up by the simulator on its next tick, so the dashboard is fully interactive.

```bash
cd /path/to/repo
# isolated demo: fresh temp dir, localhost only
PYTHONPATH=src venv/bin/python src/web.py --demo --host 127.0.0.1 --port 5000

# keep the simulated state around in a chosen directory
PYTHONPATH=src venv/bin/python src/web.py --demo --data-dir /tmp/demo-state
```

Flags supported by `src/web.py`: `--host`, `--port`, `--data-dir` (IPC/test data directory), and `--demo` (canned data, no sensors/GPIOs/relays). With no arguments the daemon behaves exactly as before: it reads/writes `/run/thermostat` on `0.0.0.0:5000`.

## Setpoint schedule (systemd timers)

Two `oneshot` services triggered by systemd **timer** units apply a fixed daily
setpoint profile by running `src/schedule.py --heat X --cool Y`:

| Timer (`OnCalendar`) | Service | `--heat` | `--cool` |
|---|---|---|---|
| `*-*-* 06:00:00` | `thermostat-schedule-morning.service` | 68 | 76 |
| `*-*-* 23:00:00` | `thermostat-schedule-night.service` | 67 | 75 |

`src/schedule.py` snaps both setpoints to the nearest whole degree and writes
them atomically via `utils.write_scalar` (same as MQTT/WebUI). The 8°F gap is
still enforced downstream by the control daemon. Both timers use
`Persistent=true` so a missed firing runs on next boot.

## Build, install & packaging

- Dependencies: Python 3.10+, `flask`, `paho-mqtt`, `bleak` (see `requirements.txt`), plus `libgpiod2` for the `gpioset` command (driven via subprocess — **not** the python `gpiod` library; do not reintroduce it).
- `sudo make install` installs: daemons → `/usr/share/thermostat/`, template → `/usr/share/thermostat/templates/`, config → `/etc/thermostat/defaults.json`, units → `/etc/systemd/system/`.
- Cross-packaging (Yocto): `make install DESTDIR=$STAGING_DIR PREFIX=/usr SYSCONFDIR=/etc UNITDIR=/lib/systemd/system`.
- Run daemons manually for testing: `python3 src/sensor.py` etc. They expect `/etc/thermostat/defaults.json` and `/run/thermostat/`.

## Testing

- Suite lives in [`tests/`](tests/) and uses only the standard library (`unittest`) — no third-party test deps required.
- All tests are hardware- and GPIO-free: IPC state is redirected into a per-test temp dir and the wall clock is spoofed (`tests/ipc_env.py`), so `/run/thermostat` and real time are never used.
- Run from the repo root: `venv/bin/python -m unittest discover -s tests -v` (or `python3 -m unittest discover -s tests -v`).
- `src/control.py` exposes `ControlDaemon._step()` (one loop iteration) so its logic is testable without running the infinite `run()` loop.
- `src/demo.py`'s temperature model and action selection are pure/injectable (fixed `now`), covered by `tests/test_demo.py` without Flask or hardware.

## Developer conventions

1. **Every IPC write must go through `utils.atomic_write()`** (or `write_scalar`/`write_json`). No plain `open(..., "w")` on shared files. Concurrent writers (MQTT + WebUI) are expected.
2. **Keep the invariants intact:** dwell timer, startup delays, normally-open relay failsafe, data-failsafe-to-idle, setpoint gap enforcement, fan-on-idle behavior.
3. **Keep tuning constants as named module-level constants**, not magic numbers scattered in code.
4. **Update `spec.md` whenever behavior changes** — it is the canonical design document. Update this file's tables too if structure/constants move.
5. Temperature is always **°F** internally and in IPC/MQTT/WebUI. Only `sensor.py` deals in °C (BLE decode).
6. When adding config keys to `defaults.json`, ensure `setup.py` seeds the corresponding IPC file.
7. GPIO changes are hardware-affecting: verify pin assignment table in `spec.md §2.1` matches any edit to `PINS` in `src/gpio.py`.
