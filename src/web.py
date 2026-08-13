#!/usr/bin/env python3
"""
Thermostat WebUI Daemon.

Flask-based web interface for manual control and temperature history.
"""

import argparse
import signal
import sys
from pathlib import Path
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

# Demo-mode simulator, created lazily so production imports never touch demo.py.
_demo_simulator = None

# Attribute name -> relative file name inside an IPC directory. Used to
# repoint utils' module-level file constants at an alternate data directory
# (demo mode) or restore them to the production defaults.
_IPC_FILE_NAMES = {
    "CURRENT_TEMP_FILE": "current_temp",
    "MIN_TEMP_FILE": "min_temp",
    "MAX_TEMP_FILE": "max_temp",
    "HISTORY_FILE": "history.json",
    "SYSTEM_MODE_FILE": "system_mode",
    "FAN_MODE_FILE": "fan_mode",
    "SET_TEMP_COOL_FILE": "set_temp_cool",
    "SET_TEMP_HEAT_FILE": "set_temp_heat",
    "HVAC_ACTION_FILE": "hvac_action",
}


def redirect_ipc(data_dir):
    """
    Point every IPC file constant used by this module at ``data_dir``.

    ``utils`` snapshots its file paths as module-level constants at import
    time, so we must patch the constants on both ``utils`` and this module.
    Returns ``None`` when ``data_dir`` is None (meaning "use production
    defaults"), otherwise returns the ``pathlib.Path`` used.
    """
    import utils

    if data_dir is None:
        # Restore production defaults from utils.IPC_DIR.
        base = utils.IPC_DIR
        for attr, fname in _IPC_FILE_NAMES.items():
            setattr(utils, attr, base / fname)
        for attr in _IPC_FILE_NAMES:
            setattr(sys.modules[__name__], attr, getattr(utils, attr))
        return None

    data_dir = Path(data_dir)
    for attr, fname in _IPC_FILE_NAMES.items():
        path = data_dir / fname
        setattr(utils, attr, path)
        setattr(sys.modules[__name__], attr, path)
    return data_dir


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


def parse_args(argv=None):
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Thermostat WebUI daemon with an optional hardware-free demo mode."
    )
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="Address to bind (default: %(default)s).",
    )
    parser.add_argument(
        "--port", type=int, default=5000,
        help="Port to listen on (default: %(default)s).",
    )
    parser.add_argument(
        "--data-dir", default=None,
        help="Directory for IPC state files (default: production "
             "/run/thermostat).",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="Run with canned sensor data (no sensors, GPIOs, or relays). "
             "When combined with --data-dir the simulated state is written "
             "there; otherwise a fresh temporary directory is used.",
    )
    return parser.parse_args(argv)


def start_demo(data_dir):
    """Create and seed the canned-data simulator (local testing only)."""
    import tempfile

    from demo import DemoSimulator

    if data_dir is None:
        data_dir = tempfile.mkdtemp(prefix="thermostat-demo-")

    global _demo_simulator
    sim = DemoSimulator(data_dir=data_dir)
    sim.seed_history()      # pre-fill the 24 h graph
    sim.start()             # begin feeding live samples
    _demo_simulator = sim   # keep a reference so it is not garbage-collected
    return data_dir


def main(argv=None):
    """Entry point."""
    args = parse_args(argv)

    # Setup signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    if args.demo:
        # Local, hardware-free demo: repoint IPC at a private data directory
        # and start the canned-data simulator.
        data_dir = start_demo(args.data_dir)
        redirect_ipc(data_dir)
        print(f"Demo mode starting on http://{args.host}:{args.port} "
              f"(state in {data_dir})")
    else:
        # Production mode: read/write the real IPC directory.
        redirect_ipc(args.data_dir)
        print(f"WebUI Daemon starting on http://{args.host}:{args.port}")

    try:
        # Run Flask app
        app.run(host=args.host, port=args.port, threaded=True)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
