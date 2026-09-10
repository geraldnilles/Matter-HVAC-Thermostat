"""
Tests for the MQTT bridge daemon (src/mqtt.py).

Focus: the matterbridge-mqtt protocol — the retained config/state/subscribe
payloads the daemon publishes and the ``write`` messages it accepts back from
the plugin (forwarded Matter controller attribute changes).

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
    "OUTDOOR_TEMP_FILE": "outdoor_temp",
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
        self.published = []      # (topic, payload, qos, retain)
        self.subscribed = []     # (topic, qos)

    def publish(self, topic, payload, qos=0, retain=False, **kwargs):
        self.published.append((topic, payload, qos, retain))

    def subscribe(self, topic, *args, **kwargs):
        qos = kwargs.get("qos", args[0] if args else 0)
        self.subscribed.append((topic, qos))


class MatterbridgeProtocolTest(unittest.TestCase):
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
        # __init__ is bypassed (object.__new__), so wire up the internals that
        # matter to the methods under test.
        self.daemon._last_state_json = None

        # Baseline state: off/auto, cool=74, heat=70 (unchanged unless a test writes them)
        utils.write_scalar(utils.SYSTEM_MODE_FILE, "off")
        utils.write_scalar(utils.FAN_MODE_FILE, "auto")
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

    def set_action(self, action):
        utils.write_scalar(utils.HVAC_ACTION_FILE, action)

    def send_write(self, payload, topic=None):
        """Drive the full _on_message dispatch path for a raw MQTT message."""
        msg = type("Msg", (), {"topic": topic or self.mqtt.TOPIC_WRITE, "payload": payload.encode()})()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.daemon._on_message(self.daemon.client, None, msg)
        return out.getvalue(), err.getvalue()

    def state_payload(self):
        """Publish state (capturing output) and return the parsed payload."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.daemon._publish_state(force=True)
        topic, payload, qos, retain = self.daemon.client.published[-1]
        return json.loads(payload)

    def thermostat(self):
        return self.state_payload()["Thermostat"]

    # ---------------- unit conversion ---------------- #
    def test_fahrenheit_to_matter_known_values(self):
        # 32F = 0C = 0; 212F = 100C = 10000
        self.assertEqual(self.mqtt.fahrenheit_to_matter(32.0), 0)
        self.assertEqual(self.mqtt.fahrenheit_to_matter(212.0), 10000)
        # 72F = 22.222C -> 2222 (rounded)
        self.assertEqual(self.mqtt.fahrenheit_to_matter(72.0), 2222)

    def test_matter_to_fahrenheit_round_trips_whole_degrees(self):
        for f in (60, 62, 68, 70, 72, 74, 76, 80):
            self.assertEqual(self.mqtt.matter_to_fahrenheit(self.mqtt.fahrenheit_to_matter(f)), float(f))

    # ---------------- config payload ---------------- #
    def test_config_advertises_thermostat_device_type(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.daemon._publish_config()
        topic, payload, qos, retain = self.daemon.client.published[-1]
        self.assertEqual(topic, self.mqtt.TOPIC_CONFIG)
        self.assertTrue(retain)
        self.assertEqual(qos, 2)
        cfg = json.loads(payload)

        self.assertEqual(cfg["deviceTypes"], ["Thermostat"])
        clusters = cfg["clusters"]
        self.assertIn("BridgedDeviceBasicInformation", clusters)
        self.assertIn("Thermostat", clusters)

        # Limits must match the 60-80F operating range, in hundredths of C.
        thermostat = clusters["Thermostat"]
        self.assertEqual(thermostat["minHeatSetpointLimit"], self.mqtt.fahrenheit_to_matter(60.0))
        self.assertEqual(thermostat["maxCoolSetpointLimit"], self.mqtt.fahrenheit_to_matter(80.0))
        # CoolingAndHeating=4 enables the HEAT+COOL features.
        self.assertEqual(thermostat["controlSequenceOfOperation"], 4)
        # systemMode is mandatory with no schema default; matter.js refuses to
        # initialize the behavior without it (Conformance "M", error 135).
        self.assertIn("systemMode", thermostat)

    def test_subscribe_declaration_lists_controller_attributes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.daemon._publish_subscribe()
        topic, payload, qos, retain = self.daemon.client.published[-1]
        self.assertEqual(topic, self.mqtt.TOPIC_SUBSCRIBE)
        self.assertTrue(retain)
        self.assertEqual(json.loads(payload), {"Thermostat": ["systemMode", "occupiedHeatingSetpoint", "occupiedCoolingSetpoint"]})

    # ---------------- state payload ---------------- #
    def test_state_contains_thermostat_cluster_only(self):
        state = self.state_payload()
        self.assertEqual(list(state.keys()), ["Thermostat"])

    def test_state_temperatures_are_hundredths_of_c(self):
        utils.write_scalar(utils.CURRENT_TEMP_FILE, 70.0)
        thermostat = self.thermostat()
        # 70F = 21.111C -> 2111; 74F = 23.333C -> 2333
        self.assertEqual(thermostat["localTemperature"], 2111)
        self.assertEqual(thermostat["occupiedHeatingSetpoint"], 2111)
        self.assertEqual(thermostat["occupiedCoolingSetpoint"], 2333)

    def test_state_system_mode_mapping(self):
        for internal, matter in (("off", 0), ("auto", 1), ("cool", 3), ("heat", 4)):
            self.set_mode(internal)
            self.assertEqual(self.thermostat()["systemMode"], matter)

    def test_state_missing_current_temp_omits_local_temperature(self):
        self.assertNotIn("localTemperature", self.thermostat())
        utils.write_scalar(utils.CURRENT_TEMP_FILE, 71.0)
        self.assertIn("localTemperature", self.thermostat())

    def test_state_includes_outdoor_temperature_when_present(self):
        """72F -> 22.22C -> 2222 hundredths of a degree C in the state payload."""
        utils.write_scalar(utils.OUTDOOR_TEMP_FILE, 72.0)
        thermostat = self.thermostat()
        self.assertIn("outdoorTemperature", thermostat)
        self.assertEqual(thermostat["outdoorTemperature"], 2222)

    def test_state_missing_outdoor_temp_omits_outdoor_temperature(self):
        if utils.OUTDOOR_TEMP_FILE.exists():
            utils.OUTDOOR_TEMP_FILE.unlink()
        self.assertNotIn("outdoorTemperature", self.thermostat())

        # A stale/unconfigured sensor is published once the file reappears.
        utils.write_scalar(utils.OUTDOOR_TEMP_FILE, 65.0)
        self.assertIn("outdoorTemperature", self.thermostat())

    def test_outdoor_temperature_change_triggers_republish(self):
        utils.write_scalar(utils.OUTDOOR_TEMP_FILE, 65.0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.daemon._publish_state(force=True)
        count = len(self.daemon.client.published)

        # Same reading again: echo suppression skips the republish.
        with contextlib.redirect_stdout(io.StringIO()):
            self.daemon._publish_state()
        self.assertEqual(len(self.daemon.client.published), count)

        # A changed outdoor reading is a changed state and republishes.
        utils.write_scalar(utils.OUTDOOR_TEMP_FILE, 66.0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.daemon._publish_state()
        self.assertEqual(len(self.daemon.client.published), count + 1)
        payload = json.loads(self.daemon.client.published[-1][1])
        self.assertIn("outdoorTemperature", payload["Thermostat"])

    def test_running_state_reflects_hvac_action(self):
        base = ("heatStage2", "coolStage2", "fanStage2", "fanStage3")

        self.set_action("heating")
        rs = self.thermostat()["thermostatRunningState"]
        self.assertTrue(rs["heat"])
        self.assertFalse(rs["cool"])
        self.assertEqual(self.thermostat()["thermostatRunningMode"], 4)  # Heat

        self.set_action("cooling")
        rs = self.thermostat()["thermostatRunningState"]
        self.assertTrue(rs["cool"])
        self.assertFalse(rs["heat"])
        self.assertEqual(self.thermostat()["thermostatRunningMode"], 3)  # Cool

        self.set_action("idle")
        rs = self.thermostat()["thermostatRunningState"]
        self.assertFalse(any(rs[f] for f in ("heat", "cool", "fan")))
        self.assertEqual(self.thermostat()["thermostatRunningMode"], 0)  # Off

        # Only single-stage fields plus the fan bit are ever set.
        self.set_action("fan")
        rs = self.thermostat()["thermostatRunningState"]
        self.assertTrue(rs["fan"])
        self.assertFalse(any(rs[f] for f in base))

    def test_fan_mode_on_sets_fan_bit(self):
        self.set_action("idle")
        utils.write_scalar(utils.FAN_MODE_FILE, "on")
        self.assertTrue(self.thermostat()["thermostatRunningState"]["fan"])

    # ---------------- write handling ---------------- #
    def test_write_system_mode_heat(self):
        out, err = self.send_write(json.dumps({"Thermostat": {"systemMode": 4}}))
        self.assertEqual(utils.read_file(utils.SYSTEM_MODE_FILE), "heat")
        self.assertEqual(err, "")

    def test_write_system_mode_cool_auto_off(self):
        for matter_mode, internal in ((3, "cool"), (1, "auto"), (0, "off")):
            _out, _err = self.send_write(json.dumps({"Thermostat": {"systemMode": matter_mode}}))
            self.assertEqual(utils.read_file(utils.SYSTEM_MODE_FILE), internal)

    def test_write_unsupported_system_mode_ignored(self):
        _out, _err = self.send_write(json.dumps({"Thermostat": {"systemMode": 9}}))
        self.assertEqual(utils.read_file(utils.SYSTEM_MODE_FILE), "off")

    def test_write_setpoints_converted_to_fahrenheit(self):
        # 2200 (22C) = 71.6F -> snapped to 72; 2500 (25C) = 77F
        _out, _err = self.send_write(json.dumps({"Thermostat": {"occupiedHeatingSetpoint": 2200, "occupiedCoolingSetpoint": 2500}}))
        self.assertEqual(utils.read_float(utils.SET_TEMP_HEAT_FILE), 72.0)
        self.assertEqual(utils.read_float(utils.SET_TEMP_COOL_FILE), 77.0)

    def test_write_unknown_attribute_ignored(self):
        _out, _err = self.send_write(json.dumps({"Thermostat": {"localTemperature": 2000}}))
        self.assertNotIn("Error", _out)

    def test_write_other_cluster_ignored(self):
        _out, _err = self.send_write(json.dumps({"OnOff": {"onOff": True}}))
        self.assertEqual(utils.read_file(utils.SYSTEM_MODE_FILE), "off")

    def test_write_invalid_json_rejected(self):
        out, _err = self.send_write("not json")
        self.assertIn("Error", out)

    def test_write_non_object_rejected(self):
        out, _err = self.send_write("[1, 2]")
        self.assertIn("Error", out)

    def test_write_on_unexpected_topic_ignored(self):
        out, _err = self.send_write(json.dumps({"Thermostat": {"systemMode": 4}}), topic="thermostat/other")
        self.assertEqual(utils.read_file(utils.SYSTEM_MODE_FILE), "off")
        self.assertIn("Ignoring message on unexpected topic", out)

    def test_write_empty_payload_ignored(self):
        out, _err = self.send_write("")
        self.assertNotIn("Error", out)

    def test_write_does_not_trigger_immediate_state_publish(self):
        # Regression for the matterbridge echo storm: a controller write must
        # NOT synchronously re-publish the full state. Re-publishing inside the
        # write handler made matterbridge's updateHandler re-set every cluster
        # attribute; each difference fired a separate $Changed callback that
        # came right back on our write topic, cascading into an unbounded loop
        # (and queuing stale IPC snapshots behind Matter's transaction mutex,
        # which made systemMode visibly oscillate 0<->3). The controller's
        # write is applied and reported by matterbridge's own Matter server;
        # our daemon only syncs the IPC copy here. State is republished from
        # the single main loop after each write burst settles.
        before = len(self.daemon.client.published)
        self.send_write(json.dumps({"Thermostat": {"systemMode": 3}}))
        # IPC is updated...
        self.assertEqual(utils.read_file(utils.SYSTEM_MODE_FILE), "cool")
        # ...but no state topic publish is issued from the write handler.
        topics = [t for t, *_ in self.daemon.client.published[before:]]
        self.assertNotIn(self.mqtt.TOPIC_STATE, topics)

    def test_write_still_records_changed_state_for_next_poll(self):
        # A subsequent (non-force) publish from the main loop sees the new IPC
        # value and emits a single coalesced state update.
        self.send_write(json.dumps({"Thermostat": {"systemMode": 4}}))
        before = len(self.daemon.client.published)
        self.daemon._publish_state()
        topics = [t for t, *_ in self.daemon.client.published[before:]]
        self.assertIn(self.mqtt.TOPIC_STATE, topics)
        payload_json = self.daemon.client.published[-1][1]
        payload = json.loads(payload_json)
        self.assertEqual(payload["Thermostat"]["systemMode"], 4)

    # ---------------- echo suppression ---------------- #
    def test_identical_state_not_republished(self):
        io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()):
            self.daemon._publish_state(force=True)
        count = len(self.daemon.client.published)
        with contextlib.redirect_stdout(io.StringIO()):
            self.daemon._publish_state()
        self.assertEqual(len(self.daemon.client.published), count)

    def test_changed_state_republished(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.daemon._publish_state(force=True)
        count = len(self.daemon.client.published)
        self.set_action("heating")
        with contextlib.redirect_stdout(io.StringIO()):
            self.daemon._publish_state()
        self.assertEqual(len(self.daemon.client.published), count + 1)

    # ---------------- connection wiring ---------------- #
    def test_on_connect_publishes_and_subscribes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.daemon._on_connect(self.daemon.client, None, {}, 0)

        topics = [t for t, _p, _q, _r in self.daemon.client.published]
        for topic in (self.mqtt.TOPIC_CONFIG, self.mqtt.TOPIC_STATE, self.mqtt.TOPIC_SUBSCRIBE):
            self.assertIn(topic, topics)
        self.assertEqual(self.daemon.client.subscribed, [(self.mqtt.TOPIC_WRITE, self.mqtt.PUBLISH_QOS)])
        # All device announcements must be retained so matterbridge can pick
        # them up when (re)starting after us.
        for topic, _payload, _qos, retain in self.daemon.client.published:
            if topic != self.mqtt.TOPIC_STATE:
                self.assertTrue(retain, f"{topic} must be retained")

    def test_on_connect_failure_does_not_publish(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.daemon._on_connect(self.daemon.client, None, {}, 5)
        self.assertEqual(self.daemon.client.published, [])


if __name__ == "__main__":
    unittest.main()
