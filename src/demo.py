#!/usr/bin/env python3
"""
Canned-data simulator for the thermostat WebUI.

Lets developers run the Flask dashboard in ``web.py`` locally with no sensors,
no GPIOs, and no relays. It generates realistic, self-consistent state —
per-room temperatures, the 24-hour history ring buffer, and a plausible HVAC
action (with hysteresis and dwell) — and keeps feeding new samples so the page
feels alive. Mode/fan/setpoint changes made through the UI are picked up and
reflected in the simulation.

State is written to an isolated data directory (default: a fresh temp dir)
using the same atomic writers as production (``utils.write_scalar``,
``utils.write_json``), so no part of ``/run/thermostat`` is ever touched.
"""

import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from utils import read_file, read_float, write_json, write_scalar

# --------------------------------------------------------------------------- #
# Tuning constants (named module-level constants per project convention)
# --------------------------------------------------------------------------- #
HISTORY_MINUTES = 24 * 60      # 1440 samples, 1-minute interval
SAMPLE_SECONDS = 60
MAX_HISTORY = HISTORY_MINUTES

HYSTERESIS = 0.5               # °F, centered on setpoint (spec §4.2)
MIN_DWELL_SECONDS = 120        # °s, spec §4.2 minimum state dwell

# Room temperature model
AMBIENT_MEAN = 68.0            # °F daily mean outdoor temperature
AMBIENT_AMP = 9.0              # °F diurnal swing (min at night, max ~15:00)
PEAK_FRACTION = 0.625          # fraction of day when outdoor temp peaks (~15:00)
LEAK = 0.03                    # per-minute fraction a room drifts toward ambient
HEAT_RATE = 0.15               # °F/min the whole home warms while heating
COOL_RATE = 0.20               # °F/min the whole home cools while cooling
NOISE = 0.06                   # +/- °F random walk per room per minute

VALID_MODES = {"off", "cool", "heat", "auto"}
VALID_FANS = {"auto", "on"}

# IPC file names inside the data directory (kept independent of utils' constants
# so this module never depends on which directory utils points at).
F_CURRENT_TEMP = "current_temp"
F_MIN_TEMP = "min_temp"
F_MAX_TEMP = "max_temp"
F_HISTORY = "history.json"
F_SYSTEM_MODE = "system_mode"
F_FAN_MODE = "fan_mode"
F_SET_COOL = "set_temp_cool"
F_SET_HEAT = "set_temp_heat"
F_ACTION = "hvac_action"

# Per-room offsets (°F) above ambient give rooms distinct temperatures so the
# UI's min (bedroom) / max (office) behavior matches production semantics.
_DEFAULT_ROOM_OFFSETS = [1.0, 0.0, 2.0]


@dataclass
class Room:
    """A single simulated sensor location."""

    name: str
    offset: float
    temp: float = 0.0


def rooms_from_names(names):
    """Build :class:`Room` objects from sensor display names (e.g. config)."""
    offsets = _DEFAULT_ROOM_OFFSETS
    return [
        Room(name=name, offset=offsets[i % len(offsets)])
        for i, name in enumerate(names)
    ]


