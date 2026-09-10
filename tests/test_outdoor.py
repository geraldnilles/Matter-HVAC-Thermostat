"""
Tests for the informational outdoor temperature sensor feature.

Covers three layers, all hardware-free:

* ``utils.get_outdoor_sensor`` / ``utils.partition_sensors`` — the config
  parsing that decides which MAC (if any) is the outdoor sensor and guarantees
  it is excluded from the room allowlist used for HVAC aggregation.
* ``demo.DemoSimulator`` — the outdoor scalar file and per-history ``outdoor``
  field used by the WebUI in local demo mode.
* ``web.read_state`` — exposing ``outdoor_temp`` / ``outdoor_configured`` so the
  dashboard can decide whether to render the card.

``src/sensor.py`` itself is not imported here because it depends on ``bleak``
(BLE), which is not installed in the test environment; its outdoor handling is
built on the pure ``utils`` helpers exercised below.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import utils  # noqa: E402
from utils import get_outdoor_sensor, partition_sensors  # noqa: E402


class TestGetOutdoorSensor(unittest.TestCase):
    def test_missing_key_returns_none(self):
        self.assertIsNone(get_outdoor_sensor({}))

    def test_empty_string_returns_none(self):
        self.assertIsNone(get_outdoor_sensor({"outdoor_sensor": ""}))

    def test_whitespace_only_returns_none(self):
        self.assertIsNone(get_outdoor_sensor({"outdoor_sensor": "   "}))

    def test_non_string_returns_none(self):
        self.assertIsNone(get_outdoor_sensor({"outdoor_sensor": 123}))

    def test_mac_is_upper_cased_and_stripped(self):
        self.assertEqual(
            get_outdoor_sensor({"outdoor_sensor": "  a4:c1:38:00:00:03 "}),
            "A4:C1:38:00:00:03",
        )


class TestPartitionSensors(unittest.TestCase):
    def test_rooms_upper_cased_and_outdoor_present(self):
        config = {
            "sensors": {"a4:c1:38:00:00:01": "Living Room"},
            "outdoor_sensor": "a4:c1:38:00:00:09",
        }
        rooms, outdoor = partition_sensors(config)
        self.assertEqual(rooms, {"A4:C1:38:00:00:01": "Living Room"})
        self.assertEqual(outdoor, "A4:C1:38:00:00:09")

    def test_outdoor_excluded_from_rooms_when_duplicated(self):
        # If the same MAC is listed as a room and as the outdoor sensor, the
        # outdoor assignment wins so it can never skew room aggregation.
        config = {
            "sensors": {"A4:C1:38:00:00:09": "Patio"},
            "outdoor_sensor": "A4:C1:38:00:00:09",
        }
        rooms, outdoor = partition_sensors(config)
        self.assertEqual(rooms, {})
        self.assertEqual(outdoor, "A4:C1:38:00:00:09")

    def test_no_outdoor_configured(self):
        rooms, outdoor = partition_sensors({"sensors": {"AA": "Room"}})
        self.assertEqual(rooms, {"AA": "Room"})
        self.assertIsNone(outdoor)

    def test_sensor_key_casing_duplicate_outdoor(self):
        # Outdoor configured lower-case must still remove an upper-case room.
        config = {
            "sensors": {"A4:C1:38:00:00:09": "Patio"},
            "outdoor_sensor": "a4:c1:38:00:00:09",
        }
        rooms, outdoor = partition_sensors(config)
        self.assertEqual(rooms, {})
        self.assertEqual(outdoor, "A4:C1:38:00:00:09")


class TestDemoOutdoor(unittest.TestCase):
    """Demo mode writes outdoor_temp and tags history samples."""

    def test_demo_writes_outdoor_scalar_and_history(self):
        from demo import DemoSimulator

        data_dir = Path(tempfile.mkdtemp(prefix="demo-outdoor-"))
        sim = DemoSimulator(data_dir=data_dir, seed=1)
        sim.seed_history(hours=2)

        outdoor = utils.read_float(data_dir / "outdoor_temp")
        self.assertIsNotNone(outdoor)

        history = utils.read_json(data_dir / "history.json")
        self.assertTrue(history)
        self.assertTrue(all(isinstance(h.get("outdoor"), float) for h in history))
        # The outdoor value is the ambient curve, not a room average; ensure it
        # is not accidentally identical-everywhere (sanity that it is populated).
        self.assertTrue(any(h["outdoor"] != h["avg"] for h in history))


class TestWebState(unittest.TestCase):
    """web.read_state exposes the outdoor fields and hides the card when unset."""

    def setUp(self):
        import web  # noqa: E402

        self.web = web
        self.tmpdir = Path(tempfile.mkdtemp(prefix="web-outdoor-"))
        self._orig_paths = {}
        for fname in ("CURRENT_TEMP_FILE", "MIN_TEMP_FILE", "MAX_TEMP_FILE",
                      "OUTDOOR_TEMP_FILE", "HISTORY_FILE", "SYSTEM_MODE_FILE",
                      "FAN_MODE_FILE", "SET_TEMP_COOL_FILE", "SET_TEMP_HEAT_FILE",
                      "HVAC_ACTION_FILE"):
            self._orig_paths[fname] = getattr(self.web, fname)
            setattr(self.web, fname, self.tmpdir / fname.lower().removesuffix("_file"))
        self._orig_defaults = self.web.DEFAULTS_PATH
        self.defaults = self.tmpdir / "defaults.json"

    def _write_defaults(self, config):
        self.defaults.write_text(json.dumps(config))
        self.web.DEFAULTS_PATH = self.defaults

    def tearDown(self):
        import shutil

        for fname, path in self._orig_paths.items():
            setattr(self.web, fname, path)
        self.web.DEFAULTS_PATH = self._orig_defaults
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_unconfigured_hides_card(self):
        # The demo default /etc path does not exist and no reading is present.
        self.web.DEFAULTS_PATH = self.tmpdir / "does-not-exist.json"
        state = self.web.read_state()
        self.assertIsNone(state["outdoor_temp"])
        self.assertFalse(state["outdoor_configured"])

    def test_configured_but_stale_shows_card_with_none(self):
        self._write_defaults({"sensors": {}, "outdoor_sensor": "AA:BB"})
        state = self.web.read_state()
        self.assertIsNone(state["outdoor_temp"])
        self.assertTrue(state["outdoor_configured"])

    def test_reading_present_exposes_value_and_card(self):
        self._write_defaults({"sensors": {}})
        utils.write_scalar(self.web.OUTDOOR_TEMP_FILE, 41.5)
        state = self.web.read_state()
        self.assertEqual(state["outdoor_temp"], 41.5)
        self.assertTrue(state["outdoor_configured"])


if __name__ == "__main__":
    unittest.main()
