"""Tests for the setpoint schedule script (``src/schedule.py``).

Exercises the schedule helper's argument parsing and atomic setpoint
application with no hardware and no real wall clock: IPC file paths are
redirected into a per-test temporary directory.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import schedule  # noqa: E402
import utils  # noqa: E402
from schedule import apply_setpoints, build_parser  # noqa: E402


class ScheduleScriptTests(unittest.TestCase):
    """Fixture that points schedule's IPC file constants into a temp dir."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="thermostat-schedule-"))
        self._orig_heat = schedule.SET_TEMP_HEAT_FILE
        self._orig_cool = schedule.SET_TEMP_COOL_FILE
        schedule.SET_TEMP_HEAT_FILE = self.tmpdir / "set_temp_heat"
        schedule.SET_TEMP_COOL_FILE = self.tmpdir / "set_temp_cool"

    def tearDown(self):
        schedule.SET_TEMP_HEAT_FILE = self._orig_heat
        schedule.SET_TEMP_COOL_FILE = self._orig_cool
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def read_heat(self):
        return utils.read_float(schedule.SET_TEMP_HEAT_FILE)

    def read_cool(self):
        return utils.read_float(schedule.SET_TEMP_COOL_FILE)

    def test_morning_profile_writes_whole_degrees(self):
        apply_setpoints(68.0, 76.0)
        self.assertEqual(self.read_heat(), 68.0)
        self.assertEqual(self.read_cool(), 76.0)

    def test_night_profile_writes_whole_degrees(self):
        apply_setpoints(67.0, 75.0)
        self.assertEqual(self.read_heat(), 67.0)
        self.assertEqual(self.read_cool(), 75.0)

    def test_fractional_input_is_snapped(self):
        # 67.4 -> 67, 75.6 -> 76 (half-up rounding, matching utils.round_degree)
        apply_setpoints(67.4, 75.6)
        self.assertEqual(self.read_heat(), 67.0)
        self.assertEqual(self.read_cool(), 76.0)

    def test_scalar_files_end_with_single_newline(self):
        apply_setpoints(68.0, 76.0)
        for path in (schedule.SET_TEMP_HEAT_FILE, schedule.SET_TEMP_COOL_FILE):
            raw = path.read_text(encoding="utf-8")
            self.assertEqual(raw, raw.rstrip("\n") + "\n")

    def test_parser_requires_heat_and_cool(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])
        with self.assertRaises(SystemExit):
            parser.parse_args(["--heat", "68"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["--cool", "76"])

    def test_parser_accepts_float_values(self):
        args = build_parser().parse_args(["--heat", "67.5", "--cool", "75.5"])
        self.assertEqual(args.heat, 67.5)
        self.assertEqual(args.cool, 75.5)


if __name__ == "__main__":
    unittest.main()
