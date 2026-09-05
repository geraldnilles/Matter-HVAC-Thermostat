#!/usr/bin/env python3
"""
Thermostat MQTT Daemon (matterbridge bridge).

Bridges the thermostat into Matter via the `matterbridge-mqtt` plugin
(https://github.com/Luligu/matterbridge-mqtt) talking to an MQTT broker
(mosquitto). matterbridge hosts the Matter device; this daemon drives it
over MQTT.

Protocol (see the plugin README): every device publishes retained `config`,
`state` and `subscribe` messages on

    <topic>/<deviceId>/config/<endpointName>
    <topic>/<deviceId>/state/<endpointName>
    <topic>/<deviceId>/subscribe/<endpointName>

and may subscribe to the `write` topic, where the plugin forwards Matter
controller attribute changes as

    { "ClusterName": { "attributeName": value } }

This daemon registers a single `Thermostat` device (device type 769) and
keeps the broker-replicated state in sync with the IPC files in
/run/thermostat/. Attribute writes from Matter controllers (system mode,
occupied setpoints) are converted back and written to IPC.
"""

import json
import signal
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt

from utils import (
    round_degree,
    CURRENT_TEMP_FILE,
    SYSTEM_MODE_FILE,
    FAN_MODE_FILE,
    SET_TEMP_COOL_FILE,
    SET_TEMP_HEAT_FILE,
    HVAC_ACTION_FILE,
    read_float,
    read_file,
    write_scalar,
)

DEFAULTS_PATH = Path("/etc/thermostat/defaults.json")
MQTT_CLIENT_ID = "thermostat"


