import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wallee_v6.config import Config
from wallee_v6.engine import Engine
from wallee_v6.human import HumanGateway
from wallee_v6.ids import action_args_hash
from wallee_v6.main import (
    _RuntimeArchive,
    _artifact_planning_current,
    _world_artifact_payload,
    _write_cycle_artifact,
    reconcile_runtime_start_state,
    reset_runtime_start_state,
)
from wallee_v6.models import (
    ActionRun,
    ActionRunStatus,
    DeviceSummary,
    ExecutionResult,
    HazardClass,
    LegalAction,
    PlanIR,
    PlanRecord,
    WorldPacket,
)
from wallee_v6.predicates import PredicateEvaluator, atom
from wallee_v6.runtime_db import RuntimeDB
from wallee_v6.safety import SafetyKernel
from wallee_v6.whiteboard import InMemoryWhiteboard


def test_validate_plan_rejects_unknown_action(runtime):
    compiler = runtime["compiler"]
    engine = runtime["engine"]
    world = compiler.compile("Unload cooled part from printer_1 into tray_A")

    plan = PlanIR(decision="EXECUTE", sequence=["DOES_NOT_EXIST"], why="bad")
    try:
        engine.validate_plan(world, plan)
    except ValueError as exc:
        assert "unknown frontier action" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_validate_plan_materializes_compact_tuning_choice(runtime):
    engine = runtime["engine"]
    world = WorldPacket(
        goal="Reduce flow a little while keeping the current print running.",
        device_summaries=[],
        facts={},
        resources=[],
        blockers=[],
        deltas=[],
        frontier=[
            LegalAction(
                action_id="A_PRUSA_TRIM_FLOW_DOWN_SMALL",
                verb="TUNE_FLOW",
                description="Reduce flow a little while keeping the current print running.",
                owner_pack="prusa_core_one_plus",
                execute_ref="trim_flow_down_small",
                hazard_class=HazardClass.LOW,
            )
        ],
        last_result={},
        pending_human=[],
    )

    actions = engine.validate_plan(
        world,
        PlanIR(
            decision="EXECUTE",
            sequence=[],
            tuning_choice={"family": "flow", "direction": "down", "magnitude": "small"},
            why="Fresh evidence supports a small flow reduction.",
        ),
    )

    assert [action.action_id for action in actions] == ["A_PRUSA_TRIM_FLOW_DOWN_SMALL"]


def test_engine_materialize_malformed_mixed_execute_prefers_legal_tuning_choice(runtime):
    engine = runtime["engine"]
    world = WorldPacket(
        goal="Reduce flow a little while keeping the current print running.",
        device_summaries=[],
        facts={"printer_1.lifecycle": "PRINTING", "printer_1.printing_phase": "active_printing", "printer_1.health": "OK"},
        resources=[],
        blockers=[],
        deltas=[],
        frontier=[
            LegalAction(
                action_id="A_PRUSA_PAUSE",
                verb="PAUSE_PROCESS",
                description="Pause the active Prusa print.",
                owner_pack="prusa_core_one_plus",
                execute_ref="pause_process",
                hazard_class=HazardClass.LOW,
            ),
            LegalAction(
                action_id="A_PRUSA_TRIM_FLOW_DOWN_SMALL",
                verb="TUNE_FLOW",
                description="Reduce flow a little while keeping the current print running.",
                owner_pack="prusa_core_one_plus",
                execute_ref="trim_flow_down_small",
                hazard_class=HazardClass.LOW,
            ),
        ],
        last_result={},
        pending_human=[],
    )

    normalized = engine.materialize_plan(
        world,
        PlanIR(
            decision="EXECUTE",
            sequence=["A_PRUSA_PAUSE"],
            tuning_choice={"family": "flow", "direction": "down", "magnitude": "small"},
            why="Malformed mixed planner output.",
        ),
    )

    assert normalized.decision == "EXECUTE"
    assert normalized.sequence == ["A_PRUSA_TRIM_FLOW_DOWN_SMALL"]
    assert normalized.tuning_choice is None


def test_engine_materialize_malformed_mixed_execute_degrades_to_no_action_when_unresolvable(runtime):
    engine = runtime["engine"]
    world = WorldPacket(
        goal="Reduce flow a little while keeping the current print running.",
        device_summaries=[],
        facts={},
        resources=[],
        blockers=[],
        deltas=[],
        frontier=[
            LegalAction(
                action_id="A_PRUSA_PAUSE",
                verb="PAUSE_PROCESS",
                description="Pause the active Prusa print.",
                owner_pack="prusa_core_one_plus",
                execute_ref="pause_process",
                hazard_class=HazardClass.LOW,
            ),
        ],
        last_result={},
        pending_human=[],
    )

    normalized = engine.materialize_plan(
        world,
        PlanIR(
            decision="EXECUTE",
            sequence=["A_PRUSA_PAUSE"],
            tuning_choice={"family": "flow", "direction": "down", "magnitude": "small"},
            why="Malformed mixed planner output.",
        ),
    )

    assert normalized.decision == "NO_ACTION"
    assert normalized.sequence == []
    assert normalized.tuning_choice is None


def test_approval_binding_requires_matching_hash_and_expiry(runtime):
    db = runtime["db"]
    human = runtime["human"]
    engine = runtime["engine"]
    compiler = runtime["compiler"]
    registry = runtime["registry"]
    whiteboard = runtime["whiteboard"]

    printer = registry.get("sim_printer")
    printer.state["printer_1.part_temp_c"] = 30.0
    registry.publish_all_raw_state(whiteboard)
    world = compiler.compile("Unload cooled part from printer_1 into tray_A")
    action = next(a for a in world.frontier if a.action_id == "A_ARM_UNLOAD")
    action.approval_required = True
    action.hazard_class = HazardClass.MEDIUM

    db.store_plan(PlanRecord(plan_id="plan_test", goal="g", world_packet=world.prompt_view(), plan_ir={"decision": "EXECUTE"}, created_ts_ms=int(time.time() * 1000)))
    run = engine._create_action_run("plan_test", action)

    wrong_hash = action_args_hash(action.verb, {"source": "wrong", "destination": "tray_A"})
    human.approve(action_run_id=run.action_run_id, args_hash=wrong_hash, approved_by="operator", ttl_seconds=60)
    assert db.has_valid_approval(run.action_run_id, run.args_hash) is False

    human.approve(action_run_id=run.action_run_id, args_hash=run.args_hash, approved_by="operator", ttl_seconds=-1)
    assert db.has_valid_approval(run.action_run_id, run.args_hash) is False

    human.approve(action_run_id=run.action_run_id, args_hash=run.args_hash, approved_by="operator", ttl_seconds=60)
    assert db.has_valid_approval(run.action_run_id, run.args_hash) is True


