"""
State-transition tests for src/control.py.

Runs with no hardware, no GPIOs, and no real clock:

* IPC file paths are redirected into a per-test temporary directory
  (see tests/ipc_env.py), so /run/thermostat is never touched.
* wall-clock time and sleep are spoofed via a fake clock, so the startup
  safety delay, dwell timer, and hysteresis can be advanced instantly.

Covers every branch of ControlDaemon._calculate_desired_action() plus the
single-step loop body (_step), dwell gating, startup delay, data failsafe,
setpoint separation, and write-back behavior.
"""

import sys
import unittest
from pathlib import Path

# Ensure the fixture module is importable when tests are run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ipc_env import ControlEnv  # noqa: E402


DELETE_TEMP = "__DELETE__"
"""Sentinel: if passed as a temperature, leave that file absent."""


class ControlStateMachineTest(unittest.TestCase):
    """Base fixture with helpers for driving one daemon state at a time."""

    def setUp(self):
        self.env = ControlEnv()
        self.control = self.env.control
        self.H = self.control.HvacAction

    def tearDown(self):
        self.env.restore()

    def daemon(self, action=None, elapsed=None,
               startup_complete=None, start=None):
        """
        Build a daemon.

        action    -- current HvacAction (None -> IDLE)
        elapsed   -- seconds already spent in current_action (None -> dwell
                     fully expired); a positive value leaves dwell active
        startup_complete -- None -> True
        start     -- absolute fake-clock start time for the daemon
        """
        start = self.env.clock() if start is None else start
        if elapsed is None:
            # Default: dwell fully expired (state entered >=120 s ago).
            change = start - self.control.MIN_DWELL_TIME
        else:
            change = start - elapsed
        return self.env.make_daemon(
            current_action=action if action is not None else self.H.IDLE,
            startup_complete=startup_complete,
            start_time=start,
            last_state_change=change,
        )

    def seed(self, min_t=None, max_t=None, mode="heat", fan="auto",
             heat_sp=70.0, cool_sp=78.0):
        """Seed the IPC sandbox. Use DELETE_TEMP to leave files absent."""
        if min_t is not None and min_t != DELETE_TEMP:
            self.env.set_min_temp(min_t)
        if max_t is not None and max_t != DELETE_TEMP:
            self.env.set_max_temp(max_t)
        self.env.set_system_mode(mode)
        self.env.set_fan_mode(fan)
        self.env.set_set_temp_heat(heat_sp)
        self.env.set_set_temp_cool(cool_sp)

    def inputs(self, d):
        return d._read_inputs()

    def desired(self, d):
        return d._calculate_desired_action(d._read_inputs())


class DataFailsafeTests(ControlStateMachineTest):
    def test_missing_both_temps_forces_idle(self):
        d = self.daemon()
        self.seed(min_t=DELETE_TEMP, max_t=DELETE_TEMP, mode="heat")
        self.assertIsNone(self.env.read_action())
        self.assertEqual(self.desired(d), self.H.IDLE)

    def test_missing_min_temp_forces_idle(self):
        d = self.daemon()
        self.seed(min_t=DELETE_TEMP, max_t=73.0, mode="heat")
        self.assertEqual(self.desired(d), self.H.IDLE)

    def test_missing_max_temp_forces_idle(self):
        d = self.daemon()
        self.seed(min_t=69.0, max_t=DELETE_TEMP, mode="heat")
        self.assertEqual(self.desired(d), self.H.IDLE)

    def test_step_writes_idle_action_file_on_failsafe(self):
        d = self.daemon()
        self.seed(min_t=DELETE_TEMP, max_t=DELETE_TEMP, mode="heat")
        d._step()
        self.assertEqual(self.env.read_action(), "idle")


class HeatingStateTests(ControlStateMachineTest):
    def test_heat_demand_from_idle_starts_heating(self):
        d = self.daemon()
        self.seed(min_t=69.4, max_t=73.0, mode="heat")
        self.assertEqual(self.desired(d), self.H.HEATING)

    def test_heating_continues_until_satisfied(self):
        d = self.daemon(self.H.HEATING, elapsed=300)
        self.seed(min_t=70.0, max_t=73.0, mode="heat")
        self.assertEqual(self.desired(d), self.H.HEATING)

    def test_heating_exactly_satisfied_goes_to_fan_purge(self):
        d = self.daemon(self.H.HEATING, elapsed=300)
        self.seed(min_t=70.5, max_t=73.0, mode="heat")
        self.assertEqual(self.desired(d), self.H.FAN)

    def test_heat_dead_band_from_idle_is_idle(self):
        d = self.daemon()
        self.seed(min_t=70.0, max_t=73.0, mode="heat")
        self.assertEqual(self.desired(d), self.H.IDLE)


