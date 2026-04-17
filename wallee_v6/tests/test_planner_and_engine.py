import json
import time
from pathlib import Path
from types import SimpleNamespace

from wallee_v6.config import Config
from wallee_v6.engine import Engine
from wallee_v6.human import HumanGateway
from wallee_v6.ids import action_args_hash
from wallee_v6.main import _write_cycle_artifact, reset_runtime_start_state
from wallee_v6.models import ActionRunStatus, DeviceSummary, ExecutionResult, HazardClass, LegalAction, PlanIR, PlanRecord, WorldPacket
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
        "wait_timed_out": True,
        "nonfatal_timeout": True,
    }


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
    assert not cycles_dir.exists()
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
    artifact = _write_cycle_artifact(
        config,
        cycle_index=0,
        goal=world.goal,
        world=world,
        plan=plan,
        report=report,
        planner=SimpleNamespace(last_provider_metadata=None),
    )

    payload = json.loads(artifact.read_text(encoding="utf-8"))

    assert payload["live_state_snapshot"]["raw_snapshot"]["printer_1.mode"] == runtime["whiteboard"].snapshot().values["printer_1.mode"]
    assert payload["world_summary"]["planner_world_packet"]["facts"] == world.prompt_view()["facts"]
    assert payload["world_summary"]["planner_world_packet"]["frontier"] == world.prompt_view()["frontier"]
    assert payload["decision_input"]["planner_world_packet"]["facts"] == world.prompt_view()["facts"]
    assert payload["post_execution"]["planner_world_packet"]["facts"] == world.prompt_view()["facts"]
    assert payload["cycle_timing"] == {}