class _RefreshingPack:
    def __init__(self) -> None:
        self.hardware_speed = 100.0

    def execute(self, **kwargs) -> ExecutionResult:
        self.hardware_speed = 85.0
        return ExecutionResult(status="success", result={"speed_pct": 85.0})


class _RefreshingRegistry:
    def __init__(self, pack: _RefreshingPack) -> None:
        self.pack = pack
        self.publish_modes: list[str] = []

    def publish_all_raw_state(self, whiteboard: InMemoryWhiteboard, *, mode: str = "full") -> None:
        self.publish_modes.append(mode)
        whiteboard.publish("printer_1.speed_pct", self.pack.hardware_speed)

    def get(self, pack_id: str) -> _RefreshingPack:
        assert pack_id == "test_pack"
        return self.pack


class _RefreshingCompiler:
    def __init__(self, whiteboard: InMemoryWhiteboard, action: LegalAction) -> None:
        self.whiteboard = whiteboard
        self.action = action

    def compile(self, goal: str, pending_human=None) -> WorldPacket:
        speed = self.whiteboard.snapshot().values.get("printer_1.speed_pct")
        return WorldPacket(
            goal=goal,
            device_summaries=[
                DeviceSummary(
                    device_id="printer_1",
                    display_name="Test Printer",
                    category="additive",
                    mode="PRINTING",
                    health="OK",
                    summary=f"speed={speed}",
                )
            ],
            facts={
                "printer_1.speed_pct": speed,
                "printer_1.current_file": "Stringing_Test_PLA_COREONE.bgcode",
                "printer_1.job_scope_token": "job:refresh:Stringing_Test_PLA_COREONE.bgcode",
            },
            resources=[],
            blockers=[],
            deltas=[],
            frontier=[self.action],
            last_result={},
            pending_human=pending_human or [],
        )