class CoolingStateTests(ControlStateMachineTest):
    def test_cool_demand_from_idle_starts_cooling(self):
        d = self.daemon()
        self.seed(min_t=73.0, max_t=78.6, mode="cool")
        self.assertEqual(self.desired(d), self.H.COOLING)

    def test_cooling_continues_until_satisfied(self):
        d = self.daemon(self.H.COOLING, elapsed=300)
        self.seed(min_t=73.0, max_t=78.0, mode="cool")
        self.assertEqual(self.desired(d), self.H.COOLING)

    def test_cooling_exactly_satisfied_goes_to_fan_purge(self):
        d = self.daemon(self.H.COOLING, elapsed=300)
        self.seed(min_t=73.0, max_t=77.5, mode="cool")
        self.assertEqual(self.desired(d), self.H.FAN)

    def test_cool_dead_band_from_idle_is_idle(self):
        d = self.daemon()
        self.seed(min_t=73.0, max_t=78.0, mode="cool")
        self.assertEqual(self.desired(d), self.H.IDLE)


class HysteresisBoundaryTests(ControlStateMachineTest):
    def test_heat_on_boundary_is_half_degree_below_strict(self):
        d = self.daemon()
        # exactly 69.5 is NOT < 69.5 -> stays idle
        self.seed(min_t=69.5, max_t=73.0, mode="heat")
        self.assertEqual(self.desired(d), self.H.IDLE)

    def test_heat_just_below_on_boundary_starts(self):
        d = self.daemon()
        self.seed(min_t=69.49, max_t=73.0, mode="heat")
        self.assertEqual(self.desired(d), self.H.HEATING)

    def test_heat_off_boundary_is_half_degree_above_inclusive(self):
        d = self.daemon(self.H.HEATING, elapsed=300)
        # >= 70.5 satisfies -> fan purge
        self.seed(min_t=70.5, max_t=73.0, mode="heat")
        self.assertEqual(self.desired(d), self.H.FAN)

    def test_heat_just_below_off_boundary_keeps_heating(self):
        d = self.daemon(self.H.HEATING, elapsed=300)
        self.seed(min_t=70.4, max_t=73.0, mode="heat")
        self.assertEqual(self.desired(d), self.H.HEATING)

    def test_cool_on_boundary_is_half_degree_above_strict(self):
        d = self.daemon()
        # exactly 78.5 is NOT > 78.5 -> stays idle
        self.seed(min_t=73.0, max_t=78.5, mode="cool")
        self.assertEqual(self.desired(d), self.H.IDLE)

    def test_cool_just_above_on_boundary_starts(self):
        d = self.daemon()
        self.seed(min_t=73.0, max_t=78.51, mode="cool")
        self.assertEqual(self.desired(d), self.H.COOLING)

    def test_cool_off_boundary_is_half_degree_below_inclusive(self):
        d = self.daemon(self.H.COOLING, elapsed=300)
        # <= 77.5 satisfies -> fan purge
        self.seed(min_t=73.0, max_t=77.5, mode="cool")
        self.assertEqual(self.desired(d), self.H.FAN)

    def test_cool_just_above_off_boundary_keeps_cooling(self):
        d = self.daemon(self.H.COOLING, elapsed=300)
        self.seed(min_t=73.0, max_t=77.6, mode="cool")
        self.assertEqual(self.desired(d), self.H.COOLING)


class FanModeTests(ControlStateMachineTest):
    def test_fan_on_with_idle_desire_yields_fan(self):
        d = self.daemon()
        self.seed(min_t=70.0, max_t=75.0, mode="heat", fan="on")
        self.assertEqual(self.desired(d), self.H.FAN)

    def test_fan_on_does_not_override_active_cooling(self):
        d = self.daemon(self.H.COOLING, elapsed=300)
        self.seed(min_t=73.0, max_t=78.6, mode="cool", fan="on")
        self.assertEqual(self.desired(d), self.H.COOLING)

    def test_fan_on_does_not_override_heat_demand(self):
        d = self.daemon()
        self.seed(min_t=69.4, max_t=73.0, mode="heat", fan="on")
        self.assertEqual(self.desired(d), self.H.HEATING)

    def test_fan_purge_wins_when_heating_satisfied_in_auto(self):
        d = self.daemon(self.H.HEATING, elapsed=300)
        self.seed(min_t=70.5, max_t=73.0, mode="heat")
        self.assertEqual(self.desired(d), self.H.FAN)


