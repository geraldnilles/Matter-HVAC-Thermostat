# HVAC Thermostat Raspberry Pi Hat - System Specification

## 1. Overview

This project defines the software stack for a custom HVAC Thermostat Raspberry Pi Hat. The system is designed to be modular, reliable, and "Unix-like," leveraging the filesystem for Inter-Process Communication (IPC) and systemd for service management.

## 2. Hardware Interface

### 2.1 GPIO Pinout

The Raspberry Pi directly drives relays for the HVAC components.

* **Active State:** High (1) = ON, Low/Hi-Z (0) = OFF.
* **Hardware Safety:** Relays are Normally Open; system defaults to OFF on power loss or reboot.

| Component | GPIO Pin | Color Code (Standard) |
| --- | --- | --- |
| **Fan** | GPIO 20 | G (Green) |
| **Compressor** | GPIO 21 | Y (Yellow) |
| **Heat** | GPIO 26 | W (White) |

### 2.2 Temperature Sensors

* **Hardware:** Govee H5075 BLE temperature sensors (Manufacturer ID `0xEC88` / `60552`).
* **Protocol:** Bluetooth Low Energy (BLE) via `bleak` Python library.
* **Data Format:** Temperature encoded in manufacturer specific data (Company ID 0xEC88). Bytes 1-3 contain a 24-bit integer where:
  * Bit 23 (0x800000) is the sign bit (1 = negative).
  * Bits 0-22 contain: `Temp(C) * 10000 + Humidity(%) * 10`.
  * To extract temperature: Strip the sign bit, perform integer division by 1000 to remove humidity data, then divide by 10.0.
  * Example: For 20.5°C and 50% humidity, the raw value is `205000 + 500 = 205500`. Extracted: `205500 // 1000 / 10.0 = 20.5°C`.

## 3. Software Architecture

### 3.1 Design Principles

* **OS:** Yocto Linux (portable to standard Debian/Raspbian).
* **Language:** Python 3 (primary), Bash (auxiliary).
* **Modularity:** Distinct systemd services for sensing, decision making, actuation, and UI.
* **Persistence:** Runtime state is volatile (`tmpfs`). Default startup values are persistent in `/etc/thermostat/defaults.json`.

### 3.2 Inter-Process Communication (IPC)

* **Mechanism:** Filesystem-based state sharing.
* **Location:** `/run/thermostat/` (tmpfs).
* **File Permissions:** All IPC files are created with mode `0644` (world-readable, owner-writable).
* **Concurrency:**
    * **General Rule:** Single-writer, multiple-reader.
    * **Exception:** `thermostat-mqtt`, `thermostat-web`, and the setpoint scheduler may all write to `set_temp_*` and `mode` files.
    * **Atomic Operations:** **Critical.** All writes to shared state files must be atomic to prevent race conditions or partial reads.
    * **Mechanism:** Writers must create a **unique** temporary file using process ID (e.g., `<target>.tmp.<PID>`), write the content, flush to disk (`fsync`), and finally atomically rename (`os.replace`) the temporary file to the target filename.
    * **Reasoning:** Unique filenames ensure that concurrent writers (e.g., MQTT and WebUI updating settings simultaneously) do not overwrite each other's temporary buffers before the atomic commit.

### 3.3 Configuration & Initialization

* **Static Configuration:** Stored in `/etc/thermostat/defaults.json`. This includes the startup setpoints, modes, and an **allowlist of sensor MAC addresses** mapped to human-readable location names.

**Configuration Schema:**
```json
{
  "sensors": {
    "A4:C1:38:XX:XX:XX": "Living Room",
    "A4:C1:38:YY:YY:YY": "Bedroom"
  },
  "system_mode": "off",
  "fan_mode": "auto",
  "set_temp_cool": 76.0,
  "set_temp_heat": 68.0,
  "mqtt": {
    "broker": "homeassistant.lan",
    "port": 1883,
    "username": "thermostat",
    "password": "secret"
  }
}
```
* The `mqtt` object stores the MQTT broker host, port, and optional authentication credentials. The broker runs on the **separate Home Assistant device** — the thermostat does **not** run a local MQTT broker (no mosquitto on the Pi). Leave `username` and `password` as empty strings (`""`) for brokers that do not require authentication.
* **Boot Process:** On system startup, before any daemons launch, a one-shot initialization service (`thermostat-setup`) copies values from `defaults.json` to the corresponding files in `/run/thermostat/` to seed the system state.

