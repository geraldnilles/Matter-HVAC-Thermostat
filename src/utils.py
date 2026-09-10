"""
Shared utilities for thermostat daemons.

Provides atomic file operations and IPC path constants.
All writes to shared state files must be atomic to prevent race conditions.
"""

import os
import math
import json
from pathlib import Path

# IPC directory (tmpfs)
IPC_DIR = Path("/run/thermostat")

# File paths for IPC
CURRENT_TEMP_FILE = IPC_DIR / "current_temp"
MIN_TEMP_FILE = IPC_DIR / "min_temp"
MAX_TEMP_FILE = IPC_DIR / "max_temp"
OUTDOOR_TEMP_FILE = IPC_DIR / "outdoor_temp"
HISTORY_FILE = IPC_DIR / "history.json"
SYSTEM_MODE_FILE = IPC_DIR / "system_mode"
FAN_MODE_FILE = IPC_DIR / "fan_mode"
SET_TEMP_COOL_FILE = IPC_DIR / "set_temp_cool"
SET_TEMP_HEAT_FILE = IPC_DIR / "set_temp_heat"
HVAC_ACTION_FILE = IPC_DIR / "hvac_action"


def round_degree(value: float) -> float:
    """
    Round a temperature to the nearest whole degree °F, ensuring clean setpoints.

    Prevents fractional artifacts (e.g. 73.125) from appearing in IPC files
    via the control daemon's setpoint-gap enforcement, MQTT, or WebUI writes.

    Uses half-up rounding (math.floor with +0.5) rather than Python's
    banker's round(), so e.g. 73.4 -> 73.0, 73.5 -> 74.0.
    """
    return math.floor(value + 0.5)


def get_outdoor_sensor(config: dict) -> str | None:
    """
    Return the normalized MAC address of the configured outdoor sensor.

    The outdoor sensor is an *informational* add-on: it is temperature data
    collected from a single sensor mounted outside the house and is never
    averaged with (or allowed to influence) the room sensors. It is therefore
    configured as its own top-level ``outdoor_sensor`` key in ``defaults.json``
    rather than inside the ``sensors`` allowlist.

    Args:
        config: Parsed ``defaults.json`` mapping.

    Returns:
        The sensor MAC address upper-cased (matching BLE ``device.address``
        normalization), or ``None`` when unset/blank/invalid.
    """
    mac = config.get("outdoor_sensor")
    if not isinstance(mac, str):
        return None
    mac = mac.strip().upper()
    return mac or None


def partition_sensors(config: dict) -> tuple[dict, str | None]:
    """
    Split ``defaults.json`` sensor config into room sensors and the outdoor one.

    Room sensors (the ``sensors`` allowlist) are aggregated into the min/max/avg
    used for HVAC control. The optional ``outdoor_sensor`` is informational and
    must be kept out of that aggregation, so this helper returns it separately
    and removes it from the room map if the same MAC appears in both.

    Args:
        config: Parsed ``defaults.json`` mapping.

    Returns:
        ``(room_sensors, outdoor_mac)`` where ``room_sensors`` maps an
        upper-cased MAC to its display name and ``outdoor_mac`` is the
        upper-cased outdoor MAC (or ``None`` when not configured).
    """
    room_sensors = {
        str(mac).upper(): name
        for mac, name in (config.get("sensors", {}) or {}).items()
    }
    outdoor_mac = get_outdoor_sensor(config)
    if outdoor_mac:
        room_sensors.pop(outdoor_mac, None)
    return room_sensors, outdoor_mac


def atomic_write(filepath: Path, content: str) -> None:
    """
    Atomically write content to a file.
    
    Creates a unique temporary file using PID, writes content, fsyncs to disk,
    then atomically renames to target. This ensures readers never see
    partial writes, even with concurrent writers (e.g., MQTT and WebUI).
    
    Per AGENTS.md (IPC section): writers must create a unique temp file, flush to disk,
    and atomically rename to prevent race conditions.
    
    Args:
        filepath: Target file path (Path object)
        content: String content to write (caller must include newlines if needed)
    """
    # Ensure parent directory exists
    filepath.parent.mkdir(parents=True, exist_ok=True)
    
    # Create unique temp file with PID to avoid collisions between concurrent writers
    temp_path = Path(f"{filepath}.tmp.{os.getpid()}")
    
    try:
        # Write to temp file
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(content)
            # Ensure data is flushed to disk before rename
            f.flush()
            os.fsync(f.fileno())
        
        # Atomic rename: this is the commit point
        os.replace(temp_path, filepath)
        
    except Exception:
        # Clean up temp file on failure, but don't mask the original exception
        if temp_path.exists():
            temp_path.unlink()
        raise


def read_file(filepath: Path, default=None) -> str | None:
    """
    Read content from a file.
    
    Args:
        filepath: File to read
        default: Value to return if file doesn't exist or error occurs
        
    Returns:
        File content as string (newlines stripped), or default value
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read().strip()
    except (FileNotFoundError, IOError):
        return default


def write_scalar(filepath: Path, value: float | str) -> None:
    """
    Write a scalar value to IPC file with proper formatting.
    
    Per AGENTS.md (IPC format rules): content must be UTF-8 plain text followed immediately 
    by a single newline character.
    
    Args:
        filepath: Target file path
        value: Value to write (float or string)
    """
    content = f"{value}\n"
    atomic_write(filepath, content)


def read_float(filepath: Path, default: float | None = None) -> float | None:
    """
    Read a float value from IPC file.
    
    Args:
        filepath: File to read
        default: Default value if file missing or invalid
        
    Returns:
        Float value or default
    """
    content = read_file(filepath)
    if content is None:
        return default
    try:
        return float(content)
    except ValueError:
        return default


def read_json(filepath: Path, default=None):
    """
    Read and parse JSON file.
    
    Args:
        filepath: File to read
        default: Default value if file missing or invalid
        
    Returns:
        Parsed JSON or default
    """
    if default is None:
        default = []
    content = read_file(filepath)
    if content is None:
        return default
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return default


def write_json(filepath: Path, data) -> None:
    """
    Write data as JSON atomically.
    
    Args:
        filepath: Target file path
        data: Data to serialize as JSON
    """
    # Use compact separators for efficiency
    content = json.dumps(data, separators=(',', ':'))
    atomic_write(filepath, content)
