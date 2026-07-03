"""The runtime control loop and its per-cycle policies."""

from __future__ import annotations

import os
import json
import time
import traceback

from .artifacts import (
    _begin_timed_step,
    _end_timed_step,
    _mark_skipped_step,
    _utc_now_iso,
    _world_allowed_frontier_ids,
    _world_planner_option_count,
    _world_tuning_action_space,
    _write_cycle_artifact_with_worlds,
    _write_runtime_state,
)
from .coerce import float_or_none as _float_or_none
from .config import Config
from .engine import Engine
from .models import PlanIR
from .planner import Planner
from .safety import consume_estop_signal

PLANNER_RETRY_ATTEMPTS = 3
PLANNER_FAILURES_BEFORE_NO_ACTION = 3
RUNTIME_CYCLE_FAILURES_BEFORE_PERSISTENT = 3


def plan_with_retry(planner: Planner, engine: Engine, world, goal: str, *, max_attempts: int = PLANNER_RETRY_ATTEMPTS):
    """Return a validated plan, retrying a bounded number of times on planner errors."""
    last_error: Exception | None = None
    retry_backoff_s = max(0.0, float(getattr(getattr(planner, "config", None), "planner_retry_backoff_seconds", 0.0)))
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
                if retry_backoff_s > 0.0:
                    time.sleep(retry_backoff_s)
                continue
            raise
    assert last_error is not None
    raise last_error


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


def _planner_bypass_no_action_plan(*, reason: str) -> PlanIR:
    return PlanIR(
        decision="NO_ACTION",
        sequence=[],
        why=reason,
    )


def _planner_pre_execution_guard_no_action_plan(*, original_plan: PlanIR, reason: str) -> PlanIR:
    selected: list[str] = []
    if original_plan.tuning_choice is not None:
        choice = original_plan.tuning_choice
        selected.append(f"{choice.family}:{choice.direction}:{choice.magnitude}")
    selected.extend(str(action_id) for action_id in original_plan.sequence)
    selected_text = ", ".join(selected) if selected else "tuning action"
    return PlanIR(
        decision="NO_ACTION",
        sequence=[],
        why=f"Pre-execution terminal guard skipped {selected_text}: {reason}",
    )


def _world_has_active_print(world) -> bool:
    lifecycle = str(world.facts.get("printer_1.lifecycle") or "").strip().upper()
    job_active = bool(world.facts.get("printer_1.job_active"))
    return lifecycle == "PRINTING" and job_active


def _write_heartbeat_beacon(config: Config, *, cycle_index: int, job_active: bool) -> None:
    """Write the liveness beacon the independent watchdog reads.

    The watchdog keys on the file's mtime (immune to clock-content bugs) and
    reads `job_active` to decide stop-vs-escalate. Written every cycle from the
    control loop, so a wedged loop stops updating it.
    """
    path = config.heartbeat_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "wall_ts": time.time(),
        "cycle_index": cycle_index,
        "job_active": bool(job_active),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _apply_remote_estop_requests(config: Config, safety, runtime_db) -> None:
    """Honor operator-written remote ESTOP / clear signals at the cycle boundary.

    A remote ESTOP is a control-plane soft stop: it fires the stop transport and
    latches at the next cycle top. It does NOT interrupt an in-flight side effect
    mid-call — the physical ESTOP button and the independent watchdog cover that
    (P6). Clearing is a deliberate, attended operator action.
    """
    estop = consume_estop_signal(config.estop_request_path)
    if estop is not None:
        requested_by = str(estop.get("requested_by") or "operator")
        reason = str(estop.get("reason") or "operator remote ESTOP")
        safety.trip(f"remote ESTOP requested by {requested_by}: {reason}")
        runtime_db.record_event(
            "runtime",
            "CRITICAL",
            "remote_estop_requested",
            context={"requested_by": requested_by, "reason": reason},
        )

    clear = consume_estop_signal(config.estop_clear_request_path)
    if clear is not None:
        cleared_by = str(clear.get("requested_by") or "operator")
        was_engaged = safety.engaged()
        safety.clear()
        runtime_db.record_event(
            "runtime",
            "WARN",
            "remote_estop_cleared",
            context={"cleared_by": cleared_by, "was_engaged": was_engaged},
        )


