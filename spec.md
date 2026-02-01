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

* **Hardware:** Multiple Govee BLE sensors.
* **Protocol:** Bluetooth Low Energy (BLE) via `bleak` Python library.

## 3. Software Architecture

### 3.1 Design Principles

* **OS:** Yocto Linux (portable to standard Debian/Raspbian).
* **Language:** Python 3 (primary), Bash (auxiliary).
* **Modularity:** Distinct systemd services for sensing, decision making, actuation, and UI.
* **Persistence:** Runtime state is volatile (`tmpfs`). Default startup values are persistent in `/etc/thermostat/defaults.json`.

### 3.2 Inter-Process Communication (IPC)

* **Mechanism:** Filesystem-based state sharing.
* **Location:** `/run/thermostat/` (tmpfs).
* **Concurrency:**
    * **General Rule:** Single-writer, multiple-reader.
    * **Exception:** `thermostat-mqtt` and `thermostat-web` may both write to `set_temp` and `mode` files.
    * **Atomic Operations:** **Critical.** All writes to shared state files must be atomic (write to temporary file -> `mv` to target) to prevent race conditions or partial reads.

### 3.3 Configuration & Initialization

* **Static Configuration:** Stored in `/etc/thermostat/defaults.json`. This includes the startup setpoints, modes, and an **allowlist of sensor MAC addresses** mapped to human-readable location names (e.g., `"A4:C1:38...": "Living Room"`).
* **Boot Process:** On system startup, before any daemons launch, a one-shot initialization service copies values from `defaults.json` to the corresponding files in `/run/thermostat/` to seed the system state.

## 4. System Components (Services)

The system is divided into five primary daemons managed by `systemd`.

### 4.1 Sensor Daemon (`thermostat-sensor`)

* **Responsibility:** Scans for BLE advertisements and maintains valid temperature readings.
* **Logic:**
    * **Whitelist Filter:** Only process advertisements from MAC addresses explicitly defined in `/etc/thermostat/defaults.json`. Ignore all unknown devices.
    * Maintain a rolling buffer of data for each sensor (last 2 minutes) to smooth out sensor noise.
    * **Stale Data:** Discard any sensor data older than 2 minutes immediately.
    * **History:** Maintain a ring buffer for the last 24 hours in RAM with a **1-minute sampling interval**. Each entry contains a timestamp, the aggregate house temperature, and the individual **filtered 2-minute rolling average** for every active sensor.
* **Sensor Aggregation Logic:**
    * While there are multiple sensors, it will only report 1 number to the `current_temp` IPC file.
    * **Mode = Heat:** Use lowest valid sensor reading.
    * **Mode = Cool:** Use highest valid sensor reading.
    * **Mode = Auto:** Use average of all valid sensor readings.

* **Output:** Writes `current_temp` and `history.json` to IPC.

### 4.2 Control Daemon (`thermostat-control`)

* **Responsibility:** The "brain." Reads state and sensors, decides relay states.
* **Inputs:** `current_temp`, `set_temp_cool`, `set_temp_heat`, `system_mode`, `fan_mode`.

* **Hysteresis:** +/- 0.5°F (1°F total swing).
* **Safety Guards:**
    * **Short-Cycle Protection:** Minimum 1-minute global lockout between state transitions.
    * **Auto Separation:** Enforce minimum 5°F gap between Heat/Cool setpoints.
    * **Data Failsafe:** If no fresh sensor data is available (cannot read `current_temp`), force system to "idle" state (all relays OFF).

* **Output:** Writes intended state to `hvac_action` (IPC).

### 4.3 GPIO Daemon (`thermostat-gpio`)

* **Responsibility:** The "muscle." Reads desired state and actuates hardware.
* **Inputs:** `hvac_action` (IPC).
* **Tools:** `libgpiod`.
* **Startup Safety:** Service must wait 60 seconds after system boot before starting (e.g., `ExecStartPre=/bin/sleep 60`) to prevent short-cycling after power loss.
* **Failsafe:** On service stop/kill, immediately set all GPIOs to 0.

### 4.4 MQTT Daemon (`thermostat-mqtt`)

* **Responsibility:** Primary control interface via Home Assistant.
* **Protocol:** MQTT Climate entity.
* **Matter Compatible Attributes:** `LocalTemperature`, `OccupiedCoolingSetpoint`, `OccupiedHeatingSetpoint`, `SystemMode`, `FanMode`, `ThermostatRunningState`.
* **Output:** Writes to `system_mode`, `fan_mode`, `set_temp_*`.

### 4.5 WebUI Daemon (`thermostat-web`)

* **Responsibility:** Backup control interface.
* **Tech Stack:** Python Flask.
* **Functionality:**
    * Simple HTML interface for manual control.
    * Visualizes 24-hour temperature history graph (reads `history.json`).

* **Output:** Writes to `system_mode`, `fan_mode`, `set_temp_*`.

## 5. File System Structure (`/run/thermostat/`)

| File Name | Writer(s) | Content | Description |
| --- | --- | --- | --- |
| `current_temp` | Sensor | `float` | The aggregated house temperature. |
| `history.json` | Sensor | `JSON` | List of objects: `[{"t": timestamp, "avg": float, "sensors": {"id": float, ...}}, ...]` |
| `system_mode` | MQTT, Web | `string` | "off", "cool", "heat", "auto". |
| `fan_mode` | MQTT, Web | `string` | "auto", "on". |
| `set_temp_cool` | MQTT, Web | `float` | Cooling target. |
| `set_temp_heat` | MQTT, Web | `float` | Heating target. |
| `hvac_action` | Control | `string` | Current action: "idle", "heating", "cooling", "fan". |

## 6. Project Repository Structure

The source code in the git repository is organized as follows:

* **`src/`**: Contains all Python daemon scripts (e.g., `sensor.py`, `control.py`, `gpio.py`) in a flat directory.
* **`systemd/`**: Contains all systemd unit files (`*.service`).
* **`config/`**: Contains the default configuration (`defaults.json`) to be installed to `/etc/thermostat/`.