### 3.4 Service Dependencies

Services start in the following order, managed by systemd `After=` directives:

1. **thermostat-setup** - Runs first after filesystems are available
2. **thermostat-sensor** - Starts after setup, bluetooth target, and time sync target (`After=time-sync.target`)
3. **thermostat-control** - Starts after setup and sensor (requires temperature data)
4. **thermostat-gpio** - Starts after setup and control (60-second delay via `ExecStartPre`)
5. **thermostat-mqtt** / **thermostat-web** - Start after setup and control (can run concurrently).
   The MQTT daemon connects to a **remote** broker on the Home Assistant device — there is no local broker dependency (`systemd/thermostat-mqtt.service` has no `mosquitto.service` in `After=`/`Wants=`).
6. **thermostat-schedule-morning** / **thermostat-schedule-night** - One-shot setpoint schedulers triggered by systemd **timer** units (`*.timer`) at fixed times of day (06:00 and 23:00), independent of the daemon startup order.

## 4. System Components (Services)

The system is divided into five primary daemons managed by `systemd`.

### 4.1 Sensor Daemon (`thermostat-sensor`)

* **Responsibility:** Scans for BLE advertisements and maintains valid temperature readings.
* **Logic:**
    * **Whitelist Filter:** Only process advertisements from MAC addresses explicitly defined in `/etc/thermostat/defaults.json`. Ignore all unknown devices.
    * **Rolling Buffer:** Maintain a 2-minute rolling buffer of readings for each sensor to smooth noise. New readings replace old ones beyond the 2-minute window.
    * **Stale Data:** Discard any sensor data older than 2 minutes immediately. An uninitialized buffer (no readings yet) is also treated as stale.
    * **Scanner Resilience:** The daemon monitors for conditions that silently stall `BleakScanner`'s D-Bus/BlueZ subscription and automatically re-initializes the scanner:
        * **Clock Jump Detection:** Each 5 s loop tick compares wall-clock elapsed time (`time.time()`) against monotonic elapsed time (`time.monotonic()`). If they diverge by more than 10 s (`CLOCK_JUMP_THRESHOLD`), a system clock step (e.g. NTP sync) is assumed and the scanner is restarted.
        * **Scan Watchdog:** If no BLE advertisement is received for more than 45 s (`SCANNER_WATCHDOG_TIMEOUT`), the scanner is restarted to recover from a silent D-Bus stall.
    * **History:** Maintain a ring buffer for the last 24 hours in RAM with a **1-minute sampling interval** (1440 entries max). Each entry is a JSON object with:
      * `t`: Unix Epoch timestamp (integer seconds)
      * `avg`: Average temperature across all valid sensors (float, 2 decimal places)
      * `sensors`: Object mapping sensor location names to their filtered 2-minute rolling average temperatures
      * `set_temp_cool`: Current cooling setpoint °F (present when available)
      * `set_temp_heat`: Current heating setpoint °F (present when available)
      * `hvac_action`: Current HVAC action string (present when available) — sampled so the WebUI can render an action bar aligned with the temperature history
* **Sensor Aggregation Logic:**
    * **Partial Failure:** If a defined sensor goes stale (no data for > 2 mins), exclude it from the calculation. Continue operation as long as at least one sensor remains valid.
    * **Total Failure:** If *zero* sensors are valid, delete the `current_temp` IPC file to immediately trigger the Control Daemon failsafe.
    * **Calculation (requires >= 1 valid sensor):**
        * **current_temp:** Always calculated as the **average** of all valid sensors (used for UI/Reporting).
        * **min_temp:** The lowest value among valid sensors (used for Heating logic).
        * **max_temp:** The highest value among valid sensors (used for Cooling logic).

* **Output:** Writes `current_temp`, `min_temp`, `max_temp`, and `history.json` to IPC.

### 4.2 Control Daemon (`thermostat-control`)