def _handle_persistent_cycle_failure(
    *,
    safety,
    human_gateway,
    consecutive_failures: int,
    already_tripped: bool,
    exc: Exception,
) -> bool:
    """Trip the interlock once when cycle failures persist; return the fired flag.

    The count was previously recorded but never acted on: a runtime failing
    every cycle would spin forever with a live machine and nobody told. The
    flag fires the trip exactly once per incident and is re-armed only after
    the operator clears the interlock (checked by the caller each cycle).
    """
    if consecutive_failures < RUNTIME_CYCLE_FAILURES_BEFORE_PERSISTENT or already_tripped:
        return already_tripped
    reason = (
        f"runtime cycle failed {consecutive_failures} consecutive times "
        f"({type(exc).__name__}: {exc})"
    )
    safety.trip(reason)
    human_gateway.call_human(
        title="Persistent runtime failure — interlock engaged",
        body=(
            f"The control loop has failed {consecutive_failures} cycles in a row; the machine has "
            f"been stopped and the interlock latched.\nLast error: {type(exc).__name__}: {exc}\n"
            "Investigate, make the machine safe, then clear-estop to resume."
        ),
        severity="critical",
        require_ack=True,
        context={"consecutive_failures": consecutive_failures, "exception_type": type(exc).__name__},
    )
    return True


def _world_is_actionable_print(world) -> bool:
    if not _world_has_active_print(world):
        return False
    printing_phase = str(world.facts.get("printer_1.printing_phase") or "").strip().lower()
    active_printing = world.facts.get("printer_1.active_printing")
    if isinstance(active_printing, bool):
        return active_printing
    return printing_phase == "active_printing"


def _plan_requests_tuning_action(plan: PlanIR, world) -> bool:
    if plan.decision != "EXECUTE":
        return False
    if plan.tuning_choice is not None:
        return True
    if not plan.sequence:
        return False
    action_by_id = {action.action_id: action for action in getattr(world, "frontier", [])}
    for action_id in plan.sequence:
        action = action_by_id.get(action_id)
        if action is not None and str(action.verb).startswith("TUNE_"):
            return True
        if str(action_id).startswith("A_PRUSA_TRIM_"):
            return True
    return False


def _world_pre_execution_terminal_guard_reason(world) -> str | None:
    if not _world_is_actionable_print(world):
        lifecycle = str(world.facts.get("printer_1.lifecycle") or "").strip() or "unknown"
        phase = str(world.facts.get("printer_1.printing_phase") or "").strip() or "unknown"
        return f"fresh world is no longer actionable (lifecycle={lifecycle}, phase={phase})"
    targets_nonzero = world.facts.get("printer_1.targets_nonzero")
    if targets_nonzero is False:
        return "fresh world reports zero nozzle/bed targets"
    progress = _float_or_none(world.facts.get("printer_1.job_progress_pct"))
    nozzle_target = _float_or_none(world.facts.get("printer_1.nozzle_target_c"))
    bed_target = _float_or_none(world.facts.get("printer_1.bed_target_c"))
    if (
        progress is not None
        and progress >= 99.0
        and nozzle_target is not None
        and nozzle_target <= 0.5
        and bed_target is not None
        and bed_target <= 0.5
    ):
        return f"fresh world is terminal ({progress:.1f}% complete, targets={nozzle_target:.1f}/{bed_target:.1f})"
    return None




def _should_stop(args, cycle_index: int) -> bool:
    if args.once:
        return True
    if not args.forever and args.cycles is not None and cycle_index >= args.cycles:
        return True
    return False