class DemoSimulator:
    """
    Generates and continuously refreshes canned thermostat state.

    State is written to ``data_dir`` (one file per IPC value, matching the
    production file names and formats). Reading always goes through the
    files on disk, which is how WebUI changes made via ``web.py`` flow back
    into the simulation.
    """

    def __init__(self, data_dir=None, rooms=None, now=None, seed=None,
                 mode="auto", fan="auto", cool=76.0, heat=68.0,
                 interval=SAMPLE_SECONDS):
        self.data_dir = Path(data_dir) if data_dir else None
        self.interval = interval
        self.rng = random.Random(seed)

        self.rooms = rooms if rooms is not None else list(
            Room(name=name, offset=offset)
            for name, offset in zip(
                ("Living Room", "Bedroom", "Office"), _DEFAULT_ROOM_OFFSETS
            )
        )

        self.mode = mode
        self.fan = fan
        self.cool = float(cool)
        self.heat = float(heat)

        self.now = now if now is not None else time.time()
        self.action = "idle"
        self.action_since = self.now

        self.history = deque(maxlen=MAX_HISTORY)
        self._avg = self._min = self._max = 0.0

        self._running = False
        self._thread = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Deterministic helpers (unit-tested)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fraction_of_day(now):
        """Seconds since local midnight as a fraction of the day [0, 1)."""
        lt = time.localtime(now)
        return (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec) / 86400.0

    def _ambient(self, now):
        """Outdoor temperature (°F) at ``now`` — a diurnal sinusoid."""
        frac = self._fraction_of_day(now)
        return AMBIENT_MEAN + AMBIENT_AMP * math.cos(
            2 * math.pi * (frac - PEAK_FRACTION)
        )

    def _demand_action(self, min_temp, max_temp):
        """
        Unlatched desired action from current temps, ignoring dwell.

        Implements the hysteresis logic from spec §4.2 (centered on setpoint)
        plus auto-mode conflict resolution and the fan-on idle rule.
        """
        mode = self.mode
        fan = self.fan
        current = self.action

        if mode == "off":
            return "fan" if fan == "on" else "idle"

        heat_allowed = mode in ("heat", "auto")
        cool_allowed = mode in ("cool", "auto")

        # Latch-based hysteresis: an active state holds until the far edge of
        # its band; otherwise it would shut off the instant it crossed the
        # setpoint.
        if current == "heating":
            want_heat = min_temp < self.heat + HYSTERESIS
        else:
            want_heat = min_temp <= self.heat - HYSTERESIS

        if current == "cooling":
            want_cool = max_temp > self.cool - HYSTERESIS
        else:
            want_cool = max_temp >= self.cool + HYSTERESIS

        want_heat = want_heat and heat_allowed
        want_cool = want_cool and cool_allowed

        # Auto mode conflict resolution (spec §4.2): compare temperature
        # differences and prioritize the larger need.
        if want_heat and want_cool:
            heat_diff = self.heat - min_temp
            cool_diff = max_temp - self.cool
            if heat_diff > cool_diff:
                want_cool = False
            else:
                want_heat = False

        if want_heat:
            return "heating"
        if want_cool:
            return "cooling"
        if fan == "on":
            return "fan"
        return "idle"

    def choose_action(self, min_temp, max_temp, now=None):
        """
        Return the action the system should occupy now, honoring dwell time.

        Pure w.r.t. ``min_temp``/``max_temp``/``now`` — does not mutate state.
        Includes the post-cycle fan purge from spec §4.2.
        """
        if now is None:
            now = self.now
        demand = self._demand_action(min_temp, max_temp)
        if demand == self.action:
            return self.action
        if now - self.action_since < MIN_DWELL_SECONDS:
            return self.action
        # Post-cycle purge: after heating/cooling completes with fan_mode=auto,
        # run the fan for one dwell period before returning to idle.
        if self.action in ("heating", "cooling") and demand == "idle" \
                and self.fan == "auto":
            return "fan"
        return demand

    # ------------------------------------------------------------------ #
    # State generation
    # ------------------------------------------------------------------ #
    def _apply_physics(self):
        """Advance room temperatures one interval under the current action."""
        ambient = self._ambient(self.now)
        for room in self.rooms:
            target = ambient + room.offset
            drift = (target - room.temp) * LEAK
            noise = self.rng.uniform(-NOISE, NOISE)
            hvac = 0.0
            if self.action == "heating":
                hvac = HEAT_RATE
            elif self.action == "cooling":
                hvac = -COOL_RATE
            room.temp = round(room.temp + drift + noise + hvac, 2)

    def _step(self, write_ipc=True):
        """
        Advance the simulation by one sampling interval and append a history
        sample. When ``write_ipc`` is true, also flush scalar files + history
        to the data directory (using atomic writers).
        """
        prev_action = self.action

        self._apply_physics()

        temps = [room.temp for room in self.rooms]
        avg = round(sum(temps) / len(temps), 2)
        mn = round(min(temps), 2)
        mx = round(max(temps), 2)

        new_action = self.choose_action(mn, mx, self.now)
        if new_action != self.action:
            self.action = new_action
            self.action_since = self.now

        # Record the action that was actually applied during this interval,
        # so the action bar lines up with the temperature curve.
        sample = {
            "t": int(self.now),
            "avg": avg,
            "sensors": {room.name: room.temp for room in self.rooms},
            "set_temp_cool": self.cool,
            "set_temp_heat": self.heat,
            "hvac_action": prev_action,
        }
        self.history.append(sample)
        self.now += self.interval

        self._avg, self._min, self._max = avg, mn, mx

        if write_ipc:
            self._write_ipc()
        return sample

    def _write_ipc(self):
        d = self.data_dir
        d.mkdir(parents=True, exist_ok=True)
        write_scalar(d / F_CURRENT_TEMP, self._avg)
        write_scalar(d / F_MIN_TEMP, self._min)
        write_scalar(d / F_MAX_TEMP, self._max)
        write_scalar(d / F_SYSTEM_MODE, self.mode)
        write_scalar(d / F_FAN_MODE, self.fan)
        write_scalar(d / F_SET_COOL, self.cool)
        write_scalar(d / F_SET_HEAT, self.heat)
        write_scalar(d / F_ACTION, self.action)
        write_json(d / F_HISTORY, list(self.history))

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def _sync_from_ipc(self):
        """Adopt mode/fan/setpoints the WebUI may have written to disk."""
        read = lambda name: read_file(self.data_dir / name)  # noqa: E731
        mode = read(F_SYSTEM_MODE)
        if mode in VALID_MODES:
            self.mode = mode
        fan = read(F_FAN_MODE)
        if fan in VALID_FANS:
            self.fan = fan
        cool = read_float(self.data_dir / F_SET_COOL)
        if cool is not None:
            self.cool = cool
        heat = read_float(self.data_dir / F_SET_HEAT)
        if heat is not None:
            self.heat = heat

    def seed_history(self, hours=24):
        """
        Pre-fill the 24-hour history ring buffer so the UI graph is populated
        immediately, then write the current state to disk.

        Starts ``hours`` ago and steps forward one minute at a time, applying
        the same physics and action logic the live thread uses.
        """
        with self._lock:
            self.now = (self.now - hours * 3600)
            ambient = self._ambient(self.now)
            for room in self.rooms:
                room.temp = round(ambient + room.offset, 2)
            self.action = "idle"
            self.action_since = self.now
            self.history.clear()

            ticks = hours * 3600 // self.interval - 1
            for _ in range(ticks):
                self._step(write_ipc=False)
            # Final step also pushes the current state to the data directory.
            self._step(write_ipc=True)

    def start(self):
        """Start the background refresh thread (no-op if already running)."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._running = True
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self):
        while self._running:
            time.sleep(self.interval)
            with self._lock:
                self._sync_from_ipc()
                self._step(write_ipc=True)

    def stop(self):
        """Stop the background thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None


def main():
    """Standalone smoke test: seed a demo and print a short summary."""
    import tempfile

    data_dir = tempfile.mkdtemp(prefix="thermostat-demo-")
    sim = DemoSimulator(data_dir=data_dir)
    sim.seed_history()
    sim._write_ipc()

    print(f"data_dir: {data_dir}")
    print(f"rooms: {[r.name for r in sim.rooms]}")
    print(f"final state: avg={sim._avg} min={sim._min} max={sim._max} "
          f"action={sim.action} mode={sim.mode} fan={sim.fan} "
          f"cool={sim.cool} heat={sim.heat}")
    print("history samples:", len(sim.history))
    print("oldest:", sim.history[0])
    print("newest:", sim.history[-1])


if __name__ == "__main__":
    main()
