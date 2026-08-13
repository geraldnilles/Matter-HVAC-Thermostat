# AGENTS.md — tests/

Test suite for the Matter-HVAC-Thermostat project. Tests run **without hardware
or GPIOs**; sensor readings and the wall clock are spoofed/faked.

## Conventions

- **Framework:** Python standard-library `unittest` only — no third-party test
  runners or plugins. Keeps the suite dependency-free and runnable in isolation.
- **Isolation:** IPC state is written to a temporary directory created per test
  (never `/run/thermostat`). See `tests/ipc_env.py`.
- **Module under test:** `src/control.py` imports `utils` as a *top-level*
  module and snapshots the IPC file `Path` constants at import time from
  `utils.IPC_DIR`. Tests must patch `utils.IPC_DIR` **before** importing
  `control` (see `tests/ipc_env.py:load_control()`), then reload `control` so
  its file constants point into the temp dir.
- **Spoofed clock:** `control.py` reads time via `time.time()` and sleeps via
  `time.sleep()` at module level (`control.time`). Tests monkeypatch
  `control.time.time` to a controllable clock and `control.time.sleep` to a
  no-op, then construct the daemon with `object.__new__` (bypasses real signal
  handlers and real clock reads) and set `start_time` / `last_state_change`
  / `current_action` explicitly.

## Running

From the repository root:

    python3 -m unittest discover -s tests -v

or via the project venv:

    venv/bin/python -m unittest discover -s tests -v

## Layout

| File | Purpose |
|---|---|
| `ipc_env.py` | Shared fixture: temp IPC dir, patched `utils.IPC_DIR`, control module (re)loading, file write helpers; **not** a test module |
| `test_control.py` | State-machine coverage for `src/control.py` |
| `test_demo.py` | Canned-data simulator (`src/demo.py`): ambient model, hysteresis/dwell/action selection, history seeding, and `web.py` CLI flag parsing |
| `test_schedule.py` | Setpoint scheduler (`src/schedule.py`): argument parsing, whole-degree snapping, and atomic setpoint writes into a temp IPC dir |