def run_control_loop(
    *,
    args,
    config: Config,
    archive,
    runtime_db,
    whiteboard,
    registry,
    world_compiler,
    human_gateway,
    safety,
    engine,
    planner,
    goal: str,
    poll_interval_s: float,
) -> None:
    """The runtime's cycle loop, extracted verbatim from main().

    Composition (build_runtime) and CLI concerns live in their own modules;
    the entrypoint e2e suite is what proves this split dropped no wiring.
    """
    cycle_index = 0
    consecutive_planner_failures = 0
    consecutive_cycle_failures = 0
    persistent_failure_tripped = False
    last_planner_attempt_monotonic: float | None = None
    while True:
        try:
            cycle_timing: dict[str, object] = {"cycle_started_at": _utc_now_iso()}
            _apply_remote_estop_requests(config, safety, runtime_db)
            if persistent_failure_tripped and not safety.engaged():
                # Operator cleared the interlock after a persistent-failure
                # trip: re-arm so a NEW incident can trip again.
                persistent_failure_tripped = False
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
                archive,
                cycle_index=cycle_index,
                phase="decision_input_ready",
                goal=goal,
                world=world,
                cycle_timing=cycle_timing,
            )
            _end_timed_step(cycle_timing, "decision_input_runtime_state_write", step_started)
            allowed_frontier_ids = _world_allowed_frontier_ids(world)
            tuning_action_space = _world_tuning_action_space(world)
            planner_option_count = _world_planner_option_count(world)
            cycle_timing["allowed_frontier_count"] = len(allowed_frontier_ids)
            cycle_timing["tuning_family_count"] = len(tuning_action_space)
            cycle_timing["planner_option_count"] = planner_option_count
            planner_exception: Exception | None = None
            stop_after_cycle = False
            bypass_reason: str | None = None
            if not _world_has_active_print(world):
                bypass_reason = "No active print is running, so skip planner calls."
                stop_after_cycle = not config.monitor_when_inactive
            elif not _world_is_actionable_print(world):
                bypass_reason = (
                    "A print is present but not in an actionable live-print phase yet, so skip planner calls until "
                    "active printing resumes."
                )
            elif planner_option_count == 0:
                bypass_reason = "No legal planner-visible actions are available, so skip planner calls."
            elif (
                config.planner_min_interval_seconds > 0.0
                and last_planner_attempt_monotonic is not None
                and (time.monotonic() - last_planner_attempt_monotonic) < config.planner_min_interval_seconds
            ):
                remaining_s = max(
                    0.0,
                    config.planner_min_interval_seconds - (time.monotonic() - last_planner_attempt_monotonic),
                )
                bypass_reason = (
                    "Planner cadence gate is holding to avoid over-calling the model; "
                    f"next slot in about {remaining_s:.1f}s."
                )
            if bypass_reason is not None:
                plan = _planner_bypass_no_action_plan(reason=bypass_reason)
                consecutive_planner_failures = 0
                cycle_timing["planner_bypassed"] = True
                cycle_timing["planner_bypass_reason"] = bypass_reason
                _mark_skipped_step(cycle_timing, "planner_call", reason=bypass_reason)
                runtime_db.record_event(
                    "planner",
                    "INFO",
                    "planner_call_bypassed",
                    context={
                        "cycle_index": cycle_index + 1,
                        "reason": bypass_reason,
                        "allowed_frontier_count": len(allowed_frontier_ids),
                        "tuning_family_count": len(tuning_action_space),
                        "planner_option_count": planner_option_count,
                        "has_active_print": _world_has_active_print(world),
                        "actionable_print": _world_is_actionable_print(world),
                        "monitor_when_inactive": config.monitor_when_inactive,
                    },
                )
            else:
                step_started = _begin_timed_step(cycle_timing, "planner_call")
                last_planner_attempt_monotonic = time.monotonic()
                try:
                    plan = plan_with_retry(planner, engine, world, goal, max_attempts=config.planner_retry_attempts)
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
                    "retry_attempts": config.planner_retry_attempts,
                }
                runtime_db.record_event("planner", "WARN", "planner_cycle_failed", context=failure_context)
                if consecutive_planner_failures < config.planner_failures_before_no_action:
                    cycle_timing["planner_failed"] = True
                    cycle_timing["planner_failed_exception"] = failure_context["exception_type"]
                    step_started = _begin_timed_step(cycle_timing, "planner_failure_runtime_state_write")
                    _write_runtime_state(
                        config,
                        archive,
                        cycle_index=cycle_index,
                        phase="planner_failed",
                        goal=goal,
                        world=world,
                        cycle_timing=cycle_timing,
                    )
                    _end_timed_step(cycle_timing, "planner_failure_runtime_state_write", step_started)
                    print(
                        "Cycle "
                        f"{cycle_index + 1}: planner failed after {config.planner_retry_attempts} attempts "
                        f"({failure_context['exception_type']}: {failure_context['exception_message']}); "
                        f"continuing without execution ({consecutive_planner_failures}/{config.planner_failures_before_no_action})"
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
            if _plan_requests_tuning_action(plan, world):
                step_started = _begin_timed_step(cycle_timing, "pre_execution_raw_publish")
                registry.publish_all_raw_state(whiteboard, mode="full")
                _end_timed_step(cycle_timing, "pre_execution_raw_publish", step_started)
                step_started = _begin_timed_step(cycle_timing, "pre_execution_compile")
                pre_execution_world = world_compiler.compile(goal, human_gateway.pending_messages())
                _end_timed_step(cycle_timing, "pre_execution_compile", step_started)
                guard_reason = _world_pre_execution_terminal_guard_reason(pre_execution_world)
                cycle_timing["pre_execution_guard_checked"] = True
                cycle_timing["pre_execution_allowed_frontier_count"] = len(
                    _world_allowed_frontier_ids(pre_execution_world)
                )
                cycle_timing["pre_execution_printing_phase"] = pre_execution_world.facts.get(
                    "printer_1.printing_phase"
                )
                cycle_timing["pre_execution_lifecycle"] = pre_execution_world.facts.get("printer_1.lifecycle")
                cycle_timing["pre_execution_targets_nonzero"] = pre_execution_world.facts.get(
                    "printer_1.targets_nonzero"
                )
                if guard_reason is not None:
                    original_plan = plan
                    plan = _planner_pre_execution_guard_no_action_plan(original_plan=original_plan, reason=guard_reason)
                    world = pre_execution_world
                    cycle_timing["pre_execution_terminal_guard_triggered"] = True
                    cycle_timing["pre_execution_terminal_guard_reason"] = guard_reason
                    cycle_timing["pre_execution_guard_original_plan"] = original_plan.model_dump()
                    runtime_db.record_event(
                        "runtime",
                        "INFO",
                        "pre_execution_terminal_guard_skipped_tuning",
                        context={
                            "cycle_index": cycle_index + 1,
                            "reason": guard_reason,
                            "original_plan": original_plan.model_dump(),
                            "fresh_lifecycle": pre_execution_world.facts.get("printer_1.lifecycle"),
                            "fresh_printing_phase": pre_execution_world.facts.get("printer_1.printing_phase"),
                            "fresh_targets_nonzero": pre_execution_world.facts.get("printer_1.targets_nonzero"),
                            "fresh_progress_pct": pre_execution_world.facts.get("printer_1.job_progress_pct"),
                            "fresh_nozzle_target_c": pre_execution_world.facts.get("printer_1.nozzle_target_c"),
                            "fresh_bed_target_c": pre_execution_world.facts.get("printer_1.bed_target_c"),
                        },
                    )
                else:
                    cycle_timing["pre_execution_terminal_guard_triggered"] = False
            else:
                _mark_skipped_step(cycle_timing, "pre_execution_raw_publish", reason="plan_has_no_tuning_action")
                _mark_skipped_step(cycle_timing, "pre_execution_compile", reason="plan_has_no_tuning_action")
                cycle_timing["pre_execution_guard_checked"] = False
            step_started = _begin_timed_step(cycle_timing, "execution")
            report = engine.execute_plan(goal, world, plan)
            _end_timed_step(cycle_timing, "execution", step_started)
            cycle_timing["execution_finished_at"] = _utc_now_iso()
            step_started = _begin_timed_step(cycle_timing, "safety_poll")
            if safety.poll():
                runtime_db.record_event(
                    "runtime",
                    "CRITICAL",
                    "safety_interlock_engaged",
                    context={"reason": safety.interlock.reason},
                )
            _write_heartbeat_beacon(
                config,
                cycle_index=cycle_index,
                job_active=_world_has_active_print(world),
            )
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
                archive,
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
                archive,
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
            if stop_after_cycle:
                print("  runtime stopping because no active print is running")
                runtime_db.record_event(
                    "runtime",
                    "INFO",
                    "runtime_stopped_inactive",
                    context={"cycle_index": cycle_index + 1, "reason": "no_active_print"},
                )
                break
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
            persistent_failure_tripped = _handle_persistent_cycle_failure(
                safety=safety,
                human_gateway=human_gateway,
                consecutive_failures=consecutive_cycle_failures,
                already_tripped=persistent_failure_tripped,
                exc=exc,
            )
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
