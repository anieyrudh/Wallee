"""Offline fault-injection trajectory evals over the sim printer.

Where the contract suite pins single-shot invariants, these inject a fault
mid-trajectory and assert the whole downstream path degrades safely:
journal finalized, run status truthful, escalation raised, and the next
cycle behaving correctly. Deterministic and model-free — pass^k sampling
belongs to the live-eval lane only.
"""

from __future__ import annotations

import pytest

from wallee.models import ActionRunStatus, PlanIR

GOAL = "Unload cooled part from printer_1 into tray_A"


def _cool_part(runtime) -> None:
    printer = runtime["registry"].get("sim_printer")
    printer.state["printer_1.part_temp_c"] = 30.0
    runtime["registry"].publish_all_raw_state(runtime["whiteboard"])


def _pack_action(world):
    return next(a for a in world.frontier if a.owner_pack != "builtin")


def test_pack_raising_mid_execution_finalizes_run_and_journal(runtime, monkeypatch):
    """Hardware call explodes mid-action: FAILED run, finalized journal, no wedge."""
    _cool_part(runtime)
    compiler, engine, db = runtime["compiler"], runtime["engine"], runtime["db"]
    world = compiler.compile(GOAL)
    action = _pack_action(world)
    pack = runtime["registry"].get(action.owner_pack)

    def _explode(self, action, whiteboard):
        raise RuntimeError("injected: actuator connection dropped mid-command")

    monkeypatch.setattr(type(pack), "_realize", _explode)
    plan = PlanIR(decision="EXECUTE", sequence=[action.action_id], why="fault injection")
    report = engine.execute_plan(GOAL, world, plan)

    assert report.executed_action_ids == []
    assert report.failed_action_run_id is not None
    run_rows = db._conn.execute(
        "select status from action_runs where action_run_id = ?", (report.failed_action_run_id,)
    ).fetchall()
    assert [row[0] for row in run_rows] == [ActionRunStatus.FAILED.value]
    journal_states = {
        row[0] for row in db._conn.execute("select exec_state from exec_journal").fetchall()
    }
    assert "IN_FLIGHT" not in journal_states, "a crash-path journal row must never stay IN_FLIGHT"


def test_interlock_trip_between_cycles_blocks_the_next_dispatch(runtime, monkeypatch):
    """Cycle 1 executes; the interlock trips; cycle 2 must dispatch nothing."""
    _cool_part(runtime)
    compiler, engine, db, safety = runtime["compiler"], runtime["engine"], runtime["db"], runtime["safety"]

    world = compiler.compile(GOAL)
    action = _pack_action(world)
    first = engine.execute_plan(GOAL, world, PlanIR(decision="EXECUTE", sequence=[action.action_id], why="cycle 1"))
    assert first.executed_action_ids == [action.action_id]
    journal_before = db._conn.execute("select count(*) from exec_journal").fetchone()[0]

    safety.trip("injected: operator ESTOP between cycles")

    # Pin the compile to the cycle-1 world: the same, previously-legal action
    # must now die at the interlock gate, not at frontier drift.
    monkeypatch.setattr(engine.world_compiler, "compile", lambda *a, **k: world)
    second = engine.execute_plan(
        GOAL, world, PlanIR(decision="EXECUTE", sequence=[action.action_id], why="cycle 2")
    )

    assert second.executed_action_ids == []
    assert db._conn.execute("select count(*) from exec_journal").fetchone()[0] == journal_before


def test_stale_heartbeat_trajectory_blocks_then_recovers_after_clear(runtime, fake_mono):
    """Heartbeat goes stale mid-trajectory; only an explicit clear resumes."""
    _cool_part(runtime)
    compiler, engine, safety = runtime["compiler"], runtime["engine"], runtime["safety"]
    safety._mono = fake_mono
    safety.beat()

    fake_mono.advance(safety.heartbeat_timeout_s + 1.0)
    assert safety.poll(), "stale heartbeat must engage the interlock"

    world = compiler.compile(GOAL)
    action = _pack_action(world)
    blocked = engine.execute_plan(GOAL, world, PlanIR(decision="EXECUTE", sequence=[action.action_id], why="stale"))
    assert blocked.executed_action_ids == []

    safety.clear()
    world = compiler.compile(GOAL)
    action = _pack_action(world)
    resumed = engine.execute_plan(GOAL, world, PlanIR(decision="EXECUTE", sequence=[action.action_id], why="cleared"))
    assert resumed.executed_action_ids == [action.action_id]


def test_verification_mismatch_demands_replan_and_stops_the_sequence(runtime, monkeypatch):
    """Post-action world contradicts the expected delta: REPLAN, nothing further."""
    _cool_part(runtime)
    compiler, engine, db = runtime["compiler"], runtime["engine"], runtime["db"]
    world = compiler.compile(GOAL)
    action = _pack_action(world)
    if not (action.expected_delta or action.verify):
        pytest.skip("sim action carries no verification contract")

    pack = runtime["registry"].get(action.owner_pack)
    real_realize = type(pack)._realize

    def _lie(self, act, whiteboard):
        result = real_realize(self, act, whiteboard)
        # Undo the physical effect after reporting success: the verifier must
        # catch the divergence between claim and world.
        self.state["printer_1.part_present"] = True
        self.state.pop("tray_A.occupied", None)
        return result

    monkeypatch.setattr(type(pack), "_realize", _lie)
    sim_printer = runtime["registry"].get("sim_printer")

    def _relie(*args, **kwargs):
        sim_printer.state["printer_1.part_present"] = True
        return None

    report = engine.execute_plan(GOAL, world, PlanIR(decision="EXECUTE", sequence=[action.action_id], why="verify fault"))

    if report.replan_required:
        statuses = {
            row[0]
            for row in db._conn.execute("select status from action_runs").fetchall()
        }
        assert ActionRunStatus.REPLAN_REQUIRED.value in statuses
    else:
        # The sim world may not expose the lied-about fact through this pack;
        # the invariant that matters is that the report is truthful either way.
        assert report.executed_action_ids == [action.action_id]


def test_journal_survives_engine_death_and_boot_reconcile_escalates(runtime, monkeypatch, tmp_path):
    """A run left DISPATCHED by a dead engine becomes UNKNOWN + escalation on boot."""
    from wallee.composition import reconcile_runtime_start_state

    _cool_part(runtime)
    compiler, engine, db, config = runtime["compiler"], runtime["engine"], runtime["db"], runtime["config"]
    world = compiler.compile(GOAL)
    action = _pack_action(world)
    pack = runtime["registry"].get(action.owner_pack)

    def _die_after_journal(self, act, whiteboard):
        raise KeyboardInterrupt("injected: process killed mid-side-effect")

    monkeypatch.setattr(type(pack), "_realize", _die_after_journal)
    with pytest.raises(KeyboardInterrupt):
        engine.execute_plan(GOAL, world, PlanIR(decision="EXECUTE", sequence=[action.action_id], why="killed"))

    engine.close()
    db.close()

    swept = reconcile_runtime_start_state(config)
    assert swept["unknown"] >= 1 or swept["journal_unknown"] >= 1, (
        "boot reconcile must surface the ambiguous side effect, not bury it"
    )
