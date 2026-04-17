"""CLI entry point for the lean Wallee v6 reference runtime."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
import shutil
import json
from pathlib import Path
import sys
import time
import traceback

from .config import Config
from .engine import Engine
from .human import HumanGateway
from .models import PlanIR
from .planner import HeuristicPlanner, OpenRouterPlanner, Planner
from .planning_context import WorldCompiler
from .predicates import PredicateEvaluator
from .registry import PackRegistry
from .runtime_db import RuntimeDB
from .safety import SafetyKernel
from .whiteboard import InMemoryWhiteboard


PLANNER_RETRY_ATTEMPTS = 3
PLANNER_FAILURES_BEFORE_NO_ACTION = 3
RUNTIME_CYCLE_FAILURES_BEFORE_PERSISTENT = 3


class _TeeTextStream:
    """Mirror runtime output to both the current console stream and a per-run log."""

    def __init__(self, *streams) -> None:
        self._streams = streams
        self.encoding = getattr(streams[0], "encoding", "utf-8") if streams else "utf-8"

    def write(self, data: str) -> int:
        for stream in self._streams:
            stream.write(data)
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()

    def isatty(self) -> bool:
        return False


def build_runtime(config: Config):
    """Assemble the main runtime components."""
    runtime_db = RuntimeDB(config.db_path)
    whiteboard = InMemoryWhiteboard()
    registry = PackRegistry(config)
    registry.load()
    registry.publish_all_raw_state(whiteboard, mode="full")

    predicate_evaluator = PredicateEvaluator()
    world_compiler = WorldCompiler(
        config=config,
        registry=registry,
        whiteboard=whiteboard,
        runtime_db=runtime_db,
        predicate_evaluator=predicate_evaluator,
    )
    human_gateway = HumanGateway(config=config, runtime_db=runtime_db)
    safety = SafetyKernel(heartbeat_timeout_s=config.heartbeat_timeout_seconds)
    engine = Engine(
        config=config,
        runtime_db=runtime_db,
        whiteboard=whiteboard,
        registry=registry,
        world_compiler=world_compiler,
        human_gateway=human_gateway,
        safety=safety,
        predicate_evaluator=predicate_evaluator,
    )
    planner = build_planner(config, repo_root=Path(__file__).resolve().parents[1])

    return runtime_db, whiteboard, registry, world_compiler, human_gateway, safety, engine, planner


def reset_runtime_start_state(config: Config) -> None:
    """Clear durable runtime state so a restarted runtime starts cleanly."""
    if not getattr(config, "flush_state_on_start", True):
        return
    runtime_db = RuntimeDB(config.db_path)
    try:
        runtime_db.clear_runtime_state()
    finally:
        runtime_db.close()
    runtime_cycles_dir = config.data_dir / "runtime_cycles"
    if runtime_cycles_dir.exists():
        shutil.rmtree(runtime_cycles_dir)
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{config.db_path}{suffix}") if suffix else config.db_path
        if suffix and path.exists():
            path.unlink()
    if config.control_lock_path.exists():
        config.control_lock_path.unlink()


def build_planner(config: Config, repo_root: Path) -> Planner:
    """Return the selected planner backend."""
    if config.planner_backend == "openrouter" and config.openrouter_api_key:
        return OpenRouterPlanner(config, repo_root / "schemas" / "plan_ir.schema.json")
    return HeuristicPlanner()


def plan_with_retry(planner: Planner, engine: Engine, world, goal: str, *, max_attempts: int = PLANNER_RETRY_ATTEMPTS):
    """Return a validated plan, retrying a bounded number of times on planner errors."""
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            plan = planner.plan(world)
            engine.validate_plan(world, plan)
            if hasattr(planner, "last_provider_metadata") and isinstance(getattr(planner, "last_provider_metadata"), dict):
                getattr(planner, "last_provider_metadata")["retry_count"] = attempt
            return plan
        except Exception as exc:  # pragma: no cover - exercised in manual runs
            last_error = exc
            if hasattr(planner, "last_provider_metadata") and isinstance(getattr(planner, "last_provider_metadata"), dict):
                getattr(planner, "last_provider_metadata")["retry_count"] = attempt
            if attempt < max_attempts - 1:
                continue
            raise
    assert last_error is not None
    raise last_error


def _install_run_log(config: Config) -> tuple[Path, object, object, object]:
    """Create a fresh per-launch log file and tee stdout/stderr into it."""
    log_dir = config.data_dir / "runtime_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    log_path = log_dir / f"continuous-runtime-{timestamp}-pid{os.getpid()}.log"
    log_handle = log_path.open("a", encoding="utf-8", buffering=1)
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = _TeeTextStream(original_stdout, log_handle)
    sys.stderr = _TeeTextStream(original_stderr, log_handle)
    print(f"runtime_log={log_path}")
    return log_path, log_handle, original_stdout, original_stderr


def _restore_run_log(log_handle, original_stdout, original_stderr) -> None:
    if log_handle is None:
        return
    sys.stdout = original_stdout
    sys.stderr = original_stderr
    log_handle.close()


def _planner_degraded_no_action_plan(*, consecutive_failures: int, exc: Exception) -> PlanIR:
    return PlanIR(
        decision="NO_ACTION",
        sequence=[],
        why=(
            "Planner generation failed on repeated consecutive cycles after bounded retries; "
            f"degrading to NO_ACTION after {consecutive_failures} failures "
            f"({type(exc).__name__}: {exc})."
        ),
    )


def _should_stop(args, cycle_index: int) -> bool:
    if args.once:
        return True
    if not args.forever and args.cycles is not None and cycle_index >= args.cycles:
        return True
    return False


def _write_cycle_artifact(config: Config, *, cycle_index: int, goal: str, world, plan, report, planner) -> Path:
    return _write_cycle_artifact_with_worlds(
        config,
        cycle_index=cycle_index,
        goal=goal,
        decision_world=world,
        post_execution_world=world,
        plan=plan,
        report=report,
        planner=planner,
        cycle_timing={},
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _begin_timed_step(cycle_timing: dict[str, object], step: str) -> float:
    cycle_timing[f"{step}_started_at"] = _utc_now_iso()
    return time.perf_counter()


def _end_timed_step(cycle_timing: dict[str, object], step: str, started_at: float) -> None:
    cycle_timing[f"{step}_completed_at"] = _utc_now_iso()
    cycle_timing[f"{step}_duration_ms"] = round((time.perf_counter() - started_at) * 1000.0, 1)


def _mark_skipped_step(cycle_timing: dict[str, object], step: str, *, reason: str) -> None:
    now = _utc_now_iso()
    cycle_timing[f"{step}_started_at"] = now
    cycle_timing[f"{step}_completed_at"] = now
    cycle_timing[f"{step}_duration_ms"] = 0.0
    cycle_timing[f"{step}_skipped"] = True
    cycle_timing[f"{step}_skip_reason"] = reason


def _world_artifact_payload(world) -> dict[str, object]:
    compilation = world.compilation.model_dump() if getattr(world, "compilation", None) is not None else {}
    return {
        "live_state_snapshot": compilation,
        "planner_world_packet": world.prompt_view(),
        "frontier_ids": [action.action_id for action in world.frontier],
        "blockers": list(world.blockers),
        "facts": world.facts,
        "pending_human": list(world.pending_human),
    }


def _write_runtime_state(
    config: Config,
    *,
    cycle_index: int,
    phase: str,
    goal: str,
    world,
    plan=None,
    report=None,
    cycle_timing: dict[str, object] | None = None,
) -> Path:
    payload = {
        "cycle_index": cycle_index + 1,
        "phase": phase,
        "goal": goal,
        "ts": _utc_now_iso(),
        "run_scope": world.facts.get("printer_1.job_scope_token"),
        "live_state_snapshot": _world_artifact_payload(world)["live_state_snapshot"],
        "world_summary": {
            "planner_world_packet": world.prompt_view(),
            "frontier_ids": [action.action_id for action in world.frontier],
            "blockers": list(world.blockers),
            "facts": world.facts,
            "pending_human": list(world.pending_human),
        },
        "planner_output": plan.model_dump() if plan is not None else None,
        "execution_report": None
        if report is None
        else {
            "plan_id": report.plan_id,
            "executed_action_ids": list(report.executed_action_ids),
            "blocked_action_run_id": report.blocked_action_run_id,
            "replan_required": report.replan_required,
            "failed_action_run_id": report.failed_action_run_id,
            "human_request_ids": list(report.human_request_ids),
            "notes": list(report.notes),
        },
        "cycle_timing": dict(cycle_timing or {}),
    }
    out_path = config.data_dir / "runtime_state.json"
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return out_path


def _write_cycle_artifact_with_worlds(
    config: Config,
    *,
    cycle_index: int,
    goal: str,
    decision_world,
    post_execution_world,
    plan,
    report,
    planner,
    cycle_timing: dict[str, object],
) -> Path:
    out_dir = config.data_dir / "runtime_cycles"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H%M%S")
    out_path = out_dir / f"{ts}-cycle-{cycle_index + 1:04d}.json"
    decision_payload = _world_artifact_payload(decision_world)
    post_execution_payload = _world_artifact_payload(post_execution_world)
    payload = {
        "cycle_index": cycle_index + 1,
        "goal": goal,
        "ts": ts,
        "run_scope": post_execution_world.facts.get("printer_1.job_scope_token"),
        "live_state_snapshot": post_execution_payload["live_state_snapshot"],
        "world_summary": {
            "planner_world_packet": post_execution_world.prompt_view(),
            "frontier_ids": post_execution_payload["frontier_ids"],
            "blockers": post_execution_payload["blockers"],
            "facts": post_execution_payload["facts"],
            "pending_human": post_execution_payload["pending_human"],
        },
        "decision_input": decision_payload,
        "post_execution": post_execution_payload,
        "planner_output": plan.model_dump(),
        "planner_provider_metadata": getattr(planner, "last_provider_metadata", None),
        "execution_report": {
            "plan_id": report.plan_id,
            "executed_action_ids": list(report.executed_action_ids),
            "blocked_action_run_id": report.blocked_action_run_id,
            "replan_required": report.replan_required,
            "failed_action_run_id": report.failed_action_run_id,
            "human_request_ids": list(report.human_request_ids),
            "notes": list(report.notes),
        },
        "cycle_timing": dict(cycle_timing),
        "control_lock_path": str(config.control_lock_path),
    }
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the lean Wallee v6 reference runtime")
    parser.add_argument("--simulate", action="store_true", help="Force simulation-mode pack loading")
    parser.add_argument("--once", action="store_true", help="Run exactly one cycle")
    parser.add_argument("--cycles", type=int, default=None, help="Number of cycles to run when not using --once")
    parser.add_argument("--forever", action="store_true", help="Run continuously until interrupted")
    parser.add_argument(
        "--poll-interval-s",
        type=float,
        default=None,
        help="Seconds between runtime cycles; defaults to WALLEE_RUNTIME_POLL_INTERVAL_S",
    )
    parser.add_argument(
        "--goal",
        required=True,
        help="Goal string given to the planner",
    )
    args = parser.parse_args(argv)
    if args.once and args.forever:
        parser.error("choose only one of --once or --forever")
    if args.once and args.cycles is not None:
        parser.error("--once cannot be combined with --cycles")
    if args.forever and args.cycles is not None:
        parser.error("--forever cannot be combined with --cycles")
    if not args.once and not args.forever and args.cycles is None:
        parser.error("choose one of --once, --forever, or explicit --cycles N")

    config = Config.from_env()
    if args.simulate:
        config.simulation_mode = True
    log_handle = None
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    runtime_db = None
    registry = None
    engine = None
    try:
        reset_runtime_start_state(config)
        log_path, log_handle, original_stdout, original_stderr = _install_run_log(config)
        runtime_db, whiteboard, registry, world_compiler, human_gateway, safety, engine, planner = build_runtime(config)
        goal = args.goal
        human_gateway.submit_goal(goal)
        runtime_db.record_event(
            "runtime",
            "INFO",
            "runtime_started",
            context={
                "goal": goal,
                "log_path": str(log_path),
                "poll_interval_override_s": args.poll_interval_s,
                "forever": bool(args.forever),
                "once": bool(args.once),
                "cycles": args.cycles,
            },
        )
        poll_interval_s = config.runtime_poll_interval_seconds if args.poll_interval_s is None else float(args.poll_interval_s)
        cycle_index = 0
        consecutive_planner_failures = 0
        consecutive_cycle_failures = 0
        while True:
            try:
                cycle_timing: dict[str, object] = {"cycle_started_at": _utc_now_iso()}
                step_started = _begin_timed_step(cycle_timing, "raw_publish")
                registry.publish_all_raw_state(whiteboard, mode="full")
                _end_timed_step(cycle_timing, "raw_publish", step_started)
                cycle_timing["raw_publish_completed_at"] = _utc_now_iso()
                step_started = _begin_timed_step(cycle_timing, "decision_input_compile")
                world = world_compiler.compile(goal, human_gateway.pending_messages())
                _end_timed_step(cycle_timing, "decision_input_compile", step_started)
                cycle_timing["decision_input_compiled_at"] = _utc_now_iso()
                step_started = _begin_timed_step(cycle_timing, "decision_input_runtime_state_write")
                _write_runtime_state(
                    config,
                    cycle_index=cycle_index,
                    phase="decision_input_ready",
                    goal=goal,
                    world=world,
                    cycle_timing=cycle_timing,
                )
                _end_timed_step(cycle_timing, "decision_input_runtime_state_write", step_started)
                step_started = _begin_timed_step(cycle_timing, "planner_call")
                planner_exception: Exception | None = None
                try:
                    plan = plan_with_retry(planner, engine, world, goal, max_attempts=PLANNER_RETRY_ATTEMPTS)
                    consecutive_planner_failures = 0
                except Exception as exc:
                    planner_exception = exc
                    consecutive_planner_failures += 1
                _end_timed_step(cycle_timing, "planner_call", step_started)
                cycle_timing["planner_failure_count"] = consecutive_planner_failures
                if planner_exception is not None:
                    failure_context = {
                        "cycle_index": cycle_index + 1,
                        "exception_type": type(planner_exception).__name__,
                        "exception_message": str(planner_exception),
                        "consecutive_failures": consecutive_planner_failures,
                        "retry_attempts": PLANNER_RETRY_ATTEMPTS,
                    }
                    runtime_db.record_event("planner", "WARN", "planner_cycle_failed", context=failure_context)
                    if consecutive_planner_failures < PLANNER_FAILURES_BEFORE_NO_ACTION:
                        cycle_timing["planner_failed"] = True
                        cycle_timing["planner_failed_exception"] = failure_context["exception_type"]
                        step_started = _begin_timed_step(cycle_timing, "planner_failure_runtime_state_write")
                        _write_runtime_state(
                            config,
                            cycle_index=cycle_index,
                            phase="planner_failed",
                            goal=goal,
                            world=world,
                            cycle_timing=cycle_timing,
                        )
                        _end_timed_step(cycle_timing, "planner_failure_runtime_state_write", step_started)
                        print(
                            "Cycle "
                            f"{cycle_index + 1}: planner failed after {PLANNER_RETRY_ATTEMPTS} attempts "
                            f"({failure_context['exception_type']}: {failure_context['exception_message']}); "
                            f"continuing without execution ({consecutive_planner_failures}/{PLANNER_FAILURES_BEFORE_NO_ACTION})"
                        )
                        cycle_index += 1
                        if _should_stop(args, cycle_index):
                            break
                        time.sleep(max(0.0, poll_interval_s))
                        continue
                    runtime_db.record_event(
                        "planner",
                        "WARN",
                        "planner_degraded_to_no_action",
                        context=failure_context,
                    )
                    plan = _planner_degraded_no_action_plan(
                        consecutive_failures=consecutive_planner_failures,
                        exc=planner_exception,
                    )
                    cycle_timing["planner_degraded_to_no_action"] = True
                    cycle_timing["planner_degraded_exception"] = failure_context["exception_type"]
                cycle_timing["plan_ready_at"] = _utc_now_iso()
                step_started = _begin_timed_step(cycle_timing, "execution")
                report = engine.execute_plan(goal, world, plan)
                _end_timed_step(cycle_timing, "execution", step_started)
                cycle_timing["execution_finished_at"] = _utc_now_iso()
                step_started = _begin_timed_step(cycle_timing, "safety_poll")
                safety.poll()
                _end_timed_step(cycle_timing, "safety_poll", step_started)
                post_execution_world = report.post_execution_world or world
                _mark_skipped_step(
                    cycle_timing,
                    "post_execution_raw_publish",
                    reason="reused_engine_verified_world",
                )
                _mark_skipped_step(
                    cycle_timing,
                    "post_execution_compile",
                    reason="reused_engine_verified_world",
                )
                cycle_timing["post_execution_compiled_at"] = cycle_timing["post_execution_compile_completed_at"]
                step_started = _begin_timed_step(cycle_timing, "cycle_artifact_write")
                artifact_path = _write_cycle_artifact_with_worlds(
                    config,
                    cycle_index=cycle_index,
                    goal=goal,
                    decision_world=world,
                    post_execution_world=post_execution_world,
                    plan=plan,
                    report=report,
                    planner=planner,
                    cycle_timing=cycle_timing,
                )
                _end_timed_step(cycle_timing, "cycle_artifact_write", step_started)
                step_started = _begin_timed_step(cycle_timing, "post_execution_runtime_state_write")
                _write_runtime_state(
                    config,
                    cycle_index=cycle_index,
                    phase="post_execution_ready",
                    goal=goal,
                    world=post_execution_world,
                    plan=plan,
                    report=report,
                    cycle_timing=cycle_timing,
                )
                _end_timed_step(cycle_timing, "post_execution_runtime_state_write", step_started)

                print(f"Cycle {cycle_index + 1}: decision={plan.decision} notes={report.notes}")
                if report.blocked_action_run_id:
                    print(f"  blocked on approval: {report.blocked_action_run_id}")
                if report.replan_required:
                    print("  replan required due to verification or frontier change")
                if report.failed_action_run_id:
                    print(f"  failed action run: {report.failed_action_run_id}")
                print(f"  artifact: {artifact_path}")
                consecutive_cycle_failures = 0
            except Exception as exc:  # pragma: no cover - exercised in manual runs
                consecutive_cycle_failures += 1
                failure_context = {
                    "cycle_index": cycle_index + 1,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "consecutive_failures": consecutive_cycle_failures,
                    "persistent_failure": consecutive_cycle_failures >= RUNTIME_CYCLE_FAILURES_BEFORE_PERSISTENT,
                    "traceback": traceback.format_exc(),
                }
                runtime_db.record_event("runtime", "ERROR", "runtime_cycle_failed", context=failure_context)
                print(
                    "Cycle "
                    f"{cycle_index + 1}: runtime cycle failed "
                    f"({failure_context['exception_type']}: {failure_context['exception_message']})"
                )
                print(failure_context["traceback"])

            cycle_index += 1
            if _should_stop(args, cycle_index):
                break
            time.sleep(max(0.0, poll_interval_s))
    finally:
        for component_name, component in (("engine", engine), ("registry", registry), ("runtime_db", runtime_db)):
            if component is None:
                continue
            try:
                if component_name == "engine":
                    component.close()
                elif component_name == "registry":
                    component.close_all()
                else:
                    component.close()
            except Exception:
                print(f"runtime teardown error in {component_name}")
                print(traceback.format_exc())
        _restore_run_log(log_handle, original_stdout, original_stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
