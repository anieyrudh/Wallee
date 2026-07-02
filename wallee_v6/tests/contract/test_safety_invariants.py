"""Executable contracts for Wallee's six safety promises.

Each promise from the README/architecture docs is pinned here as a test:

  P1  The LLM proposes; deterministic code validates and dispatches.
  P2  Closed, bounded action space validated outside the prompt.
  P3  An independent safety layer can stop the machine even if control dies.
  P5  Durable audit trail; crash-recoverable.
  P6  Human approval gates hazardous actions; a human can always ESTOP.

(P4 — packs own hardware, core stays generic — lives in
`test_core_genericity.py` with its own Phase-3 milestone, so this file can
reach zero xfails at the Phase-2 exit without waiting on the eviction work.)

Invariants that are NOT yet true are `xfail(strict=True)` with a reason
pointing at the execution-plan item that schedules the fix: the gap and the
progress are both machine-visible, and nothing flips silently. Deleting or
renaming a test here without updating MANIFEST fails CI
(scripts/check_contract_manifest.py).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from wallee_v6.main import build_runtime, reconcile_runtime_start_state
from wallee_v6.models import ActionRunStatus, HazardClass, PlanIR
from wallee_v6.safety import SafetyKernel

from .conftest import PLAN, REPO_ROOT, make_hazardous, pin_world

GOAL = "Unload cooled part from printer_1 into tray_A"
SENTINEL = "G28 ; INJECTED-FREE-TEXT-MUST-NEVER-REACH-A-DRIVER"


# ---------------------------------------------------------------------------
# P1 — the LLM proposes; deterministic code disposes
# ---------------------------------------------------------------------------


def test_plan_referencing_unknown_action_is_rejected(runtime):
    compiler, engine, db = runtime["compiler"], runtime["engine"], runtime["db"]
    world = compiler.compile(GOAL)

    plan = PlanIR(decision="EXECUTE", sequence=["A_DOES_NOT_EXIST"], why="hallucinated")
    with pytest.raises(ValueError):
        engine.validate_plan(world, plan)

    assert db._conn.execute("select count(*) from exec_journal").fetchone()[0] == 0


def test_planner_free_text_never_reaches_pack_execution(unload_runtime):
    """Model prose (why / call_human_message) must never flow into a driver.

    Action arguments come only from the frontier compiler; the planner picks
    an id, it does not author arguments.
    """
    runtime = unload_runtime
    compiler, engine, registry = runtime["compiler"], runtime["engine"], runtime["registry"]
    world = compiler.compile(GOAL)
    action = next(a for a in world.frontier if a.owner_pack != "builtin")

    recorded: list[str] = []
    for pack_name in ("sim_printer", "sim_arm"):
        pack = registry.get(pack_name)
        original = pack.execute

        def recording_execute(_original=original, **kwargs):
            recorded.append(json.dumps({k: str(v) for k, v in kwargs.items()}, sort_keys=True))
            return _original(**kwargs)

        pack.execute = recording_execute

    plan = PlanIR(decision="EXECUTE", sequence=[action.action_id], why=SENTINEL)
    engine.execute_plan(GOAL, world, plan)

    assert all(SENTINEL not in call for call in recorded)


def test_plan_ir_has_no_free_form_args_channel():
    """PlanIR's shape IS the trust boundary: ids and enums only, no payloads."""
    assert set(PlanIR.model_fields) <= {
        "decision",
        "sequence",
        "tuning_choice",
        "why",
        "call_human_message",
    }
    sequence_item_type = PlanIR.model_fields["sequence"].annotation
    assert sequence_item_type == list[str]


# ---------------------------------------------------------------------------
# P2 — closed, bounded action space validated outside the prompt
# ---------------------------------------------------------------------------


def test_tuning_choice_materializes_to_exactly_one_frontier_action(runtime):
    from wallee_v6.models import LegalAction, WorldPacket

    engine = runtime["engine"]
    world = WorldPacket(
        goal="tune",
        device_summaries=[],
        facts={},
        resources=[],
        blockers=[],
        deltas=[],
        frontier=[
            LegalAction(
                action_id="A_PRUSA_TRIM_FLOW_DOWN_SMALL",
                verb="TUNE_FLOW",
                description="Reduce flow a little.",
                owner_pack="prusa_core_one_plus",
                execute_ref="trim_flow_down_small",
                hazard_class=HazardClass.LOW,
            )
        ],
        last_result={},
        pending_human=[],
    )
    plan = PlanIR(
        decision="EXECUTE",
        sequence=[],
        tuning_choice={"family": "flow", "direction": "down", "magnitude": "small"},
        why="bounded tuning",
    )

    actions = engine.validate_plan(world, plan)

    assert [a.action_id for a in actions] == ["A_PRUSA_TRIM_FLOW_DOWN_SMALL"]


