#!/usr/bin/env python3
"""
Thermostat MQTT Daemon.

Publishes thermostat state to Home Assistant via MQTT.
Subscribes to command topics for remote control.
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

# Configuration
DEFAULTS_PATH = Path("/etc/thermostat/defaults.json")
MQTT_CLIENT_ID = "thermostat"


def _load_mqtt_config() -> tuple:
    """
    Load MQTT broker settings from defaults.json.

    Reads the "mqtt" section of /etc/thermostat/defaults.json (installed
    from config/defaults.json by the Makefile). Falls back to built-in
    defaults if the file is missing, malformed, or lacks an "mqtt" section.
    """
    default_broker = "homeassistant.lan"
    default_port = 1883
    default_username = ""
    default_password = ""

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
        )
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Warning: Could not load MQTT config from {DEFAULTS_PATH}: {e}")
        return default_broker, default_port, default_username, default_password


# Load MQTT broker settings from the config file at startup
MQTT_BROKER, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD = _load_mqtt_config()

# Topic prefixes
TOPIC_PREFIX = "thermostat"
TOPIC_STATE = f"{TOPIC_PREFIX}/state"
TOPIC_AVAILABILITY = f"{TOPIC_PREFIX}/availability"
TOPIC_DISCOVERY = "homeassistant/climate/thermostat/config"

# Command topics (subscribe)
TOPIC_CMD_MODE = f"{TOPIC_PREFIX}/mode/set"
TOPIC_CMD_FAN = f"{TOPIC_PREFIX}/fan/set"
TOPIC_CMD_COOL = f"{TOPIC_PREFIX}/cool/set"
TOPIC_CMD_HEAT = f"{TOPIC_PREFIX}/heat/set"

POLL_INTERVAL = 5.0  # seconds


class MqttDaemon:
    """MQTT client daemon for Home Assistant integration."""
    
    def __init__(self):
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT_ID)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.running = True

        # Set authentication credentials if configured
        if MQTT_USERNAME:
            self.client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        
        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown gracefully."""
        print(f"\nReceived signal {signum}, shutting down...")
        self.running = False
        self.client.publish(TOPIC_AVAILABILITY, "offline", retain=True)
        self.client.disconnect()
        sys.exit(0)
    
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        """Callback when connected to MQTT broker."""
        if rc == 0:
            print(f"Connected to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}")
            
            # Publish availability
            self.client.publish(TOPIC_AVAILABILITY, "online", retain=True)
            
            # Publish Home Assistant discovery configuration
            self._publish_discovery()
            
            # Subscribe to command topics
            self.client.subscribe(TOPIC_CMD_MODE)
            self.client.subscribe(TOPIC_CMD_FAN)
            self.client.subscribe(TOPIC_CMD_COOL)
            self.client.subscribe(TOPIC_CMD_HEAT)
            print("Subscribed to command topics")
            
            # Publish initial state
            self._publish_state()
        else:
            print(f"Failed to connect to MQTT broker, return code: {rc}")
    
    def _on_message(self, client, userdata, msg):
        """Handle incoming MQTT messages."""
        topic = msg.topic
        payload = msg.payload.decode("utf-8")
        
        print(f"Received command: {topic} = {payload}")
        
        try:
            if topic == TOPIC_CMD_MODE:
                valid_modes = ["off", "cool", "heat", "auto"]
                if payload in valid_modes:
                    write_scalar(SYSTEM_MODE_FILE, payload)
                else:
                    print(f"Invalid mode: {payload}")
            
            elif topic == TOPIC_CMD_FAN:
                valid_fans = ["auto", "on"]
                if payload in valid_fans:
                    write_scalar(FAN_MODE_FILE, payload)
                else:
                    print(f"Invalid fan mode: {payload}")
            
            elif topic == TOPIC_CMD_COOL:
                try:
                    temp = round_degree(float(payload))
                    write_scalar(SET_TEMP_COOL_FILE, temp)
                except ValueError:
                    print(f"Invalid cool setpoint: {payload}")
            
            elif topic == TOPIC_CMD_HEAT:
                try:
                    temp = round_degree(float(payload))
                    write_scalar(SET_TEMP_HEAT_FILE, temp)
                except ValueError:
                    print(f"Invalid heat setpoint: {payload}")
                    
            # Publish updated state immediately to MQTT broker
            self._publish_state()
        except Exception as e:
            print(f"Error processing command: {e}")
    
    def _publish_discovery(self):
        """Publish Home Assistant MQTT Discovery configuration with retain flag."""
        discovery_payload = {
            "name": "HVAC Thermostat",
            "unique_id": "thermostat_hvac_control",
            "device": {
                "identifiers": ["thermostat_hvac_control"],
                "name": "HVAC Thermostat",
                "model": "Raspberry Pi HVAC Thermostat",
                "manufacturer": "Custom",
            },
            "availability_topic": TOPIC_AVAILABILITY,
            "payload_available": "online",
            "payload_not_available": "offline",
            "action_topic": TOPIC_STATE,
            "action_template": "{{ value_json.thermostat_running_state }}",
            "current_temperature_topic": TOPIC_STATE,
            "current_temperature_template": "{{ value_json.local_temperature }}",
            "mode_state_topic": TOPIC_STATE,
            "mode_state_template": "{{ value_json.system_mode }}",
            "mode_command_topic": TOPIC_CMD_MODE,
            "modes": ["off", "cool", "heat", "auto"],
            "fan_mode_state_topic": TOPIC_STATE,
            "fan_mode_state_template": "{{ value_json.fan_mode }}",
            "fan_mode_command_topic": TOPIC_CMD_FAN,
            "fan_modes": ["auto", "on"],
            "temperature_high_state_topic": TOPIC_STATE,
            "temperature_high_state_template": "{{ value_json.occupied_cooling_setpoint }}",
            "temperature_high_command_topic": TOPIC_CMD_COOL,
            "temperature_low_state_topic": TOPIC_STATE,
            "temperature_low_state_template": "{{ value_json.occupied_heating_setpoint }}",
            "temperature_low_command_topic": TOPIC_CMD_HEAT,
            "temperature_unit": "F",
            "precision": 0.1,
            "temp_step": 1.0,
            "min_temp": 60,
            "max_temp": 80,
        }
        self.client.publish(TOPIC_DISCOVERY, json.dumps(discovery_payload), retain=True)
        print(f"Published Home Assistant discovery config to {TOPIC_DISCOVERY}")

    def _publish_state(self):
        """Read IPC files and publish state to MQTT."""
        current = read_float(CURRENT_TEMP_FILE)
        system_mode = read_file(SYSTEM_MODE_FILE, default="off")
        fan_mode = read_file(FAN_MODE_FILE, default="auto")
        set_cool = read_float(SET_TEMP_COOL_FILE, default=74.0)
        set_heat = read_float(SET_TEMP_HEAT_FILE, default=70.0)
        hvac_action = read_file(HVAC_ACTION_FILE, default="idle")
        
        # Build state payload (Matter-compatible attributes)
        state = {
            "local_temperature": current,
            "system_mode": system_mode,
            "fan_mode": fan_mode,
            "occupied_cooling_setpoint": set_cool,
            "occupied_heating_setpoint": set_heat,
            "thermostat_running_state": hvac_action,
        }
        
        # Remove None values
        state = {k: v for k, v in state.items() if v is not None}
        
        self.client.publish(TOPIC_STATE, json.dumps(state))
    
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
            self.client.publish(TOPIC_AVAILABILITY, "offline", retain=True)
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
