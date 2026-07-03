from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wallee import main as runtime_main
from wallee.config import Config
from wallee.models import PlanIR


class _StubWorld:
    frontier = []
    blockers = []
    facts = {"printer_1.lifecycle": "PRINTING", "printer_1.job_active": True, "printer_1.printing_phase": "active_printing", "printer_1.active_printing": True}
    pending_human = []
    deltas = []
    device_summaries = []
    resources = []
    compilation = None
    last_result = {}

    def prompt_view(self) -> dict[str, object]:
        return {
            "goal": "Reduce stringing.",
            "devices": [],
            "facts": {},
            "resources": [],
            "blockers": [],
            "deltas": [],
            "frontier": [],
            "last_result": {},
            "pending_human": [],
            "decision_contract": {},
            "decision_signals": {"allowed_frontier_ids": ["A_TEST_ACTION"], "tuning_action_space": {}},
        }


class _StubRuntimeDB:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, object]]] = []

    def clear_runtime_state(self) -> None:
        return None

    def close(self) -> None:
        return None

    def record_event(self, component: str, level: str, message: str, *, context: dict[str, object] | None = None) -> None:
        self.events.append((component, level, message, context or {}))


class _StubRegistry:
    def publish_all_raw_state(self, whiteboard, mode: str = "full") -> None:
        return None

    def close_all(self) -> None:
        return None


class _StubCompiler:
    def compile(self, goal: str, pending_messages) -> _StubWorld:
        return _StubWorld()


class _StubHumanGateway:
    def submit_goal(self, goal: str) -> None:
        return None

    def pending_messages(self):
        return []


class _StubSafety:
    def poll(self) -> None:
        return None


class _StubEngine:
    def __init__(self, runtime_db: _StubRuntimeDB) -> None:
        self.runtime_db = runtime_db
        self.executed_plans: list[PlanIR] = []

    def validate_plan(self, world, plan: PlanIR) -> list[object]:
        return []

    def execute_plan(self, goal: str, world, plan: PlanIR):
        self.executed_plans.append(plan)
        if plan.decision == "NO_ACTION":
            self.runtime_db.record_event("engine", "INFO", "no_action", context={"decision": plan.decision})
        return SimpleNamespace(
            plan_id="plan_test",
            executed_action_ids=[],
            blocked_action_run_id=None,
            replan_required=False,
            failed_action_run_id=None,
            human_request_ids=[],
            notes=["planner selected no action"] if plan.decision == "NO_ACTION" else [],
            post_execution_world=None,
        )

    def close(self) -> None:
        return None


def _make_config(tmp_path: Path) -> Config:
    data_dir = tmp_path / ".runtime"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Config(
        data_dir=data_dir,
        db_path=data_dir / "runtime.db",
        knowledge_dir=tmp_path / "knowledge",
        pack_root=tmp_path / "packs",
        outbox_dir=data_dir / "outbox",
        planner_backend="heuristic",
        openrouter_api_key=None,
        openrouter_model="openai/gpt-5.4",
        openrouter_response_healing=True,
        reasoning_effort="low",
        planner_request_timeout_seconds=20.0,
        planner_retry_attempts=3,
        planner_retry_backoff_seconds=1.0,
        planner_failures_before_no_action=3,
        planner_min_interval_seconds=0.0,
        planner_vision_lead_seconds=15.0,
        frontier_limit=8,
        delta_limit_per_device=3,
        approval_ttl_seconds=120,
        approval_wait_window_seconds=900,
        heartbeat_timeout_seconds=3,
        runtime_poll_interval_seconds=0.0,
        flush_state_on_start=True,
        monitor_when_inactive=True,
        simulation_mode=True,
        enabled_packs=("sim_printer",),
        auto_approve_low_hazard=True,
        safe_to_unload_temp_c=35.0,
        control_lock_path=data_dir / "control.lock",
    )


class _IdleWorld(_StubWorld):
    facts = {"printer_1.lifecycle": "IDLE", "printer_1.job_active": False, "printer_1.printing_phase": "not_printing", "printer_1.active_printing": False}


class _PausedWorld(_StubWorld):
    facts = {"printer_1.lifecycle": "PAUSED", "printer_1.job_active": True, "printer_1.printing_phase": "not_printing", "printer_1.active_printing": False}


class _EmptyFrontierWorld(_StubWorld):
    def prompt_view(self) -> dict[str, object]:
        payload = super().prompt_view()
        payload["decision_signals"] = {"allowed_frontier_ids": [], "tuning_action_space": {}}
        return payload


