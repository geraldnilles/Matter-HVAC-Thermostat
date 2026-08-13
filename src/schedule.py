#!/usr/bin/env python3
"""
Thermostat setpoint schedule script.

Applies a heat/cool setpoint pair to the thermostat's IPC files. Intended to
be driven by systemd timer units on a fixed daily cadence (e.g. a wake-up and
a sleep profile). Values are snapped to the nearest whole degree and written
atomically via ``utils.write_scalar``, matching the conventions used by the
MQTT and WebUI daemons.
"""

import argparse
import sys

from utils import (
    round_degree,
    SET_TEMP_COOL_FILE,
    SET_TEMP_HEAT_FILE,
    write_scalar,
)


def apply_setpoints(heat: float, cool: float) -> None:
    """
    Write the heating and cooling setpoints to IPC atomically.

    Both setpoints are snapped to the nearest whole degree °F before writing,
    consistent with every other setpoint writer (MQTT, WebUI, control).
    """
    heat = round_degree(heat)
    cool = round_degree(cool)

    write_scalar(SET_TEMP_HEAT_FILE, heat)
    write_scalar(SET_TEMP_COOL_FILE, cool)

    print(f"Applied schedule setpoints: heat={heat:.0f}°F cool={cool:.0f}°F")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser (separated for testability)."""
    parser = argparse.ArgumentParser(
        description="Apply thermostat heat/cool setpoints."
    )
    parser.add_argument(
        "--heat",
        type=float,
        required=True,
        help="Heating setpoint in °F",
    )
    parser.add_argument(
        "--cool",
        type=float,
        required=True,
        help="Cooling setpoint in °F",
    )
    return parser


def main(argv=None) -> int:
    """CLI entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    apply_setpoints(args.heat, args.cool)
    return 0


if __name__ == "__main__":
    sys.exit(main())