def _load_mqtt_config() -> tuple:
    """
    Load MQTT broker and matterbridge-mqtt plugin settings from defaults.json.

    Reads the "mqtt" section of /etc/thermostat/defaults.json (installed from
    config/defaults.json by the Makefile). Falls back to built-in defaults if
    the file is missing, malformed, or lacks an "mqtt" section.
    """
    default_broker = "localhost"
    default_port = 1883
    default_username = ""
    default_password = ""
    default_topic = "matterbridge"
    default_device_id = "thermostat"

    try:
        with open(DEFAULTS_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        mqtt_cfg = config.get("mqtt", {})
        broker = mqtt_cfg.get("broker", default_broker)

        # Coerce port to int in case it's stored as a string
        try:
            port = int(mqtt_cfg.get("port", default_port))
        except (TypeError, ValueError):
            print(f"Warning: Invalid MQTT port '{mqtt_cfg.get('port')}', using {default_port}")
            port = default_port

        return (
            broker,
            port,
            mqtt_cfg.get("username", default_username),
            mqtt_cfg.get("password", default_password),
            mqtt_cfg.get("topic", default_topic),
            mqtt_cfg.get("device_id", default_device_id),
        )
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Warning: Could not load MQTT config from {DEFAULTS_PATH}: {e}")
        return (
            default_broker,
            default_port,
            default_username,
            default_password,
            default_topic,
            default_device_id,
        )


# Load MQTT broker / plugin settings from the config file at startup
(
    MQTT_BROKER,
    MQTT_PORT,
    MQTT_USERNAME,
    MQTT_PASSWORD,
    MQTT_BASE_TOPIC,
    DEVICE_ID,
) = _load_mqtt_config()

# Topics (matterbridge-mqtt layout: <topic>/<deviceId>/<subTopic>/<endpointName>)
TOPIC_CONFIG = f"{MQTT_BASE_TOPIC}/{DEVICE_ID}/config/root"
TOPIC_STATE = f"{MQTT_BASE_TOPIC}/{DEVICE_ID}/state/root"
TOPIC_SUBSCRIBE = f"{MQTT_BASE_TOPIC}/{DEVICE_ID}/subscribe/root"
TOPIC_WRITE = f"{MQTT_BASE_TOPIC}/{DEVICE_ID}/write/root"

POLL_INTERVAL = 5.0  # seconds

# Publish QoS: the plugin's examples use QoS 2 for config/state/subscribe.
PUBLISH_QOS = 2

# ---------------------------------------------------------------------------
# Matter Thermostat cluster (id 513) constants.
#
# Verified against matterbridge-mqtt's clusters.json and matterbridge's
# MatterbridgeThermostatServer registry entry, which installs the behavior
# with the AutoMode, Heating and Cooling features pre-enabled:
#     if (clusterId === Thermostat.id)
#         return MatterbridgeThermostatServer.with('AutoMode', 'Heating', 'Cooling');
# ---------------------------------------------------------------------------

# Device type "Thermostat" (id 769). The plugin appends BridgedNode itself.
DEVICE_TYPE_NAME = "Thermostat"

# Cluster names as used in config/state/subscribe/write payloads.
CLUSTER_BASIC_INFORMATION = "BridgedDeviceBasicInformation"
CLUSTER_THERMOSTAT = "Thermostat"

# SystemModeEnum (verified in matterbridge-mqtt/clusters.json):
# Off=0, Auto=1 (AUTO feature), Cool=3 (COOL), Heat=4 (HEAT)
SYSTEM_MODE_TO_MATTER = {"off": 0, "auto": 1, "cool": 3, "heat": 4}
SYSTEM_MODE_FROM_MATTER = {v: k for k, v in SYSTEM_MODE_TO_MATTER.items()}

# ThermostatRunningModeEnum: Off=0, Cool=3, Heat=4
RUNNING_MODE_TO_MATTER = {"idle": 0, "fan": 0, "cooling": 3, "heating": 4}

# RelayStateBitmap fields (camelCase, as matterbridge itself publishes them):
# Heat=bit0, Cool=bit1, Fan=bit2, HeatStage2=bit3, CoolStage2=bit4,
# FanStage2=bit5, FanStage3=bit6. Only the single-stage bits are used here.
RUNNING_STATE_FIELDS = ("heat", "cool", "fan", "heatStage2", "coolStage2", "fanStage2", "fanStage3")
HVAC_ACTION_TO_RUNNING_STATE = {"heating": "heat", "cooling": "cool", "fan": "fan"}

# ControlSequenceOfOperationEnum: CoolingAndHeating=4 (HEAT + COOL features)
CONTROL_SEQUENCE_COOLING_AND_HEATING = 4

# Setpoint limits in °F (mirrors the 60–80 °F operating range).
MIN_TEMP_F = 60.0
MAX_TEMP_F = 80.0

# MinSetpointDeadBand in tenths of °C (SignedTemperature). 44 ≈ 4.4 °C ≈ 8 °F,
# matching control.MIN_SETPOINT_GAP, so controllers cannot write overlapping
# setpoints even though the control daemon would re-separate them anyway.
MIN_SETPOINT_DEAD_BAND_TENTHS_C = 44

# Attributes the daemon wants pushed back on the plugin's write topic.
SUBSCRIBED_THERMOSTAT_ATTRIBUTES = ["systemMode", "occupiedHeatingSetpoint", "occupiedCoolingSetpoint"]


def fahrenheit_to_matter(value: float) -> int:
    """
    Convert °F to a Matter `temperature` value (hundredths of °C, integer).

    Matter Thermostat temperatures are int16 in hundredths of a degree
    Celsius; e.g. 72 °F -> 22.222 °C -> 2222.
    """
    return round((value - 32.0) * 500.0 / 9.0)


def matter_to_fahrenheit(value) -> float:
    """
    Convert a Matter `temperature` value (hundredths of °C) to whole °F.

    The hundredths-of-a-degree quantisation error (max ~0.16 °F) is absorbed
    by round_degree, so published values round-trip exactly.
    """
    return round_degree(float(value) * 9.0 / 500.0 + 32.0)


class MqttDaemon:
    """MQTT client daemon for the matterbridge-mqtt plugin."""

    def __init__(self):
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT_ID)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.running = True

        # Set authentication credentials if configured
        if MQTT_USERNAME:
            self.client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

        # Last state payload published (JSON string); used to suppress echo
        # loops: every state publish makes matterbridge re-set the attributes,
        # which the plugin forwards back to the write topic.
        self._last_state_json = None

        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown gracefully."""
        print(f"\nReceived signal {signum}, shutting down...")
        self.running = False
        self.client.disconnect()
        sys.exit(0)

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        """Callback when connected to MQTT broker."""
        if rc == 0:
            print(f"Connected to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}")

            # Register the device with matterbridge (retained so matterbridge
            # picks it up even when (re)starting after us).
            self._publish_config()

            # Declare which attribute changes we want forwarded back.
            self._publish_subscribe()

            # Publish current state and subscribe to controller writes.
            self._publish_state()
            self.client.subscribe(TOPIC_WRITE, qos=PUBLISH_QOS)
            print(f"Subscribed to write topic: {TOPIC_WRITE}")
        else:
            print(f"Failed to connect to MQTT broker, return code: {rc}")

    # ---------------- payloads ---------------- #

    def _publish_config(self):
        """
        Publish the retained device `config` message.

        Fixed attributes live here (they are part of the device definition);
        per-sample values (temperatures, setpoints, modes) are published as
        `state`. The plugin validates `deviceTypes` and `clusters` names
        against the Matter data model, then requires the Thermostat behavior
        with AutoMode/Heating/Cooling features (matterbridge core registry).
        """
        limits = {
            "minHeatSetpointLimit": fahrenheit_to_matter(MIN_TEMP_F),
            "maxHeatSetpointLimit": fahrenheit_to_matter(MAX_TEMP_F),
            "minCoolSetpointLimit": fahrenheit_to_matter(MIN_TEMP_F),
            "maxCoolSetpointLimit": fahrenheit_to_matter(MAX_TEMP_F),
            "absMinHeatSetpointLimit": fahrenheit_to_matter(MIN_TEMP_F),
            "absMaxHeatSetpointLimit": fahrenheit_to_matter(MAX_TEMP_F),
            "absMinCoolSetpointLimit": fahrenheit_to_matter(MIN_TEMP_F),
            "absMaxCoolSetpointLimit": fahrenheit_to_matter(MAX_TEMP_F),
        }
        config_payload = {
            "deviceTypes": [DEVICE_TYPE_NAME],
            "clusters": {
                CLUSTER_BASIC_INFORMATION: {
                    "nodeLabel": "HVAC Thermostat",
                    "serialNumber": DEVICE_ID,
                    "productName": "HVAC Thermostat",
                    "vendorName": "Matterbridge",
                },
                CLUSTER_THERMOSTAT: {
                    # matter.js validates the behavior state at creation and
                    # mandatory attributes without a schema default MUST be
                    # set here: SystemMode has no default (Conformance "M",
                    # error 135). The retained `state` message keeps the live
                    # value in sync right after registration.
                    "systemMode": 0,
                    "controlSequenceOfOperation": CONTROL_SEQUENCE_COOLING_AND_HEATING,
                    "minSetpointDeadBand": MIN_SETPOINT_DEAD_BAND_TENTHS_C,
                    **limits,
                },
            },
        }
        self.client.publish(TOPIC_CONFIG, json.dumps(config_payload), qos=PUBLISH_QOS, retain=True)
        print(f"Published device config to {TOPIC_CONFIG}")

    def _publish_subscribe(self):
        """Publish the retained `subscribe` declaration for the device."""
        subscribe_payload = {CLUSTER_THERMOSTAT: list(SUBSCRIBED_THERMOSTAT_ATTRIBUTES)}
        self.client.publish(TOPIC_SUBSCRIBE, json.dumps(subscribe_payload), qos=PUBLISH_QOS, retain=True)
        print(f"Published subscribe declaration to {TOPIC_SUBSCRIBE}")

    def _build_state(self) -> dict:
        """Build the Thermostat `state` payload from the IPC files."""
        current = read_float(CURRENT_TEMP_FILE)
        system_mode = read_file(SYSTEM_MODE_FILE, default="off")
        fan_mode = read_file(FAN_MODE_FILE, default="auto")
        set_cool = read_float(SET_TEMP_COOL_FILE, default=74.0)
        set_heat = read_float(SET_TEMP_HEAT_FILE, default=70.0)
        hvac_action = read_file(HVAC_ACTION_FILE, default="idle")

        thermostat = {
            "systemMode": SYSTEM_MODE_TO_MATTER.get(system_mode, 0),
            "occupiedHeatingSetpoint": fahrenheit_to_matter(set_heat),
            "occupiedCoolingSetpoint": fahrenheit_to_matter(set_cool),
        }
        if current is not None:
            thermostat["localTemperature"] = fahrenheit_to_matter(current)

        # RelayStateBitmap (camelCase fields, as matterbridge publishes them).
        running_state = {field: False for field in RUNNING_STATE_FIELDS}
        active = HVAC_ACTION_TO_RUNNING_STATE.get(hvac_action)
        if active is not None:
            running_state[active] = True
        # The fan relay can also run while heating/cooling (fan-on-idle or
        # fan_mode=on); reflect it in the Fan bit.
        if fan_mode == "on" or hvac_action == "fan":
            running_state["fan"] = True
        thermostat["thermostatRunningState"] = running_state

        # ThermostatRunningMode (Off=0, Cool=3, Heat=4).
        thermostat["thermostatRunningMode"] = RUNNING_MODE_TO_MATTER.get(hvac_action, 0)

        return {CLUSTER_THERMOSTAT: thermostat}

    def _publish_state(self, force: bool = False):
        """
        Read IPC files and publish the retained Thermostat `state` payload.

        Publishing the same state again makes matterbridge re-set the cluster
        attributes, which the plugin forwards back to the write topic; that
        echo would round-trip into another publish. Unless `force` is set
        (initial connect), identical payloads are skipped to break the loop.
        """
        state = self._build_state()
        payload = json.dumps(state)
        if not force and payload == self._last_state_json:
            return
        self._last_state_json = payload
        self.client.publish(TOPIC_STATE, payload, qos=PUBLISH_QOS, retain=True)

    # ---------------- writes from Matter controllers ---------------- #

    def _on_message(self, client, userdata, msg):
        """Handle incoming MQTT messages on the write topic."""
        topic = msg.topic
        payload = msg.payload.decode("utf-8")

        if topic != TOPIC_WRITE:
            print(f"Ignoring message on unexpected topic: {topic} = {payload}")
            return
        if not payload.strip():
            # Empty retained payloads are only meaningful on config topics.
            return

        print(f"Received write: {topic} = {payload}")

        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            print(f"Error: invalid JSON on write topic: {payload}")
            return
        if not isinstance(message, dict):
            print(f"Error: write payload is not an object: {payload}")
            return

        attrs = message.get(CLUSTER_THERMOSTAT)
        if attrs is None:
            print(f"Ignoring write for unsupported cluster(s): {sorted(message)}")
            return

        try:
            self._apply_thermostat_attributes(attrs)
        except (TypeError, ValueError) as e:
            print(f"Error processing write: {e}")
            return

        # Reflect the new state immediately (skipped if nothing changed).
        self._publish_state()

    def _apply_thermostat_attributes(self, attrs):
        """
        Apply forwarded Thermostat attribute changes to the IPC files.

        Values arrive in Matter units: SystemModeEnum numbers and
        temperatures in hundredths of °C. Unknown attributes are logged and
        skipped so future plugin/controller features fail soft.
        """
        if not isinstance(attrs, dict):
            print(f"Error: '{CLUSTER_THERMOSTAT}' write payload is not an object: {attrs!r}")
            return

        for attr, value in attrs.items():
            if attr == "systemMode":
                mode = SYSTEM_MODE_FROM_MATTER.get(value)
                if mode is None:
                    print(f"Ignoring unsupported systemMode value: {value!r}")
                    continue
                write_scalar(SYSTEM_MODE_FILE, mode)
            elif attr == "occupiedCoolingSetpoint":
                write_scalar(SET_TEMP_COOL_FILE, matter_to_fahrenheit(value))
            elif attr == "occupiedHeatingSetpoint":
                write_scalar(SET_TEMP_HEAT_FILE, matter_to_fahrenheit(value))
            else:
                print(f"Ignoring unsupported attribute write: {attr} = {value!r}")

    # ---------------- main loop ---------------- #

    def run(self):
        """Main daemon loop."""
        print(f"Connecting to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}...")

        try:
            self.client.connect(MQTT_BROKER, MQTT_PORT, 60)
            self.client.loop_start()

            while self.running:
                time.sleep(POLL_INTERVAL)
                self._publish_state()

        except Exception as e:
            print(f"MQTT error: {e}", file=sys.stderr)
            raise


def main():
    """Entry point."""
    daemon = MqttDaemon()

    try:
        daemon.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
