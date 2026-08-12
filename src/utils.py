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
HISTORY_FILE = IPC_DIR / "history.json"
SYSTEM_MODE_FILE = IPC_DIR / "system_mode"
FAN_MODE_FILE = IPC_DIR / "fan_mode"
SET_TEMP_COOL_FILE = IPC_DIR / "set_temp_cool"
SET_TEMP_HEAT_FILE = IPC_DIR / "set_temp_heat"
HVAC_ACTION_FILE = IPC_DIR / "hvac_action"


def round_half(value: float) -> float:
    """
    Round a temperature to the nearest 0.5°F step, ensuring clean setpoints.

    Prevents fractional artifacts (e.g. 73.125) from appearing in IPC files
    via the control daemon's setpoint-gap enforcement, MQTT, or WebUI writes.

    Uses half-up rounding (math.floor with +0.5) rather than Python's
    banker's round(), so e.g. 73.25 -> 73.5, not 73.0.
    """
    return math.floor(value * 2 + 0.5) / 2


def atomic_write(filepath: Path, content: str) -> None:
    """
    Atomically write content to a file.
    
    Creates a unique temporary file using PID, writes content, fsyncs to disk,
    then atomically renames to target. This ensures readers never see
    partial writes, even with concurrent writers (e.g., MQTT and WebUI).
    
    Per spec 3.2: Writers must create a unique temp file, flush to disk,
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
    
    Per spec 5.1.1: content must be UTF-8 plain text followed immediately 
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