* **Responsibility:** The "brain." Reads state and sensors, decides relay states.
* **Inputs:** `min_temp`, `max_temp`, `set_temp_cool`, `set_temp_heat`, `system_mode`, `fan_mode`.
* **Thermostat Logic:**
    * **Heating Rules:** Compare `min_temp` against `set_temp_heat`. (Heat the coldest room).
    * **Cooling Rules:** Compare `max_temp` against `set_temp_cool`. (Cool the hottest room).
* **Logic Priorities:**
    * **Fan Mode "on":** The Fan relay is **ON**, regardless of `system_mode`. This allows air circulation even if `system_mode` is "off". If the system is actively heating or cooling, it maintains that state; only when idle does "fan on" switch to fan-only mode.
    * **System Mode "off":** The Compressor and Heat relays are forced **OFF**.
    * **Fan Mode "auto":** The Fan relay matches the state of the Compressor/Heat relays (ON when active, OFF when idle).
    * **Post-Cycle Fan Purge:** When a heating or cooling cycle completes (`want_heat` and `want_cool` become false while in `heating` or `cooling` state), the system transitions to the `fan` action for the duration of the minimum dwell time (120 s) before transitioning to `idle` (when `fan_mode` is `auto`). This purges residual heat or cool air from the HVAC equipment into the home.

* **Hysteresis:** +/- 0.5°F (1°F total swing). Logic is **Centered** on the setpoint.
    * *Example (Heating):* If Setpoint is 70°F -> Turn ON at 69.5°F, Turn OFF at 70.5°F.
    * *Example (Cooling):* If Setpoint is 75°F -> Turn ON at 75.5°F, Turn OFF at 74.5°F.

* **Auto Mode Conflict Resolution:** When `system_mode` is "auto" and both heating and cooling demand exist simultaneously (e.g., different rooms have extreme temperatures), the system compares the temperature differences:
    * Calculate `heat_diff = set_temp_heat - min_temp` and `cool_diff = max_temp - set_temp_cool`
    * If `heat_diff > cool_diff`, prioritize heating; otherwise prioritize cooling
    * This prevents rapid switching between heating and cooling

* **Safety Guards:**
    * **Startup Safety:** Upon service start (or restart), the daemon must pause for 60 seconds internally before calculating any logic or writing to `hvac_action`. This safety delay is implemented within the Control Daemon Python code (`STARTUP_DELAY = 60`). This ensures compressor safety if the daemon crashes and restarts, as the internal memory of the "last state change" is lost.
    * **Minimum Dwell Time:** Enforce a strict 2-minute duration for all states. Once the system enters a state (idle, heating, cooling, fan), it must remain in that state for at least 120 seconds before transitioning to any other state. **This rule overrides all other inputs, including manual user changes to mode or setpoint.**
        * *Example:* If the system is actively cooling and the user switches the mode to "Off", the system **MUST** complete the full 120-second cooling cycle before shutting down relays.
    * **Auto Separation:** Enforce minimum 8°F gap between Heat/Cool setpoints. When setpoints violate this gap (e.g., setting heat to 72°F when cool is 75°F), **both** setpoints are automatically adjusted symmetrically around their average (snapped to the nearest whole degree) to maintain the 8°F minimum separation (e.g., adjusting to heat=70°F and cool=78°F). Adjusted setpoints are immediately written back to IPC files so the UI displays effective values. All setpoint writes (MQTT, WebUI, control write-back) are snapped to whole-degree steps to prevent fractional artifacts (e.g. 73.125°F) in IPC files.
    * **Data Failsafe:** If no fresh sensor data is available (cannot read `min_temp` or `max_temp`), force system to "idle" state (all relays OFF).

* **Output:** Writes intended state to `hvac_action` (IPC).

### 4.3 GPIO Daemon (`thermostat-gpio`)

* **Responsibility:** The "muscle." Reads desired state and actuates hardware.
* **Inputs:** `hvac_action` (IPC).
* **Tools:** `gpioset` shell command from `libgpiod` package.
* **Startup Safety:** Service waits 60 seconds after system boot before starting via `ExecStartPre=/bin/sleep 60` in systemd unit. This is separate from the Control Daemon's internal 60-second delay.
* **Failsafe:** On service stop/kill (SIGTERM/SIGINT), immediately set all GPIOs to LOW (OFF).
* **Pin Configuration:** All pins configured as outputs with initial state LOW (INACTIVE).

