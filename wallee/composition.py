"""Runtime composition: build, reset, and reconcile the component graph."""

from __future__ import annotations

import json
from pathlib import Path

from .config import Config
from .engine import Engine
from .human import HumanGateway
from .models import ActionRunStatus
from .planner import HeuristicPlanner, OpenRouterPlanner, Planner
from .planning_context import WorldCompiler
from .predicates import PredicateEvaluator
from .registry import PackRegistry
from .runtime_db import RuntimeDB
from .safety import SafetyKernel
from .stop_transport import execute_stop
from .whiteboard import InMemoryWhiteboard


def _wire_stop_transport(config: Config, registry, safety, human_gateway) -> None:
    """Register a physical-stop callback on the interlock from pack profiles.

    Each enabled pack may declare a declarative `safety_profile` in its
    manifest. We persist those profiles to disk (so the independent watchdog
    can stop the machine even if the runtime never started) and register one
    trip callback that fires every profile's stop and escalates the result.
    Without this, `trip()` engages the latch but nothing physically stops —
    the exact "decorative safety kernel" defect this closes.
    """
    profiles = [
        manifest.safety_profile.model_dump()
        for manifest in registry.manifests()
        if manifest.safety_profile is not None and manifest.safety_profile.transport != "none"
    ]
    # Persist for the out-of-process watchdog (Phase 2.4). Written even when
    # empty so a stale profile from a previous config cannot linger.
    profile_path = config.safety_dir / "profile.json"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(json.dumps(profiles, indent=2, sort_keys=True), encoding="utf-8")
    if not profiles:
        return

    def _on_trip(reason: str) -> None:
        for profile in profiles:
            outcome = execute_stop(profile, data_dir=config.data_dir)
            human_gateway.call_human(
                title="Safety interlock engaged — machine stop issued",
                body=f"reason: {reason}\ntransport: {profile.get('transport')}\nresult: {outcome.detail}",
                severity="critical",
                require_ack=True,
                context={"reason": reason, "stop_ok": outcome.ok, "detail": outcome.detail},
            )

    safety.register_interlock_callback(_on_trip)


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
    safety = SafetyKernel(
        heartbeat_timeout_s=config.heartbeat_timeout_seconds,
        latch_path=config.estop_latch_path,
    )
    _wire_stop_transport(config, registry, safety, human_gateway)
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
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{config.db_path}{suffix}") if suffix else config.db_path
        if suffix and path.exists():
            path.unlink()
    if config.control_lock_path.exists():
        config.control_lock_path.unlink()


def reconcile_runtime_start_state(config: Config) -> dict[str, int]:
    """Sweep crash residue from a previous run before the runtime starts.

    A run that died mid-cycle can leave rows that silently poison the next
    run: DISPATCHED rows hold their locks forever (the world compiler derives
    held locks from durable DISPATCHED runs, so pause/cancel-class actions
    vanish from every future frontier), and PROPOSED/AUTHORIZED rows describe
    work nobody is doing. Dispatched work may or may not have reached
    hardware, so it is marked UNKNOWN and escalated to a human rather than
    guessed at.
    """
    swept = {"unknown": 0, "aborted": 0}
    if not config.db_path.exists():
        return swept
    runtime_db = RuntimeDB(config.db_path)
    try:
        human_gateway = HumanGateway(config=config, runtime_db=runtime_db)
        for run in runtime_db.list_action_runs_by_status(ActionRunStatus.DISPATCHED):
            runtime_db.transition_action_run(
                run.action_run_id,
                ActionRunStatus.UNKNOWN,
                error={"reason": "runtime restarted while this action was DISPATCHED; outcome unknown"},
            )
            runtime_db.record_event(
                "runtime",
                "CRITICAL",
                "boot_reconcile_dispatched_outcome_unknown",
                context={"action_run_id": run.action_run_id, "action_id": run.action_id, "verb": run.verb},
            )
            human_gateway.call_human(
                title=f"Crash recovery: outcome of {run.action_id} is unknown",
                body=(
                    f"The runtime restarted while action run {run.action_run_id} ({run.verb} via "
                    f"{run.action_id}) was DISPATCHED. The side effect may or may not have reached "
                    "the machine. Verify machine state before resuming unattended operation."
                ),
                severity="critical",
                require_ack=True,
                context={"action_run_id": run.action_run_id, "action_id": run.action_id},
            )
            swept["unknown"] += 1
        for stale_status in (ActionRunStatus.PROPOSED, ActionRunStatus.AUTHORIZED):
            for run in runtime_db.list_action_runs_by_status(stale_status):
                runtime_db.transition_action_run(
                    run.action_run_id,
                    ActionRunStatus.ABORTED,
                    error={"reason": f"stale {stale_status.value} run from a previous runtime start"},
                )
                swept["aborted"] += 1
        # An IN_FLIGHT exec-journal row means the pack committed the durability
        # barrier and may have started a hardware side effect before the crash.
        # Finalize it UNKNOWN so a retry of the same idempotency key does not
        # assume the effect never happened.
        swept["journal_unknown"] = runtime_db.finalize_in_flight_journal_unknown()
        if swept["unknown"] or swept["aborted"] or swept.get("journal_unknown"):
            runtime_db.record_event("runtime", "WARN", "boot_reconcile_summary", context=dict(swept))
    finally:
        runtime_db.close()
    return swept


def build_planner(config: Config, repo_root: Path) -> Planner:
    """Return the selected planner backend."""
    if config.planner_backend == "openrouter" and config.openrouter_api_key:
        return OpenRouterPlanner(config, repo_root / "schemas" / "plan_ir.schema.json")
    return HeuristicPlanner()
