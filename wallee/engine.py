"""Deterministic execution engine for Wallee v6.5."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Any, Callable

from .coerce import (
    normalize_issue_level as _normalize_issue_level,
    normalize_strength as _normalize_strength,
    split_pipe as _split_fact_text,
)
from .config import Config
from .human import HumanGateway
from .ids import action_args_hash, idempotency_key, new_id
from .models import (
    ActionRun,
    ActionRunStatus,
    ExecutionResult,
    LegalAction,
    PlanIR,
    PlanRecord,
    WorldPacket,
    _action_direction_from_id,
    _action_family_from_id,
    _action_magnitude_from_id,
)
from .planning_context import WorldCompiler, run_scope_from_facts as _run_scope_from_facts
from .predicates import PredicateEvaluator
from .registry import PackRegistry
from .runtime_db import RuntimeDB
from .runtime_control import ControlLease
from .safety import SafetyKernel
from .whiteboard import BaseWhiteboard


def _run_scope_from_world(world: WorldPacket) -> str | None:
    return _run_scope_from_facts(world.facts)


@dataclass
class ExecutionReport:
    """High-level outcome of executing one plan decision."""

    plan_id: str
    executed_action_ids: list[str] = field(default_factory=list)
    blocked_action_run_id: str | None = None
    replan_required: bool = False
    failed_action_run_id: str | None = None
    human_request_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    post_execution_world: WorldPacket | None = None


class LockManager:
    """Simple in-process lock manager for resource domains.

    Because the reference runtime is single-process for control, an in-memory
    lock manager is enough to prevent accidental concurrent dispatch.  If the
    runtime ever becomes multi-process, this is one of the first places that
    must grow a durable coordination layer.
    """

    def __init__(self) -> None:
        self._held: set[str] = set()
        self._lock = threading.RLock()

    def acquire(self, keys: list[str]) -> bool:
        with self._lock:
            if any(key in self._held for key in keys):
                return False
            self._held.update(keys)
            return True

    def release(self, keys: list[str]) -> None:
        with self._lock:
            for key in keys:
                self._held.discard(key)


class Engine:
    """Deterministic execution engine.

    The engine does not talk to the internet and does not ask the LLM follow-up
    questions.  Its job is to be the boring part of the system that turns an
    untrusted plan proposal into either verified execution or a safe refusal.
    """

    def __init__(
        self,
        *,
        config: Config,
        runtime_db: RuntimeDB,
        whiteboard: BaseWhiteboard,
        registry: PackRegistry,
        world_compiler: WorldCompiler,
        human_gateway: HumanGateway,
        safety: SafetyKernel,
        predicate_evaluator: PredicateEvaluator | None = None,
        control_lease: ControlLease | None = None,
        now_ms_fn: Callable[[], int] | None = None,
    ) -> None:
        self.config = config
        self.runtime_db = runtime_db
        self.whiteboard = whiteboard
        self.registry = registry
        self.world_compiler = world_compiler
        self.human_gateway = human_gateway
        self.safety = safety
        self.predicate_evaluator = predicate_evaluator or PredicateEvaluator()
        self.lock_manager = LockManager()
        self.control_lease = control_lease or ControlLease(config.control_lock_path)
        # Injectable wall clock: approval-wait expiry must be testable without
        # real elapsed time.
        self._now_ms = now_ms_fn or (lambda: int(time.time() * 1000))

    def validate_plan(self, world: WorldPacket, plan: PlanIR) -> list[LegalAction]:
        """Validate that *plan* only references current frontier actions."""
        plan = self.materialize_plan(world, plan)
        frontier = {action.action_id: action for action in world.frontier}

        if plan.decision == "NO_ACTION":
            return []

        if plan.decision == "CALL_HUMAN":
            return []

        if not plan.sequence:
            raise ValueError("EXECUTE decision requires at least one action ID")

        # Validate against what the planner was actually shown, not the full
        # frontier: an explicit action truncated out of the prompt is not a
        # legal selection even though it still exists in the frontier.
        visible = world.planner_visible_action_ids()
        actions: list[LegalAction] = []
        for action_id in plan.sequence:
            if action_id not in frontier:
                raise ValueError(f"plan referenced unknown frontier action {action_id!r}")
            if action_id not in visible:
                raise ValueError(f"plan referenced action {action_id!r} that was not offered to the planner")
            actions.append(frontier[action_id])
        return actions

    def materialize_plan(self, world: WorldPacket, plan: PlanIR) -> PlanIR:
        """Resolve compact tuning choices into one exact legal frontier action."""
        if plan.decision != "EXECUTE" or plan.tuning_choice is None:
            return plan
        matches = [
            action.action_id
            for action in world.frontier
            if _action_family_from_id(action.action_id) == plan.tuning_choice.family
            and _action_direction_from_id(action.action_id) == plan.tuning_choice.direction
            and _action_magnitude_from_id(action.action_id) == plan.tuning_choice.magnitude
        ]
        if plan.sequence:
            if len(matches) == 1:
                return plan.model_copy(update={"sequence": [matches[0]], "tuning_choice": None})
            return PlanIR(
                decision="NO_ACTION",
                sequence=[],
                tuning_choice=None,
                why=(
                    "Malformed planner output included both an explicit sequence and a tuning choice, and the tuning "
                    "choice did not resolve cleanly to one legal action. Skip this cycle."
                ),
                call_human_message=None,
            )
        if len(matches) != 1:
            raise ValueError(
                "compact tuning choice did not resolve to exactly one legal frontier action "
                f"({plan.tuning_choice.family=} {plan.tuning_choice.direction=} "
                f"{plan.tuning_choice.magnitude=} matches={matches})"
            )
        return plan.model_copy(update={"sequence": [matches[0]], "tuning_choice": None})

    def execute_plan(self, goal: str, world: WorldPacket, plan: PlanIR) -> ExecutionReport:
        """Execute a short-horizon plan.

        The plan itself is stored first for auditability.  That means even a
        later refusal or approval block still leaves a durable record of what the
        planner wanted.
        """
        # Resolve any runs parked awaiting approval from a previous cycle
        # before considering fresh work — a recorded approval must actually
        # be consumed, and an ESTOP must dispose of stale pending approvals.
        self.resume_pending_approvals(goal)

        plan = self.materialize_plan(world, plan)
        plan_id = new_id("plan")
        run_scope = _run_scope_from_world(world)
        self.runtime_db.store_plan(
            PlanRecord(
                plan_id=plan_id,
                run_scope=run_scope,
                goal=goal,
                world_packet=world.prompt_view(),
                world_compilation=world.compilation.model_dump() if world.compilation is not None else {},
                plan_ir=plan.model_dump(),
                created_ts_ms=int(time.time() * 1000),
            )
        )
        report = ExecutionReport(plan_id=plan_id)

        if plan.decision == "NO_ACTION":
            report.notes.append("planner selected no action")
            self.runtime_db.record_event("engine", "INFO", "no_action", context={"plan_id": plan_id})
            self.safety.beat()
            return report

        if plan.decision == "CALL_HUMAN":
            request_id = self.human_gateway.call_human(
                title="Planner escalation",
                body=plan.call_human_message or plan.why,
                severity="warn",
                require_ack=False,
                context={"plan_id": plan_id},
            )
            report.human_request_ids.append(request_id)
            self.safety.beat()
            return report

        actions = self.validate_plan(world, plan)
        return self._execute_actions(plan_id, goal, actions, refresh_from_frontier=True)

    def execute_operator_action(self, goal: str, world: WorldPacket, action: LegalAction, *, why: str = "operator-triggered managed proof") -> ExecutionReport:
        """Execute one bounded action without LLM choice.

        This path is for operator-managed proofs.  The action must already be a
        bounded, deterministic action object supplied by code, not free-form
        text from a planner.
        """
        plan_id = new_id("plan")
        run_scope = _run_scope_from_world(world)
        plan = PlanIR(decision="EXECUTE", sequence=[action.action_id], why=why)
        self.runtime_db.store_plan(
            PlanRecord(
                plan_id=plan_id,
                run_scope=run_scope,
                goal=goal,
                world_packet=world.prompt_view(),
                world_compilation=world.compilation.model_dump() if world.compilation is not None else {},
                plan_ir=plan.model_dump(),
                created_ts_ms=int(time.time() * 1000),
            )
        )
        return self._execute_actions(plan_id, goal, [action], refresh_from_frontier=False)

    def _execute_actions(self, plan_id: str, goal: str, actions: list[LegalAction], *, refresh_from_frontier: bool) -> ExecutionReport:
        report = ExecutionReport(plan_id=plan_id)
        if actions and not self.control_lease.acquire():
            report.replan_required = True
            report.notes.append("control lease unavailable")
            self.runtime_db.record_event(
                "engine",
                "WARN",
                "control_lease_unavailable",
                context={"plan_id": plan_id, "lock_path": str(self.config.control_lock_path)},
            )
            return report
        for action in actions:
            # Prove liveness on every pass, before any work and regardless of
            # how this iteration ends: a beat that only fires on success lets
            # the watchdog mistake a failing loop for a wedged one.
            self.safety.beat()

            # Interlock gate — re-checked per action so a stop that lands
            # mid-sequence (from the watchdog, a stale heartbeat, or an
            # operator) halts the remaining actions, not just the first.
            if self.safety.engaged():
                report.replan_required = True
                report.notes.append(f"blocked by safety interlock: {self.safety.interlock.reason}")
                self.runtime_db.record_event(
                    "engine",
                    "CRITICAL",
                    "dispatch_blocked_interlock",
                    context={
                        "plan_id": plan_id,
                        "action_id": action.action_id,
                        "reason": self.safety.interlock.reason,
                    },
                )
                break

            if refresh_from_frontier:
                current_world = self.world_compiler.compile(goal, self.human_gateway.pending_messages())
                refreshed = {candidate.action_id: candidate for candidate in current_world.frontier}
                if action.action_id not in refreshed:
                    report.replan_required = True
                    report.notes.append(f"frontier no longer contains {action.action_id}")
                    self.runtime_db.record_event(
                        "engine",
                        "WARN",
                        "replan_required_frontier_changed",
                        context={"plan_id": plan_id, "action_id": action.action_id},
                    )
                    break
                action = refreshed[action.action_id]

            run = self._create_action_run(plan_id, action)

            if not self.lock_manager.acquire(action.required_locks):
                self.runtime_db.transition_action_run(run.action_run_id, ActionRunStatus.REPLAN_REQUIRED)
                report.replan_required = True
                report.notes.append(f"lock conflict on {action.required_locks}")
                break

            try:
                if action.approval_required:
                    self.runtime_db.transition_action_run(run.action_run_id, ActionRunStatus.WAITING_APPROVAL)
                    approved = self.human_gateway.request_approval(
                        action_run_id=run.action_run_id,
                        args_hash=run.args_hash,
                        hazard_class=action.hazard_class,
                        reason=f"{action.verb} requires approval: {action.description}",
                    )
                    if not approved:
                        report.blocked_action_run_id = run.action_run_id
                        report.notes.append(f"waiting for approval for {run.action_run_id}")
                        break

                self._authorize_toctou_and_dispatch(run, action, goal, plan_id, report)
                if report.replan_required or report.failed_action_run_id:
                    break

                self.safety.beat()
            except Exception as exc:
                # A dispatch that raises unexpectedly is a failed pass, not a
                # dead loop: record it, prove liveness, and stop the sequence.
                self.runtime_db.record_event(
                    "engine",
                    "ERROR",
                    "dispatch_raised",
                    context={"plan_id": plan_id, "action_id": action.action_id, "error": str(exc)},
                )
                report.failed_action_run_id = run.action_run_id
                report.notes.append(f"dispatch raised for {action.action_id}: {exc}")
                self.safety.beat()
                break
            finally:
                self.lock_manager.release(action.required_locks)
        return report

    def _authorize_toctou_and_dispatch(
        self, run: ActionRun, action: LegalAction, goal: str, plan_id: str, report: ExecutionReport
    ) -> None:
        """Advance an authorized-or-approved run to DISPATCHED and execute it.

        The single shared dispatch tail used by both the normal path and the
        approval-resume path, so a resumed run passes through exactly the same
        TOCTOU re-check, journal barrier, and finalization as a fresh one.
        The caller must already hold the action's locks and have cleared the
        interlock gate.
        """
        self.runtime_db.transition_action_run(run.action_run_id, ActionRunStatus.AUTHORIZED)

        latest_world = self.world_compiler.compile(goal, self.human_gateway.pending_messages())
        if not self.predicate_evaluator.evaluate(action.preconditions, latest_world.facts):
            self.runtime_db.transition_action_run(run.action_run_id, ActionRunStatus.REPLAN_REQUIRED)
            report.replan_required = True
            report.notes.append(f"TOCTOU predicate failed for {action.action_id}")
            return

        self.runtime_db.transition_action_run(run.action_run_id, ActionRunStatus.DISPATCHED)

        if action.owner_pack == "builtin":
            result = self._execute_builtin(action, goal)
        else:
            pack = self.registry.get(action.owner_pack)
            result = pack.execute(
                action=action,
                action_run_id=run.action_run_id,
                idempotency_key=run.idempotency_key,
                args_hash=run.args_hash,
                db=self.runtime_db,
                whiteboard=self.whiteboard,
            )

        outcome = self._finalize_action(run.action_run_id, action, goal, result)
        report.executed_action_ids.append(action.action_id)
        report.notes.extend(outcome.notes)
        report.human_request_ids.extend(outcome.human_request_ids)
        if outcome.post_execution_world is not None:
            report.post_execution_world = outcome.post_execution_world
        if outcome.replan_required:
            report.replan_required = True
        if outcome.failed_action_run_id:
            report.failed_action_run_id = outcome.failed_action_run_id

    def _resolve_action_for_run(self, run: ActionRun, goal: str) -> LegalAction | None:
        """Find the current frontier action a WAITING_APPROVAL run refers to.

        Recompiling the world gives the full action (execute_ref, preconditions)
        the durable run row does not store, and confirms the approved action is
        still legal before it is resumed.
        """
        world = self.world_compiler.compile(goal, self.human_gateway.pending_messages())
        for candidate in world.frontier:
            if candidate.action_id == run.action_id and action_args_hash(candidate.verb, candidate.args) == run.args_hash:
                return candidate
        return None

    def resume_pending_approvals(self, goal: str) -> ExecutionReport:
        """Resolve runs parked in WAITING_APPROVAL from a previous cycle.

        For each parked run, in priority order:
          1. ESTOP dominates — a tripped interlock aborts the run, approved or not.
          2. A valid, matching approval resumes it through the shared gated
             dispatch path (exactly once).
          3. An unanswered approval past the wait window is aborted and the
             operator is asked to re-approve rather than leaving it forever.
        """
        report = ExecutionReport(plan_id="approval-resume")
        pending = self.runtime_db.list_action_runs_by_status(ActionRunStatus.WAITING_APPROVAL)
        if not pending:
            return report
        if not self.control_lease.acquire():
            report.notes.append("control lease unavailable for approval resume")
            return report
        try:
            for run in pending:
                if self.safety.engaged():
                    self._abort_run(run, "interlock engaged while awaiting approval", report, event="approval_aborted_interlock")
                    continue

                if self.runtime_db.has_valid_approval(run.action_run_id, run.args_hash):
                    action = self._resolve_action_for_run(run, goal)
                    if action is None:
                        self._abort_run(run, "approved action no longer in frontier", report, event="approval_aborted_stale_frontier")
                        continue
                    if not self.lock_manager.acquire(action.required_locks):
                        report.notes.append(f"lock conflict resuming {run.action_run_id}; will retry")
                        continue
                    try:
                        self._authorize_toctou_and_dispatch(run, action, goal, run.plan_id, report)
                        self.safety.beat()
                    finally:
                        self.lock_manager.release(action.required_locks)
                    continue

                age_ms = self._now_ms() - run.updated_ts_ms
                if age_ms > self.config.approval_wait_window_seconds * 1000:
                    self._abort_run(run, "approval expired unanswered", report, event="approval_expired")
                    self.human_gateway.call_human(
                        title="Approval expired — re-approval needed",
                        body=(
                            f"Action {run.action_id} ({run.verb}) waited past the approval window "
                            f"({self.config.approval_wait_window_seconds}s) with no operator decision and was aborted. "
                            "Re-propose and approve if the intervention is still wanted."
                        ),
                        severity="warn",
                        require_ack=True,
                        context={"action_run_id": run.action_run_id, "action_id": run.action_id},
                    )
        finally:
            self.control_lease.release()
        return report

    def _abort_run(self, run: ActionRun, reason: str, report: ExecutionReport, *, event: str) -> None:
        self.runtime_db.transition_action_run(run.action_run_id, ActionRunStatus.ABORTED, error={"reason": reason})
        self.runtime_db.record_event(
            "engine",
            "CRITICAL" if "interlock" in event else "WARN",
            event,
            context={"action_run_id": run.action_run_id, "action_id": run.action_id, "reason": reason},
        )
        report.notes.append(f"aborted {run.action_run_id}: {reason}")

    def close(self) -> None:
        self.control_lease.release()

    def __del__(self) -> None:  # pragma: no cover - defensive cleanup
        try:
            self.close()
        except Exception:
            pass

    def _create_action_run(self, plan_id: str, action: LegalAction) -> ActionRun:
        now_ms = int(time.time() * 1000)
        run_scope = self.runtime_db.get_plan_run_scope(plan_id)
        args_hash = action_args_hash(action.verb, action.args)
        run = ActionRun(
            action_run_id=new_id("run"),
            plan_id=plan_id,
            run_scope=run_scope,
            action_id=action.action_id,
            verb=action.verb,
            owner_pack=action.owner_pack,
            args=action.args,
            args_hash=args_hash,
            idempotency_key=idempotency_key(plan_id, action.action_id, action.verb, args_hash),
            required_locks=action.required_locks,
            status=ActionRunStatus.PROPOSED,
            hazard_class=action.hazard_class,
            verify=action.verify,
            expected_delta=action.expected_delta,
            target_device=action.target_device,
            created_ts_ms=now_ms,
            updated_ts_ms=now_ms,
        )
        self.runtime_db.create_action_run(run)
        return run

    def _execute_builtin(self, action: LegalAction, goal: str) -> ExecutionResult:
        if action.verb == "WAIT_UNTIL":
            raw_timeout_s = action.args.get("timeout_s", 1)
            timeout_s = 1.0 if raw_timeout_s is None else max(0.0, float(raw_timeout_s))
            raw_slice_s = action.args.get("slice_s", 2.0)
            slice_s = 2.0 if raw_slice_s is None else max(0.0, float(raw_slice_s))
            nonfatal_timeout = bool(action.args.get("nonfatal_timeout", False))
            wait_slice_s = min(timeout_s, slice_s)
            deadline = time.monotonic() + wait_slice_s
            predicate = action.verify or action.preconditions
            while time.monotonic() < deadline:
                world = self.world_compiler.compile(goal, self.human_gateway.pending_messages())
                if self.predicate_evaluator.evaluate(predicate, world.facts):
                    return ExecutionResult(status="success", result={"waited": True})
                remaining_s = deadline - time.monotonic()
                if remaining_s <= 0.0:
                    break
                time.sleep(min(0.05, remaining_s))
            if nonfatal_timeout:
                return ExecutionResult(
                    status="success",
                    result={
                        "waited": True,
                        "predicate_satisfied": False,
                        "wait_pending": True,
                        "wait_slice_s": wait_slice_s,
                        "wait_timed_out": True,
                        "nonfatal_timeout": True,
                    },
                )
            return ExecutionResult(status="failed", error={"reason": "wait_until_timeout"})

        if action.verb == "CALL_HUMAN":
            request_id = self.human_gateway.call_human(
                title="Operator attention requested",
                body=str(action.args.get("message", action.description)),
                severity="warn",
                require_ack=False,
            )
            return ExecutionResult(status="success", result={"request_id": request_id})

        if action.verb == "WAIT":
            duration_s = float(action.args.get("seconds", 1))
            time.sleep(max(0.0, min(duration_s, 5.0)))
            return ExecutionResult(status="success", result={"slept_s": duration_s})

        return ExecutionResult(status="failed", error={"reason": f"unknown builtin verb {action.verb}"})

    def _finalize_action(
        self,
        action_run_id: str,
        action: LegalAction,
        goal: str,
        result: ExecutionResult,
    ) -> ExecutionReport:
        report = ExecutionReport(plan_id="")
        if result.status == "in_flight":
            self.runtime_db.transition_action_run(action_run_id, ActionRunStatus.UNKNOWN, error=result.error)
            self.safety.trip("daemon reported in_flight ambiguity")
            request_id = self.human_gateway.call_human(
                title="Unsafe ambiguity detected",
                body=f"{action.verb} may already have started; manual intervention required",
                severity="critical",
                require_ack=True,
                context={"action_run_id": action_run_id},
            )
            report.human_request_ids.append(request_id)
            report.failed_action_run_id = action_run_id
            return report

        if result.status in {"failed", "expired"}:
            self.runtime_db.transition_action_run(action_run_id, ActionRunStatus.FAILED, error=result.error)
            report.failed_action_run_id = action_run_id
            report.notes.append(f"execution failed for {action.action_id}")
            return report

        self._publish_raw_state(mode="verify")
        verified_world = self.world_compiler.compile(goal, self.human_gateway.pending_messages())
        wait_pending = (
            action.verb == "WAIT_UNTIL"
            and isinstance(result.result, dict)
            and bool(result.result.get("nonfatal_timeout"))
        )
        if wait_pending:
            self.runtime_db.transition_action_run(
                action_run_id,
                ActionRunStatus.DONE,
                result=_enrich_result_with_verified_world(result.result, verified_world),
            )
            report.post_execution_world = verified_world
            report.notes.append(f"wait still pending for {action.action_id}")
            return report
        if self._verify_action(action, verified_world):
            self.runtime_db.transition_action_run(
                action_run_id,
                ActionRunStatus.DONE,
                result=_enrich_result_with_verified_world(result.result, verified_world),
            )
            report.post_execution_world = verified_world
            return report

        self.runtime_db.transition_action_run(
            action_run_id,
            ActionRunStatus.REPLAN_REQUIRED,
            error={"reason": "verification_mismatch", "expected_delta": action.expected_delta},
        )
        report.replan_required = True
        report.notes.append(f"verification mismatch for {action.action_id}")
        return report

    def _verify_action(self, action: LegalAction, world: WorldPacket) -> bool:
        if action.verify is not None:
            return self.predicate_evaluator.evaluate(action.verify, world.facts)
        if action.expected_delta:
            return self.predicate_evaluator.evaluate({"all": action.expected_delta}, world.facts)
        return True

    def _publish_raw_state(self, *, mode: str) -> None:
        publish = getattr(self.registry, "publish_all_raw_state")
        try:
            publish(self.whiteboard, mode=mode)
        except TypeError as exc:
            if "unexpected keyword argument 'mode'" not in str(exc):
                raise
            publish(self.whiteboard)


def _enrich_result_with_verified_world(result: dict[str, Any] | None, world: WorldPacket) -> dict[str, Any]:
    payload = dict(result or {})
    payload["post_action_vision_signal"] = _world_vision_signal(world)
    payload["post_action_world"] = {
        "compiled_at": world.compilation.compiled_at if world.compilation is not None else None,
        "compiled_ts_ms": world.compilation.compiled_ts_ms if world.compilation is not None else None,
        "job_progress_pct": world.facts.get("printer_1.job_progress_pct"),
        "raw_observed_at": world.facts.get("printer_1.raw_observed_at"),
        "vision_observed_at": world.facts.get("printer_1.vision_observed_at"),
    }
    return payload


def _world_vision_signal(world: WorldPacket) -> dict[str, Any]:
    facts = world.facts
    summary = str(facts.get("printer_1.vision_advisory_summary") or "").strip() or None
    finding_types = _split_fact_text(facts.get("printer_1.vision_advisory_finding_types"))
    usable = bool(facts.get("printer_1.nozzle_cam_usable", False)) and bool(summary)
    strength = _normalize_strength(facts.get("printer_1.vision_advisory_strength"))
    issue_level = _normalize_issue_level(facts.get("printer_1.vision_advisory_issue_level"))
    comparison_delta = str(facts.get("printer_1.vision_comparison_delta") or "").strip() or None
    comparison_confidence = _normalize_strength(facts.get("printer_1.vision_comparison_confidence"))
    comparison_summary = str(facts.get("printer_1.vision_comparison_summary") or "").strip() or None
    return {
        "usable": usable,
        "summary": summary if usable else None,
        "finding_types": finding_types if usable else [],
        "strength": strength if usable else None,
        "issue_level": issue_level if usable else None,
        "comparison_delta": comparison_delta if usable else None,
        "comparison_confidence": comparison_confidence if usable else None,
        "comparison_summary": comparison_summary if usable else None,
    }


