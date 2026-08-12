#!/usr/bin/env python3
"""
Thermostat GPIO Daemon.

Reads hvac_action from IPC and drives relay GPIO pins.
Uses libgpiod for hardware control.
"""

import signal
import subprocess
import sys
import time

from utils import HVAC_ACTION_FILE, read_file

# GPIO pin definitions (BCM numbering per spec 2.1)
PINS = {"fan": 20, "compressor": 21, "heat": 26}

CHIP = "gpiochip0"
POLL_INTERVAL = 1.0  # seconds

# Global reference to the gpioset subprocess
gpio_process = None


def detect_chip_flag() -> bool:
    """
    Check if gpioset requires the -c/--chip flag (libgpiod v2+).

    libgpiod v1 syntax: gpioset <chip> <line>=<val> ...
    libgpiod v2 syntax: gpioset -c <chip> <line>=<val> ...
    """
    try:
        res = subprocess.run(["gpioset", "--version"], capture_output=True, text=True, check=False)
        output = (res.stdout or "") + (res.stderr or "")
        if "v1." in output or " 1." in output:
            return False
    except Exception:
        pass
    # Default to True (v2) for Debian 12 / Bookworm on Raspberry Pi OS
    return True


USE_CHIP_FLAG = detect_chip_flag()


def build_gpioset_cmd(chip: str, pin_values: list[tuple[int, int]]) -> list[str]:
    """Construct gpioset command arguments matching the installed libgpiod version."""
    line_args = [f"{pin}={val}" for pin, val in pin_values]
    if USE_CHIP_FLAG:
        return ["gpioset", "-c", chip] + line_args
    return ["gpioset", chip] + line_args


def set_all_low():
    """Set all GPIO pins to LOW (failsafe) and cleanup subprocess."""
    global gpio_process
    
    if gpio_process is not None:
        gpio_process.terminate()
        try:
            gpio_process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            gpio_process.kill()
            gpio_process.wait()
        gpio_process = None
    
    # One-shot set to 0 (release immediately)
    cmd = build_gpioset_cmd(CHIP, [
        (PINS['fan'], 0),
        (PINS['compressor'], 0),
        (PINS['heat'], 0)
    ])
    subprocess.run(cmd, check=False)


def apply_state(action: str):
    """
    Apply HVAC action by spawning gpioset to hold lines.
    
    States:
    - idle: all off
    - heating: heat on, fan on
    - cooling: compressor on, fan on  
    - fan: fan on only
    """
    global gpio_process
    
    # Map action to pin values (1=ACTIVE, 0=INACTIVE)
    values = {
        "heating": (1, 0, 1),  # fan, compressor, heat
        "cooling": (1, 1, 0),
        "fan":     (1, 0, 0),
        "idle":    (0, 0, 0)
    }.get(action, (0, 0, 0))
    
    fan_val, comp_val, heat_val = values
    
    # Kill previous gpioset process to release lines
    if gpio_process is not None:
        gpio_process.terminate()
        try:
            gpio_process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            gpio_process.kill()
            gpio_process.wait()
    
    # Spawn new gpioset to hold lines in new state
    cmd = build_gpioset_cmd(CHIP, [
        (PINS['fan'], fan_val),
        (PINS['compressor'], comp_val),
        (PINS['heat'], heat_val)
    ])
    
    try:
        gpio_process = subprocess.Popen(cmd)
    except FileNotFoundError:
        print("Error: gpioset not found. Install libgpiod.", file=sys.stderr)
        sys.exit(1)


def signal_handler(signum, frame):
    """Handle shutdown signals by setting all pins LOW (failsafe)."""
    print(f"\nReceived signal {signum}, shutting down safely...")
    set_all_low()
    sys.exit(0)


def main():
    """Main entry point."""
    # Startup safety delay handled by systemd (ExecStartPre=/bin/sleep 60)
    print("GPIO Daemon starting...")
    
    # Setup signal handlers for graceful shutdown (failsafe to OFF)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        print("GPIO Daemon starting, using gpioset...")
        print("Entering main loop, monitoring hvac_action...")
        
        # Set initial state to idle (all LOW)
        apply_state("idle")
        
        while True:
            # Read current desired action (default to idle if missing)
            action = read_file(HVAC_ACTION_FILE, default="idle")
            
            # Apply to hardware
            apply_state(action)
            
            time.sleep(POLL_INTERVAL)
            
    except Exception as e:
        print(f"Error in main loop: {e}", file=sys.stderr)
        set_all_low()
        sys.exit(1)
    finally:
        set_all_low()


if __name__ == "__main__":
    main()
