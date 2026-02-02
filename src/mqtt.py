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
    CURRENT_TEMP_FILE,
    MIN_TEMP_FILE,
    MAX_TEMP_FILE,
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
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_CLIENT_ID = "thermostat"

# Topic prefixes
TOPIC_PREFIX = "thermostat"
TOPIC_STATE = f"{TOPIC_PREFIX}/state"
TOPIC_AVAILABILITY = f"{TOPIC_PREFIX}/availability"

# Command topics (subscribe)
TOPIC_CMD_MODE = f"{TOPIC_PREFIX}/mode/set"
TOPIC_CMD_FAN = f"{TOPIC_PREFIX}/fan/set"
TOPIC_CMD_COOL = f"{TOPIC_PREFIX}/cool/set"
TOPIC_CMD_HEAT = f"{TOPIC_PREFIX}/heat/set"

POLL_INTERVAL = 5.0  # seconds


class MqttDaemon:
    """MQTT client daemon for Home Assistant integration."""
    
    def __init__(self):
        self.client = mqtt.Client(client_id=MQTT_CLIENT_ID)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.running = True
        
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
    
    def _on_connect(self, client, userdata, flags, rc):
        """Callback when connected to MQTT broker."""
        if rc == 0:
            print(f"Connected to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}")
            
            # Publish availability
            self.client.publish(TOPIC_AVAILABILITY, "online", retain=True)
            
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
                    temp = float(payload)
                    write_scalar(SET_TEMP_COOL_FILE, temp)
                except ValueError:
                    print(f"Invalid cool setpoint: {payload}")
            
            elif topic == TOPIC_CMD_HEAT:
                try:
                    temp = float(payload)
                    write_scalar(SET_TEMP_HEAT_FILE, temp)
                except ValueError:
                    print(f"Invalid heat setpoint: {payload}")
        
        except Exception as e:
            print(f"Error processing command: {e}")
    
    def _publish_state(self):
        """Read IPC files and publish state to MQTT."""
        current = read_float(CURRENT_TEMP_FILE)
        min_temp = read_float(MIN_TEMP_FILE)
        max_temp = read_float(MAX_TEMP_FILE)
        system_mode = read_file(SYSTEM_MODE_FILE, default="off")
        fan_mode = read_file(FAN_MODE_FILE, default="auto")
        set_cool = read_float(SET_TEMP_COOL_FILE, default=74.0)
        set_heat = read_float(SET_TEMP_HEAT_FILE, default=70.0)
        hvac_action = read_file(HVAC_ACTION_FILE, default="idle")
        
        # Build state payload (Matter-compatible attributes)
        state = {
            "local_temperature": current,
            "min_temperature": min_temp,
            "max_temperature": max_temp,
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
