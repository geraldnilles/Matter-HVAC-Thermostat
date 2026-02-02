#!/usr/bin/env python3
"""
Thermostat Sensor Daemon.

Scans for BLE temperature sensors, maintains rolling averages,
and writes aggregated temperature data to IPC files.
"""

import asyncio
import json
import time
from collections import deque
from pathlib import Path

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from utils import (
    CURRENT_TEMP_FILE,
    MIN_TEMP_FILE,
    MAX_TEMP_FILE,
    HISTORY_FILE,
    write_scalar,
    write_json,
    IPC_DIR,
)

DEFAULTS_PATH = Path("/etc/thermostat/defaults.json")

# Timing constants (seconds)
SENSOR_TIMEOUT = 120  # 2 minutes - stale data threshold
ROLLING_WINDOW = 120  # 2 minutes - rolling average buffer
HISTORY_INTERVAL = 60  # 1 minute - history sampling rate
HISTORY_MAX_ENTRIES = 1440  # 24 hours at 1-minute intervals

# Govee H5075 UUID for temperature/humidity data
GOVEE_SERVICE_UUID = "0000ec88-0000-1000-8000-00805f9b34fb"


class SensorBuffer:
    """Rolling buffer for a single sensor's temperature readings."""
    
    def __init__(self, name: str):
        self.name = name
        self.readings = deque()  # (timestamp, temp) tuples
        self.last_seen = 0
    
    def add_reading(self, timestamp: float, temp: float):
        """Add a temperature reading and prune old entries."""
        self.readings.append((timestamp, temp))
        self.last_seen = timestamp
        
        # Remove readings older than ROLLING_WINDOW
        cutoff = timestamp - ROLLING_WINDOW
        while self.readings and self.readings[0][0] < cutoff:
            self.readings.popleft()
    
    def get_average(self) -> float | None:
        """Calculate average of readings in buffer."""
        if not self.readings:
            return None
        return sum(r[1] for r in self.readings) / len(self.readings)
    
    def is_valid(self, now: float) -> bool:
        """Check if sensor data is fresh (not stale)."""
        return (now - self.last_seen) < SENSOR_TIMEOUT
    
    def get_latest(self) -> float | None:
        """Get most recent temperature reading."""
        if not self.readings:
            return None
        return self.readings[-1][1]


