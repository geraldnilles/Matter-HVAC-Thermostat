#!/usr/bin/env python3
"""
Thermostat GPIO Daemon.

Reads hvac_action from IPC and drives relay GPIO pins.
Uses libgpiod for hardware control.
"""

import signal
import sys
import time

import gpiod
from gpiod.line import Direction, Value

from utils import HVAC_ACTION_FILE, read_file

# GPIO pin definitions (BCM numbering per spec 2.1)
PIN_FAN = 20
PIN_COMPRESSOR = 21
PIN_HEAT = 26

CHIP_PATH = "/dev/gpiochip0"
POLL_INTERVAL = 1.0  # seconds

# Global references for signal handler cleanup
lines = None


def setup_gpio():
    """Initialize GPIO lines as outputs, all LOW."""
    global lines
    
    chip = gpiod.Chip(CHIP_PATH)
    
    # Request lines as outputs, initially LOW (OFF - safety)
    lines = chip.request_lines(
        consumer="thermostat-gpio",
        config={
            PIN_FAN: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE),
            PIN_COMPRESSOR: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE),
            PIN_HEAT: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE),
        }
    )
    print("GPIO initialized: Fan=20, Compressor=21, Heat=26")


def set_all_low():
    """Set all GPIO pins to LOW (failsafe)."""
    if lines:
        lines.set_values({
            PIN_FAN: Value.INACTIVE,
            PIN_COMPRESSOR: Value.INACTIVE,
            PIN_HEAT: Value.INACTIVE,
        })


def apply_state(action: str):
    """
    Apply HVAC action to GPIO pins.
    
    States:
    - idle: all off
    - heating: heat on, fan on
    - cooling: compressor on, fan on  
    - fan: fan on only
    """
    if action == "heating":
        lines.set_values({
            PIN_FAN: Value.ACTIVE,
            PIN_COMPRESSOR: Value.INACTIVE,
            PIN_HEAT: Value.ACTIVE,
        })
    elif action == "cooling":
        lines.set_values({
            PIN_FAN: Value.ACTIVE,
            PIN_COMPRESSOR: Value.ACTIVE,
            PIN_HEAT: Value.INACTIVE,
        })
    elif action == "fan":
        lines.set_values({
            PIN_FAN: Value.ACTIVE,
            PIN_COMPRESSOR: Value.INACTIVE,
            PIN_HEAT: Value.INACTIVE,
        })
    else:  # idle or unknown
        set_all_low()


def signal_handler(signum, frame):
    """Handle shutdown signals by setting all pins LOW (failsafe)."""
    print(f"\nReceived signal {signum}, shutting down safely...")
    set_all_low()
    sys.exit(0)


def main():
    """Main entry point."""
    # Startup safety delay: prevent short-cycling after power loss
    print("GPIO Daemon starting, waiting 60 seconds for system stability...")
    time.sleep(60)
    
    # Setup signal handlers for graceful shutdown (failsafe to OFF)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        setup_gpio()
        
        print("Entering main loop, monitoring hvac_action...")
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


if __name__ == "__main__":
    main()
