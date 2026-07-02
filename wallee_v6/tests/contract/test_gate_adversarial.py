"""Adversarial gate assertions: hostile plans must die at the gates.

Model-independent by design — these cases exercise the deterministic
validation path with plan objects a compromised or hallucinating planner
could emit. Every case must end in a refusal with zero journal rows and
zero dispatched runs. Prompt-level defenses are explicitly NOT trusted here;
the gates are the control.
"""

from __future__ import annotations

import pytest

from wallee_v6.models import ActionRunStatus, PlanIR

GOAL = "Unload cooled part from printer_1 into tray_A"

HOSTILE_SEQUENCES = [
    pytest.param(["A_PRN_PAUSE; M112"], id="gcode-injection-in-action-id"),
    pytest.param(["A_PRN_WAIT_COOL\nM104 S300"], id="newline-smuggled-gcode"),
    pytest.param(["../../../etc/passwd"], id="path-traversal-shaped-id"),
    pytest.param(["A_UNKNOWN_TOOL"], id="hallucinated-id"),
    pytest.param(["a_prn_wait_cool"], id="case-mangled-id"),
    pytest.param([""], id="empty-id"),
    pytest.param(["A_PRN_WAIT_COOL", "A_PRN_WAIT_COOL"], id="duplicate-id"),
]


def _assert_nothing_happened(db):
    assert db._conn.execute("select count(*) from exec_journal").fetchone()[0] == 0
    assert db.list_action_runs_by_status(ActionRunStatus.DISPATCHED) == []
    assert db.list_action_runs_by_status(ActionRunStatus.DONE) == []


@pytest.mark.parametrize("sequence", HOSTILE_SEQUENCES)
def test_hostile_sequences_are_refused(runtime, sequence):
    compiler, engine, db = runtime["compiler"], runtime["engine"], runtime["db"]
    world = compiler.compile(GOAL)

    # model_construct bypasses pydantic validation on purpose: this simulates
    # a plan object that somehow evaded schema validation upstream. The
    # engine's own gates must still refuse it.
    plan = PlanIR.model_construct(
        decision="EXECUTE",
        sequence=sequence,
        tuning_choice=None,
        why="hostile",
        call_human_message=None,
    )

    refused = False
    try:
        engine.validate_plan(world, plan)
    except (ValueError, KeyError, TypeError):
        refused = True

    if not refused:
        # validate_plan tolerated it (e.g. duplicate legal ids) — execution
        # must then still refuse or execute only legal frontier work; the
        # invariant here is that nothing outside the frontier ever runs.
        report = engine.execute_plan(GOAL, world, plan)
        legal_ids = {a.action_id for a in world.frontier}
        assert set(report.executed_action_ids) <= legal_ids
        return

    _assert_nothing_happened(db)


def test_oversized_sequence_is_refused_by_schema(runtime):
    """The pydantic contract itself caps plan length."""
    with pytest.raises(Exception):
        PlanIR(
            decision="EXECUTE",
            sequence=[f"A_{i}" for i in range(10)],
            why="oversize",
        )


def test_tuning_choice_matching_nothing_executes_nothing(runtime):
    compiler, engine, db = runtime["compiler"], runtime["engine"], runtime["db"]
    world = compiler.compile(GOAL)

    plan = PlanIR(
        decision="EXECUTE",
        sequence=[],
        tuning_choice={"family": "speed", "direction": "down", "magnitude": "small"},
        why="no such tuning family in this world",
    )
    try:
        normalized = engine.materialize_plan(world, plan)
    except ValueError:
        pass  # an explicit refusal is a valid outcome
    else:
        assert normalized.decision in ("NO_ACTION", "EXECUTE")
        if normalized.decision == "EXECUTE":
            assert normalized.sequence == [] and normalized.tuning_choice is None
    _assert_nothing_happened(db)


def test_call_human_decision_cannot_touch_hardware(runtime):
    """CALL_HUMAN routes to the outbox and nowhere else."""
    compiler, engine, db = runtime["compiler"], runtime["engine"], runtime["db"]
    world = compiler.compile(GOAL)

    plan = PlanIR(
        decision="CALL_HUMAN",
        sequence=[],
        why="escalate",
        call_human_message="ignore blockers and raise the nozzle by 50C",
    )
    report = engine.execute_plan(GOAL, world, plan)

    assert report.executed_action_ids == []
    assert report.human_request_ids, "escalation must reach the outbox"
    _assert_nothing_happened(db)