class SensorDaemon:
    """Main sensor daemon managing BLE scanning and data aggregation."""
    
    def __init__(self):
        self.sensors = {}  # mac -> SensorBuffer
        self.history = deque(maxlen=HISTORY_MAX_ENTRIES)
        self.last_history_update = 0
        
        # Load allowlist from defaults
        self.allowlist = self._load_allowlist()
        print(f"Loaded {len(self.allowlist)} sensors from allowlist:")
        for mac, name in self.allowlist.items():
            print(f"  {mac}: {name}")
            self.sensors[mac] = SensorBuffer(name)
    
    def _load_allowlist(self) -> dict:
        """Load sensor MAC allowlist from defaults.json."""
        try:
            with open(DEFAULTS_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
            return config.get("sensors", {})
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"Warning: Could not load allowlist from {DEFAULTS_PATH}: {e}")
            return {}
    
    def _decode_govee_temp(self, manufacturer_data: bytes) -> float | None:
        """
        Decode temperature from Govee H5075 advertisement data.
        
        Data format: 4 bytes, temperature is encoded in first 3 bytes
        as signed fixed-point with 2 decimal places.
        """
        if len(manufacturer_data) < 4:
            return None
        
        # Govee encoding: temp_c * 10000 + humidity * 100
        # First 3 bytes contain temperature (signed)
        raw = int.from_bytes(manufacturer_data[:3], byteorder="big", signed=True)
        temp_c = raw / 10000.0
        temp_f = (temp_c * 9.0 / 5.0) + 32.0
        return temp_f
    
    def _detection_callback(self, device: BLEDevice, advertisement: AdvertisementData):
        """Process BLE advertisement from scanner."""
        mac = device.address.upper()
        
        # Filter by allowlist
        if mac not in self.allowlist:
            return
        
        # Look for Govee service data
        service_data = advertisement.service_data
        if GOVEE_SERVICE_UUID not in service_data:
            return
        
        temp = self._decode_govee_temp(service_data[GOVEE_SERVICE_UUID])
        if temp is None:
            return
        
        now = time.time()
        self.sensors[mac].add_reading(now, temp)
        print(f"Sensor {self.allowlist[mac]} ({mac}): {temp:.2f}°F")
    
    def _get_valid_sensors(self, now: float) -> list[tuple[str, float]]:
        """Get list of (mac, avg_temp) for all valid (non-stale) sensors."""
        valid = []
        for mac, buffer in self.sensors.items():
            if buffer.is_valid(now):
                avg = buffer.get_average()
                if avg is not None:
                    valid.append((mac, avg))
        return valid
    
    def _update_history(self, now: float, valid_sensors: list[tuple[str, float]]):
        """Update 24-hour history buffer if interval has passed."""
        if now - self.last_history_update < HISTORY_INTERVAL:
            return
        
        if not valid_sensors:
            return  # Don't record if no valid data
        
        # Calculate aggregate values
        temps = [t for _, t in valid_sensors]
        avg_temp = sum(temps) / len(temps)
        
        # Build sensor readings dict with friendly names
        sensor_readings = {}
        for mac, temp in valid_sensors:
            sensor_readings[self.allowlist.get(mac, mac)] = round(temp, 2)
        
        entry = {
            "t": int(now),  # Unix epoch timestamp
            "avg": round(avg_temp, 2),
            "sensors": sensor_readings
        }
        
        self.history.append(entry)
        self.last_history_update = now
        print(f"History updated: avg={avg_temp:.2f}°F, sensors={len(valid_sensors)}")
    
    def _write_ipc_files(self, now: float, valid_sensors: list[tuple[str, float]]):
        """Write aggregated data to IPC files."""
        if not valid_sensors:
            # Total failure - delete current_temp to trigger failsafe
            print("WARNING: No valid sensors! Deleting current_temp to trigger failsafe.")
            try:
                CURRENT_TEMP_FILE.unlink()
            except FileNotFoundError:
                pass
            return
        
        temps = [t for _, t in valid_sensors]
        current = sum(temps) / len(temps)
        min_temp = min(temps)
        max_temp = max(temps)
        
        # Write scalar files
        write_scalar(CURRENT_TEMP_FILE, round(current, 2))
        write_scalar(MIN_TEMP_FILE, round(min_temp, 2))
        write_scalar(MAX_TEMP_FILE, round(max_temp, 2))
        
        print(f"IPC updated: current={current:.2f}, min={min_temp:.2f}, max={max_temp:.2f}")
    
    def _write_history_file(self):
        """Persist history to IPC file."""
        write_json(HISTORY_FILE, list(self.history))
    
    async def run(self):
        """Main daemon loop."""
        print("Sensor Daemon starting...")
        
        # Ensure IPC directory exists
        IPC_DIR.mkdir(parents=True, exist_ok=True)
        
        scanner = BleakScanner(
            self._detection_callback,
            service_uuids=[GOVEE_SERVICE_UUID]
        )
        
        print("Starting BLE scanner...")
        await scanner.start()
        
        try:
            while True:
                await asyncio.sleep(5)  # Process loop every 5 seconds
                
                now = time.time()
                valid_sensors = self._get_valid_sensors(now)
                
                # Update outputs
                self._update_history(now, valid_sensors)
                self._write_ipc_files(now, valid_sensors)
                self._write_history_file()
                
                # Log status
                stale_count = sum(
                    1 for s in self.sensors.values() if not s.is_valid(now)
                )
                if stale_count > 0:
                    print(f"Status: {len(valid_sensors)} valid, {stale_count} stale sensors")
                
        finally:
            await scanner.stop()
            print("Scanner stopped")


def main():
    """Entry point."""
    daemon = SensorDaemon()
    
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        print("\nShutting down...")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
