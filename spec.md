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

* **Hardware:** Govee H5075 BLE temperature sensors (Service UUID `0000ec88-0000-1000-8000-00805f9b34fb`).
* **Protocol:** Bluetooth Low Energy (BLE) via `bleak` Python library.
* **Data Format:** Temperature encoded in manufacturer data as signed fixed-point with 2 decimal places (temp_c * 10000 + humidity * 100).

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
    * **Exception:** `thermostat-mqtt` and `thermostat-web` may both write to `set_temp_*` and `mode` files.
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
  "set_temp_cool": 75.0,
  "set_temp_heat": 70.0
}
```
* **Boot Process:** On system startup, before any daemons launch, a one-shot initialization service copies values from `defaults.json` to the corresponding files in `/run/thermostat/` to seed the system state.

## 4. System Components (Services)

The system is divided into five primary daemons managed by `systemd`.

### 4.1 Sensor Daemon (`thermostat-sensor`)

* **Responsibility:** Scans for BLE advertisements and maintains valid temperature readings.
* **Logic:**
    * **Whitelist Filter:** Only process advertisements from MAC addresses explicitly defined in `/etc/thermostat/defaults.json`. Ignore all unknown devices.
    * Maintain a rolling buffer of data for each sensor (last 2 minutes) to smooth out sensor noise.
    * **Stale Data:** Discard any sensor data older than 2 minutes immediately.
    * **History:** Maintain a ring buffer for the last 24 hours in RAM with a **1-minute sampling interval** (1440 entries max). Each entry is a JSON object with:
      * `t`: Unix Epoch timestamp (integer seconds)
      * `avg`: Average temperature across all valid sensors (float, 2 decimal places)
      * `sensors`: Object mapping sensor location names to their filtered 2-minute rolling average temperatures
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
    * **Fan Mode "on":** The Fan relay is **ON**, regardless of `system_mode`. This allows air circulation even if `system_mode` is "off".
    * **System Mode "off":** The Compressor and Heat relays are forced **OFF**.
    * **Fan Mode "auto":** The Fan relay matches the state of the Compressor/Heat relays (ON when active, OFF when idle).

* **Hysteresis:** +/- 0.5°F (1°F total swing). Logic is **Centered** on the setpoint.
    * *Example (Heating):* If Setpoint is 70°F -> Turn ON at 69.5°F, Turn OFF at 70.5°F.
    * *Example (Cooling):* If Setpoint is 75°F -> Turn ON at 75.5°F, Turn OFF at 74.5°F.
* **Safety Guards:**
    * **Startup Safety:** Upon service start (or restart), the daemon must pause for 60 seconds before calculating any logic or writing to `hvac_action`. This safety delay is implemented internally within the Control Daemon (constant `STARTUP_DELAY = 60`). This ensures compressor safety if the daemon crashes and restarts, as the internal memory of the "last state change" is lost.
    * **Minimum Dwell Time:** Enforce a strict 1-minute duration for all states. Once the system enters a state (idle, heating, cooling, fan), it must remain in that state for at least 60 seconds before transitioning to any other state. **This rule overrides all other inputs, including manual user changes to mode or setpoint.**
        * *Example:* If the system is actively cooling and the user switches the mode to "Off", the system **MUST** complete the full 60-second cooling cycle before shutting down relays.
    * **Auto Separation:** Enforce minimum 7°F gap between Heat/Cool setpoints. When setpoints violate this gap (e.g., setting heat to 72°F when cool is 75°F), both setpoints are automatically adjusted to maintain the average temperature while enforcing the 7°F minimum separation (e.g., adjusting to heat=67.5°F and cool=74.5°F). Adjusted setpoints are written back to IPC files so the UI displays effective values.
    * **Data Failsafe:** If no fresh sensor data is available (cannot read `min_temp` or `max_temp`), force system to "idle" state (all relays OFF).

* **Output:** Writes intended state to `hvac_action` (IPC).

### 4.3 GPIO Daemon (`thermostat-gpio`)

* **Responsibility:** The "muscle." Reads desired state and actuates hardware.
* **Inputs:** `hvac_action` (IPC).
* **Tools:** `libgpiod` Python bindings (`gpiod` module).
* **Startup Safety:** Service must wait 60 seconds after system boot before starting (e.g., `ExecStartPre=/bin/sleep 60`) to prevent short-cycling after power loss.
* **Failsafe:** On service stop/kill (SIGTERM/SIGINT), immediately set all GPIOs to LOW (OFF).
* **Pin Configuration:** All pins configured as outputs with initial state LOW (INACTIVE).

### 4.4 MQTT Daemon (`thermostat-mqtt`)

* **Responsibility:** Primary control interface via Home Assistant. Can write to `system_mode`, `fan_mode`, `set_temp_cool`, `set_temp_heat` (concurrent with WebUI).
* **Protocol:** MQTT Climate entity.
* **Topic Structure:**
    * **State Publication:** `thermostat/state` (JSON with temperature, mode, setpoints, action)
    * **Availability:** `thermostat/availability` (`online`/`offline`)
    * **Command Topics (Inbound):**
        * `thermostat/mode/set` - Values: `off`, `cool`, `heat`, `auto`
        * `thermostat/fan/set` - Values: `auto`, `on`
        * `thermostat/cool/set` - Float value for cooling setpoint
        * `thermostat/heat/set` - Float value for heating setpoint
* **Matter Compatible Attributes:** Maps to:
  * `local_temperature`: Current average temperature
  * `min_temperature`: Minimum sensor reading (for heating logic)
  * `max_temperature`: Maximum sensor reading (for cooling logic)
  * `system_mode`: Current mode (off/cool/heat/auto)
  * `fan_mode`: Current fan mode (auto/on)
  * `occupied_cooling_setpoint`: Cooling setpoint
  * `occupied_heating_setpoint`: Heating setpoint
  * `thermostat_running_state`: Current HVAC action (idle/heating/cooling/fan)
* **Availability:** Publishes `online`/`offline` status to `thermostat/availability` with retain flag.
* **Output:** Writes to `system_mode`, `fan_mode`, `set_temp_cool`, `set_temp_heat`.

### 4.5 WebUI Daemon (`thermostat-web`)

* **Responsibility:** Backup control interface. Can write to `system_mode`, `fan_mode`, `set_temp_cool`, `set_temp_heat` (concurrent with MQTT).
* **Tech Stack:** Python Flask with threaded request handling.
* **Functionality:**
    * Simple HTML interface for manual control.
    * Visualizes 24-hour temperature history graph (reads `history.json`).
* **REST API Endpoints:**
    * `GET /` - HTML interface
    * `GET /api/state` - JSON with current state (temps, modes, setpoints, action)
    * `POST /api/mode` - Body: `{"mode": "off"|"cool"|"heat"|"auto"}`
    * `POST /api/fan` - Body: `{"fan": "auto"|"on"}`
    * `POST /api/setpoint` - Body: `{"type": "cool"|"heat", "value": float}`

* **Output:** Writes to `system_mode`, `fan_mode`, `set_temp_cool`, `set_temp_heat`.

## 5. File System & Installation Paths

### 5.1 Runtime IPC (tmpfs: `/run/thermostat/`)

| File Name | Writer(s) | Content | Description |
| --- | --- | --- | --- |
| `current_temp` | Sensor | `float` | Average of all valid sensors (for display/MQTT). |
| `min_temp` | Sensor | `float` | Lowest valid sensor reading (for Heating logic). |
| `max_temp` | Sensor | `float` | Highest valid sensor reading (for Cooling logic). |
| `history.json` | Sensor | `JSON` | List of objects: `[{"t": timestamp, "avg": float, "sensors": {"id": float, ...}}, ...]` |
| `system_mode` | MQTT, Web | `string` | "off", "cool", "heat", "auto". |
| `fan_mode` | MQTT, Web | `string` | "auto", "on". |
| `set_temp_cool` | MQTT, Web | `float` | Cooling target. Both services may write concurrently. |
| `set_temp_heat` | MQTT, Web | `float` | Heating target. Both services may write concurrently. |
| `hvac_action` | Control | `string` | Current action: "idle", "heating", "cooling", "fan". |

### 5.1.1 Data Format Standards

To ensure interoperability between Python, Bash, and web interfaces, strictly adhere to these formatting rules:

* **Scalar Files (Floats/Strings):** content must be written as **UTF-8 plain text** followed immediately by a single newline character (`\n`).
    * *Correct:* `72.5\n`
    * *Incorrect:* `72.5` (no newline) or `72.5\0` (null terminated)
* **Implementation:** All writes use atomic rename via unique PID-based temp files to prevent race conditions between concurrent writers (e.g., MQTT and WebUI simultaneously updating setpoints).
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
    * Install Systemd Units to `$(DESTDIR)$(UNITDIR)/`.

## 6. Project Repository Structure

The git repository is organized as follows:

```text
.
├── .gitignore
├── Makefile                 # Standard install target for Yocto/Packaging
├── requirements.txt         # Python dependencies (flask, paho-mqtt, bleak, libgpiod)
├── spec.md                  # This specification file
├── config/
│   └── defaults.json        # Default settings and sensor allowlist
├── src/
│   ├── control.py           # Control Daemon logic
│   ├── gpio.py              # GPIO Daemon logic
│   ├── mqtt.py              # MQTT Daemon logic
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
    ├── thermostat-sensor.service
    ├── thermostat-setup.service  # One-shot service that runs src/setup.py on boot
    └── thermostat-web.service

```