def test_frontier_actions_carry_bounded_pack_owned_metadata(runtime):
    """Every legal action is pack-authored: args, hazard class, owner."""
    compiler = runtime["compiler"]
    world = compiler.compile(GOAL)

    assert world.frontier, "sim world must offer at least one action"
    for action in world.frontier:
        assert action.owner_pack
        assert isinstance(action.hazard_class, HazardClass)
        assert isinstance(action.args, dict)


# ---------------------------------------------------------------------------
# P3 — independent safety layer can stop the machine even if control dies
# ---------------------------------------------------------------------------


def test_interlock_has_no_time_based_unlatch(fake_mono):
    """A tripped interlock stays tripped across 24 fake hours.

    This pins the property whose absence was the legacy ESTOP bug (a stop
    flag that silently expired after 600 s).
    """
    kernel = SafetyKernel(heartbeat_timeout_s=3.0, mono_fn=fake_mono)
    kernel.trip("contract-test")

    fake_mono.advance(24 * 3600)
    kernel.poll()

    assert kernel.interlock.engaged is True
    assert kernel.interlock.reason == "contract-test"


@pytest.mark.xfail(
    strict=True,
    reason=f"Phase 2 (interlock wiring): engine must refuse dispatch while tripped — {PLAN} §5",
)
def test_tripped_interlock_blocks_dispatch(runtime):
    compiler, engine, db, safety = (
        runtime["compiler"],
        runtime["engine"],
        runtime["db"],
        runtime["safety"],
    )
    world = compiler.compile(GOAL)
    action = world.frontier[0]

    safety.trip("contract-test")
    plan = PlanIR(decision="EXECUTE", sequence=[action.action_id], why="must be blocked")
    report = engine.execute_plan(GOAL, world, plan)

    assert report.executed_action_ids == []
    assert db._conn.execute("select count(*) from exec_journal").fetchone()[0] == 0


@pytest.mark.xfail(
    strict=True,
    reason=f"Phase 2 (stop transport): build_runtime must register a stop callback — {PLAN} §5",
)
def test_interlock_trip_invokes_registered_stop_callback(tmp_path, monkeypatch):
    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WALLEE_ENABLED_PACKS", "sim_printer,sim_arm")
    monkeypatch.setenv("WALLEE_SIMULATION", "1")
    from wallee_v6.config import Config

    config = Config.from_env(repo_root=REPO_ROOT)
    runtime_db, _, _, _, _, safety, engine, _ = build_runtime(config)
    try:
        assert safety._callbacks, (
            "the real composition must register at least one physical-stop "
            "callback; a trip with no callback stops nothing"
        )
    finally:
        engine.close()
        runtime_db.close()


@pytest.mark.xfail(
    strict=True,
    reason=f"Phase 2 (interlock wiring): stale heartbeat must block the next dispatch — {PLAN} §5",
)
def test_stale_heartbeat_blocks_next_dispatch(runtime, fake_mono):
    compiler, engine, db, safety = (
        runtime["compiler"],
        runtime["engine"],
        runtime["db"],
        runtime["safety"],
    )
    safety._mono = fake_mono
    safety.beat()
    world = compiler.compile(GOAL)
    action = world.frontier[0]

    fake_mono.advance(safety.heartbeat_timeout_s * 10)
    safety.poll()  # trips
    assert safety.interlock.engaged is True

    plan = PlanIR(decision="EXECUTE", sequence=[action.action_id], why="must be blocked")
    report = engine.execute_plan(GOAL, world, plan)

    assert report.executed_action_ids == []
    assert db._conn.execute("select count(*) from exec_journal").fetchone()[0] == 0


