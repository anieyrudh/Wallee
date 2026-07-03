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

from wallee.main import build_runtime, reconcile_runtime_start_state
from wallee.models import ActionRunStatus, HazardClass, PlanIR
from wallee.safety import SafetyKernel

from .conftest import REPO_ROOT, make_hazardous, pin_world

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
    from wallee.models import LegalAction, WorldPacket

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


def test_interlock_trip_invokes_registered_stop_callback(tmp_path, monkeypatch):
    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WALLEE_ENABLED_PACKS", "sim_printer,sim_arm")
    monkeypatch.setenv("WALLEE_SIMULATION", "1")
    from wallee.config import Config

    config = Config.from_env(repo_root=REPO_ROOT)
    runtime_db, _, _, _, _, safety, engine, _ = build_runtime(config)
    try:
        assert safety._callbacks, (
            "the real composition must register at least one physical-stop "
            "callback; a trip with no callback stops nothing"
        )
        # Tripping the interlock must actually issue the stop. The sim pack's
        # file transport records it to a JSONL log — proof the callback ran
        # end to end, not merely that a callback was registered.
        safety.trip("contract-test")
        stop_log = config.data_dir / "safety" / "stop_commands.jsonl"
        assert stop_log.exists() and stop_log.read_text(encoding="utf-8").strip(), (
            "trip fired no stop command"
        )
    finally:
        engine.close()
        runtime_db.close()


def test_remote_estop_request_trips_interlock_stops_and_clears(tmp_path, monkeypatch):
    """P6: an operator's remote ESTOP request stops the machine and latches.

    The request is written by the operator CLI and honored by the control loop
    at the cycle boundary: it fires the physical stop, latches durably, blocks
    all further dispatch, and only an explicit attended clear releases it.
    """
    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WALLEE_ENABLED_PACKS", "sim_printer,sim_arm")
    monkeypatch.setenv("WALLEE_SIMULATION", "1")
    from wallee.config import Config
    from wallee.main import _apply_remote_estop_requests
    from wallee.safety import write_estop_signal

    config = Config.from_env(repo_root=REPO_ROOT)
    runtime_db, _, _, compiler, _, safety, engine, _ = build_runtime(config)
    try:
        write_estop_signal(config.estop_request_path, reason="lid open", requested_by="op")
        assert not safety.engaged()

        _apply_remote_estop_requests(config, safety, runtime_db)

        assert safety.engaged(), "the remote request must engage the interlock"
        assert config.estop_latch_path.exists(), "the trip must latch durably"
        assert not config.estop_request_path.exists(), "the request is consumed once, never replayed"
        stop_log = config.data_dir / "safety" / "stop_commands.jsonl"
        assert stop_log.exists() and stop_log.read_text(encoding="utf-8").strip(), (
            "a remote ESTOP must physically stop the machine, not merely latch"
        )

        world = compiler.compile(GOAL)
        action = world.frontier[0]
        report = engine.execute_plan(
            GOAL, world, PlanIR(decision="EXECUTE", sequence=[action.action_id], why="must be blocked")
        )
        assert report.executed_action_ids == []
        assert engine.runtime_db._conn.execute("select count(*) from exec_journal").fetchone()[0] == 0

        # Only a deliberate, attended clear releases the latch.
        write_estop_signal(config.estop_clear_request_path, reason="made safe", requested_by="op")
        _apply_remote_estop_requests(config, safety, runtime_db)
        assert not safety.engaged()
        assert not config.estop_latch_path.exists()
    finally:
        engine.close()
        runtime_db.close()


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