### 4.4 MQTT Daemon (`thermostat-mqtt`)

* **Responsibility:** Primary control interface via Home Assistant. Can write to `system_mode`, `fan_mode`, `set_temp_cool`, `set_temp_heat` (concurrent with WebUI).
* **Configuration:** Broker host, port, and optional username/password are loaded from the `mqtt` object in `/etc/thermostat/defaults.json`. If `username` is empty, the daemon connects without authentication.
* **Broker Location:** The broker is **not** hosted on the Pi — the thermostat connects to the MQTT broker that runs on the separate Home Assistant device (e.g., the Mosquitto broker add-on/`mosquitto` package in Home Assistant OS). No local MQTT broker service is installed or started by this project.
* **Protocol:** MQTT Climate entity.
* **Mode Name Mapping:** The internal `auto` mode (used everywhere in IPC, WebUI, and config) is published to Home Assistant as `heat_cool` (Home Assistant's term for a climate device that can heat and cool simultaneously). Incoming `heat_cool` commands are translated back to `auto` before being written to IPC. No other mode names are affected.
* **Topic Structure:**
    * **State Publication:** `thermostat/state` (JSON with temperature, mode, setpoints, action)
    * **Availability:** `thermostat/availability` (`online`/`offline`)
    * **HA Discovery:** `homeassistant/climate/thermostat/config` (Retained JSON payload)
    * **Command Topics (Inbound):**
        * `thermostat/mode/set` - Values: `off`, `cool`, `heat`, `heat_cool`
        * `thermostat/fan/set` - Values: `auto`, `on`
        * `thermostat/cool/set` - Float value for cooling setpoint
        * `thermostat/heat/set` - Float value for heating setpoint
* **Matter Compatible Attributes:** Maps to:
  * `local_temperature`: Current average temperature
  * `system_mode`: Current mode (off/cool/heat/heat_cool)
  * `fan_mode`: Current fan mode (auto/on)
  * `occupied_cooling_setpoint`: Cooling setpoint
  * `occupied_heating_setpoint`: Heating setpoint
  * `thermostat_running_state`: Current HVAC action (idle/heating/cooling/fan)
* **Matter Device Type (fan mode not advertised):** The HA discovery payload intentionally omits all `fan_mode_*` discovery keys (`fan_mode_state_topic`, `fan_mode_command_topic`, `fan_modes`). Home Assistant infers its `FAN_MODE` supported feature from those keys, and `home-assistant-matter-hub` then classifies the entity as a Matter **Room Air Conditioner** (device type `0x0072`) rather than a plain **Thermostat** (`0x002A`). Omitting them keeps the Thermostat classification while fan control remains available through the WebUI and the control/GPIO daemons via the `fan_mode` IPC file; the `thermostat/fan/set` command topic stays subscribed for compatibility. Note: the `fan_mode` value is still included in the `thermostat/state` payload for other MQTT consumers.
* **Availability:** Publishes `online`/`offline` status to `thermostat/availability` with retain flag.
* **Update Interval:** Publishes state every 5 seconds.
* **Output:** Writes to `system_mode`, `fan_mode`, `set_temp_cool`, `set_temp_heat`.

### 4.5 WebUI Daemon (`thermostat-web`)

* **Responsibility:** Backup control interface. Can write to `system_mode`, `fan_mode`, `set_temp_cool`, `set_temp_heat` (concurrent with MQTT).
* **Tech Stack:** Python Flask with threaded request handling (`threaded=True`).
* **Network:** Binds to `0.0.0.0:5000`.
* **Functionality:**
    * Simple HTML interface for manual control.
    * Setpoint controls use a single pair of +/− buttons that adjust the heating and cooling setpoints simultaneously, preserving the 8°F minimum separation enforced by the control daemon.
    * Visualizes 24-hour temperature history graph (reads `history.json`). Plots a line for each room from the per-sensor readings, plus a bold average line, and dashed heating/cooling setpoint reference lines; a color-coded legend identifies each line.
    * Renders a horizontal HVAC action bar directly below the temperature history. Each bar segment is colored by the action at that time (heating, cooling, fan, idle), and the bar uses the same horizontal time scale as the temperature graph so action segments line up with the temperature curve.
    * Auto-refreshes state every 30 seconds via JavaScript.
    * Displays a "Current Room Temperatures" table at the bottom of the dashboard, listing each room and its live temperature read from the most recent `history.json` sample's `sensors` map (the newest sample that contains per-room readings).
* **REST API Endpoints:**
    * `GET /` - HTML interface
    * `GET /api/state` - JSON with current state (temps, modes, setpoints, action)
    * `POST /api/mode` - Body: `{"mode": "off"|"cool"|"heat"|"auto"}`
    * `POST /api/fan` - Body: `{"fan": "auto"|"on"}`
    * `POST /api/setpoint` - Body: `{"type": "cool"|"heat", "value": float}` (single-setpoint override; retained for compatibility)
    * `POST /api/setpoints` - Body: `{"delta": float}` — adjusts both `set_temp_cool` and `set_temp_heat` by `delta` °F (used by the WebUI +/- buttons)

* **Output:** Writes to `system_mode`, `fan_mode`, `set_temp_cool`, `set_temp_heat`.

* **Local demo mode (no hardware):** For isolated UI/UX testing without sensors, GPIOs, or relays, `src/web.py` accepts command-line flags:
    * `--host` / `--port` — bind address/port (defaults `0.0.0.0:5000`).
    * `--data-dir` — directory for IPC state files (defaults to `/run/thermostat` in production mode).
    * `--demo` — run against canned data generated by `src/demo.py` instead of real IPC state. Writes simulated `current_temp`, `min_temp`, `max_temp`, `history.json`, `system_mode`, `fan_mode`, `set_temp_cool`, `set_temp_heat`, and `hvac_action` into an isolated directory (a fresh temp dir unless `--data-dir` is given). The simulator honors WebUI mode/fan/setpoint writes on its next tick, so the dashboard remains interactive. With no flags, behavior is identical to production.

### 4.6 Setpoint Scheduler (`thermostat-schedule`)

* **Responsibility:** Automatically apply a fixed daily heat/cool setpoint
  profile. Not a long-running daemon; instead two `oneshot` service units are
  triggered by two systemd **timer** units at fixed times of day.
* **Inputs:** Schedule profile constants passed as CLI arguments
  (`--heat` / `--cool`), in °F.
* **Logic:**
    * The scheduling helper (`src/schedule.py`) parses the two setpoint
      arguments, snaps each to the nearest whole degree via
      `utils.round_degree()`, and writes them atomically with
      `utils.write_scalar()` (same conventions as MQTT/WebUI).
    * The 8°F minimum gap is still enforced downstream by the Control Daemon's
      setpoint-separation invariant; the schedule itself does not perform gap
      enforcement.
* **Default schedule:**
    | Profile | Timer (`OnCalendar`) | `--heat` | `--cool` |
    | --- | --- | --- | --- |
    | Morning | `*-*-* 06:00:00` | 68 | 76 |
    | Night | `*-*-* 23:00:00` | 67 | 75 |
* **Timers:**
    * `thermostat-schedule-morning.timer` → `thermostat-schedule-morning.service`
    * `thermostat-schedule-night.timer` → `thermostat-schedule-night.service`
    * Both timers use `Persistent=true` so a missed firing (e.g. the Pi was
      powered off at the scheduled time) runs on the next boot.
* **Output:** Writes to `set_temp_cool`, `set_temp_heat`.

## 5. File System & Installation Paths

### 5.1 Runtime IPC (tmpfs: `/run/thermostat/`)

| File Name | Writer(s) | Content | Description |
| --- | --- | --- | --- |
| `current_temp` | Sensor | `float` | Average of all valid sensors (for display/MQTT). |
| `min_temp` | Sensor | `float` | Lowest valid sensor reading (for Heating logic). |
| `max_temp` | Sensor | `float` | Highest valid sensor reading (for Cooling logic). |
| `history.json` | Sensor | `JSON` | List of objects: `[{"t": timestamp, "avg": float, "sensors": {"name": float, ...}, "set_temp_cool": float, "set_temp_heat": float, "hvac_action": string}, ...]` (setpoints and `hvac_action` optional when absent) |
| `system_mode` | MQTT, Web | `string` | "off", "cool", "heat", "auto". |
| `fan_mode` | MQTT, Web | `string` | "auto", "on". |
| `set_temp_cool` | MQTT, Web, Control, Scheduler | `float` | Cooling target. May be adjusted by Control Daemon to enforce 8°F gap. |
| `set_temp_heat` | MQTT, Web, Control, Scheduler | `float` | Heating target. May be adjusted by Control Daemon to enforce 8°F gap. |
| `hvac_action` | Control | `string` | Current action: "idle", "heating", "cooling", "fan". |

### 5.1.1 Data Format Standards

To ensure interoperability between Python, Bash, and web interfaces, strictly adhere to these formatting rules:

* **Scalar Files (Floats/Strings):** content must be written as **UTF-8 plain text** followed immediately by a single newline character (`\n`).
    * *Correct:* `72.5\n`
    * *Incorrect:* `72.5` (no newline) or `72.5\0` (null terminated)
* **Implementation:** All writes use atomic rename via unique PID-based temp files (e.g., `filename.tmp.1234`) to prevent race conditions between concurrent writers (e.g., MQTT and WebUI simultaneously updating setpoints).
* **Timestamps:** All timestamps (specifically in `history.json`) must be recorded as **Unix Epoch** time (numeric seconds since Jan 1, 1970).

### 5.2 Application & Configuration

| Directory/File | Description |
| --- | --- |
| `/usr/share/thermostat/` | Main installation directory. Contains Python logic (`src/*.py`). |
| `/usr/share/thermostat/templates/` | Web interface templates (`index.html`). |
| `/etc/thermostat/defaults.json` | Persistent configuration file (sensor allowlist, default setpoints). |
| `/etc/systemd/system/` | Systemd unit files (`thermostat-*.service`). |

### 5.3 Installation Mechanism

* **Tool:** Standard GNU `Makefile`.
* **Target:** `make install`
* **Variables:** The Makefile must support standard override variables for packaging:
    * `DESTDIR`: Root directory for packaging (e.g., Yocto build root).
    * `PREFIX`: Installation prefix (default: `/usr`).
    * `SYSCONFDIR`: Configuration directory (default: `/etc`).
    * `UNITDIR`: Systemd unit directory (default: `/etc/systemd/system`, configurable to `/lib/systemd/system`).
* **Actions:**
    * Create necessary directory structures (e.g., `$(DESTDIR)$(PREFIX)/share/thermostat/`).
    * Install Python source files to `$(DESTDIR)$(PREFIX)/share/thermostat/`.
    * Install Templates to `$(DESTDIR)$(PREFIX)/share/thermostat/templates/`.
    * Install Default Configuration to `$(DESTDIR)$(SYSCONFDIR)/thermostat/`.
    * Install Systemd Units (services *and* timers) to `$(DESTDIR)$(UNITDIR)/`.

## 6. Project Repository Structure

The git repository is organized as follows:

```text
.
├── .gitignore
├── Makefile                 # Standard install target for Yocto/Packaging
├── requirements.txt         # Python dependencies (flask, paho-mqtt, bleak)
├── spec.md                  # This specification file
├── config/
│   └── defaults.json        # Default settings and sensor allowlist
├── src/
│   ├── control.py           # Control Daemon logic
│   ├── gpio.py              # GPIO Daemon logic
│   ├── mqtt.py              # MQTT Daemon logic
│   ├── schedule.py          # Setpoint scheduler helper (driven by systemd timers)
│   ├── sensor.py            # Sensor Daemon logic
│   ├── setup.py             # Initialization script (parses defaults.json to IPC)
│   ├── utils.py             # Shared constants, IPC file handlers, and locking
│   ├── web.py               # WebUI Daemon logic
│   └── templates/
│       └── index.html       # Flask HTML template for WebUI
└── systemd/
    ├── thermostat-control.service
    ├── thermostat-gpio.service
    ├── thermostat-mqtt.service
    ├── thermostat-schedule-morning.service  # 6am profile (oneshot)
    ├── thermostat-schedule-morning.timer
    ├── thermostat-schedule-night.service    # 11pm profile (oneshot)
    ├── thermostat-schedule-night.timer
    ├── thermostat-sensor.service
    ├── thermostat-setup.service  # One-shot service that runs src/setup.py on boot
    └── thermostat-web.service

```