class OffModeAndFanTests(ControlStateMachineTest):
    def test_off_mode_forces_idle(self):
        d = self.daemon()
        self.seed(min_t=60.0, max_t=100.0, mode="off")
        self.assertEqual(self.desired(d), self.H.IDLE)

    def test_off_mode_with_fan_on_yields_fan(self):
        d = self.daemon()
        self.seed(min_t=60.0, max_t=100.0, mode="off", fan="on")
        self.assertEqual(self.desired(d), self.H.FAN)

    def test_unknown_mode_does_not_heat_or_cool(self):
        d = self.daemon()
        self.seed(min_t=60.0, max_t=100.0, mode="eco")
        self.assertEqual(self.desired(d), self.H.IDLE)


class AutoConflictTests(ControlStateMachineTest):
    def test_heat_wins_when_heat_deviation_larger_from_idle(self):
        d = self.daemon()
        # heat_diff = 70-64 = 6 > cool_diff = 82-78 = 4 -> heating
        self.seed(min_t=64.0, max_t=82.0, mode="auto")
        self.assertEqual(self.desired(d), self.H.HEATING)

    def test_cool_wins_when_cool_deviation_larger_from_idle(self):
        d = self.daemon()
        # heat_diff = 70-66 = 4 < cool_diff = 83-78 = 5 -> cooling
        self.seed(min_t=66.0, max_t=83.0, mode="auto")
        self.assertEqual(self.desired(d), self.H.COOLING)

    def test_exact_tie_prefers_cooling_from_idle(self):
        d = self.daemon()
        # heat_diff = 70-64 = 6 == cool_diff = 84-78 = 6 -> cooling
        self.seed(min_t=64.0, max_t=84.0, mode="auto")
        self.assertEqual(self.desired(d), self.H.COOLING)

    def test_active_heating_wins_conflict_via_priority(self):
        d = self.daemon(self.H.HEATING, elapsed=300)
        # heating already active; dwell expired -> stays heating
        self.seed(min_t=64.0, max_t=84.0, mode="auto")
        self.assertEqual(self.desired(d), self.H.HEATING)

    def test_active_cooling_wins_conflict_via_priority(self):
        d = self.daemon(self.H.COOLING, elapsed=300)
        # cooling already active; dwell expired -> stays cooling
        self.seed(min_t=64.0, max_t=84.0, mode="auto")
        self.assertEqual(self.desired(d), self.H.COOLING)


class SetpointSeparationTests(ControlStateMachineTest):
    def test_too_close_setpoints_expand_to_eight_degrees(self):
        d = self.daemon()
        self.seed(min_t=70.0, max_t=74.0, mode="heat",
                  heat_sp=72.0, cool_sp=75.0)
        self.desired(d)
        cool = self.env.read_set_temp_cool()
        heat = self.env.read_set_temp_heat()
        self.assertGreaterEqual(cool - heat, 8.0)
        self.assertAlmostEqual(cool + heat, 148.0, delta=0.001)

    def test_spec_example_72_75_becomes_70_78(self):
        d = self.daemon()
        self.seed(min_t=70.0, max_t=74.0, mode="heat",
                  heat_sp=72.0, cool_sp=75.0)
        self.desired(d)
        self.assertEqual(self.env.read_set_temp_heat(), 70.0)
        self.assertEqual(self.env.read_set_temp_cool(), 78.0)

    def test_valid_eight_degree_gap_not_modified(self):
        d = self.daemon()
        self.seed(min_t=70.0, max_t=74.0, mode="heat",
                  heat_sp=70.0, cool_sp=78.0)
        self.desired(d)
        self.assertEqual(self.env.read_set_temp_heat(), 70.0)
        self.assertEqual(self.env.read_set_temp_cool(), 78.0)

    def test_fractional_setpoints_snap_to_whole_degree(self):
        d = self.daemon()
        self.seed(min_t=70.0, max_t=74.0, mode="heat",
                  heat_sp=70.25, cool_sp=78.25)
        self.desired(d)
        self.assertEqual(self.env.read_set_temp_heat(), 70.0)
        self.assertEqual(self.env.read_set_temp_cool(), 78.0)


