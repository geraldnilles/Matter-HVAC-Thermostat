#!/usr/bin/env python3
"""
Thermostat WebUI Daemon.

Flask-based web interface for manual control and temperature history.
"""

import signal
import sys
from flask import Flask, render_template, request, jsonify

from utils import (
    round_degree,
    CURRENT_TEMP_FILE,
    MIN_TEMP_FILE,
    MAX_TEMP_FILE,
    HISTORY_FILE,
    SYSTEM_MODE_FILE,
    FAN_MODE_FILE,
    SET_TEMP_COOL_FILE,
    SET_TEMP_HEAT_FILE,
    HVAC_ACTION_FILE,
    read_float,
    read_file,
    read_json,
    write_scalar,
)

app = Flask(__name__)

# Global flag for graceful shutdown
running = True


def read_state():
    """Read current thermostat state from IPC files."""
    return {
        "current_temp": read_float(CURRENT_TEMP_FILE),
        "min_temp": read_float(MIN_TEMP_FILE),
        "max_temp": read_float(MAX_TEMP_FILE),
        "system_mode": read_file(SYSTEM_MODE_FILE, default="off"),
        "fan_mode": read_file(FAN_MODE_FILE, default="auto"),
        "set_temp_cool": read_float(SET_TEMP_COOL_FILE, default=74.0),
        "set_temp_heat": read_float(SET_TEMP_HEAT_FILE, default=70.0),
        "hvac_action": read_file(HVAC_ACTION_FILE, default="idle"),
        "history": read_json(HISTORY_FILE, default=[]),
    }


@app.route("/")
def index():
    """Render main thermostat interface."""
    state = read_state()
    return render_template("index.html", state=state)


@app.route("/api/state")
def api_state():
    """Return current state as JSON."""
    return jsonify(read_state())


@app.route("/api/mode", methods=["POST"])
def set_mode():
    """Set system mode."""
    data = request.get_json()
    mode = data.get("mode", "").lower()
    
    valid_modes = ["off", "cool", "heat", "auto"]
    if mode not in valid_modes:
        return jsonify({"error": f"Invalid mode. Must be one of: {valid_modes}"}), 400
    
    write_scalar(SYSTEM_MODE_FILE, mode)
    return jsonify({"success": True, "mode": mode})


@app.route("/api/fan", methods=["POST"])
def set_fan():
    """Set fan mode."""
    data = request.get_json()
    fan = data.get("fan", "").lower()
    
    valid_fans = ["auto", "on"]
    if fan not in valid_fans:
        return jsonify({"error": f"Invalid fan mode. Must be one of: {valid_fans}"}), 400
    
    write_scalar(FAN_MODE_FILE, fan)
    return jsonify({"success": True, "fan": fan})


@app.route("/api/setpoint", methods=["POST"])
def set_setpoint():
    """Set a single temperature setpoint."""
    data = request.get_json()
    setpoint_type = data.get("type", "").lower()
    value = data.get("value")
    
    if setpoint_type not in ["cool", "heat"]:
        return jsonify({"error": "Type must be 'cool' or 'heat'"}), 400
    
    try:
        temp = round_degree(float(value))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid temperature value"}), 400
    
    if setpoint_type == "cool":
        write_scalar(SET_TEMP_COOL_FILE, temp)
    else:
        write_scalar(SET_TEMP_HEAT_FILE, temp)
    
    return jsonify({"success": True, "type": setpoint_type, "value": temp})


@app.route("/api/setpoints", methods=["POST"])
def set_setpoints():
    """Adjust both heat and cool setpoints simultaneously by a delta."""
    data = request.get_json()
    delta = data.get("delta")
    
    try:
        delta = float(delta)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid delta value"}), 400
    
    cool = read_float(SET_TEMP_COOL_FILE, default=74.0)
    heat = read_float(SET_TEMP_HEAT_FILE, default=70.0)
    
    cool = round_degree(cool + delta)
    heat = round_degree(heat + delta)
    
    write_scalar(SET_TEMP_COOL_FILE, cool)
    write_scalar(SET_TEMP_HEAT_FILE, heat)
    
    return jsonify({
        "success": True,
        "set_temp_cool": cool,
        "set_temp_heat": heat,
    })


def signal_handler(signum, frame):
    """Handle shutdown signals."""
    global running
    print(f"\nReceived signal {signum}, shutting down...")
    running = False
    sys.exit(0)


def main():
    """Entry point."""
    # Setup signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    print("WebUI Daemon starting on http://0.0.0.0:5000")
    
    try:
        # Run Flask app
        app.run(host="0.0.0.0", port=5000, threaded=True)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
