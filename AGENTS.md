# AGENTS.md — Matter HVAC Thermostat (Technical Reference)

Technical guide for developers and coding agents working on this repository. It documents how the system is structured, where each piece of functionality lives, and the invariants that must not be broken.

> **Canonical reference:** this file is the sole authoritative technical
> reference for the system (architecture, IPC format standards, daemon behaviors,
> configuration). If you change behavior, update the code and keep this document
> in sync.

## What this project is

A Matter-compatible HVAC thermostat running on a Raspberry Pi. A relay HAT provides the actual switching; the software is six independent `systemd` services that communicate via atomic file-based IPC in a `tmpfs` directory (`/run/thermostat/`). [matterbridge](https://github.com/Luligu/matterbridge) + the `matterbridge-mqtt` plugin bridge MQTT → Matter so the thermostat appears natively in iOS/Android Home apps.

## Runtime architecture

Design principles: target OS is **Yocto Linux** (portable to standard
Debian/Raspbian); implementation language is **Python 3** (primary) with Bash
used only as auxiliary scripts. Runtime state is **volatile** (`tmpfs` under
`/run/thermostat/`); only the persistent startup defaults in
`/etc/thermostat/defaults.json` survive reboot. Modularity derives from the
distinct systemd services for sensing, decision-making, actuation, and UI.

### Service overview & startup order

| Order | Service | Role | Unit |
|---|---|---|---|
| 1 | `thermostat-setup` | One-shot bootstrap; seeds `/run/thermostat/` from `/etc/thermostat/defaults.json` (skips files that already exist) | `systemd/thermostat-setup.service` |
| 2 | `thermostat-sensor` | BLE scan + aggregation; writes temps + 24 h history; `After=thermostat-setup bluetooth.target time-sync.target` | `systemd/thermostat-sensor.service` |
| 3 | `thermostat-control` | The "brain"; thermostat logic, hysteresis, safety timers; writes `hvac_action` | `systemd/thermostat-control.service` |
| 4 | `thermostat-gpio` | The "muscle"; reads `hvac_action`, drives relays via `gpioset`; extra 60 s boot delay (`ExecStartPre`) | `systemd/thermostat-gpio.service` |
| 5 | `thermostat-mqtt` | matterbridge-mqtt device bridge; hosts the Matter Thermostat via the plugin | `systemd/thermostat-mqtt.service` |
| 5 | `thermostat-web` | Flask WebUI + REST API on `0.0.0.0:5000` | `systemd/thermostat-web.service` |
| 6 | `thermostat-schedule` | Timer-driven setpoint profiles (6am / 11pm) | `systemd/thermostat-schedule-*.service` + `*.timer` |

Dependency ordering: setup → sensor → control → gpio/mqtt/web. All daemons use `Restart=always` (5 s restarts). The GPIO service's `ExecStartPre=/bin/sleep 60` provides compressor-protection boot delay. The MQTT service connects to the mosquitto broker used by the matterbridge-mqtt plugin.

### Inter-process communication (IPC)

All shared state lives in `/run/thermostat/` (tmpfs). **Every write must be atomic** — use `utils.atomic_write()` (PID-suffixed temp file → `flush` + `fsync` → `os.replace`). Never write IPC files directly.

| File | Writer(s) | Content |
|---|---|---|
| `current_temp` | sensor | Average °F across all valid sensors |
| `min_temp` | sensor | Coldest valid sensor °F — drives heating |
| `max_temp` | sensor | Hottest valid sensor °F — drives cooling |
| `outdoor_temp` | sensor | Informational outdoor sensor °F (only when `outdoor_sensor` is configured; file is removed while the sensor is stale). **Never** folded into `current_temp`/`min_temp`/`max_temp` — excluded from HVAC control |
| `history.json` | sensor | 24 h ring buffer, 1-min samples, `{"t": epoch, "avg": °F, "sensors": {name: °F}, "outdoor": °F, "set_temp_cool": °F, "set_temp_heat": °F, "hvac_action": string}` (outdoor/action/setpoints omitted when absent) |
| `system_mode` | mqtt, web | `off` \| `cool` \| `heat` \| `auto` |
| `fan_mode` | mqtt, web | `auto` \| `on` |
| `set_temp_cool` | mqtt, web, control, schedule | Cooling setpoint °F |
| `set_temp_heat` | mqtt, web, control, schedule | Heating setpoint °F |
| `hvac_action` | control | `idle` \| `heating` \| `cooling` \| `fan` |

All IPC files are created with mode `0644` (world-readable, owner-writable). The general concurrency rule is **single-writer, multiple-reader**, with one exception: `thermostat-mqtt`, `thermostat-web`, and the setpoint scheduler may all write to the `set_temp_*` and `mode` files. Writers always create a **unique** temporary file named `<target>.tmp.<PID>`, write content, flush + `fsync`, then atomically `os.replace()` to the destination so concurrent writers never corrupt each other's temporary buffers.

Format rules: scalar files are UTF-8 text with exactly one trailing newline; structured files are compact JSON (`json.dumps(..., separators=(',', ':'))`). A bare value with no trailing newline, or NUL termination, is invalid. All timestamps (e.g., in `history.json`) are Unix epoch seconds.

## Configuration & initialization

Mutable, authoritative defaults live in `/etc/thermostat/defaults.json` (in the repo, `config/defaults.json`). It holds the startup modes/setpoints and the **allowlist** of sensor MAC addresses mapped to human-readable location names. Schema (matching `config/defaults.json`):

```json
{
  "system_mode": "off",
  "fan_mode": "auto",
  "set_temp_cool": 76.0,
  "set_temp_heat": 68.0,
  "sensors": {
    "A4:C1:38:00:00:01": "Living Room",
    "A4:C1:38:00:00:02": "Bedroom"
  },
  "outdoor_sensor": "",
  "mqtt": {
    "broker": "localhost",
    "port": 1883,
    "username": "",
    "password": "",
    "topic": "matterbridge",
    "device_id": "thermostat"
  }
}
```

The optional top-level `outdoor_sensor` key names **one** MAC address for a Govee sensor mounted outside the house. Its reading is collected for information only — it is never averaged with the room sensors and never feeds the control daemon (an empty string disables the feature). The `sensors` map is the allowlist; only these MACs are ever decoded/processed. The `mqtt` object stores the broker host, port, optional username/password, the matterbridge-mqtt plugin base `topic`, and this device's `device_id` (the `<deviceId>` path segment in every published topic). The broker is **mosquitto** (default `localhost:1883`), the same broker the matterbridge-mqtt plugin subscribes to. Leave `username` / `password` as empty strings (`""`) for brokers that do not require authentication.

On system boot, the one-shot `thermostat-setup` service (`src/setup.py`) copies the scalar values (`system_mode`, `fan_mode`, `set_temp_cool`, `set_temp_heat`) present in `defaults.json` into the matching `/run/thermostat/` files. **Files that already exist are never overwritten** — state survives restarts, and an external backup/restore service may populate `/run/thermostat/` either before or after `thermostat-setup` runs without its values being clobbered.

## Where to look (module map)

| Functionality | Location |
|---|---|
| IPC paths, atomic write/read helpers, scalar+JSON readers/writers | `src/utils.py` — **read this first** |
| Boot-time seeding of IPC from `defaults.json`; one-shot service | `src/setup.py` |
| Govee H5075 BLE decoding, allowlist, rolling averages, aggregation, informational outdoor sensor, 24 h history, failure→failsafe | `src/sensor.py` |
| Hysteresis, mode logic, auto conflict resolution, 8 °F setpoint gap, 120 s dwell, 60 s startup delay, data failsafe | `src/control.py` |
| Relay actuation (`gpioset`, libgpiod v1/v2 auto-detect), pin mapping, shutdown failsafe to OFF | `src/gpio.py` |
| matterbridge-mqtt device protocol (retained config/state/subscribe, write handling) | `src/mqtt.py` |
| Flask WebUI + REST API endpoints, history graph | `src/web.py`, `src/templates/index.html` |
| Hardware-free canned-data simulator for the WebUI (local testing) | `src/demo.py` |
| Timer-driven setpoint profile helper (one-shot, snap + atomic write) | `src/schedule.py` |
| Default config: sensor MAC allowlist, optional `outdoor_sensor`, initial modes/setpoints, MQTT broker + matterbridge topic/device_id | `config/defaults.json` |
| systemd units and ordering (services + timers) | `systemd/*.service`, `systemd/*.timer` |
| Install/packaging rules (`DESTDIR`/`PREFIX`/`SYSCONFDIR`/`UNITDIR`) | `Makefile` |
| Python deps: `flask`, `paho-mqtt`, `bleak` | `requirements.txt` |

The repository mirrors the install tree: top-level `Makefile`, `config/`, `src/` (daemons, `templates/`, `static/`), `systemd/` (units + timers), and `tests/`.

## Hardware interface

- **Electrical state:** GPIOs are driven **active-high** — High (1) = relay ON, Low/Hi-Z (0) = relay OFF (`gpioset ... pin=1` / `pin=0`). All pins are set LOW (inactive) at daemon start (initial state on every boot), so no relay is ever accidentally energized before the control loop provides `hvac_action`.
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

Example decode: 20.5 °C / 50 % humidity yields `raw = 205000 + 500 = 205500`; `205500 // 1000 / 10.0 = 20.5 °C`.

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

## Sensor daemon behavior (`src/sensor.py`)

Data path: BLE advertisements → per-sensor rolling buffer → aggregation → IPC.

- **Allowlist only:** advertisements from MACs not in `defaults.json`'s `sensors` map are ignored.
- **Rolling buffer:** each sensor keeps a 2-minute (`ROLLING_WINDOW`) rolling buffer as a noise filter; a reading older than 2 minutes is discarded immediately, and an uninitialized buffer is also treated as stale.
- **Aggregation (normal, ≥ 1 valid sensor):**
  - `current_temp`: arithmetic mean across **all** valid sensors (used for UI / MQTT reporting).
  - `min_temp`: lowest valid reading across all sensors (drives heating).
  - `max_temp`: highest valid reading across all sensors (drives cooling).
- **Partial failure:** if an allowlisted sensor goes stale it is excluded; operation continues while ≥ 1 sensor remains valid.
- **Outdoor sensor (informational):** when `outdoor_sensor` is set, that MAC is decoded but kept in a *separate* buffer (and removed from the room allowlist, so it can never enter the aggregation). Its rolling average is written to `outdoor_temp` and stamped as the `outdoor` field on each `history.json` sample; it is cleared (file removed) when the sensor is stale. Room aggregation, failsafe, and HVAC control are completely unaffected. Works even during the total-room-failure failsafe, since the file is independent of `current_temp`.
- **Total failure:** if *zero* sensors are valid, the daemon **deletes** the `current_temp` IPC file so the Control Daemon immediately sees missing sensor input and forces idle.
- **24-hour history:** an in-RAM ring buffer sampled every 1 minute (`HISTORY_INTERVAL`), capped at `HISTORY_MAX_ENTRIES` (1440 = 24 h), persisted as `history.json`. Each entry stores Unix epoch `t`, `avg` °F (2 decimals), per-room `sensors` (each room's filtered 2-minute rolling average), and — when known — the informational `outdoor` reading plus `set_temp_cool` / `set_temp_heat` / `hvac_action` so the WebUI can render setpoints, the outdoor line, and an action bar aligned with the chart.
- **Scanner resilience:** restart BleakScanner whenever its D-Bus / BlueZ subscription silently stalls. Each 5-second loop tick compares wall-clock time to monotonic time; divergence over `CLOCK_JUMP_THRESHOLD` (10 s) signals an NTP step and restarts the scanner. If no advertisement arrives for `SCANNER_WATCHDOG_TIMEOUT` (45 s), the scanner is likewise restarted.

## Thermostat logic invariants (`src/control.py`)

- **Heating** compares `min_temp` (coldest room) vs `set_temp_heat`; **cooling** compares `max_temp` vs `set_temp_cool`.
- Hysteresis: ON below `set − 0.5`, OFF above `set + 0.5`. Active state uses "continue until satisfied" logic; inactive state uses demand detection.
- **Auto mode conflict:** if both heat and cool demand, the larger deviation from setpoint wins; an already-active action takes priority.
- **`fan_mode: on`** → `hvac_action = fan` whenever the system would otherwise be idle, even in `system_mode: off`. Purpose: keeps air circulating through the home while `system_mode` is `off` (or between cycles).
- **`system_mode: off`** forces Compressor and Heat relays OFF; only the `fan_mode` override can still run the fan.
- **`fan_mode: auto`** (default): fan follows the Compressor/Heat relays — ON when actively heating/cooling, OFF when idle. Fan "on" allows circulation even while `system_mode` is `off`; an active heat/cool call keeps that action (fan may already be on), and only when otherwise idle does "fan on" select fan-only.

- **Data failsafe:** missing `min_temp`/`max_temp` → force `idle`.
- Setpoint separation is re-derived from local IPC values on every cycle; adjusted setpoints are **written back** to IPC so the UI/MQTT show effective values. Setpoint separation expands symmetrically around a midpoint **snapped to the nearest whole degree** (see `utils.round_degree()`); all setpoint writes (MQTT, WebUI) are snapped to the nearest whole degree to prevent fractional artifacts, and control snaps its IPC inputs on every cycle so stale fractional values self-heal.
- The 120 s dwell timer blocks *all* state changes (checked before any write) — it is not bypassed by user commands. Example: if the user switches mode to `off` while actively cooling, the compressor keeps running until the full 120 s cooling cycle completes before relays drop.
- **Startup safety rationale:** after a crash/restart the daemon's in-memory record of the last state change is lost, so the Control Daemon itself pauses `STARTUP_DELAY` (60 s) before any logic or `hvac_action` writes. The GPIO daemon adds a *separate* 60 s boot delay via `ExecStartPre=/bin/sleep 60` (compressor protection after power loss).
- **Post-cycle fan purge:** When heating or cooling satisfies demand, the daemon transitions to `hvac_action = fan` for `MIN_DWELL_TIME` (120 s) before returning to `idle` (if `fan_mode: auto`), extracting residual thermal energy from the HVAC unit.

## MQTT interface (`src/mqtt.py`)

Targets [matterbridge](https://github.com/Luligu/matterbridge) + the [`matterbridge-mqtt`](https://www.npmjs.com/package/matterbridge-mqtt) plugin (mosquitto broker). The daemon registers a Matter `Thermostat` device (type 769) via the plugin's device protocol — **all** device announcements are **retained** QoS 2 on `<topic>/<deviceId>/...` (endpoint `root`), and publishing an empty retained `config` payload deletes the device registration.

Topics:

| Topic | Direction | Payload |
|---|---|---|
| `<topic>/<device_id>/config/root` | out, retained | Device registration: `{"deviceTypes": ["Thermostat"], "clusters": {...fixed attrs...}}` |
| `<topic>/<device_id>/state/root` | out (5 s), retained | Current attribute values: `{"Thermostat": {...}}` |
| `<topic>/<device_id>/subscribe/root` | out, retained | `{"Thermostat": ["systemMode", "occupiedHeatingSetpoint", "occupiedCoolingSetpoint"]}` |
| `<topic>/<device_id>/write/root` | in | Plugin-forwarded controller writes: `{"Thermostat": {"systemMode": 4, ...}}` |

**Attribute mapping (verified against matterbridge-mqtt's `clusters.json` and matterbridge's `MatterbridgeThermostatServer` registry entry, which installs the Thermostat behavior with `AutoMode`, `Heating`, `Cooling` features):** temperatures are **hundredths of °C**; `systemMode` uses `SystemModeEnum` (`off`/`auto`/`cool`/`heat` → `0`/`1`/`3`/`4`); `thermostatRunningState` is a `RelayStateBitmap` object (`heat`/`cool`/`fan` camelCase bits mirroring `hvac_action`, plus `fan` when `fan_mode` is `on`); `thermostatRunningMode` is `Off`=0/`Cool`=3/`Heat`=4; `controlSequenceOfOperation` = `CoolingAndHeating` = 4; `minSetpointDeadBand` is in tenths of °C (44 ≈ 8 °F). Fixed attributes (limits 60–80 °F, dead band, control sequence) live in the retained `config`; live values go in `state`. Inbound setpoints are converted back to whole °F via `utils.round_degree()`; unknown attributes/clusters are logged and ignored. Identical consecutive state payloads are suppressed (echo loop prevention, since republished state is forwarded back to `write`).

MQTT config comes from the `mqtt` section of `defaults.json` (broker, port, username/password, `topic`, `device_id`); falls back to `localhost:1883`, base topic `matterbridge`, device id `thermostat`. On connect: publishes config + subscribe + initial state (all retained), subscribes the `write` topic.

> **Do NOT expose a Matter `FanControl` cluster** — the thermostat is registered as a plain `Thermostat` device type. Fan control remains available through the WebUI and the `fan_mode` IPC file; the `fan` bit of `thermostatRunningState` still reflects fan activity. See the MQTT interface section above.

## WebUI REST API (`src/web.py`)

Backup control interface (may write `system_mode`, `fan_mode`, `set_temp_cool`,
`set_temp_heat` concurrently with MQTT). Flask runs with threaded request
handling (`threaded=True`) and binds to `0.0.0.0:5000` in production.

- `GET /` — HTML dashboard (auto-refresh 30 s) with live temp, mode/setpoint controls, 24 h history graph (room lines + bold average + dashed heat/cool setpoint lines) plus a horizontal HVAC action bar below it (color-coded heating/cooling/fan/idle segments on the same time scale), an "Estimated Energy Cost" card (client-side integration of per-action power draw over the last 24 h at $0.20/kWh, with a ×30 monthly projection; power model: idle 0 W, fan 400 W, heating 500 W, cooling 4000 W) and a "Current Room Temperatures" table at the bottom (each room's live temp from the most recent `history.json` sample whose `sensors` map contains per-room readings); setpoint controls use a single +/- button pair that adjusts heat and cool setpoints simultaneously. When an outdoor sensor is configured, the outdoor reading is shown as a third equal-sized sub-entry (labelled "Outdoor", alongside "Min" and "Max") under the current-temperature card (the sub-entry is hidden otherwise) and is drawn as a dashed line on the history graph; it is never part of the average line
- `GET /api/state` — full state as JSON (includes `history`; adds `outdoor_temp` (nullable) and `outdoor_configured` (bool) for the informational outdoor sensor)
- `POST /api/mode` — `{"mode": "off"|"cool"|"heat"|"auto"}`
- `POST /api/fan` — `{"fan": "auto"|"on"}`
- `POST /api/setpoint` — `{"type": "cool"|"heat", "value": <float>}` (single-setpoint override; retained for compatibility)
- `POST /api/setpoints` — `{"delta": <float>}` — adjusts both setpoints together (used by WebUI +/- buttons)

### Local demo mode (no hardware)

The WebUI can run in a fully local, hardware-free demo mode that feeds it canned sensor data. `src/demo.py` generates a realistic per-room temperature model, an outdoor temperature (the ambient curve is also written to `outdoor_temp` and the `outdoor` history field so the outdoor sub-entry/chart render in demo mode), a populated 24 h history ring buffer, and a hysteresis/dwell-aware `hvac_action`, then keeps generating samples so the page auto-refresh feels live. Mode/fan/setpoint changes made in the UI are written to the demo data directory and picked up by the simulator on its next tick, so the dashboard is fully interactive.

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
4. **This file (AGENTS.md) is the canonical design document.** Update it whenever behavior changes, and keep its tables current if structure/constants move.
5. Temperature is always **°F** internally and in IPC/MQTT/WebUI. Only `sensor.py` deals in °C (BLE decode).
6. When adding config keys to `defaults.json`, ensure `setup.py` seeds the corresponding IPC file.
7. GPIO changes are hardware-affecting: verify the pin assignment table in the **Hardware interface** section above matches any edit to `PINS` in `src/gpio.py`.