class DwellTimerTests(ControlStateMachineTest):
    def test_dwell_remaining_matches_elapsed(self):
        d = self.daemon(self.H.HEATING, elapsed=60)
        self.assertAlmostEqual(d._dwell_time_remaining(), 60.0)

    def test_dwell_expired_allows_transition(self):
        d = self.daemon(self.H.HEATING, elapsed=121)
        self.assertEqual(d._dwell_time_remaining(), 0.0)
        self.assertTrue(d._should_change_state(self.H.FAN))

    def test_dwell_blocks_state_change_between_different_actions(self):
        d = self.daemon(self.H.HEATING, elapsed=60)
        self.assertFalse(d._should_change_state(self.H.FAN))

    def test_same_action_is_never_a_state_change(self):
        d = self.daemon(self.H.HEATING, elapsed=60)
        self.assertFalse(d._should_change_state(self.H.HEATING))

    def test_just_entered_state_has_full_dwell(self):
        # elapsed=0 -> the state was entered "now", so the full 120 s remain
        d = self.daemon(self.H.IDLE, elapsed=0)
        self.assertAlmostEqual(
            d._dwell_time_remaining(), self.control.MIN_DWELL_TIME
        )
        self.assertFalse(d._should_change_state(self.H.HEATING))


class LoopBodyTests(ControlStateMachineTest):
    def test_write_action_writes_file_and_updates_state(self):
        d = self.daemon()
        d._write_action(self.H.HEATING)
        self.assertEqual(d.current_action, self.H.HEATING)
        self.assertEqual(self.env.read_action(), "heating")

    def test_step_reads_calculates_and_writes(self):
        d = self.daemon()
        self.seed(min_t=69.4, max_t=73.0, mode="heat")
        d._step()
        self.assertEqual(d.current_action, self.H.HEATING)
        self.assertEqual(self.env.read_action(), "heating")

    def test_step_writes_current_state_when_dwell_active(self):
        d = self.daemon(self.H.HEATING, elapsed=10)
        # 70.5 satisfies heating, but dwell keeps us writing "heating"
        self.seed(min_t=70.5, max_t=73.0, mode="heat")
        d._step()
        self.assertEqual(d.current_action, self.H.HEATING)
        self.assertEqual(self.env.read_action(), "heating")

    def test_step_allows_fan_purge_after_dwell(self):
        d = self.daemon(self.H.HEATING, elapsed=121)
        self.seed(min_t=70.5, max_t=73.0, mode="heat")
        d._step()
        self.assertEqual(d.current_action, self.H.FAN)
        self.assertEqual(self.env.read_action(), "fan")


class StartupDelayTests(ControlStateMachineTest):
    def test_step_noop_during_startup_delay(self):
        d = self.daemon(startup_complete=False, start=self.env.clock())
        self.seed(min_t=69.4, max_t=73.0, mode="heat")
        d._step()
        self.assertIsNone(self.env.read_action())
        self.assertEqual(d.current_action, self.H.IDLE)
        self.assertFalse(d.startup_complete)

    def test_step_completes_startup_exactly_at_delay(self):
        d = self.daemon(startup_complete=False,
                        start=self.env.clock() - 59.0)
        self.seed(min_t=69.4, max_t=73.0, mode="heat")
        d._step()
        self.assertFalse(d.startup_complete)

    def test_step_completes_startup_after_delay_and_writes_idle(self):
        d = self.daemon(startup_complete=False,
                        start=self.env.clock() - 60.0)
        self.seed(min_t=69.4, max_t=73.0, mode="heat")
        # First step finishes startup and begins the idle dwell; the dwell
        # gate blocks heating in this same iteration, so the file records
        # the current (idle) state.
        d._step()
        self.assertTrue(d.startup_complete)
        self.assertEqual(d.current_action, self.H.IDLE)
        self.assertEqual(self.env.read_action(), "idle")

    def test_heating_begins_after_idle_dwell_expires_post_startup(self):
        d = self.env.make_daemon(
            current_action=self.H.IDLE,
            startup_complete=False,
            start_time=self.env.clock() - 60.0,
            last_state_change=self.env.clock(),
        )
        self.seed(min_t=69.4, max_t=73.0, mode="heat")
        d._step()  # completes startup, starts dwell, writes "idle"
        self.assertTrue(d.startup_complete)
        self.assertEqual(self.env.read_action(), "idle")

        # Push the fake clock past the dwell and step again -> heating.
        self.env.clock.advance(self.control.MIN_DWELL_TIME)
        d._step()
        self.assertEqual(d.current_action, self.H.HEATING)
        self.assertEqual(self.env.read_action(), "heating")


if __name__ == "__main__":
    unittest.main(verbosity=2)