def test_execute_plan_refreshes_raw_state_before_verification(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WALLEE_ENABLED_PACKS", "sim_printer")
    monkeypatch.setenv("WALLEE_SIMULATION", "1")
    config = Config.from_env(repo_root=repo_root)

    runtime_db = RuntimeDB(config.db_path)
    whiteboard = InMemoryWhiteboard()
    pack = _RefreshingPack()
    registry = _RefreshingRegistry(pack)
    registry.publish_all_raw_state(whiteboard)

    action = LegalAction(
        action_id="A_TEST_TRIM",
        verb="TUNE_SPEED",
        description="Reduce print speed a little",
        owner_pack="test_pack",
        execute_ref="trim_speed_down_small",
        args={},
        verify=atom("printer_1.speed_pct", "==", 85.0),
    )
    compiler = _RefreshingCompiler(whiteboard, action)
    human = HumanGateway(config=config, runtime_db=runtime_db)
    engine = Engine(
        config=config,
        runtime_db=runtime_db,
        whiteboard=whiteboard,
        registry=registry,  # type: ignore[arg-type]
        world_compiler=compiler,  # type: ignore[arg-type]
        human_gateway=human,
        safety=SafetyKernel(heartbeat_timeout_s=config.heartbeat_timeout_seconds),
        predicate_evaluator=PredicateEvaluator(),
    )

    try:
        world = compiler.compile("Reduce print speed a little")
        plan = PlanIR(decision="EXECUTE", sequence=["A_TEST_TRIM"], why="test refresh")
        report = engine.execute_plan("Reduce print speed a little", world, plan)

        assert report.failed_action_run_id is None
        assert report.replan_required is False
        assert report.post_execution_world is not None
        assert report.post_execution_world.facts["printer_1.speed_pct"] == 85.0
        assert registry.publish_modes == ["full", "verify"]
        done_runs = runtime_db.list_action_runs_by_status(ActionRunStatus.DONE)
        assert len(done_runs) == 1
        assert done_runs[0].action_id == "A_TEST_TRIM"
    finally:
        engine.close()
        runtime_db.close()


class _OperatorOnlyCompiler:
    def __init__(self, whiteboard: InMemoryWhiteboard) -> None:
        self.whiteboard = whiteboard

    def compile(self, goal: str, pending_human=None) -> WorldPacket:
        speed = self.whiteboard.snapshot().values.get("printer_1.speed_pct")
        return WorldPacket(
            goal=goal,
            device_summaries=[
                DeviceSummary(
                    device_id="printer_1",
                    display_name="Test Printer",
                    category="additive",
                    mode="PRINTING",
                    health="OK",
                    summary=f"speed={speed}",
                )
            ],
            facts={
                "printer_1.speed_pct": speed,
                "printer_1.current_file": "Stringing_Test_PLA_COREONE.bgcode",
                "printer_1.job_scope_token": "job:operator:Stringing_Test_PLA_COREONE.bgcode",
            },
            resources=[],
            blockers=[],
            deltas=[],
            frontier=[],
            last_result={},
            pending_human=pending_human or [],
        )


def test_execute_operator_action_runs_bounded_action_without_frontier_membership(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WALLEE_ENABLED_PACKS", "sim_printer")
    monkeypatch.setenv("WALLEE_SIMULATION", "1")
    config = Config.from_env(repo_root=repo_root)

    runtime_db = RuntimeDB(config.db_path)
    whiteboard = InMemoryWhiteboard()
    pack = _RefreshingPack()
    registry = _RefreshingRegistry(pack)
    registry.publish_all_raw_state(whiteboard)
    compiler = _OperatorOnlyCompiler(whiteboard)
    human = HumanGateway(config=config, runtime_db=runtime_db)
    engine = Engine(
        config=config,
        runtime_db=runtime_db,
        whiteboard=whiteboard,
        registry=registry,  # type: ignore[arg-type]
        world_compiler=compiler,  # type: ignore[arg-type]
        human_gateway=human,
        safety=SafetyKernel(heartbeat_timeout_s=config.heartbeat_timeout_seconds),
        predicate_evaluator=PredicateEvaluator(),
    )
    action = LegalAction(
        action_id="A_OPERATOR_ONLY",
        verb="TUNE_SPEED",
        description="Operator-only restore",
        owner_pack="test_pack",
        execute_ref="restore_speed_default",
        args={},
        verify=atom("printer_1.speed_pct", "==", 85.0),
    )

    try:
        world = compiler.compile("Operator proof")
        report = engine.execute_operator_action("Operator proof", world, action)

        assert report.failed_action_run_id is None
        assert report.replan_required is False
        assert report.post_execution_world is not None
        assert report.post_execution_world.facts["printer_1.speed_pct"] == 85.0
        done_runs = runtime_db.list_action_runs_by_status(ActionRunStatus.DONE)
        assert len(done_runs) == 1
        assert done_runs[0].action_id == "A_OPERATOR_ONLY"
        assert done_runs[0].run_scope == "state:unknown:job:operator:Stringing_Test_PLA_COREONE.bgcode"
    finally:
        engine.close()
        runtime_db.close()


class _StaticCompiler:
    def __init__(self, worlds: list[WorldPacket]) -> None:
        self._worlds = list(worlds)
        self._index = 0

    def compile(self, goal: str, pending_human=None) -> WorldPacket:
        if self._index >= len(self._worlds):
            return self._worlds[-1]
        world = self._worlds[self._index]
        self._index += 1
        return world


def test_wait_until_can_timeout_nonfatally(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WALLEE_ENABLED_PACKS", "sim_printer")
    monkeypatch.setenv("WALLEE_SIMULATION", "1")
    config = Config.from_env(repo_root=repo_root)

    runtime_db = RuntimeDB(config.db_path)
    whiteboard = InMemoryWhiteboard()
    whiteboard.publish("printer_1.safe_to_unload", False)
    compiler = _StaticCompiler(
        [
            WorldPacket(
                goal="Wait for cooldown",
                device_summaries=[],
                facts={"printer_1.safe_to_unload": False},
                resources=[],
                blockers=[],
                deltas=[],
                frontier=[],
                last_result={},
                pending_human=[],
            )
        ]
    )
    human = HumanGateway(config=config, runtime_db=runtime_db)
    engine = Engine(
        config=config,
        runtime_db=runtime_db,
        whiteboard=whiteboard,
        registry=SimpleNamespace(),  # type: ignore[arg-type]
        world_compiler=compiler,  # type: ignore[arg-type]
        human_gateway=human,
        safety=SafetyKernel(heartbeat_timeout_s=config.heartbeat_timeout_seconds),
        predicate_evaluator=PredicateEvaluator(),
    )
    action = LegalAction(
        action_id="A_TEST_WAIT",
        verb="WAIT_UNTIL",
        description="Wait for cooldown",
        owner_pack="builtin",
        execute_ref="builtin.wait_until",
        args={"timeout_s": 0, "nonfatal_timeout": True},
        verify=atom("printer_1.safe_to_unload", "==", True),
    )

    try:
        result = engine._execute_builtin(action, "Wait for cooldown")
    finally:
        engine.close()
        runtime_db.close()

    assert result.status == "success"
    assert result.result == {
        "waited": True,
        "predicate_satisfied": False,
        "wait_pending": True,
        "wait_slice_s": 0.0,
        "wait_timed_out": True,
        "nonfatal_timeout": True,
    }


def test_wait_until_uses_short_slice_per_cycle(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WALLEE_ENABLED_PACKS", "sim_printer")
    monkeypatch.setenv("WALLEE_SIMULATION", "1")
    config = Config.from_env(repo_root=repo_root)

    runtime_db = RuntimeDB(config.db_path)
    whiteboard = InMemoryWhiteboard()
    compiler = _StaticCompiler(
        [
            WorldPacket(
                goal="Wait for cooldown",
                device_summaries=[],
                facts={"printer_1.safe_to_unload": False},
                resources=[],
                blockers=[],
                deltas=[],
                frontier=[],
                last_result={},
                pending_human=[],
            )
        ]
    )
    human = HumanGateway(config=config, runtime_db=runtime_db)
    engine = Engine(
        config=config,
        runtime_db=runtime_db,
        whiteboard=whiteboard,
        registry=SimpleNamespace(),  # type: ignore[arg-type]
        world_compiler=compiler,  # type: ignore[arg-type]
        human_gateway=human,
        safety=SafetyKernel(heartbeat_timeout_s=config.heartbeat_timeout_seconds),
        predicate_evaluator=PredicateEvaluator(),
    )
    action = LegalAction(
        action_id="A_TEST_WAIT_SLICE",
        verb="WAIT_UNTIL",
        description="Wait for cooldown in short slices",
        owner_pack="builtin",
        execute_ref="builtin.wait_until",
        args={"timeout_s": 30, "slice_s": 2.0, "nonfatal_timeout": True},
        verify=atom("printer_1.safe_to_unload", "==", True),
    )

    monotonic_values = iter([100.0, 100.0, 100.01, 102.01])
    sleep_calls: list[float] = []
    try:
        with patch("wallee_v6.engine.time.monotonic", side_effect=lambda: next(monotonic_values)):
            with patch("wallee_v6.engine.time.sleep", side_effect=lambda seconds: sleep_calls.append(seconds)):
                result = engine._execute_builtin(action, "Wait for cooldown")
    finally:
        engine.close()
        runtime_db.close()

    assert result.status == "success"
    assert result.result["wait_pending"] is True
    assert result.result["wait_slice_s"] == 2.0
    assert sleep_calls == [0.05]


def test_nonfatal_wait_timeout_does_not_become_verification_mismatch(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WALLEE_ENABLED_PACKS", "sim_printer")
    monkeypatch.setenv("WALLEE_SIMULATION", "1")
    config = Config.from_env(repo_root=repo_root)

    runtime_db = RuntimeDB(config.db_path)
    whiteboard = InMemoryWhiteboard()
    compiler = _StaticCompiler(
        [
            WorldPacket(
                goal="Wait for cooldown",
                device_summaries=[],
                facts={"printer_1.safe_to_unload": False},
                resources=[],
                blockers=[],
                deltas=[],
                frontier=[],
                last_result={},
                pending_human=[],
            )
        ]
    )
    human = HumanGateway(config=config, runtime_db=runtime_db)
    registry = SimpleNamespace(publish_all_raw_state=lambda whiteboard, mode="full": None)
    engine = Engine(
        config=config,
        runtime_db=runtime_db,
        whiteboard=whiteboard,
        registry=registry,  # type: ignore[arg-type]
        world_compiler=compiler,  # type: ignore[arg-type]
        human_gateway=human,
        safety=SafetyKernel(heartbeat_timeout_s=config.heartbeat_timeout_seconds),
        predicate_evaluator=PredicateEvaluator(),
    )
    action = LegalAction(
        action_id="A_TEST_WAIT_PENDING",
        verb="WAIT_UNTIL",
        description="Wait for cooldown",
        owner_pack="builtin",
        execute_ref="builtin.wait_until",
        args={"timeout_s": 30, "slice_s": 2.0, "nonfatal_timeout": True},
        verify=atom("printer_1.safe_to_unload", "==", True),
    )

    try:
        runtime_db.store_plan(
            PlanRecord(
                plan_id="plan_wait",
                goal="Wait for cooldown",
                world_packet={"goal": "Wait for cooldown"},
                plan_ir={"decision": "EXECUTE", "sequence": ["A_TEST_WAIT_PENDING"], "why": "test"},
                created_ts_ms=int(time.time() * 1000),
            )
        )
        run = engine._create_action_run("plan_wait", action)
        runtime_db.transition_action_run(run.action_run_id, ActionRunStatus.AUTHORIZED)
        runtime_db.transition_action_run(run.action_run_id, ActionRunStatus.DISPATCHED)
        report = engine._finalize_action(
            run.action_run_id,
            action,
            "Wait for cooldown",
            ExecutionResult(
                status="success",
                result={
                    "waited": True,
                    "predicate_satisfied": False,
                    "wait_pending": True,
                    "wait_slice_s": 2.0,
                    "wait_timed_out": True,
                    "nonfatal_timeout": True,
                },
            ),
        )
        row = runtime_db._conn.execute(
            "SELECT status, result_json, error_json FROM action_runs WHERE action_run_id = ?",
            (run.action_run_id,),
        ).fetchone()
    finally:
        engine.close()
        runtime_db.close()

    assert report.replan_required is False
    assert report.failed_action_run_id is None
    assert "wait still pending for A_TEST_WAIT_PENDING" in report.notes
    assert row["status"] == ActionRunStatus.DONE.value
    assert row["error_json"] is None


def test_engine_control_lease_allows_only_one_live_writer(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WALLEE_ENABLED_PACKS", "sim_printer")
    monkeypatch.setenv("WALLEE_SIMULATION", "1")
    config = Config.from_env(repo_root=repo_root)

    def _make_engine():
        runtime_db = RuntimeDB(config.db_path)
        whiteboard = InMemoryWhiteboard()
        pack = _RefreshingPack()
        registry = _RefreshingRegistry(pack)
        registry.publish_all_raw_state(whiteboard)
        compiler = _OperatorOnlyCompiler(whiteboard)
        human = HumanGateway(config=config, runtime_db=runtime_db)
        engine = Engine(
            config=config,
            runtime_db=runtime_db,
            whiteboard=whiteboard,
            registry=registry,  # type: ignore[arg-type]
            world_compiler=compiler,  # type: ignore[arg-type]
            human_gateway=human,
            safety=SafetyKernel(heartbeat_timeout_s=config.heartbeat_timeout_seconds),
            predicate_evaluator=PredicateEvaluator(),
        )
        action = LegalAction(
            action_id="A_OPERATOR_ONLY",
            verb="TUNE_SPEED",
            description="Operator-only restore",
            owner_pack="test_pack",
            execute_ref="restore_speed_default",
            args={},
            verify=atom("printer_1.speed_pct", "==", 85.0),
        )
        return runtime_db, compiler, engine, action

    db1, compiler1, engine1, action1 = _make_engine()
    db2, compiler2, engine2, action2 = _make_engine()
    try:
        world1 = compiler1.compile("Operator proof")
        report1 = engine1.execute_operator_action("Operator proof", world1, action1)
        assert report1.failed_action_run_id is None
        assert report1.replan_required is False

        world2 = compiler2.compile("Operator proof")
        report2 = engine2.execute_operator_action("Operator proof", world2, action2)
        assert report2.replan_required is True
        assert "control lease unavailable" in report2.notes
    finally:
        engine1.close()
        engine2.close()
        db1.close()
        db2.close()


def test_reset_runtime_start_state_clears_db_cycles_and_control_lock(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WALLEE_FLUSH_STATE_ON_START", "1")
    config = Config.from_env(repo_root=repo_root)

    runtime_db = RuntimeDB(config.db_path)
    runtime_db.store_plan(
        PlanRecord(
            plan_id="plan_test",
            goal="g",
            world_packet={"goal": "g"},
            plan_ir={"decision": "NO_ACTION", "sequence": [], "why": "test"},
            created_ts_ms=int(time.time() * 1000),
        )
    )
    runtime_db.record_event("test", "INFO", "event")
    runtime_db.close()

    cycles_dir = config.data_dir / "runtime_cycles"
    cycles_dir.mkdir(parents=True, exist_ok=True)
    (cycles_dir / "cycle.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
    config.control_lock_path.write_text("held", encoding="utf-8")

    reset_runtime_start_state(config)

    runtime_db = RuntimeDB(config.db_path)
    try:
        assert runtime_db.latest_completed_action() == {}
        assert runtime_db._conn.execute("select count(*) from plans").fetchone()[0] == 0
        assert runtime_db._conn.execute("select count(*) from events").fetchone()[0] == 0
    finally:
        runtime_db.close()
    assert cycles_dir.exists()
    assert (cycles_dir / "cycle.json").exists()
    assert not config.control_lock_path.exists()


def test_execute_plan_persists_live_state_separately_from_planner_world(runtime):
    compiler = runtime["compiler"]
    engine = runtime["engine"]
    db = runtime["db"]

    world = compiler.compile("Unload cooled part from printer_1 into tray_A")
    plan = PlanIR(decision="NO_ACTION", sequence=[], why="test separation")
    report = engine.execute_plan("Unload cooled part from printer_1 into tray_A", world, plan)

    row = db._conn.execute(
        "SELECT world_packet_json, world_compilation_json FROM plans WHERE plan_id = ?",
        (report.plan_id,),
    ).fetchone()

    planner_world = json.loads(row["world_packet_json"])
    compilation = json.loads(row["world_compilation_json"])

    assert planner_world["facts"] == world.prompt_view()["facts"]
    assert compilation["whiteboard_sequence"] == world.compilation.whiteboard_sequence
    assert compilation["raw_snapshot"]["printer_1.mode"] == runtime["whiteboard"].snapshot().values["printer_1.mode"]
    assert compilation["raw_snapshot"] != planner_world["facts"]


def test_cycle_artifact_keeps_live_snapshot_separate_from_planner_world(runtime):
    compiler = runtime["compiler"]
    engine = runtime["engine"]
    config = runtime["config"]

    world = compiler.compile("Unload cooled part from printer_1 into tray_A")
    plan = PlanIR(decision="NO_ACTION", sequence=[], why="test artifact separation")
    report = engine.execute_plan("Unload cooled part from printer_1 into tray_A", world, plan)
    archive = _RuntimeArchive(config, repo_root=Path(__file__).resolve().parents[1])
    artifact = _write_cycle_artifact(
        archive,
        cycle_index=0,
        goal=world.goal,
        world=world,
        plan=plan,
        report=report,
        planner=SimpleNamespace(last_provider_metadata=None),
    )

    payload = json.loads(artifact.read_text(encoding="utf-8"))
    prompt_view = world.prompt_view()
    expected_explicit_frontier_ids = world.prompt_view()["decision_signals"]["allowed_frontier_ids"]
    expected_executable_frontier_ids = [action.action_id for action in world.frontier]
    expected_tuning_frontier_ids = [action.action_id for action in world.frontier if action.verb.startswith("TUNE_")]
    expected_capability_actions = {
        "speed": None,
        "flow": None,
        "nozzle": None,
        "bed": None,
        "pressure_advance": None,
        "accel": None,
    }
    expected_executable_actions_by_family = {
        family: [action.action_id for action in world.frontier if action.verb.startswith("TUNE_") and family in action.action_id.lower()]
        for family in prompt_view["decision_signals"]["tuning_action_space"].keys()
    }
    expected_executable_actions_by_family = {
        family: action_ids for family, action_ids in expected_executable_actions_by_family.items() if action_ids
    }

    assert payload["live_state_snapshot"]["raw_snapshot"]["printer_1.mode"] == runtime["whiteboard"].snapshot().values["printer_1.mode"]
    assert payload["world_summary"]["planner_world_packet"]["facts"] == prompt_view["facts"]
    assert payload["world_summary"]["planner_world_packet"]["frontier"] == prompt_view["frontier"]
    assert payload["world_summary"]["explicit_frontier_ids"] == expected_explicit_frontier_ids
    assert payload["world_summary"]["allowed_frontier_ids"] == expected_executable_frontier_ids
    assert payload["world_summary"]["tuning_frontier_ids"] == expected_tuning_frontier_ids
    assert payload["world_summary"]["tuning_action_space"] == prompt_view["decision_signals"]["tuning_action_space"]
    assert payload["world_summary"]["capability_actions"] == expected_capability_actions
    assert payload["world_summary"]["executable_actions_by_family"] == expected_executable_actions_by_family
    assert payload["world_summary"]["family_blockers"] == prompt_view["decision_signals"]["family_blockers"]
    assert payload["world_summary"]["shadow_actions"] == expected_capability_actions
    assert payload["world_summary"]["planning_current"] == {
        "pressure_advance": {
            "observed_live_value": None,
            "live_value": None,
            "baseline_value": None,
            "effective_value": None,
            "source": "missing_live_readback",
        },
        "accel": {
            "observed_live_value": None,
            "live_value": None,
            "baseline_value": None,
            "effective_value": None,
            "source": "missing_live_readback",
        },
    }
    assert payload["world_summary"]["compact_tuning_space"] == prompt_view["decision_signals"]["tuning_action_space"]
    assert payload["world_summary"]["facts"]["printer_1.frontier_ids_json"] == json.dumps(
        [action.action_id for action in world.frontier],
        sort_keys=True,
    )
    assert payload["world_summary"]["facts"]["printer_1.explicit_frontier_ids_json"] == json.dumps(
        expected_explicit_frontier_ids,
        sort_keys=True,
    )
    assert payload["world_summary"]["facts"]["printer_1.allowed_frontier_ids_json"] == json.dumps(
        expected_executable_frontier_ids,
        sort_keys=True,
    )
    assert payload["world_summary"]["facts"]["printer_1.tuning_frontier_ids_json"] == json.dumps(
        expected_tuning_frontier_ids,
        sort_keys=True,
    )
    assert payload["world_summary"]["facts"]["printer_1.tuning_action_space_json"] == json.dumps(
        prompt_view["decision_signals"]["tuning_action_space"],
        sort_keys=True,
    )
    assert payload["world_summary"]["facts"]["printer_1.shadow_actions_json"] == json.dumps(
        payload["world_summary"]["shadow_actions"],
        sort_keys=True,
    )
    assert payload["world_summary"]["facts"]["printer_1.capability_actions_json"] == json.dumps(
        payload["world_summary"]["capability_actions"],
        sort_keys=True,
    )
    assert payload["world_summary"]["facts"]["printer_1.executable_actions_by_family_json"] == json.dumps(
        payload["world_summary"]["executable_actions_by_family"],
        sort_keys=True,
    )
    assert payload["world_summary"]["facts"]["printer_1.family_blockers_json"] == json.dumps(
        payload["world_summary"]["family_blockers"],
        sort_keys=True,
    )
    assert payload["world_summary"]["facts"]["printer_1.planning_current_json"] == json.dumps(
        payload["world_summary"]["planning_current"],
        sort_keys=True,
    )
    assert payload["world_summary"]["facts"]["printer_1.compact_tuning_space_json"] == json.dumps(
        prompt_view["decision_signals"]["tuning_action_space"],
        sort_keys=True,
    )
    assert payload["decision_input"]["planner_world_packet"]["facts"] == prompt_view["facts"]
    assert payload["decision_input"]["explicit_frontier_ids"] == expected_explicit_frontier_ids
    assert payload["decision_input"]["allowed_frontier_ids"] == expected_executable_frontier_ids
    assert payload["decision_input"]["tuning_frontier_ids"] == expected_tuning_frontier_ids
    assert payload["decision_input"]["tuning_action_space"] == prompt_view["decision_signals"]["tuning_action_space"]
    assert payload["decision_input"]["capability_actions"] == payload["world_summary"]["capability_actions"]
    assert payload["decision_input"]["executable_actions_by_family"] == payload["world_summary"]["executable_actions_by_family"]
    assert payload["decision_input"]["family_blockers"] == payload["world_summary"]["family_blockers"]
    assert payload["decision_input"]["shadow_actions"] == payload["world_summary"]["shadow_actions"]
    assert payload["decision_input"]["planning_current"] == payload["world_summary"]["planning_current"]
    assert payload["decision_input"]["compact_tuning_space"] == prompt_view["decision_signals"]["tuning_action_space"]
    assert payload["decision_input"]["facts"]["printer_1.frontier_ids_json"] == json.dumps(
        [action.action_id for action in world.frontier],
        sort_keys=True,
    )
    assert payload["decision_input"]["facts"]["printer_1.explicit_frontier_ids_json"] == json.dumps(
        expected_explicit_frontier_ids,
        sort_keys=True,
    )
    assert payload["decision_input"]["facts"]["printer_1.allowed_frontier_ids_json"] == json.dumps(
        expected_executable_frontier_ids,
        sort_keys=True,
    )
    assert payload["decision_input"]["facts"]["printer_1.tuning_frontier_ids_json"] == json.dumps(
        expected_tuning_frontier_ids,
        sort_keys=True,
    )
    assert payload["decision_input"]["facts"]["printer_1.tuning_action_space_json"] == json.dumps(
        prompt_view["decision_signals"]["tuning_action_space"],
        sort_keys=True,
    )
    assert payload["decision_input"]["facts"]["printer_1.shadow_actions_json"] == json.dumps(
        payload["decision_input"]["shadow_actions"],
        sort_keys=True,
    )
    assert payload["decision_input"]["facts"]["printer_1.capability_actions_json"] == json.dumps(
        payload["decision_input"]["capability_actions"],
        sort_keys=True,
    )
    assert payload["decision_input"]["facts"]["printer_1.executable_actions_by_family_json"] == json.dumps(
        payload["decision_input"]["executable_actions_by_family"],
        sort_keys=True,
    )
    assert payload["decision_input"]["facts"]["printer_1.family_blockers_json"] == json.dumps(
        payload["decision_input"]["family_blockers"],
        sort_keys=True,
    )
    assert payload["decision_input"]["facts"]["printer_1.planning_current_json"] == json.dumps(
        payload["decision_input"]["planning_current"],
        sort_keys=True,
    )
    assert payload["decision_input"]["facts"]["printer_1.compact_tuning_space_json"] == json.dumps(
        prompt_view["decision_signals"]["tuning_action_space"],
        sort_keys=True,
    )
    assert payload["post_execution"]["planner_world_packet"]["facts"] == prompt_view["facts"]
    assert payload["post_execution"]["explicit_frontier_ids"] == expected_explicit_frontier_ids
    assert payload["post_execution"]["allowed_frontier_ids"] == expected_executable_frontier_ids
    assert payload["post_execution"]["tuning_frontier_ids"] == expected_tuning_frontier_ids
    assert payload["post_execution"]["tuning_action_space"] == prompt_view["decision_signals"]["tuning_action_space"]
    assert payload["post_execution"]["capability_actions"] == payload["world_summary"]["capability_actions"]
    assert payload["post_execution"]["executable_actions_by_family"] == payload["world_summary"]["executable_actions_by_family"]
    assert payload["post_execution"]["family_blockers"] == payload["world_summary"]["family_blockers"]
    assert payload["post_execution"]["shadow_actions"] == payload["world_summary"]["shadow_actions"]
    assert payload["post_execution"]["planning_current"] == payload["world_summary"]["planning_current"]
    assert payload["post_execution"]["compact_tuning_space"] == prompt_view["decision_signals"]["tuning_action_space"]
    assert payload["post_execution"]["facts"]["printer_1.frontier_ids_json"] == json.dumps(
        [action.action_id for action in world.frontier],
        sort_keys=True,
    )
    assert payload["post_execution"]["facts"]["printer_1.explicit_frontier_ids_json"] == json.dumps(
        expected_explicit_frontier_ids,
        sort_keys=True,
    )
    assert payload["post_execution"]["facts"]["printer_1.allowed_frontier_ids_json"] == json.dumps(
        expected_executable_frontier_ids,
        sort_keys=True,
    )
    assert payload["post_execution"]["facts"]["printer_1.tuning_frontier_ids_json"] == json.dumps(
        expected_tuning_frontier_ids,
        sort_keys=True,
    )
    assert payload["post_execution"]["facts"]["printer_1.tuning_action_space_json"] == json.dumps(
        prompt_view["decision_signals"]["tuning_action_space"],
        sort_keys=True,
    )
    assert payload["post_execution"]["facts"]["printer_1.shadow_actions_json"] == json.dumps(
        payload["post_execution"]["shadow_actions"],
        sort_keys=True,
    )
    assert payload["post_execution"]["facts"]["printer_1.capability_actions_json"] == json.dumps(
        payload["post_execution"]["capability_actions"],
        sort_keys=True,
    )
    assert payload["post_execution"]["facts"]["printer_1.executable_actions_by_family_json"] == json.dumps(
        payload["post_execution"]["executable_actions_by_family"],
        sort_keys=True,
    )
    assert payload["post_execution"]["facts"]["printer_1.family_blockers_json"] == json.dumps(
        payload["post_execution"]["family_blockers"],
        sort_keys=True,
    )
    assert payload["post_execution"]["facts"]["printer_1.planning_current_json"] == json.dumps(
        payload["post_execution"]["planning_current"],
        sort_keys=True,
    )
    assert payload["post_execution"]["facts"]["printer_1.compact_tuning_space_json"] == json.dumps(
        prompt_view["decision_signals"]["tuning_action_space"],
        sort_keys=True,
    )
    assert payload["cycle_timing"] == {}


def test_artifact_planning_current_uses_trusted_live_value_when_accel_readback_skews_low():
    world = SimpleNamespace(
        facts={
            "printer_1.print_accel_mm_s2": 500.0,
            "printer_1.active_print_accel_baseline_mm_s2": 3000.0,
            "printer_1.pressure_advance": 0.03,
            "printer_1.active_pressure_advance_baseline": 0.03,
        },
        last_result={},
        recent_results=[],
    )

    payload = _artifact_planning_current(world)

    assert payload["accel"] == {
        "observed_live_value": 500.0,
        "live_value": 3000.0,
        "baseline_value": 3000.0,
        "effective_value": 3000.0,
        "source": "stabilized_baseline",
    }
    assert payload["pressure_advance"] == {
        "observed_live_value": 0.03,
        "live_value": 0.03,
        "baseline_value": 0.03,
        "effective_value": 0.03,
        "source": "live_readback",
    }


def test_world_artifact_payload_hides_accel_from_visible_allowed_frontier_ids():
    world = WorldPacket(
        goal="Improve the active print conservatively.",
        device_summaries=[],
        facts={},
        resources=[],
        blockers=[],
        deltas=[],
        frontier=[
            LegalAction(
                action_id="A_PRUSA_CANCEL",
                verb="CANCEL",
                description="Cancel the print.",
                owner_pack="prusa_core_one_plus",
                execute_ref="cancel",
                hazard_class=HazardClass.HIGH,
            ),
            LegalAction(
                action_id="A_PRUSA_TRIM_SPEED_DOWN_SMALL",
                verb="TUNE_SPEED",
                description="Reduce print speed a little while keeping the current print running.",
                owner_pack="prusa_core_one_plus",
                execute_ref="trim_speed_down_small",
                hazard_class=HazardClass.LOW,
            ),
            LegalAction(
                action_id="A_PRUSA_TRIM_ACCEL_DOWN_SMALL",
                verb="TUNE_ACCEL",
                description="Reduce print acceleration a little while keeping the current print running.",
                owner_pack="prusa_core_one_plus",
                execute_ref="trim_accel_down_small",
                hazard_class=HazardClass.LOW,
            ),
        ],
        last_result={},
        pending_human=[],
    )

    payload = _world_artifact_payload(world)

    assert payload["frontier_ids"] == [
        "A_PRUSA_CANCEL",
        "A_PRUSA_TRIM_SPEED_DOWN_SMALL",
        "A_PRUSA_TRIM_ACCEL_DOWN_SMALL",
    ]
    assert payload["allowed_frontier_ids"] == [
        "A_PRUSA_CANCEL",
        "A_PRUSA_TRIM_SPEED_DOWN_SMALL",
    ]
    assert payload["tuning_frontier_ids"] == ["A_PRUSA_TRIM_SPEED_DOWN_SMALL"]
    assert payload["tuning_action_space"] == {
        "speed": {
            "family": "speed",
            "summary": "Adjust print motion rate.",
            "allowed_directions": ["down"],
            "allowed_magnitudes": ["small"],
        }
    }
    assert json.loads(payload["facts"]["printer_1.allowed_frontier_ids_json"]) == payload["allowed_frontier_ids"]
    assert json.loads(payload["facts"]["printer_1.tuning_frontier_ids_json"]) == payload["tuning_frontier_ids"]


def test_lock_conflict_parks_run_as_replan_required_without_raising(runtime, monkeypatch):
    compiler = runtime["compiler"]
    engine = runtime["engine"]
    db = runtime["db"]

    world = compiler.compile("Unload cooled part from printer_1 into tray_A")
    action = world.frontier[0]

    # Another holder owns this action's locks. Before the Phase-0 hotfix the
    # PROPOSED -> REPLAN_REQUIRED transition was illegal and this path crashed
    # with ValueError instead of refusing safely.
    monkeypatch.setattr(engine.lock_manager, "acquire", lambda keys: False)

    plan = PlanIR(decision="EXECUTE", sequence=[action.action_id], why="pin lock-conflict transition")
    report = engine.execute_plan("Unload cooled part from printer_1 into tray_A", world, plan)

    assert report.replan_required
    parked = db.list_action_runs_by_status(ActionRunStatus.REPLAN_REQUIRED)
    assert [run.action_id for run in parked] == [action.action_id]


def test_toctou_precondition_failure_parks_run_as_replan_required_without_raising(runtime, monkeypatch):
    compiler = runtime["compiler"]
    engine = runtime["engine"]
    db = runtime["db"]

    world = compiler.compile("Unload cooled part from printer_1 into tray_A")
    action = next(a for a in world.frontier if a.preconditions is not None)

    # The world changes inside the TOCTOU window: the frontier refresh still
    # offers the action, but the precondition fact flips before the post-
    # authorization re-check. Before the Phase-0 hotfix the
    # AUTHORIZED -> REPLAN_REQUIRED transition was illegal and crashed.
    real_compile = engine.world_compiler.compile
    calls = {"n": 0}

    def compile_with_toctou_flip(goal, pending_human=None):
        calls["n"] += 1
        recompiled = real_compile(goal, pending_human) if pending_human is not None else real_compile(goal)
        if calls["n"] >= 2:
            recompiled.facts["printer_1.part_present"] = False
        return recompiled

    monkeypatch.setattr(engine.world_compiler, "compile", compile_with_toctou_flip)

    plan = PlanIR(decision="EXECUTE", sequence=[action.action_id], why="pin TOCTOU transition")
    report = engine.execute_plan("Unload cooled part from printer_1 into tray_A", world, plan)

    assert report.replan_required
    parked = db.list_action_runs_by_status(ActionRunStatus.REPLAN_REQUIRED)
    assert [run.action_id for run in parked] == [action.action_id]


def _crash_residue_run(db, *, status: ActionRunStatus, required_locks: list[str], suffix: str) -> ActionRun:
    now_ms = int(time.time() * 1000)
    plan_id = f"plan_residue_{suffix}"
    db.store_plan(
        PlanRecord(
            plan_id=plan_id,
            goal="g",
            world_packet={"goal": "g"},
            plan_ir={"decision": "EXECUTE", "sequence": ["A_PRN_PAUSE"], "why": "residue"},
            created_ts_ms=now_ms,
        )
    )
    run = ActionRun(
        action_run_id=f"run_residue_{suffix}",
        plan_id=plan_id,
        action_id="A_PRN_PAUSE",
        verb="PAUSE_PROCESS",
        owner_pack="sim_printer",
        args={},
        args_hash=action_args_hash("PAUSE_PROCESS", {}),
        idempotency_key=f"idem_residue_{suffix}",
        required_locks=required_locks,
        status=ActionRunStatus.PROPOSED,
        hazard_class=HazardClass.LOW,
        created_ts_ms=now_ms,
        updated_ts_ms=now_ms,
    )
    db.create_action_run(run)
    if status is ActionRunStatus.DISPATCHED:
        db.transition_action_run(run.action_run_id, ActionRunStatus.AUTHORIZED)
        db.transition_action_run(run.action_run_id, ActionRunStatus.DISPATCHED)
    elif status is ActionRunStatus.AUTHORIZED:
        db.transition_action_run(run.action_run_id, ActionRunStatus.AUTHORIZED)
    return run


def test_flush_state_on_start_defaults_off(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("WALLEE_FLUSH_STATE_ON_START", raising=False)
    config = Config.from_env(repo_root=repo_root)

    assert config.flush_state_on_start is False

    runtime_db = RuntimeDB(config.db_path)
    runtime_db.record_event("test", "INFO", "survives_restart")
    runtime_db.close()

    reset_runtime_start_state(config)

    runtime_db = RuntimeDB(config.db_path)
    try:
        count = runtime_db._conn.execute("select count(*) from events").fetchone()[0]
        assert count == 1
    finally:
        runtime_db.close()


def test_boot_reconcile_marks_dispatched_unknown_frees_locks_and_escalates(runtime):
    db = runtime["db"]
    config = runtime["config"]
    compiler = runtime["compiler"]

    residue = _crash_residue_run(
        db, status=ActionRunStatus.DISPATCHED, required_locks=["printer_1.motion"], suffix="dispatched"
    )
    # Crash residue poisons every future frontier: the world compiler treats
    # durable DISPATCHED rows as held locks.
    assert compiler._active_locks() == {"printer_1.motion"}

    swept = reconcile_runtime_start_state(config)

    assert swept["unknown"] == 1 and swept["aborted"] == 0
    assert db.list_action_runs_by_status(ActionRunStatus.DISPATCHED) == []
    unknown_runs = db.list_action_runs_by_status(ActionRunStatus.UNKNOWN)
    assert [run.action_run_id for run in unknown_runs] == [residue.action_run_id]
    assert compiler._active_locks() == set()

    outbox_payloads = [json.loads(p.read_text()) for p in config.outbox_dir.glob("human_*.json")]
    crash_notices = [p for p in outbox_payloads if "Crash recovery" in p["title"]]
    assert len(crash_notices) == 1
    assert crash_notices[0]["severity"] == "critical"
    assert crash_notices[0]["require_ack"] is True


def test_boot_reconcile_aborts_stale_proposed_and_authorized_runs(runtime):
    db = runtime["db"]
    config = runtime["config"]

    proposed = _crash_residue_run(db, status=ActionRunStatus.PROPOSED, required_locks=[], suffix="proposed")
    authorized = _crash_residue_run(db, status=ActionRunStatus.AUTHORIZED, required_locks=[], suffix="authorized")

    swept = reconcile_runtime_start_state(config)

    assert swept["unknown"] == 0 and swept["aborted"] == 2
    aborted_ids = {run.action_run_id for run in db.list_action_runs_by_status(ActionRunStatus.ABORTED)}
    assert aborted_ids == {proposed.action_run_id, authorized.action_run_id}
    assert db.list_action_runs_by_status(ActionRunStatus.PROPOSED) == []
    assert db.list_action_runs_by_status(ActionRunStatus.AUTHORIZED) == []