class _TerminalWorld(_StubWorld):
    facts = {
        "printer_1.lifecycle": "FINISHED",
        "printer_1.job_active": False,
        "printer_1.printing_phase": "not_printing",
        "printer_1.active_printing": False,
        "printer_1.targets_nonzero": False,
        "printer_1.job_progress_pct": 100.0,
        "printer_1.nozzle_target_c": 0.0,
        "printer_1.bed_target_c": 0.0,
    }

    def prompt_view(self) -> dict[str, object]:
        payload = super().prompt_view()
        payload["decision_signals"] = {"allowed_frontier_ids": [], "tuning_action_space": {}}
        return payload


class _PreExecutionTerminalCompiler:
    def __init__(self) -> None:
        self.calls = 0

    def compile(self, goal: str, pending_messages):
        self.calls += 1
        if self.calls == 1:
            return _StubWorld()
        return _TerminalWorld()


class _CountingRegistry(_StubRegistry):
    def __init__(self) -> None:
        self.publish_modes: list[str] = []

    def publish_all_raw_state(self, whiteboard, mode: str = "full") -> None:
        self.publish_modes.append(mode)


def test_main_requires_explicit_goal():
    with pytest.raises(SystemExit) as exc:
        runtime_main.main(["--once"])

    assert exc.value.code == 2


def test_main_requires_explicit_run_mode():
    with pytest.raises(SystemExit) as exc:
        runtime_main.main(["--goal", "Reduce stringing."])

    assert exc.value.code == 2


def test_main_rejects_cycles_with_forever():
    with pytest.raises(SystemExit) as exc:
        runtime_main.main(["--goal", "Reduce stringing.", "--forever", "--cycles", "2"])

    assert exc.value.code == 2


