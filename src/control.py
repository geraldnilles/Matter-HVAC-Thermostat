#!/usr/bin/env python3
"""
Thermostat Control Daemon.

The 'brain' of the system. Reads sensor data and setpoints, implements
hysteresis and safety timers, and decides HVAC actions.
"""

import signal
import sys
import time
from datetime import datetime
from enum import Enum

from utils import (
    round_degree,
    MIN_TEMP_FILE,
    MAX_TEMP_FILE,
    SYSTEM_MODE_FILE,
    FAN_MODE_FILE,
    SET_TEMP_COOL_FILE,
    SET_TEMP_HEAT_FILE,
    HVAC_ACTION_FILE,
    read_float,
    read_file,
    write_scalar,
)


class SystemMode(Enum):
    OFF = "off"
    COOL = "cool"
    HEAT = "heat"
    AUTO = "auto"


class FanMode(Enum):
    AUTO = "auto"
    ON = "on"


class HvacAction(Enum):
    IDLE = "idle"
    HEATING = "heating"
    COOLING = "cooling"
    FAN = "fan"


# Timing constants (seconds)
STARTUP_DELAY = 60  # 60-second startup safety
MIN_DWELL_TIME = 120  # 120-second minimum dwell time
POLL_INTERVAL = 1.0  # Main loop polling rate

# Temperature constants
HYSTERESIS = 0.5  # +/- 0.5°F (1°F total swing)
MIN_SETPOINT_GAP = 8.0  # Minimum 8°F between heat and cool setpoints


