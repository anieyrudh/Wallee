"""Shared helpers for the safety-contract suite.

The `runtime` fixture itself comes from `wallee_v6/tests/conftest.py` (the
whole-runtime-in-tmp_path composition over the sim packs); this file only adds
contract-specific helpers.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every not-yet-true invariant is a strict xfail whose reason must reference
# the execution-plan item that schedules the fix. scripts/check_contract_manifest.py
# enforces this format, so a gap can never be marked quietly.
PLAN = "docs/REFACTOR_EXECUTION_PLAN.md"


class FakeMonotonic:
    """Injectable monotonic clock for TTL/latch tests.

    Wall-clock-driven tests cannot rule out timed un-latching (the legacy
    ESTOP bug class expired after 600 s); a fake clock can.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_mono():
    return FakeMonotonic()


@pytest.fixture
def second_db_connection(runtime):
    """An independent SQLite connection to the runtime DB.

    Used to prove *committed* durability (not same-connection visibility)
    at the moment a side effect starts.
    """
    conn = sqlite3.connect(runtime["config"].db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def make_hazardous(action, hazard_class):
    """Mark a frontier action as approval-gated for approval-path tests."""
    action.approval_required = True
    action.hazard_class = hazard_class
    return action


@pytest.fixture
def unload_runtime(runtime):
    """Runtime whose sim world offers a pack-owned action.

    The default sim state only offers builtin actions (WAIT_COOL,
    CALL_HUMAN); cooling the part makes the sim_arm unload action legal,
    which exercises the real pack execute/journal path.
    """
    printer = runtime["registry"].get("sim_printer")
    printer.state["printer_1.part_temp_c"] = 30.0
    runtime["registry"].publish_all_raw_state(runtime["whiteboard"])
    return runtime


def pin_world(monkeypatch, engine, world):
    """Freeze the engine's world refresh to a specific (possibly mutated) world.

    execute_plan recompiles the world for its frontier refresh and TOCTOU
    re-check; tests that mutate frontier metadata (e.g. approval_required)
    must pin the compile result or the mutation is silently replaced.
    """
    monkeypatch.setattr(engine.world_compiler, "compile", lambda *args, **kwargs: world)
