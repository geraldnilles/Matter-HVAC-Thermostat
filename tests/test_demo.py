"""Tests for the hardware-free WebUI demo simulator (``src/demo.py``).

These exercise the deterministic pieces — the diurnal ambient curve, the
hysteresis/dwell/action selection, and history seeding — with no Flask app,
no sensors, and no wall-clock dependency. Time is replaced with a fixed value
so the generated history has predictable bounds.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from demo import (  # noqa: E402
    MAX_HISTORY,
    MIN_DWELL_SECONDS,
    DemoSimulator,
)


class TestAmbientCurve(unittest.TestCase):
    """The outdoor temperature model (pure function of now)."""

    def test_minimum_near_midnight(self):
        sim = DemoSimulator()
        now = sim._fraction_of_day
        # Build times via localtime-free epoch math is awkward; use
        # DemoSimulator._fraction_of_day and a fixed epoch chosen at midnight.
        import time
        epoch = 1786464000  # 2026-08-11 00:00:00 local (check via localtime)
        # Normalize to any midnight: find the fraction and subtract to get
        # this instant expressed relative to a known midnight.
        midnight = epoch - sim._fraction_of_day(epoch) * 86400.0

        def ambient_at(hour):
            return sim._ambient(midnight + hour * 3600)

        morning = ambient_at(6)     # ~06:00, cold
        afternoon = ambient_at(15)  # ~15:00, warm
        self.assertLess(morning, afternoon)
        self.assertAlmostEqual(sim._ambient(midnight),
                               sim._ambient(midnight + 86400), places=6)


class TestActionSelection(unittest.TestCase):
    """Hysteresis + dwell + post-cycle purge behavior."""

    def setUp(self):
        self.sim = DemoSimulator(mode="auto", fan="auto", cool=76.0, heat=68.0)
        self.sim.now = 1000.0
        self.sim.action_since = self.sim.now - MIN_DWELL_SECONDS - 1

    def _at(self, min_t, max_t, action=None, since_before=None):
        if action is not None:
            self.sim.action = action
        if since_before is not None:
            self.sim.action_since = self.sim.now - since_before
        return self.sim.choose_action(min_t, max_t, self.sim.now)

    def test_idle_when_within_band(self):
        # 70 min, 75 max: inside both bands -> no demand.
        self.assertEqual(self._at(70.0, 75.0), "idle")

    def test_heat_when_cold(self):
        # Bedroom below heat setpoint by more than hysteresis.
        self.assertEqual(self._at(60.0, 70.0), "heating")

    def test_cool_when_hot(self):
        # Office above cool setpoint by more than hysteresis.
        self.assertEqual(self._at(70.0, 80.0), "cooling")

    def test_auto_conflict_cooling_wins_when_far_hot(self):
        # Both demands true; cooling diff is larger -> prioritize cooling.
        self.assertEqual(self._at(62.0, 85.0), "cooling")

    def test_auto_conflict_heating_wins_when_far_cold(self):
        # Both demands true; heating diff is larger -> prioritize heating.
        self.assertEqual(self._at(50.0, 78.0), "heating")

    def test_hysteresis_holds_heating_until_upper_edge(self):
        # Currently heating; min crossed setpoint but not far enough to cut off.
        self.assertEqual(self._at(68.2, 72.0, action="heating",
                                  since_before=MIN_DWELL_SECONDS + 1),
                         "heating")
        # Crossing the upper edge releases heating into the post-cycle
        # fan purge (spec 4.2), not straight to idle.
        self.assertEqual(self._at(68.6, 72.0, action="heating",
                                  since_before=MIN_DWELL_SECONDS + 1),
                         "fan")

    def test_dwell_latches_action(self):
        # Demand says idle but we just switched recently -> stay heating.
        self.assertEqual(self._at(70.0, 72.0, action="heating",
                                  since_before=MIN_DWELL_SECONDS - 10),
                         "heating")

    def test_post_cycle_fan_purge(self):
        # Heating completes and fan_mode=auto -> fan purge for one dwell.
        self.assertEqual(self._at(70.0, 72.0, action="heating",
                                  since_before=MIN_DWELL_SECONDS + 1),
                         "fan")
        # Purge done -> idle.
        self.assertEqual(self._at(70.0, 72.0, action="fan",
                                  since_before=MIN_DWELL_SECONDS + 1),
                         "idle")

    def test_fan_on_idles_to_fan(self):
        self.sim.fan = "on"
        self.assertEqual(self._at(70.0, 75.0), "fan")

    def test_mode_off_forces_idle(self):
        self.sim.mode = "off"
        self.assertEqual(self._at(60.0, 80.0), "idle")

    def test_mode_off_with_fan_on_gives_fan(self):
        self.sim.mode = "off"
        self.sim.fan = "on"
        self.assertEqual(self._at(60.0, 80.0), "fan")


class TestHistorySeed(unittest.TestCase):
    """Seeding produces a full, ordered, scheme-valid ring buffer."""

    def test_seed_fills_history(self):
        import tempfile
        data_dir = Path(tempfile.mkdtemp(prefix="demo-test-"))
        sim = DemoSimulator(data_dir=data_dir, seed=42)
        sim.seed_history(hours=24)

        self.assertEqual(len(sim.history), MAX_HISTORY)

        # Timestamps ascend by one sampling interval.
        timestamps = [h["t"] for h in sim.history]
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertTrue(all(
            timestamps[i + 1] - timestamps[i] == sim.interval
            for i in range(len(timestamps) - 1)
        ))

        # Every sample carries the required keys and valid values.
        for h in sim.history:
            self.assertIsInstance(h["t"], int)
            self.assertIsInstance(h["avg"], float)
            self.assertIsInstance(h["sensors"], dict)
            self.assertIn("set_temp_cool", h)
            self.assertIn("set_temp_heat", h)
            self.assertIn(h["hvac_action"], ("idle", "heating", "cooling", "fan"))

        # The ring buffer stays bounded on further steps.
        old_tail = sim.history[0]["t"]
        for _ in range(5):
            sim._step(write_ipc=False)
        self.assertEqual(len(sim.history), MAX_HISTORY)
        self.assertGreater(sim.history[0]["t"], old_tail)

    def test_seed_writes_ipc_files(self):
        import tempfile
        data_dir = Path(tempfile.mkdtemp(prefix="demo-test-"))
        sim = DemoSimulator(data_dir=data_dir, seed=7)
        sim.seed_history(hours=6)

        from utils import read_file, read_float, read_json
        self.assertIsNotNone(read_float(data_dir / "current_temp"))
        self.assertIsNotNone(read_float(data_dir / "min_temp"))
        self.assertIsNotNone(read_float(data_dir / "max_temp"))
        self.assertEqual(read_file(data_dir / "system_mode"), sim.mode)
        self.assertEqual(read_file(data_dir / "fan_mode"), sim.fan)
        self.assertIsNotNone(read_float(data_dir / "set_temp_cool"))
        self.assertIsNotNone(read_float(data_dir / "set_temp_heat"))
        self.assertIn(read_file(data_dir / "hvac_action"),
                      ("idle", "heating", "cooling", "fan"))
        self.assertGreater(len(read_json(data_dir / "history.json")), 0)


class TestCLIOptions(unittest.TestCase):
    """web.py CLI flags parse correctly (no server started)."""

    def test_parse_defaults(self):
        import web
        args = web.parse_args([])
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 5000)
        self.assertFalse(args.demo)
        self.assertIsNone(args.data_dir)

    def test_parse_demo_flags(self):
        import web
        args = web.parse_args(["--host", "127.0.0.1", "--port", "8080",
                               "--demo", "--data-dir", "/tmp/x"])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8080)
        self.assertTrue(args.demo)
        self.assertEqual(args.data_dir, "/tmp/x")


if __name__ == "__main__":
    unittest.main()