class ControlDaemon:
    """Main control daemon implementing thermostat logic."""
    
    def __init__(self):
        self.current_action = HvacAction.IDLE
        self.last_state_change = 0
        self.startup_complete = False
        self.start_time = time.time()
        
        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown gracefully."""
        print(f"\nReceived signal {signum}, shutting down...")
        # Ensure we leave system in safe state
        self._write_action(HvacAction.IDLE)
        sys.exit(0)
    
    def _seconds_since_startup(self) -> float:
        """Return seconds elapsed since daemon start."""
        return time.time() - self.start_time
    
    def _seconds_in_current_state(self) -> float:
        """Return seconds elapsed since last state change."""
        return time.time() - self.last_state_change
    
    def _dwell_time_remaining(self) -> float:
        """Return remaining dwell time (0 if complete)."""
        elapsed = self._seconds_in_current_state()
        remaining = MIN_DWELL_TIME - elapsed
        return max(0.0, remaining)
    
    def _enforce_setpoint_separation(self, cool_temp: float, heat_temp: float) -> tuple[float, float]:
        """
        Enforce minimum 8°F gap between heat and cool setpoints.

        Incoming setpoints are first snapped to the nearest whole degree
        (defense-in-depth: MQTT/WebUI also snap), which both heals any
        previously-corrupted fractional IPC values and keeps the effective
        setpoints round.

        If setpoints are still too close, expand them symmetrically around
        their average and snap the expanded setpoints to the nearest whole
        degree, so the effective setpoints remain whole numbers. For
        simplicity, we always ensure cool >= heat + 8.
        """
        # Snap inputs to nearest whole degree so stale/corrupt IPC values self-heal
        cool_temp = round_degree(cool_temp)
        heat_temp = round_degree(heat_temp)

        if cool_temp < heat_temp + MIN_SETPOINT_GAP:
            # Gap too small - expand it symmetrically around midpoint,
            # then snap expanded values to whole degrees
            midpoint = (cool_temp + heat_temp) / 2
            cool_temp = round_degree(midpoint + MIN_SETPOINT_GAP / 2)
            heat_temp = round_degree(midpoint - MIN_SETPOINT_GAP / 2)
        return cool_temp, heat_temp
    
    def _read_inputs(self) -> dict:
        """Read all input files."""
        return {
            "min_temp": read_float(MIN_TEMP_FILE),
            "max_temp": read_float(MAX_TEMP_FILE),
            "system_mode": read_file(SYSTEM_MODE_FILE, default="off"),
            "fan_mode": read_file(FAN_MODE_FILE, default="auto"),
            "set_temp_cool": read_float(SET_TEMP_COOL_FILE, default=74.0),
            "set_temp_heat": read_float(SET_TEMP_HEAT_FILE, default=70.0),
        }
    
    def _write_action(self, action: HvacAction):
        """Write HVAC action to IPC file."""
        write_scalar(HVAC_ACTION_FILE, action.value)
        if action != self.current_action:
            print(f"State change: {self.current_action.value} -> {action.value} "
                  f"(dwell complete after {self._seconds_in_current_state():.1f}s)")
            self.last_state_change = time.time()
            self.current_action = action
    
    def _calculate_desired_action(self, inputs: dict) -> HvacAction:
        """
        Calculate desired HVAC action based on inputs and hysteresis.
        
        This implements the core thermostat logic but does NOT check dwell time.
        """
        min_temp = inputs["min_temp"]
        max_temp = inputs["max_temp"]
        system_mode = inputs["system_mode"]
        fan_mode = inputs["fan_mode"]
        set_temp_cool = inputs["set_temp_cool"]
        set_temp_heat = inputs["set_temp_heat"]
        
        # Data failsafe: if no sensor data, force idle
        if min_temp is None or max_temp is None:
            print("WARNING: No sensor data available, forcing idle")
            return HvacAction.IDLE
        
        # Enforce setpoint separation
        original_cool = set_temp_cool
        original_heat = set_temp_heat
        set_temp_cool, set_temp_heat = self._enforce_setpoint_separation(
            set_temp_cool, set_temp_heat
        )
        
        # Write back adjusted setpoints if they changed so UI shows effective values
        if set_temp_cool != original_cool:
            write_scalar(SET_TEMP_COOL_FILE, set_temp_cool)
        if set_temp_heat != original_heat:
            write_scalar(SET_TEMP_HEAT_FILE, set_temp_heat)
        
        # Determine what heating and cooling want to do
        # Heating: compare min_temp (coldest room) against set_temp_heat
        heat_demand = min_temp < (set_temp_heat - HYSTERESIS)
        heat_satisfied = min_temp >= (set_temp_heat + HYSTERESIS)
        
        # Cooling: compare max_temp (hottest room) against set_temp_cool
        cool_demand = max_temp > (set_temp_cool + HYSTERESIS)
        cool_satisfied = max_temp <= (set_temp_cool - HYSTERESIS)
        
        # Determine desired state based on current action and inputs
        heating_active = self.current_action == HvacAction.HEATING
        cooling_active = self.current_action == HvacAction.COOLING
        
        want_heat = False
        want_cool = False
        
        if system_mode in ("heat", "auto"):
            if heating_active:
                # Continue heating until satisfied
                want_heat = not heat_satisfied
            else:
                # Start heating if demanded
                want_heat = heat_demand
        
        if system_mode in ("cool", "auto"):
            if cooling_active:
                # Continue cooling until satisfied
                want_cool = not cool_satisfied
            else:
                # Start cooling if demanded
                want_cool = cool_demand
        
        # Priority: only one can be active at a time
        # If both want to run, continue current action or default to idle
        if want_heat and want_cool:
            if heating_active:
                want_cool = False
            elif cooling_active:
                want_heat = False
            else:
                # Neither active, pick based on which is more needed
                heat_diff = set_temp_heat - min_temp
                cool_diff = max_temp - set_temp_cool
                if heat_diff > cool_diff:
                    want_cool = False
                else:
                    want_heat = False
        
        # Determine action
        if want_heat:
            desired = HvacAction.HEATING
        elif want_cool:
            desired = HvacAction.COOLING
        elif self.current_action in (HvacAction.HEATING, HvacAction.COOLING):
            # Post-cycle fan purge: when heating/cooling completes, run the
            # fan for the minimum dwell time to extract residual thermal energy
            desired = HvacAction.FAN
        else:
            desired = HvacAction.IDLE
        
        # Fan mode "on" overrides: if fan is on and system would be idle,
        # we go to fan-only mode
        if fan_mode == "on" and desired == HvacAction.IDLE:
            desired = HvacAction.FAN
        
        return desired
    
    def _should_change_state(self, desired: HvacAction) -> bool:
        """
        Check if state change is allowed considering dwell time.
        
        The 120-second dwell timer overrides ALL other inputs.
        """
        if desired == self.current_action:
            return False
        
        remaining = self._dwell_time_remaining()
        if remaining > 0:
            print(f"Dwell timer active: {remaining:.1f}s remaining, "
                  f"staying in {self.current_action.value}")
            return False
        
        return True
    
    def run(self):
        """Main control loop."""
        print("Control Daemon starting...")
        print(f"Waiting {STARTUP_DELAY}s startup safety delay...")
        
        while True:
            time.sleep(POLL_INTERVAL)
            
            # Startup safety delay
            if not self.startup_complete:
                elapsed = self._seconds_since_startup()
                if elapsed < STARTUP_DELAY:
                    continue
                self.startup_complete = True
                self.last_state_change = time.time()  # Start dwell timer after init
                print("Startup complete, entering control loop")
            
            # Read all inputs
            inputs = self._read_inputs()
            
            # Calculate what we want to do
            desired = self._calculate_desired_action(inputs)
            
            # Check if we're allowed to change (dwell timer)
            if self._should_change_state(desired):
                self._write_action(desired)
            else:
                # Just ensure the file matches our internal state
                write_scalar(HVAC_ACTION_FILE, self.current_action.value)


def main():
    """Entry point."""
    daemon = ControlDaemon()
    
    try:
        daemon.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
        daemon._write_action(HvacAction.IDLE)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        # Try to failsafe
        try:
            write_scalar(HVAC_ACTION_FILE, "idle")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