def test_heartbeat_beaten_on_failure_paths(runtime, fake_mono, monkeypatch):
    compiler, engine, registry, safety = (
        runtime["compiler"],
        runtime["engine"],
        runtime["registry"],
        runtime["safety"],
    )
    safety._mono = fake_mono
    safety.beat()  # rebase the heartbeat into fake-clock time
    world = compiler.compile(GOAL)
    action = world.frontier[0]

    pack = registry.get(action.owner_pack) if action.owner_pack != "builtin" else None
    if pack is not None:
        def exploding_execute(**kwargs):
            raise RuntimeError("simulated hardware fault")

        monkeypatch.setattr(pack, "execute", exploding_execute)
    else:
        monkeypatch.setattr(engine, "_execute_builtin", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    before = safety._last_heartbeat_mono
    fake_mono.advance(1.0)
    plan = PlanIR(decision="EXECUTE", sequence=[action.action_id], why="failing pass")
    engine.execute_plan(GOAL, world, plan)

    assert safety._last_heartbeat_mono > before, (
        "a failed engine pass must still prove liveness; otherwise the "
        "watchdog cannot distinguish a wedged loop from a failing one"
    )


def test_shipped_safety_service_is_not_a_placeholder():
    unit = (REPO_ROOT / "deploy" / "systemd" / "wallee-safety.service").read_text(encoding="utf-8")
    exec_lines = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
    assert exec_lines, "unit must define ExecStart"
    for line in exec_lines:
        assert "sleep" not in line and "placeholder" not in line.lower(), line
        assert "wallee.safety_watchdog" in line, "unit must run the real watchdog"
    # The main runtime must require the watchdog, not run it optionally.
    main_unit = (REPO_ROOT / "deploy" / "systemd" / "wallee-main.service").read_text(encoding="utf-8")
    assert "Requires=wallee-safety.service" in main_unit
    # And the main unit must actually be bootable (required args present).
    main_exec = next(line for line in main_unit.splitlines() if line.startswith("ExecStart="))
    assert "--goal" in main_exec and ("--forever" in main_exec or "--once" in main_exec or "--cycles" in main_exec)


def test_watchdog_stops_sim_machine_when_runtime_sigkilled(tmp_path):
    """The end-to-end independence proof: SIGKILL the runtime mid-'print',
    and the out-of-process watchdog stops the (sim) machine and latches.

    Uses the file stop transport, so no hardware is needed.
    """
    import os
    import signal
    import subprocess
    import sys
    import time as _time

    safety_dir = tmp_path / "safety"
    safety_dir.mkdir(parents=True)
    # A pack stop profile (file transport) and an active-job heartbeat, as the
    # runtime would have written before dying.
    (safety_dir / "profile.json").write_text(
        json.dumps([{"transport": "file", "primary_request": {"method": "POST", "path": "/sim/stop"}}]),
        encoding="utf-8",
    )
    beacon = safety_dir / "heartbeat.json"
    beacon.write_text(json.dumps({"wall_ts": _time.time(), "job_active": True}), encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "WALLEE_DATA_DIR": str(tmp_path),
            "WALLEE_SAFETY_HEARTBEAT_TIMEOUT_S": "1",
            "WALLEE_SAFETY_CHECK_INTERVAL_S": "0.2",
            "WALLEE_SAFETY_BOOT_GRACE_S": "0",
        }
    )
    watchdog = subprocess.Popen(
        [sys.executable, "-m", "wallee.safety_watchdog"],
        cwd=REPO_ROOT,
        env=env,
    )
    # A stand-in "runtime" that keeps the heartbeat fresh, then is SIGKILLed.
    beater = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import json,os,time,sys\n"
                "p=sys.argv[1]\n"
                "while True:\n"
                " open(p,'w').write(json.dumps({'wall_ts':time.time(),'job_active':True}))\n"
                " os.utime(p,None)\n"
                " time.sleep(0.1)\n"
            ),
            str(beacon),
        ],
    )
    try:
        _time.sleep(1.0)  # let both settle; heartbeat stays fresh
        os.kill(beater.pid, signal.SIGKILL)  # the runtime dies mid-job
        beater.wait(timeout=5)

        stop_log = safety_dir / "stop_commands.jsonl"
        latch = safety_dir / "estop.latch.json"
        deadline = _time.time() + 10
        while _time.time() < deadline:
            if stop_log.exists() and stop_log.read_text(encoding="utf-8").strip() and latch.exists():
                break
            _time.sleep(0.2)

        assert latch.exists(), "watchdog did not latch after the runtime died"
        assert stop_log.exists() and stop_log.read_text(encoding="utf-8").strip(), "watchdog issued no stop"
        outbox = [p for p in (tmp_path / "outbox").glob("watchdog_*.json")]
        assert outbox, "watchdog did not escalate to the operator"
    finally:
        for proc in (watchdog, beater):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


def test_interlock_trip_survives_restart(runtime):
    config, safety = runtime["config"], runtime["safety"]
    safety.trip("contract-test")

    # A fresh kernel pointed at the same durable latch must rediscover the
    # trip — the latch lives on disk, not only in process memory.
    reborn = SafetyKernel(
        heartbeat_timeout_s=safety.heartbeat_timeout_s,
        latch_path=config.estop_latch_path,
    )
    assert reborn.engaged() is True
    assert reborn.interlock.reason == "contract-test"

    # And it clears only by explicit action, removing the durable latch.
    reborn.clear()
    assert not config.estop_latch_path.exists()
    third = SafetyKernel(
        heartbeat_timeout_s=safety.heartbeat_timeout_s,
        latch_path=config.estop_latch_path,
    )
    assert third.engaged() is False


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
    from wallee.main import reset_runtime_start_state

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

    from wallee.ids import action_args_hash
    from wallee.models import ActionRun, PlanRecord

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


