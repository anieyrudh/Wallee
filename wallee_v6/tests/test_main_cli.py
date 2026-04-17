from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wallee_v6 import main as runtime_main
from wallee_v6.config import Config
from wallee_v6.models import PlanIR


class _StubWorld:
    frontier = []
    blockers = []
    facts = {}
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
            "decision_signals": {"allowed_frontier_ids": []},
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
        reasoning_effort="low",
        frontier_limit=8,
        delta_limit_per_device=3,
        approval_ttl_seconds=120,
        heartbeat_timeout_seconds=3,
        runtime_poll_interval_seconds=0.0,
        flush_state_on_start=True,
        simulation_mode=True,
        enabled_packs=("sim_printer",),
        auto_approve_low_hazard=True,
        safe_to_unload_temp_c=35.0,
        control_lock_path=data_dir / "control.lock",
    )


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

    log_paths = sorted((config.data_dir / "runtime_logs").glob("continuous-runtime-*.log"))
    assert len(log_paths) == 1
    log_text = log_paths[0].read_text(encoding="utf-8")
    assert "runtime_log=" in log_text
    assert "Cycle 1: decision=NO_ACTION" in log_text


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