@pytest.mark.xfail(
    strict=True,
    reason=f"Phase 2 (heartbeat semantics): failure paths must still beat — {PLAN} §5",
)
def test_heartbeat_beaten_on_failure_paths(runtime, fake_mono, monkeypatch):
    compiler, engine, registry, safety = (
        runtime["compiler"],
        runtime["engine"],
        runtime["registry"],
        runtime["safety"],
    )
    safety._mono = fake_mono
    world = compiler.compile(GOAL)
    action = world.frontier[0]

    pack = registry.get(action.owner_pack) if action.owner_pack != "builtin" else None
    if pack is not None:
        def exploding_execute(**kwargs):
            raise RuntimeError("simulated hardware fault")

        monkeypatch.setattr(pack, "execute", exploding_execute)
    else:
        monkeypatch.setattr(engine, "_execute_builtin", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    fake_mono.advance(1.0)
    before = safety._last_heartbeat_mono
    plan = PlanIR(decision="EXECUTE", sequence=[action.action_id], why="failing pass")
    engine.execute_plan(GOAL, world, plan)

    assert safety._last_heartbeat_mono > before, (
        "a failed engine pass must still prove liveness; otherwise the "
        "watchdog cannot distinguish a wedged loop from a failing one"
    )


@pytest.mark.xfail(
    strict=True,
    reason=f"Phase 2 (watchdog process): the shipped safety unit is a sleep placeholder — {PLAN} §5",
)
def test_shipped_safety_service_is_not_a_placeholder():
    unit = (REPO_ROOT / "deploy" / "systemd" / "wallee-safety.service").read_text(encoding="utf-8")
    exec_lines = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
    assert exec_lines, "unit must define ExecStart"
    for line in exec_lines:
        assert "sleep" not in line and "placeholder" not in line.lower(), line


@pytest.mark.xfail(
    strict=True,
    reason=f"Phase 2 (durable latch): a trip must survive restart — {PLAN} §5",
)
def test_interlock_trip_survives_restart(runtime):
    config, safety = runtime["config"], runtime["safety"]
    safety.trip("contract-test")

    reborn = SafetyKernel(heartbeat_timeout_s=safety.heartbeat_timeout_s)
    # A restarted kernel must rediscover the latch from durable state under
    # config.data_dir; today the trip lives only in process memory.
    assert reborn.interlock.engaged is True, str(config.data_dir)


# ---------------------------------------------------------------------------
# P5 — durable audit trail; crash-recoverable
# ---------------------------------------------------------------------------


def test_in_flight_journal_precedes_side_effect(unload_runtime, second_db_connection):
    """The IN_FLIGHT row must be COMMITTED before the effect starts.

    Observed through an independent SQLite connection at the moment the
    pack's effect hook fires — same-connection visibility would prove
    nothing about durability.
    """
    runtime = unload_runtime
    compiler, engine, registry = runtime["compiler"], runtime["engine"], runtime["registry"]
    world = compiler.compile(GOAL)
    action = next(a for a in world.frontier if a.owner_pack != "builtin")

    seen: list[str] = []

    def hook(idempotency_key: str) -> None:
        row = second_db_connection.execute(
            "SELECT exec_state FROM exec_journal WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        seen.append(row["exec_state"] if row else "MISSING")

    registry.get(action.owner_pack).effect_started_hook = hook

    plan = PlanIR(decision="EXECUTE", sequence=[action.action_id], why="durability barrier")
    report = engine.execute_plan(GOAL, world, plan)

    assert action.action_id in report.executed_action_ids
    assert seen == ["IN_FLIGHT"]


def test_journal_survives_restart_by_default(unload_runtime):
    runtime = unload_runtime
    config, engine, compiler, db = (
        runtime["config"],
        runtime["engine"],
        runtime["compiler"],
        runtime["db"],
    )
    world = compiler.compile(GOAL)
    action = next(a for a in world.frontier if a.owner_pack != "builtin")
    engine.execute_plan(GOAL, world, PlanIR(decision="EXECUTE", sequence=[action.action_id], why="x"))
    assert db._conn.execute("select count(*) from exec_journal").fetchone()[0] == 1

    # Simulated restart: the default startup path must NOT wipe the journal.
    from wallee_v6.main import reset_runtime_start_state

    reset_runtime_start_state(config)
    reconcile_runtime_start_state(config)

    survivor = sqlite3.connect(config.db_path)
    try:
        assert survivor.execute("select count(*) from exec_journal").fetchone()[0] == 1
    finally:
        survivor.close()


def test_restart_with_dispatched_run_escalates_to_human(runtime):
    """Crash recovery marks unknown-outcome work UNKNOWN and pages a human."""
    import time as _time

    from wallee_v6.ids import action_args_hash
    from wallee_v6.models import ActionRun, PlanRecord

    db, config = runtime["db"], runtime["config"]
    now_ms = int(_time.time() * 1000)
    db.store_plan(
        PlanRecord(
            plan_id="plan_crash",
            goal="g",
            world_packet={"goal": "g"},
            plan_ir={"decision": "EXECUTE", "sequence": ["A_PRN_PAUSE"], "why": "x"},
            created_ts_ms=now_ms,
        )
    )
    run = ActionRun(
        action_run_id="run_crash",
        plan_id="plan_crash",
        action_id="A_PRN_PAUSE",
        verb="PAUSE_PROCESS",
        owner_pack="sim_printer",
        args={},
        args_hash=action_args_hash("PAUSE_PROCESS", {}),
        idempotency_key="idem_crash",
        status=ActionRunStatus.PROPOSED,
        hazard_class=HazardClass.LOW,
        created_ts_ms=now_ms,
        updated_ts_ms=now_ms,
    )
    db.create_action_run(run)
    db.transition_action_run("run_crash", ActionRunStatus.AUTHORIZED)
    db.transition_action_run("run_crash", ActionRunStatus.DISPATCHED)

    swept = reconcile_runtime_start_state(config)

    assert swept["unknown"] == 1
    unknown = db.list_action_runs_by_status(ActionRunStatus.UNKNOWN)
    assert [r.action_run_id for r in unknown] == ["run_crash"]
    payloads = [json.loads(p.read_text()) for p in config.outbox_dir.glob("human_*.json")]
    assert any(p["severity"] == "critical" and p["require_ack"] for p in payloads)


@pytest.mark.xfail(
    strict=True,
    reason=f"Phase 2/4 (DB hygiene): exec_journal needs wall-clock timestamps — {PLAN} §7",
)
def test_journal_records_wall_clock(runtime):
    db = runtime["db"]
    columns = {row[1] for row in db._conn.execute("PRAGMA table_info(exec_journal)")}
    # Monotonic values are meaningless across restarts — exactly the case the
    # journal exists for.
    assert "started_ts_ms" in columns


def test_every_executed_action_has_full_lineage(unload_runtime):
    runtime = unload_runtime
    compiler, engine, db = runtime["compiler"], runtime["engine"], runtime["db"]
    world = compiler.compile(GOAL)
    action = next(a for a in world.frontier if a.owner_pack != "builtin")
    engine.execute_plan(GOAL, world, PlanIR(decision="EXECUTE", sequence=[action.action_id], why="x"))

    rows = db._conn.execute(
        """
        SELECT j.idempotency_key, r.status, r.plan_id, p.plan_id AS plan_exists
        FROM exec_journal j
        JOIN action_runs r ON r.action_run_id = j.action_run_id
        LEFT JOIN plans p ON p.plan_id = r.plan_id
        WHERE j.exec_state = 'SUCCESS'
        """
    ).fetchall()
    assert rows, "expected at least one successful journal row"
    for row in rows:
        assert row["status"] in ("DONE",), row["status"]
        assert row["plan_exists"] is not None


@pytest.mark.xfail(
    strict=True,
    reason=f"Phase 4 (DB hygiene): plans must be INSERT-only, never silently replaced — {PLAN} §7",
)
def test_plan_ids_are_never_silently_replaced(runtime):
    import time as _time

    from wallee_v6.models import PlanRecord

    db = runtime["db"]
    record = PlanRecord(
        plan_id="plan_dup",
        goal="g",
        world_packet={"goal": "g"},
        plan_ir={"decision": "NO_ACTION", "sequence": [], "why": "first"},
        created_ts_ms=int(_time.time() * 1000),
    )
    db.store_plan(record)
    with pytest.raises(sqlite3.IntegrityError):
        db.store_plan(record)


# ---------------------------------------------------------------------------
# P6 — human approval gates hazardous actions; a human can always ESTOP
# ---------------------------------------------------------------------------


def test_approval_required_action_never_executes_unapproved(runtime, monkeypatch):
    compiler, engine, db = runtime["compiler"], runtime["engine"], runtime["db"]
    world = compiler.compile(GOAL)
    action = make_hazardous(world.frontier[0], HazardClass.MEDIUM)
    pin_world(monkeypatch, engine, world)

    plan = PlanIR(decision="EXECUTE", sequence=[action.action_id], why="needs approval")
    report = engine.execute_plan(GOAL, world, plan)

    assert report.executed_action_ids == []
    assert report.blocked_action_run_id is not None
    assert db._conn.execute("select count(*) from exec_journal").fetchone()[0] == 0
    waiting = db.list_action_runs_by_status(ActionRunStatus.WAITING_APPROVAL)
    assert len(waiting) == 1


def test_auto_approve_never_applies_above_low_hazard(runtime, monkeypatch):
    compiler, engine, db = runtime["compiler"], runtime["engine"], runtime["db"]
    assert runtime["config"].auto_approve_low_hazard is True
    world = compiler.compile(GOAL)
    action = make_hazardous(world.frontier[0], HazardClass.HIGH)
    pin_world(monkeypatch, engine, world)

    report = engine.execute_plan(GOAL, world, PlanIR(decision="EXECUTE", sequence=[action.action_id], why="x"))

    assert report.executed_action_ids == []
    assert db._conn.execute("select count(*) from exec_journal").fetchone()[0] == 0


@pytest.mark.xfail(
    strict=True,
    reason=f"Phase 2 (approval resume): approved runs must execute exactly once — {PLAN} §5",
)
def test_approved_hazardous_action_executes_end_to_end(runtime, monkeypatch):
    """The full operator loop: block -> approve -> execute (exactly once).

    Today the engine parks a run as WAITING_APPROVAL and no code path ever
    revisits it; a recorded approval is consumed by nothing.
    """
    compiler, engine, db, human = (
        runtime["compiler"],
        runtime["engine"],
        runtime["db"],
        runtime["human"],
    )
    world = compiler.compile(GOAL)
    action = make_hazardous(world.frontier[0], HazardClass.MEDIUM)
    pin_world(monkeypatch, engine, world)
    plan = PlanIR(decision="EXECUTE", sequence=[action.action_id], why="operator loop")

    report = engine.execute_plan(GOAL, world, plan)
    blocked_id = report.blocked_action_run_id
    assert blocked_id is not None

    blocked = next(r for r in db.list_action_runs_by_status(ActionRunStatus.WAITING_APPROVAL))
    human.approve(action_run_id=blocked.action_run_id, args_hash=blocked.args_hash, approved_by="operator")

    # A subsequent engine pass must resume the approved run.
    engine.execute_plan(GOAL, world, plan)

    done = db.list_action_runs_by_status(ActionRunStatus.DONE)
    assert [r.action_run_id for r in done] == [blocked.action_run_id]
    assert db._conn.execute("select count(*) from exec_journal").fetchone()[0] == 1


@pytest.mark.xfail(
    strict=True,
    reason=f"Phase 2 (approval resume): unanswered approvals must expire to ABORTED with escalation — {PLAN} §5",
)
def test_unanswered_approval_expires_to_aborted_with_escalation(runtime, fake_mono, monkeypatch):
    compiler, engine, db = runtime["compiler"], runtime["engine"], runtime["db"]
    world = compiler.compile(GOAL)
    action = make_hazardous(world.frontier[0], HazardClass.MEDIUM)
    pin_world(monkeypatch, engine, world)

    engine.execute_plan(GOAL, world, PlanIR(decision="EXECUTE", sequence=[action.action_id], why="x"))
    assert db.list_action_runs_by_status(ActionRunStatus.WAITING_APPROVAL)

    # Far beyond any approval TTL, a maintenance pass must abort the run
    # rather than leaving it waiting forever.
    ttl = runtime["config"].approval_ttl_seconds
    fake_mono.advance(ttl * 10)
    engine.execute_plan(GOAL, world, PlanIR(decision="NO_ACTION", sequence=[], why="tick"))

    assert db.list_action_runs_by_status(ActionRunStatus.WAITING_APPROVAL) == []
    aborted = db.list_action_runs_by_status(ActionRunStatus.ABORTED)
    assert len(aborted) == 1


@pytest.mark.xfail(
    strict=True,
    reason=f"Phase 2 (interlock wiring + approval resume): ESTOP must dominate a pending approval — {PLAN} §5",
)
def test_human_estop_available_while_waiting_approval(runtime, monkeypatch):
    compiler, engine, db, human, safety = (
        runtime["compiler"],
        runtime["engine"],
        runtime["db"],
        runtime["human"],
        runtime["safety"],
    )
    world = compiler.compile(GOAL)
    action = make_hazardous(world.frontier[0], HazardClass.MEDIUM)
    pin_world(monkeypatch, engine, world)
    plan = PlanIR(decision="EXECUTE", sequence=[action.action_id], why="x")

    engine.execute_plan(GOAL, world, plan)
    blocked = next(r for r in db.list_action_runs_by_status(ActionRunStatus.WAITING_APPROVAL))

    # Human trips ESTOP while the approval is pending, then (mistakenly or
    # maliciously) the approval still arrives.
    safety.trip("human estop")
    human.approve(action_run_id=blocked.action_run_id, args_hash=blocked.args_hash, approved_by="operator")
    engine.execute_plan(GOAL, world, plan)

    # Nothing may execute — AND the parked run must be actively disposed of
    # (aborted under the interlock), not left waiting forever. Without the
    # second clause this test would pass vacuously today, since the missing
    # resume path means approvals are never consumed at all.
    assert db._conn.execute("select count(*) from exec_journal").fetchone()[0] == 0
    assert db.list_action_runs_by_status(ActionRunStatus.WAITING_APPROVAL) == []
    assert db.list_action_runs_by_status(ActionRunStatus.ABORTED) != []
