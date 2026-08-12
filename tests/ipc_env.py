"""
Shared test fixture for control-logic tests.

Provides an isolated IPC environment (temporary directory instead of
/run/thermostat) and a controllable fake clock so tests run with no hardware,
no GPIOs, and no wall-clock dependency.

This module is a helper, not a test module; unittest discovery ignores it
because its name does not start with ``test_``.

Implementation note: ``utils`` binds its IPC file paths as module-level
constants (``MIN_TEMP_FILE = IPC_DIR / "min_temp"``) evaluated once at import.
Patching ``utils.IPC_DIR`` alone is therefore insufficient; we must also patch
each file constant on both ``utils`` and ``control`` so every read/write lands
inside the sandbox directory.
"""

import importlib
import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

# Make ``import utils`` resolve to src/utils.py in every test process.
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import utils  # noqa: E402

_ORIGINAL_IPC_DIR = utils.IPC_DIR

# attribute name -> relative file name inside the IPC directory
_FILE_NAMES = {
    "CURRENT_TEMP_FILE": "current_temp",
    "MIN_TEMP_FILE": "min_temp",
    "MAX_TEMP_FILE": "max_temp",
    "HISTORY_FILE": "history.json",
    "SYSTEM_MODE_FILE": "system_mode",
    "FAN_MODE_FILE": "fan_mode",
    "SET_TEMP_COOL_FILE": "set_temp_cool",
    "SET_TEMP_HEAT_FILE": "set_temp_heat",
    "HVAC_ACTION_FILE": "hvac_action",
}


class FakeClock:
    """A mutable, manually-advanced clock used to spoof time.time()."""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ControlEnv:
    """
    Wraps an isolated IPC directory plus a patched copy of ``control``.

    The ``control`` module is (re)loaded *after* the ``utils`` file constants
    have been pointed at the temporary directory, so the daemon reads and
    writes only inside the sandbox.
    """

    def __init__(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="thermostat-tests-"))
        self.clock = FakeClock()

        # 1. Point every utils file constant into the sandbox.
        utils.IPC_DIR = self.tmpdir
        for attr, fname in _FILE_NAMES.items():
            setattr(utils, attr, self.tmpdir / fname)

        # 2. (Re)load control so its ``from utils import ...`` picks up the
        #    patched paths.
        self.control = self._load_control()
        # Defensive: make sure control's view matches even if it was imported
        # earlier and reload kept stale references.
        for attr, fname in _FILE_NAMES.items():
            setattr(self.control, attr, self.tmpdir / fname)

        # 3. Swap in the fake clock. ``control.time`` is the real ``time``
        #    module; we replace it with a shim exposing time() and sleep().
        fake_time = types.SimpleNamespace(
            time=self.clock,
            sleep=lambda seconds: self.clock.advance(seconds),
        )
        self.control.time = fake_time

    def _load_control(self):
        if "control" in sys.modules:
            importlib.reload(sys.modules["control"])
        else:
            importlib.import_module("control")
        return sys.modules["control"]

    # ------------------------------------------------------------------ #
    # File seeding helpers (seeded via utils' atomic writers so production
    # parsing/writing code paths are exercised).
    # ------------------------------------------------------------------ #
    def set_min_temp(self, value):
        utils.write_scalar(utils.MIN_TEMP_FILE, value)

    def set_max_temp(self, value):
        utils.write_scalar(utils.MAX_TEMP_FILE, value)

    def set_system_mode(self, value):
        utils.write_scalar(utils.SYSTEM_MODE_FILE, value)

    def set_fan_mode(self, value):
        utils.write_scalar(utils.FAN_MODE_FILE, value)

    def set_set_temp_cool(self, value):
        utils.write_scalar(utils.SET_TEMP_COOL_FILE, value)

    def set_set_temp_heat(self, value):
        utils.write_scalar(utils.SET_TEMP_HEAT_FILE, value)

    def clear_temp_files(self):
        """Remove sensor files to simulate the data-failsafe condition."""
        for path in (utils.MIN_TEMP_FILE, utils.MAX_TEMP_FILE):
            if path.exists():
                path.unlink()

    def read_action(self):
        return utils.read_file(utils.HVAC_ACTION_FILE)

    def read_set_temp_cool(self):
        return utils.read_float(utils.SET_TEMP_COOL_FILE)

    def read_set_temp_heat(self):
        return utils.read_float(utils.SET_TEMP_HEAT_FILE)

    # ------------------------------------------------------------------ #
    # Daemon construction — bypasses ControlDaemon.__init__ so no real
    # signal handlers are installed and the fake clock fully controls time.
    # ------------------------------------------------------------------ #
    def make_daemon(self, current_action=None, startup_complete=None,
                    start_time=None, last_state_change=None):
        daemon = object.__new__(self.control.ControlDaemon)
        daemon.current_action = (
            current_action if current_action is not None
            else self.control.HvacAction.IDLE
        )
        daemon.startup_complete = (
            startup_complete if startup_complete is not None else True
        )
        daemon.start_time = start_time if start_time is not None else self.clock()
        daemon.last_state_change = (
            last_state_change if last_state_change is not None else self.clock()
        )
        return daemon

    def restore(self):
        """Restore module state to production paths (best-effort)."""
        utils.IPC_DIR = _ORIGINAL_IPC_DIR
        for attr, fname in _FILE_NAMES.items():
            setattr(utils, attr, _ORIGINAL_IPC_DIR / fname)
        self._load_control()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
