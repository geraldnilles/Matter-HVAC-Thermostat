"""
Tests for src/setup.py boot-time IPC seeding.

Verifies the "existing files win" contract: setup seeds /run/thermostat/
from defaults.json on a fresh tmpfs but never overwrites files that are
already present. This is what allows an external backup/restore service
to populate the IPC directory either before or after thermostat-setup
runs without its values being clobbered.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import utils  # noqa: E402
import setup  # noqa: E402


class SetupEnv:
    """Retargets utils/setup IPC paths into a per-test temporary directory."""

    _FILE_ATTRS = (
        "SYSTEM_MODE_FILE",
        "FAN_MODE_FILE",
        "SET_TEMP_COOL_FILE",
        "SET_TEMP_HEAT_FILE",
    )

    def __init__(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="thermostat-setup-tests-"))
        # Both modules import these names by value ("from utils import ..."),
        # so each must be patched independently.
        utils.IPC_DIR = self.tmpdir
        setup.IPC_DIR = self.tmpdir
        for attr in self._FILE_ATTRS:
            path = self.tmpdir / attr.lower().removesuffix("_file")
            setattr(utils, attr, path)
            setattr(setup, attr, path)

    def restore(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


DEFAULTS = {
    "system_mode": "off",
    "fan_mode": "auto",
    "set_temp_cool": 76.0,
    "set_temp_heat": 68.0,
}


class TestInitializeIpcFiles(unittest.TestCase):
    def setUp(self):
        self.env = SetupEnv()

    def tearDown(self):
        self.env.restore()

    def test_fresh_directory_seeds_all_defaults(self):
        """First boot (empty tmpfs): every mapped key is written from config."""
        setup.initialize_ipc_files(DEFAULTS)
        self.assertEqual(utils.read_file(utils.SYSTEM_MODE_FILE), "off")
        self.assertEqual(utils.read_file(utils.FAN_MODE_FILE), "auto")
        self.assertEqual(utils.read_float(utils.SET_TEMP_COOL_FILE), 76.0)
        self.assertEqual(utils.read_float(utils.SET_TEMP_HEAT_FILE), 68.0)

    def test_existing_files_are_not_overwritten(self):
        """Restore-before-setup: pre-existing values survive initialization."""
        utils.write_scalar(utils.SYSTEM_MODE_FILE, "cool")
        utils.write_scalar(utils.FAN_MODE_FILE, "on")
        utils.write_scalar(utils.SET_TEMP_COOL_FILE, 72)
        utils.write_scalar(utils.SET_TEMP_HEAT_FILE, 70)

        setup.initialize_ipc_files(DEFAULTS)

        self.assertEqual(utils.read_file(utils.SYSTEM_MODE_FILE), "cool")
        self.assertEqual(utils.read_file(utils.FAN_MODE_FILE), "on")
        self.assertEqual(utils.read_float(utils.SET_TEMP_COOL_FILE), 72.0)
        self.assertEqual(utils.read_float(utils.SET_TEMP_HEAT_FILE), 70.0)

    def test_partial_state_backfills_only_missing_files(self):
        """Only absent files are seeded; present ones keep their values."""
        utils.write_scalar(utils.SYSTEM_MODE_FILE, "heat")

        setup.initialize_ipc_files(DEFAULTS)

        self.assertEqual(utils.read_file(utils.SYSTEM_MODE_FILE), "heat")
        self.assertEqual(utils.read_file(utils.FAN_MODE_FILE), "auto")

    def test_rerun_is_idempotent_and_preserves_external_writes(self):
        """Setup re-runs (service restart) never clobber later external writes."""
        setup.initialize_ipc_files(DEFAULTS)
        utils.write_scalar(utils.SYSTEM_MODE_FILE, "auto")  # restore-after-setup
        setup.initialize_ipc_files(DEFAULTS)                # re-run
        self.assertEqual(utils.read_file(utils.SYSTEM_MODE_FILE), "auto")


if __name__ == "__main__":
    unittest.main()
