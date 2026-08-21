#!/usr/bin/env python3
"""
Thermostat initialization script.

One-shot service that runs on boot to seed IPC files from persistent configuration.
Reads /etc/thermostat/defaults.json and writes initial values to /run/thermostat/.
"""

import json
import sys
from pathlib import Path

from utils import (
    SYSTEM_MODE_FILE,
    FAN_MODE_FILE,
    SET_TEMP_COOL_FILE,
    SET_TEMP_HEAT_FILE,
    write_scalar,
    IPC_DIR,
)

DEFAULTS_PATH = Path("/etc/thermostat/defaults.json")


def load_defaults(filepath: Path) -> dict:
    """Load and parse the defaults configuration file."""
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def initialize_ipc_files(config: dict) -> None:
    """
    Write initial values from config to IPC files.
    
    Only writes files that don't already exist, preserving state across
    service restarts while ensuring first-boot initialization.
    """
    # Ensure IPC directory exists
    IPC_DIR.mkdir(parents=True, exist_ok=True)
    
    # Map config keys to IPC files
    mappings = {
        "system_mode": SYSTEM_MODE_FILE,
        "fan_mode": FAN_MODE_FILE,
        "set_temp_cool": SET_TEMP_COOL_FILE,
        "set_temp_heat": SET_TEMP_HEAT_FILE,
    }
    
    for key, filepath in mappings.items():
        if key not in config:
            continue
        if filepath.exists():
            # Pre-existing state wins: never clobber values already present in
            # the IPC directory. This preserves state across service restarts
            # and lets an external backup/restore service seed /run/thermostat/
            # either before or after this service runs.
            print(f"Skipped {filepath}: already exists, preserving existing value")
            continue
        # First boot (or freshly wiped tmpfs): seed from defaults.json
        write_scalar(filepath, config[key])
        print(f"Initialized {filepath}: {config[key]}")
    
    print("Thermostat IPC initialization complete.")


def main():
    """Main entry point."""
    if not DEFAULTS_PATH.exists():
        print(f"Error: Defaults file not found: {DEFAULTS_PATH}", file=sys.stderr)
        sys.exit(1)
    
    try:
        config = load_defaults(DEFAULTS_PATH)
        initialize_ipc_files(config)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in {DEFAULTS_PATH}: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error during initialization: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