def test_journal_records_wall_clock(unload_runtime):
    runtime = unload_runtime
    compiler, engine, db = runtime["compiler"], runtime["engine"], runtime["db"]
    columns = {row[1] for row in db._conn.execute("PRAGMA table_info(exec_journal)")}
    # Monotonic values are meaningless across restarts — exactly the case the
    # journal exists for.
    assert {"started_ts_ms", "ended_ts_ms"} <= columns

    world = compiler.compile(GOAL)
    action = next(a for a in world.frontier if a.owner_pack != "builtin")
    engine.execute_plan(GOAL, world, PlanIR(decision="EXECUTE", sequence=[action.action_id], why="x"))
    row = db._conn.execute(
        "SELECT started_ts_ms, ended_ts_ms FROM exec_journal WHERE exec_state = 'SUCCESS'"
    ).fetchone()
    assert row is not None and row["started_ts_ms"] and row["ended_ts_ms"]


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


def test_plan_ids_are_never_silently_replaced(runtime):
    import time as _time

    from wallee.models import PlanRecord

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


def test_approved_hazardous_action_executes_end_to_end(unload_runtime, monkeypatch):
    """The full operator loop: block -> approve -> execute (exactly once).

    Uses a pack-owned action so the resume drives the real dispatch/journal
    path. Before Phase 2 the engine parked a run as WAITING_APPROVAL and no
    code path ever revisited it; a recorded approval was consumed by nothing.
    """
    runtime = unload_runtime
    compiler, engine, db, human, registry = (
        runtime["compiler"],
        runtime["engine"],
        runtime["db"],
        runtime["human"],
        runtime["registry"],
    )
    # Mark the pack-owned unload action approval-gated at the pack boundary so
    # every fresh compile re-marks it AND post-execution verification still
    # sees real post-state (pinning the world would break verification).
    arm = registry.get("sim_arm")
    original_candidates = arm.candidate_actions

    def hazardous_candidates(w):
        actions = original_candidates(w)
        for a in actions:
            if a.action_id == "A_ARM_UNLOAD":
                make_hazardous(a, HazardClass.MEDIUM)
        return actions

    monkeypatch.setattr(arm, "candidate_actions", hazardous_candidates)

    world = compiler.compile(GOAL)
    pack_action = next(a for a in world.frontier if a.action_id == "A_ARM_UNLOAD")
    plan = PlanIR(decision="EXECUTE", sequence=[pack_action.action_id], why="operator loop")

    report = engine.execute_plan(GOAL, world, plan)
    assert report.blocked_action_run_id is not None

    blocked = next(r for r in db.list_action_runs_by_status(ActionRunStatus.WAITING_APPROVAL))
    human.approve(action_run_id=blocked.action_run_id, args_hash=blocked.args_hash, approved_by="operator")

    # A subsequent engine pass resumes the approved run through the shared
    # gated dispatch path — exactly once.
    engine.execute_plan(GOAL, world, PlanIR(decision="NO_ACTION", sequence=[], why="tick"))

    done = db.list_action_runs_by_status(ActionRunStatus.DONE)
    assert [r.action_run_id for r in done] == [blocked.action_run_id]
    assert db._conn.execute("select count(*) from exec_journal").fetchone()[0] == 1


def test_unanswered_approval_expires_to_aborted_with_escalation(runtime, monkeypatch):
    compiler, engine, db, config = (
        runtime["compiler"],
        runtime["engine"],
        runtime["db"],
        runtime["config"],
    )
    world = compiler.compile(GOAL)
    action = make_hazardous(world.frontier[0], HazardClass.MEDIUM)
    pin_world(monkeypatch, engine, world)

    engine.execute_plan(GOAL, world, PlanIR(decision="EXECUTE", sequence=[action.action_id], why="x"))
    parked = next(iter(db.list_action_runs_by_status(ActionRunStatus.WAITING_APPROVAL)))
    outbox_before = len(list(config.outbox_dir.glob("human_*.json")))

    # Far beyond the approval wait window, a maintenance pass must abort the
    # run rather than leave it waiting forever. Drive the engine's wall clock.
    future = parked.updated_ts_ms + (config.approval_wait_window_seconds + 1) * 1000
    monkeypatch.setattr(engine, "_now_ms", lambda: future)
    engine.execute_plan(GOAL, world, PlanIR(decision="NO_ACTION", sequence=[], why="tick"))

    assert db.list_action_runs_by_status(ActionRunStatus.WAITING_APPROVAL) == []
    aborted = db.list_action_runs_by_status(ActionRunStatus.ABORTED)
    assert [r.action_run_id for r in aborted] == [parked.action_run_id]
    # The operator is asked to re-approve rather than silently dropped.
    assert len(list(config.outbox_dir.glob("human_*.json"))) > outbox_before


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
