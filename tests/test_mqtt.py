"""
Tests for the MQTT bridge daemon (src/mqtt.py).

Focus: the single-setpoint command (``thermostat/temperature/set``), which is
only honoured in the single-setpoint modes (``cool``/``heat``) and must be
ignored with a logged error in the temperature-range mode (``auto``) and in
``off``.

No MQTT broker is contacted: the daemon is built with ``object.__new__`` and a
recording fake client, and IPC state is redirected into a temp dir (same
approach as ``tests/ipc_env.py``).
"""

import contextlib
import importlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import utils  # noqa: E402

# mqtt.py snapshots the IPC paths from utils at import time, so patch utils
# first, then (re)load mqtt so its ``from utils import ...`` picks them up.
_ORIGINAL_IPC_DIR = utils.IPC_DIR
_FILE_NAMES = {
    "CURRENT_TEMP_FILE": "current_temp",
    "SYSTEM_MODE_FILE": "system_mode",
    "FAN_MODE_FILE": "fan_mode",
    "SET_TEMP_COOL_FILE": "set_temp_cool",
    "SET_TEMP_HEAT_FILE": "set_temp_heat",
    "HVAC_ACTION_FILE": "hvac_action",
}


def _load_mqtt():
    """(Re)import mqtt with no broker config present, swallowing its warnings."""
    with contextlib.redirect_stdout(io.StringIO()):
        if "mqtt" in sys.modules:
            importlib.reload(sys.modules["mqtt"])
        else:
            importlib.import_module("mqtt")
    return sys.modules["mqtt"]


class FakeClient:
    """Records publishes/subscribes in place of a real paho MQTT client."""

    def __init__(self):
        self.published = []      # (topic, payload, retain)
        self.subscribed = []     # topic

    def publish(self, topic, payload, retain=False, **kwargs):
        self.published.append((topic, payload, retain))

    def subscribe(self, topic, *args, **kwargs):
        self.subscribed.append(topic)


class SingleSetpointTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="thermostat-mqtt-tests-"))
        utils.IPC_DIR = self.tmpdir
        for attr, fname in _FILE_NAMES.items():
            setattr(utils, attr, self.tmpdir / fname)

        self.mqtt = _load_mqtt()
        for attr, fname in _FILE_NAMES.items():
            setattr(self.mqtt, attr, self.tmpdir / fname)

        self.daemon = object.__new__(self.mqtt.MqttDaemon)
        self.daemon.client = FakeClient()
        self.daemon.running = True

        # Baseline state: cool=74, heat=70 (unchanged unless a test writes them)
        utils.write_scalar(utils.SET_TEMP_COOL_FILE, 74.0)
        utils.write_scalar(utils.SET_TEMP_HEAT_FILE, 70.0)

    def tearDown(self):
        utils.IPC_DIR = _ORIGINAL_IPC_DIR
        for attr, fname in _FILE_NAMES.items():
            setattr(utils, attr, _ORIGINAL_IPC_DIR / fname)
        _load_mqtt()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---------------- helpers ---------------- #
    def set_mode(self, mode):
        utils.write_scalar(utils.SYSTEM_MODE_FILE, mode)

    def send_temp(self, payload):
        """Invoke the single-setpoint handler, capturing stdout+stderr."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.mqtt.MqttDaemon._handle_single_setpoint(self.daemon, payload)
        return out.getvalue(), err.getvalue()

    def send_message(self, topic, payload):
        """Drive the full _on_message dispatch path for a raw MQTT message."""
        msg = type("Msg", (), {"topic": topic, "payload": payload.encode()})()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.daemon._on_message(self.daemon.client, None, msg)
        return out.getvalue(), err.getvalue()

    def cool(self):
        return utils.read_float(utils.SET_TEMP_COOL_FILE)

    def heat(self):
        return utils.read_float(utils.SET_TEMP_HEAT_FILE)

    # ---------------- single-setpoint modes ---------------- #
    def test_cool_mode_updates_cool_setpoint_only(self):
        self.set_mode("cool")
        _out, err = self.send_temp("72")
        self.assertEqual(self.cool(), 72.0)
        self.assertEqual(self.heat(), 70.0, "heat setpoint must be untouched")
        self.assertEqual(err, "")

    def test_heat_mode_updates_heat_setpoint_only(self):
        self.set_mode("heat")
        _out, err = self.send_temp("68")
        self.assertEqual(self.heat(), 68.0)
        self.assertEqual(self.cool(), 74.0, "cool setpoint must be untouched")
        self.assertEqual(err, "")

    def test_value_snapped_to_whole_degree(self):
        self.set_mode("cool")
        self.send_temp("71.6")
        self.assertEqual(self.cool(), 72.0)

    # ---------------- range mode / off must be refused ---------------- #
    def test_auto_mode_ignored_with_error(self):
        self.set_mode("auto")
        _out, err = self.send_temp("72")
        self.assertEqual(self.cool(), 74.0)
        self.assertEqual(self.heat(), 70.0)
        self.assertIn("Error", err)
        self.assertIn("auto", err)

    def test_off_mode_ignored_with_error(self):
        self.set_mode("off")
        _out, err = self.send_temp("72")
        self.assertEqual(self.cool(), 74.0)
        self.assertEqual(self.heat(), 70.0)
        self.assertIn("Error", err)

    def test_missing_mode_file_defaults_to_off_and_is_refused(self):
        utils.SYSTEM_MODE_FILE.unlink(missing_ok=True)
        _out, err = self.send_temp("72")
        self.assertEqual(self.cool(), 74.0)
        self.assertEqual(self.heat(), 70.0)
        self.assertIn("Error", err)

    # ---------------- payload validation ---------------- #
    def test_non_numeric_payload_rejected_without_write(self):
        self.set_mode("cool")
        out, _err = self.send_temp("banana")
        self.assertEqual(self.cool(), 74.0)
        self.assertIn("Invalid temperature setpoint", out)

    def test_empty_payload_rejected_without_write(self):
        self.set_mode("cool")
        self.send_temp("")
        self.assertEqual(self.cool(), 74.0)

    # ---------------- dispatch wiring ---------------- #
    def test_on_message_routes_temperature_topic(self):
        self.set_mode("heat")
        _out, _err = self.send_message(self.mqtt.TOPIC_CMD_TEMP, "66")
        self.assertEqual(self.heat(), 66.0)

    def test_range_topics_still_work_regardless_of_mode(self):
        """The explicit cool/heat command topics are never mode-gated."""
        self.set_mode("auto")
        self.send_message(self.mqtt.TOPIC_CMD_COOL, "79")
        self.send_message(self.mqtt.TOPIC_CMD_HEAT, "65")
        self.assertEqual(self.cool(), 79.0)
        self.assertEqual(self.heat(), 65.0)

    def test_temperature_topic_is_subscribed_on_connect(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.daemon._on_connect(self.daemon.client, None, {}, 0)
        self.assertIn(self.mqtt.TOPIC_CMD_TEMP, self.daemon.client.subscribed)
        # pre-existing subscriptions must survive
        for topic in (
            self.mqtt.TOPIC_CMD_MODE,
            self.mqtt.TOPIC_CMD_FAN,
            self.mqtt.TOPIC_CMD_COOL,
            self.mqtt.TOPIC_CMD_HEAT,
        ):
            self.assertIn(topic, self.daemon.client.subscribed)

    # ---------------- HA discovery payload ---------------- #
    def test_discovery_advertises_single_setpoint_and_range(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.daemon._publish_discovery()
        topic, payload, retain = self.daemon.client.published[-1]
        self.assertEqual(topic, self.mqtt.TOPIC_DISCOVERY)
        self.assertTrue(retain)
        cfg = json.loads(payload)

        self.assertEqual(cfg["temperature_command_topic"], self.mqtt.TOPIC_CMD_TEMP)
        self.assertEqual(cfg["temperature_state_topic"], self.mqtt.TOPIC_STATE)
        self.assertIn("temperature_state_template", cfg)

        # Range setpoint topics must remain in place.
        self.assertEqual(cfg["temperature_high_command_topic"], self.mqtt.TOPIC_CMD_COOL)
        self.assertEqual(cfg["temperature_low_command_topic"], self.mqtt.TOPIC_CMD_HEAT)

        # Regression guard: fan_mode_* keys break Matter device classification.
        for key in ("fan_mode_state_topic", "fan_mode_command_topic", "fan_modes"):
            self.assertNotIn(key, cfg)

    def test_temperature_state_template_resolves_per_mode(self):
        """The HA template must mirror the setpoint the mode actually acts on."""
        with contextlib.redirect_stdout(io.StringIO()):
            self.daemon._publish_discovery()
        cfg = json.loads(self.daemon.client.published[-1][1])
        template = cfg["temperature_state_template"]

        def render(state):
            # Emulate HA's value_json context for this Jinja-style expression.
            expr = template.strip("{}").strip()
            return eval(
                expr,
                {"__builtins__": {}},
                {"value_json": type("J", (), state)()},
            )

        self.set_mode("cool")
        with contextlib.redirect_stdout(io.StringIO()):
            self.daemon._publish_state()
        state = json.loads(self.daemon.client.published[-1][1])
        self.assertEqual(render(state), 74.0)

        self.set_mode("heat")
        with contextlib.redirect_stdout(io.StringIO()):
            self.daemon._publish_state()
        state = json.loads(self.daemon.client.published[-1][1])
        self.assertEqual(render(state), 70.0)


if __name__ == "__main__":
    unittest.main()