def test_main_rotates_to_fresh_per_run_log(tmp_path, monkeypatch):
    config = _make_config(tmp_path)
    runtime_db = _StubRuntimeDB()
    engine = _StubEngine(runtime_db)

    monkeypatch.setattr(runtime_main.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(
        runtime_main,
        "build_runtime",
        lambda config: (
            runtime_db,
            object(),
            _StubRegistry(),
            _StubCompiler(),
            _StubHumanGateway(),
            _StubSafety(),
            engine,
            object(),
        ),
    )
    monkeypatch.setattr(
        runtime_main,
        "plan_with_retry",
        lambda planner, engine, world, goal, max_attempts=runtime_main.PLANNER_RETRY_ATTEMPTS: PlanIR(
            decision="NO_ACTION",
            sequence=[],
            why="test no action",
        ),
    )

    assert runtime_main.main(["--goal", "Reduce stringing.", "--once"]) == 0

    assert (config.data_dir / "runtime_logs").is_symlink()
    run_dirs = sorted((config.data_dir / "runs").glob("*-pid*"))
    assert len(run_dirs) == 1
    log_paths = sorted((run_dirs[0] / "runtime_logs").glob("continuous-runtime-*.log"))
    assert len(log_paths) == 1
    log_text = log_paths[0].read_text(encoding="utf-8")
    assert "runtime_log=" in log_text
    assert "Cycle 1: decision=NO_ACTION" in log_text
    latest_run = json.loads((config.data_dir / "latest_run.json").read_text(encoding="utf-8"))
    assert latest_run["run_dir"] == str(run_dirs[0])


def test_main_degrades_to_no_action_after_three_consecutive_planner_failures(tmp_path, monkeypatch):
    config = _make_config(tmp_path)
    runtime_db = _StubRuntimeDB()
    engine = _StubEngine(runtime_db)

    monkeypatch.setattr(runtime_main.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(
        runtime_main,
        "build_runtime",
        lambda config: (
            runtime_db,
            object(),
            _StubRegistry(),
            _StubCompiler(),
            _StubHumanGateway(),
            _StubSafety(),
            engine,
            object(),
        ),
    )

    def _raise_planner_failure(planner, engine, world, goal, max_attempts=runtime_main.PLANNER_RETRY_ATTEMPTS):
        raise json.JSONDecodeError("Expecting value", "", 0)

    monkeypatch.setattr(runtime_main, "plan_with_retry", _raise_planner_failure)
    monkeypatch.setattr(runtime_main.time, "sleep", lambda _: None)

    assert runtime_main.main(["--goal", "Reduce stringing.", "--cycles", "3"]) == 0

    assert len(engine.executed_plans) == 1
    assert engine.executed_plans[0].decision == "NO_ACTION"
    assert "degrading to NO_ACTION" in engine.executed_plans[0].why
    assert [event[2] for event in runtime_db.events].count("planner_cycle_failed") == 3
    assert any(event[2] == "planner_degraded_to_no_action" for event in runtime_db.events)


def test_main_continues_after_runtime_cycle_exception(tmp_path, monkeypatch):
    config = _make_config(tmp_path)
    runtime_db = _StubRuntimeDB()
    engine = _StubEngine(runtime_db)

    class _FlakyRegistry(_StubRegistry):
        def __init__(self) -> None:
            self.calls = 0

        def publish_all_raw_state(self, whiteboard, mode: str = "full") -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient raw publish failure")

    registry = _FlakyRegistry()

    monkeypatch.setattr(runtime_main.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(
        runtime_main,
        "build_runtime",
        lambda config: (
            runtime_db,
            object(),
            registry,
            _StubCompiler(),
            _StubHumanGateway(),
            _StubSafety(),
            engine,
            object(),
        ),
    )
    monkeypatch.setattr(
        runtime_main,
        "plan_with_retry",
        lambda planner, engine, world, goal, max_attempts=runtime_main.PLANNER_RETRY_ATTEMPTS: PlanIR(
            decision="NO_ACTION",
            sequence=[],
            why="test no action",
        ),
    )
    monkeypatch.setattr(runtime_main.time, "sleep", lambda _: None)

    assert runtime_main.main(["--goal", "Reduce stringing.", "--cycles", "2"]) == 0

    assert len(engine.executed_plans) == 1
    assert any(event[2] == "runtime_cycle_failed" for event in runtime_db.events)


def test_main_archives_legacy_runtime_cycles_instead_of_deleting_them(tmp_path, monkeypatch):
    config = _make_config(tmp_path)
    runtime_db = _StubRuntimeDB()
    engine = _StubEngine(runtime_db)

    legacy_cycles = config.data_dir / "runtime_cycles"
    legacy_cycles.mkdir(parents=True, exist_ok=True)
    legacy_artifact = legacy_cycles / "old-cycle.json"
    legacy_artifact.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(runtime_main.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(
        runtime_main,
        "build_runtime",
        lambda config: (
            runtime_db,
            object(),
            _StubRegistry(),
            _StubCompiler(),
            _StubHumanGateway(),
            _StubSafety(),
            engine,
            object(),
        ),
    )
    monkeypatch.setattr(
        runtime_main,
        "plan_with_retry",
        lambda planner, engine, world, goal, max_attempts=runtime_main.PLANNER_RETRY_ATTEMPTS: PlanIR(
            decision="NO_ACTION",
            sequence=[],
            why="test no action",
        ),
    )

    assert runtime_main.main(["--goal", "Reduce stringing.", "--once"]) == 0

    legacy_archives = sorted((config.data_dir / "runs").glob("legacy-runtime_cycles-*"))
    assert len(legacy_archives) == 1
    assert (legacy_archives[0] / "runtime_cycles" / "old-cycle.json").exists()
    assert (config.data_dir / "runtime_cycles").is_symlink()


def test_main_stops_when_no_active_print_and_monitoring_not_requested(tmp_path, monkeypatch):
    config = _make_config(tmp_path)
    config.monitor_when_inactive = False
    runtime_db = _StubRuntimeDB()
    engine = _StubEngine(runtime_db)

    monkeypatch.setattr(runtime_main.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(
        runtime_main,
        "build_runtime",
        lambda config: (
            runtime_db,
            object(),
            _StubRegistry(),
            type("IdleCompiler", (), {"compile": lambda self, goal, pending_messages: _IdleWorld()})(),
            _StubHumanGateway(),
            _StubSafety(),
            engine,
            object(),
        ),
    )

    def _unexpected_plan(*args, **kwargs):
        raise AssertionError("planner should not be called when no active print is running")

    monkeypatch.setattr(runtime_main, "plan_with_retry", _unexpected_plan)

    assert runtime_main.main(["--goal", "Reduce stringing.", "--forever"]) == 0
    assert len(engine.executed_plans) == 1
    assert engine.executed_plans[0].decision == "NO_ACTION"
    assert any(event[2] == "planner_call_bypassed" for event in runtime_db.events)
    assert any(event[2] == "runtime_stopped_inactive" for event in runtime_db.events)


def test_main_bypasses_planner_when_print_not_actionable(tmp_path, monkeypatch):
    config = _make_config(tmp_path)
    runtime_db = _StubRuntimeDB()
    engine = _StubEngine(runtime_db)

    monkeypatch.setattr(runtime_main.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(
        runtime_main,
        "build_runtime",
        lambda config: (
            runtime_db,
            object(),
            _StubRegistry(),
            type("PausedCompiler", (), {"compile": lambda self, goal, pending_messages: _PausedWorld()})(),
            _StubHumanGateway(),
            _StubSafety(),
            engine,
            object(),
        ),
    )

    def _unexpected_plan(*args, **kwargs):
        raise AssertionError("planner should not be called when print is not actionable")

    monkeypatch.setattr(runtime_main, "plan_with_retry", _unexpected_plan)

    assert runtime_main.main(["--goal", "Reduce stringing.", "--once"]) == 0
    assert len(engine.executed_plans) == 1
    assert engine.executed_plans[0].decision == "NO_ACTION"
    assert any(event[2] == "planner_call_bypassed" for event in runtime_db.events)


def test_main_bypasses_planner_when_allowed_frontier_is_empty(tmp_path, monkeypatch):
    config = _make_config(tmp_path)
    runtime_db = _StubRuntimeDB()
    engine = _StubEngine(runtime_db)

    monkeypatch.setattr(runtime_main.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(
        runtime_main,
        "build_runtime",
        lambda config: (
            runtime_db,
            object(),
            _StubRegistry(),
            type("EmptyFrontierCompiler", (), {"compile": lambda self, goal, pending_messages: _EmptyFrontierWorld()})(),
            _StubHumanGateway(),
            _StubSafety(),
            engine,
            object(),
        ),
    )

    def _unexpected_plan(*args, **kwargs):
        raise AssertionError("planner should not be called when allowed frontier is empty")

    monkeypatch.setattr(runtime_main, "plan_with_retry", _unexpected_plan)

    assert runtime_main.main(["--goal", "Reduce stringing.", "--once"]) == 0
    assert len(engine.executed_plans) == 1
    assert engine.executed_plans[0].decision == "NO_ACTION"
    assert any(event[2] == "planner_call_bypassed" for event in runtime_db.events)


def test_bypassed_cycle_does_not_carry_stale_planner_provider_metadata(tmp_path, monkeypatch):
    config = _make_config(tmp_path)
    runtime_db = _StubRuntimeDB()
    engine = _StubEngine(runtime_db)
    planner = SimpleNamespace(last_provider_metadata={"model": "stale-model", "latency_ms": 1234.5})

    monkeypatch.setattr(runtime_main.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(
        runtime_main,
        "build_runtime",
        lambda config: (
            runtime_db,
            object(),
            _StubRegistry(),
            type("EmptyFrontierCompiler", (), {"compile": lambda self, goal, pending_messages: _EmptyFrontierWorld()})(),
            _StubHumanGateway(),
            _StubSafety(),
            engine,
            planner,
        ),
    )

    def _unexpected_plan(*args, **kwargs):
        raise AssertionError("planner should not be called when allowed frontier is empty")

    monkeypatch.setattr(runtime_main, "plan_with_retry", _unexpected_plan)

    assert runtime_main.main(["--goal", "Reduce stringing.", "--once"]) == 0

    run_dirs = sorted((config.data_dir / "runs").glob("*-pid*"))
    assert len(run_dirs) == 1
    artifacts = sorted((run_dirs[0] / "runtime_cycles").glob("*.json"))
    assert len(artifacts) == 1
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))

    assert payload["cycle_timing"]["planner_bypassed"] is True
    assert payload["planner_provider_metadata"] is None


def test_main_pre_execution_terminal_guard_skips_late_tuning_action(tmp_path, monkeypatch):
    config = _make_config(tmp_path)
    runtime_db = _StubRuntimeDB()
    engine = _StubEngine(runtime_db)
    registry = _CountingRegistry()
    compiler = _PreExecutionTerminalCompiler()

    monkeypatch.setattr(runtime_main.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(
        runtime_main,
        "build_runtime",
        lambda config: (
            runtime_db,
            object(),
            registry,
            compiler,
            _StubHumanGateway(),
            _StubSafety(),
            engine,
            object(),
        ),
    )
    monkeypatch.setattr(
        runtime_main,
        "plan_with_retry",
        lambda planner, engine, world, goal, max_attempts=runtime_main.PLANNER_RETRY_ATTEMPTS: PlanIR(
            decision="EXECUTE",
            sequence=[],
            tuning_choice={"family": "accel", "direction": "down", "magnitude": "small"},
            why="fresh stringing supports accel down",
        ),
    )

    assert runtime_main.main(["--goal", "Reduce stringing.", "--once"]) == 0

    assert registry.publish_modes == ["full", "full"]
    assert compiler.calls == 2
    assert len(engine.executed_plans) == 1
    executed_plan = engine.executed_plans[0]
    assert executed_plan.decision == "NO_ACTION"
    assert "Pre-execution terminal guard skipped accel:down:small" in executed_plan.why
    assert any(event[2] == "pre_execution_terminal_guard_skipped_tuning" for event in runtime_db.events)

    run_dirs = sorted((config.data_dir / "runs").glob("*-pid*"))
    artifacts = sorted((run_dirs[0] / "runtime_cycles").glob("*.json"))
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert payload["cycle_timing"]["pre_execution_terminal_guard_triggered"] is True
    assert payload["planner_output"]["decision"] == "NO_ACTION"
