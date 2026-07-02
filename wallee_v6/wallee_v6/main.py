"""CLI entry point for the lean Wallee v6.5 reference runtime."""

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
from .models import ActionRunStatus, PlanIR
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


class _RuntimeArchive:
    """Manage append-only per-run artifacts while preserving live inspection paths."""

    def __init__(self, config: Config, *, repo_root: Path) -> None:
        self.config = config
        self.repo_root = repo_root
        self.started_at = datetime.now(timezone.utc)
        self.run_id = self.started_at.strftime("%Y-%m-%dT%H%M%SZ") + f"-pid{os.getpid()}"
        self.runs_root = config.data_dir / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.run_dir = self.runs_root / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_cycles_dir = self.run_dir / "runtime_cycles"
        self.runtime_cycles_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_logs_dir = self.run_dir / "runtime_logs"
        self.runtime_logs_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_state_history_dir = self.run_dir / "runtime_state_history"
        self.runtime_state_history_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_state_current_path = self.run_dir / "runtime_state.json"
        self.vision_bundle_root = self.run_dir / "vision_bundle"
        self.vision_bundle_root.mkdir(parents=True, exist_ok=True)
        self._copied_refs: set[str] = set()
        self._job_scope_tokens: set[str] = set()
        self._write_run_meta()
        self._refresh_live_views()

    def _write_run_meta(self) -> None:
        payload = {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
            "pid": os.getpid(),
            "data_dir": str(self.config.data_dir),
            "repo_root": str(self.repo_root),
            "remote_code": str(self.repo_root),
            "run_dir": str(self.run_dir),
            "job_scope_tokens": sorted(self._job_scope_tokens),
        }
        (self.run_dir / "run_meta.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _archive_existing_live_path(self, path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        if path.is_symlink():
            path.unlink()
            return
        legacy_root = self.runs_root / f"legacy-{path.name}-{self.started_at.strftime('%Y-%m-%dT%H%M%SZ')}"
        legacy_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(legacy_root / path.name))

    def _replace_live_symlink(self, live_path: Path, target: Path) -> None:
        self._archive_existing_live_path(live_path)
        live_path.symlink_to(target, target_is_directory=target.is_dir())

    def _refresh_live_views(self) -> None:
        self._replace_live_symlink(self.config.data_dir / "runtime_cycles", self.runtime_cycles_dir)
        self._replace_live_symlink(self.config.data_dir / "runtime_logs", self.runtime_logs_dir)
        current_run = self.config.data_dir / "current_run"
        self._replace_live_symlink(current_run, self.run_dir)
        latest_run_path = self.config.data_dir / "latest_run.json"
        latest_run_path.write_text(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "run_dir": str(self.run_dir),
                    "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def observe_job_scope(self, value: object) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        token = value.strip()
        if token in self._job_scope_tokens:
            return
        self._job_scope_tokens.add(token)
        alias_root = self.runs_root / "by_job_scope" / _safe_slug(token)
        alias_root.mkdir(parents=True, exist_ok=True)
        alias_path = alias_root / self.run_id
        if alias_path.exists() or alias_path.is_symlink():
            alias_path.unlink()
        alias_path.symlink_to(self.run_dir, target_is_directory=True)
        self._write_run_meta()

    def runtime_log_path(self) -> Path:
        return self.runtime_logs_dir / f"continuous-runtime-{self.started_at.strftime('%Y-%m-%dT%H%M%SZ')}-pid{os.getpid()}.log"

    def write_runtime_state_history(self, *, cycle_index: int, phase: str, payload: dict[str, object]) -> Path:
        ts = payload.get("ts") if isinstance(payload.get("ts"), str) else _utc_now_iso()
        stem = str(ts).replace(":", "").replace("+00:00", "Z")
        out_path = self.runtime_state_history_dir / f"{stem}-cycle-{cycle_index + 1:04d}-{_safe_slug(phase)}.json"
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        self.runtime_state_current_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return out_path

    def _resolve_ref_path(self, ref: str) -> Path | None:
        raw = Path(ref)
        candidates = [raw] if raw.is_absolute() else [self.repo_root / raw, self.config.data_dir / raw]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _copy_ref(self, *, ref: str | None, kind: str) -> str | None:
        if not isinstance(ref, str) or not ref.strip():
            return None
        normalized = ref.strip()
        source_path = self._resolve_ref_path(normalized)
        if source_path is None:
            return None
        destination_dir = self.vision_bundle_root / kind
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / source_path.name
        if normalized not in self._copied_refs:
            shutil.copy2(source_path, destination)
            self._copied_refs.add(normalized)
        return str(destination)

    def capture_referenced_artifacts(self, *, decision_payload: dict[str, object], post_execution_payload: dict[str, object]) -> dict[str, list[str]]:
        refs: dict[str, list[str]] = {"observations": [], "replays": [], "frames": []}
        seen = {key: set() for key in refs}
        for payload in (decision_payload, post_execution_payload):
            facts = payload.get("facts")
            if not isinstance(facts, dict):
                continue
            observation_ref = facts.get("printer_1.vision_observation_ref")
            frame_ref = facts.get("printer_1.vision_frame_ref")
            for kind, value in (("observations", observation_ref), ("frames", frame_ref)):
                copied = self._copy_ref(ref=value if isinstance(value, str) else None, kind=kind)
                if copied and copied not in seen[kind]:
                    seen[kind].add(copied)
                    refs[kind].append(copied)
            if isinstance(observation_ref, str) and observation_ref:
                replay_ref = observation_ref.replace("/observations/", "/replays/")
                copied_replay = self._copy_ref(ref=replay_ref, kind="replays")
                if copied_replay and copied_replay not in seen["replays"]:
                    seen["replays"].add(copied_replay)
                    refs["replays"].append(copied_replay)
        manifest_path = self.run_dir / "vision_bundle" / "manifest.json"
        manifest_payload = {
            "run_id": self.run_id,
            "copied_at": _utc_now_iso(),
            "artifacts": refs,
        }
        manifest_path.write_text(json.dumps(manifest_payload, indent=2, sort_keys=True), encoding="utf-8")
        return refs


def _safe_slug(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value.strip())
    parts = [part for part in cleaned.split("-") if part]
    return "-".join(parts) or "unknown"


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
        if swept["unknown"] or swept["aborted"]:
            runtime_db.record_event("runtime", "WARN", "boot_reconcile_summary", context=dict(swept))
    finally:
        runtime_db.close()
    return swept


def build_planner(config: Config, repo_root: Path) -> Planner:
    """Return the selected planner backend."""
    if config.planner_backend == "openrouter" and config.openrouter_api_key:
        return OpenRouterPlanner(config, repo_root / "schemas" / "plan_ir.schema.json")
    return HeuristicPlanner()


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


def _install_run_log(archive: _RuntimeArchive) -> tuple[Path, object, object, object]:
    """Create a fresh per-launch log file and tee stdout/stderr into it."""
    log_path = archive.runtime_log_path()
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


def _world_explicit_frontier_ids(world) -> list[str]:
    prompt_view = world.prompt_view()
    decision_signals = prompt_view.get("decision_signals") if isinstance(prompt_view, dict) else None
    allowed_ids = decision_signals.get("allowed_frontier_ids") if isinstance(decision_signals, dict) else None
    if isinstance(allowed_ids, list):
        return [str(value) for value in allowed_ids]
    return [action.action_id for action in getattr(world, "frontier", []) if not action.verb.startswith("TUNE_")]


def _world_allowed_frontier_ids(world) -> list[str]:
    return [action.action_id for action in getattr(world, "frontier", [])]


def _world_tuning_frontier_ids(world) -> list[str]:
    return [action.action_id for action in getattr(world, "frontier", []) if action.verb.startswith("TUNE_")]


def _world_visible_allowed_frontier_ids(world, tuning_action_space: dict[str, object]) -> list[str]:
    visible_families = {str(family) for family in tuning_action_space.keys()}
    visible_action_ids: list[str] = []
    for action in getattr(world, "frontier", []):
        if not action.verb.startswith("TUNE_"):
            visible_action_ids.append(action.action_id)
            continue
        family = _artifact_action_family(action.action_id)
        if family is not None and family in visible_families:
            visible_action_ids.append(action.action_id)
    return visible_action_ids


def _world_visible_tuning_frontier_ids(world, tuning_action_space: dict[str, object]) -> list[str]:
    visible_families = {str(family) for family in tuning_action_space.keys()}
    visible_action_ids: list[str] = []
    for action in getattr(world, "frontier", []):
        if not action.verb.startswith("TUNE_"):
            continue
        family = _artifact_action_family(action.action_id)
        if family is not None and family in visible_families:
            visible_action_ids.append(action.action_id)
    return visible_action_ids


def _world_prompt_allowed_frontier_ids(world) -> list[str]:
    prompt_view = world.prompt_view()
    decision_signals = prompt_view.get("decision_signals") if isinstance(prompt_view, dict) else None
    allowed_ids = decision_signals.get("allowed_frontier_ids") if isinstance(decision_signals, dict) else None
    if isinstance(allowed_ids, list):
        return [str(value) for value in allowed_ids]
    return [action.action_id for action in getattr(world, "frontier", [])]


def _world_tuning_action_space(world) -> dict[str, object]:
    prompt_view = world.prompt_view()
    decision_signals = prompt_view.get("decision_signals") if isinstance(prompt_view, dict) else None
    tuning_action_space = decision_signals.get("tuning_action_space") if isinstance(decision_signals, dict) else None
    return tuning_action_space if isinstance(tuning_action_space, dict) else {}


def _world_planner_option_count(world) -> int:
    explicit_count = len(_world_prompt_allowed_frontier_ids(world))
    tuning_space = _world_tuning_action_space(world)
    tuning_count = 0
    for value in tuning_space.values():
        if isinstance(value, dict):
            tuning_count += 1
    return explicit_count + tuning_count


def _world_has_active_print(world) -> bool:
    lifecycle = str(world.facts.get("printer_1.lifecycle") or "").strip().upper()
    job_active = bool(world.facts.get("printer_1.job_active"))
    return lifecycle == "PRINTING" and job_active


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


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _should_stop(args, cycle_index: int) -> bool:
    if args.once:
        return True
    if not args.forever and args.cycles is not None and cycle_index >= args.cycles:
        return True
    return False


def _write_cycle_artifact(archive: _RuntimeArchive, *, cycle_index: int, goal: str, world, plan, report, planner) -> Path:
    return _write_cycle_artifact_with_worlds(
        archive,
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


def _artifact_action_family(action_id: str) -> str | None:
    text = str(action_id).upper()
    if "PRESSURE_ADVANCE" in text:
        return "pressure_advance"
    if "_ACCEL_" in text:
        return "accel"
    if "SPEED" in text:
        return "speed"
    if "FLOW" in text:
        return "flow"
    if "NOZZLE" in text:
        return "nozzle"
    if "BED" in text:
        return "bed"
    return None


def _artifact_float_or_none(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _artifact_string_or_none(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _artifact_split_actions(value) -> list[str] | None:
    text = _artifact_string_or_none(value)
    if text is None:
        return None
    return [item for item in text.split("|") if item]


def _artifact_recent_verified_family_result_value(world, *, family: str, result_field: str) -> float | None:
    candidates = []
    last_result = getattr(world, "last_result", None)
    if isinstance(last_result, dict) and last_result:
        candidates.append(last_result)
    recent_results = getattr(world, "recent_results", None)
    if isinstance(recent_results, list):
        candidates.extend(item for item in recent_results if isinstance(item, dict))
    for item in candidates:
        if _artifact_string_or_none(item.get("status")) != "DONE":
            continue
        action_id = _artifact_string_or_none(item.get("action_id"))
        if action_id is None or _artifact_action_family(action_id) != family:
            continue
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        value = _artifact_float_or_none(result.get(result_field))
        if value is not None:
            return value
    return None


def _artifact_stabilize_relative_scalar_current(
    *,
    live_value: float | None,
    baseline_value: float | None,
    small_step_pct: float,
    precision: int,
) -> tuple[float | None, str]:
    if live_value is None:
        return None, "missing_live_readback"
    if baseline_value is None:
        return live_value, "live_readback"
    minimum_tolerance = 0.0005 if precision >= 4 else 1.0
    half_small_step = abs(baseline_value) * (max(0.0, small_step_pct) / 100.0) * 0.5
    trust_tolerance = max(minimum_tolerance, half_small_step)
    if abs(live_value - baseline_value) > trust_tolerance:
        return baseline_value, "stabilized_baseline"
    return live_value, "live_readback"


def _artifact_planning_current(world) -> dict[str, dict[str, object]]:
    facts = getattr(world, "facts", {}) if hasattr(world, "facts") else {}
    family_specs = (
        ("pressure_advance", "pressure_advance", "printer_1.pressure_advance", "printer_1.active_pressure_advance_baseline", 10.0, 4),
        ("accel", "print_accel_mm_s2", "printer_1.print_accel_mm_s2", "printer_1.active_print_accel_baseline_mm_s2", 10.0, 1),
    )
    payload: dict[str, dict[str, object]] = {}
    for family, result_field, live_key, baseline_key, small_step_pct, precision in family_specs:
        observed_live_value = _artifact_float_or_none(facts.get(live_key))
        baseline_value = _artifact_float_or_none(facts.get(baseline_key))
        verified_value = _artifact_recent_verified_family_result_value(
            world,
            family=family,
            result_field=result_field,
        )
        if verified_value is not None:
            effective_value = verified_value
            source = "recent_verified_result"
        else:
            effective_value, source = _artifact_stabilize_relative_scalar_current(
                live_value=observed_live_value,
                baseline_value=baseline_value,
                small_step_pct=small_step_pct,
                precision=precision,
            )
        trusted_live_value = observed_live_value if source == "live_readback" else effective_value
        payload[family] = {
            "observed_live_value": observed_live_value,
            "live_value": trusted_live_value,
            "baseline_value": baseline_value,
            "effective_value": effective_value,
            "source": source,
        }
    return payload


def _artifact_capability_actions(world) -> dict[str, list[str] | None]:
    facts = getattr(world, "facts", {}) if hasattr(world, "facts") else {}
    families = {
        "speed": "printer_1.speed_shadow_actions",
        "flow": "printer_1.flow_shadow_actions",
        "nozzle": "printer_1.nozzle_shadow_actions",
        "bed": "printer_1.bed_shadow_actions",
        "pressure_advance": "printer_1.pressure_advance_shadow_actions",
        "accel": "printer_1.accel_shadow_actions",
    }
    return {family: _artifact_split_actions(facts.get(key)) for family, key in families.items()}


def _artifact_shadow_actions(world) -> dict[str, list[str] | None]:
    return _artifact_capability_actions(world)


def _artifact_executable_actions_by_family(world, tuning_action_space: dict[str, object]) -> dict[str, list[str]]:
    payload: dict[str, list[str]] = {}
    visible_families = {str(family) for family in tuning_action_space.keys()}
    for action in getattr(world, "frontier", []):
        family = _artifact_action_family(action.action_id)
        if family is None or family not in visible_families:
            continue
        payload.setdefault(family, []).append(action.action_id)
    return payload


def _world_artifact_payload(world) -> dict[str, object]:
    compilation = world.compilation.model_dump() if getattr(world, "compilation", None) is not None else {}
    planner_world_packet = world.prompt_view()
    decision_signals = (
        planner_world_packet.get("decision_signals")
        if isinstance(planner_world_packet, dict)
        else None
    )
    explicit_frontier_ids = _world_explicit_frontier_ids(world)
    executable_frontier_ids = _world_allowed_frontier_ids(world)
    visible_frontier_ids = executable_frontier_ids
    tuning_frontier_ids = _world_tuning_frontier_ids(world)
    visible_tuning_frontier_ids = tuning_frontier_ids
    tuning_action_space: dict[str, object] = {}
    if isinstance(decision_signals, dict):
        raw_allowed_frontier_ids = decision_signals.get("allowed_frontier_ids")
        if isinstance(raw_allowed_frontier_ids, list):
            explicit_frontier_ids = [str(action_id) for action_id in raw_allowed_frontier_ids]
        raw_tuning_action_space = decision_signals.get("tuning_action_space")
        if isinstance(raw_tuning_action_space, dict):
            tuning_action_space = raw_tuning_action_space
    visible_frontier_ids = _world_visible_allowed_frontier_ids(world, tuning_action_space)
    visible_tuning_frontier_ids = _world_visible_tuning_frontier_ids(world, tuning_action_space)
    family_blockers: dict[str, list[str]] = {}
    if isinstance(decision_signals, dict):
        raw_family_blockers = decision_signals.get("family_blockers")
        if isinstance(raw_family_blockers, dict):
            family_blockers = {
                str(family): [str(blocker) for blocker in blockers]
                for family, blockers in raw_family_blockers.items()
                if isinstance(blockers, list)
            }
    capability_actions = _artifact_capability_actions(world)
    shadow_actions = capability_actions
    executable_actions_by_family = _artifact_executable_actions_by_family(world, tuning_action_space)
    planning_current = _artifact_planning_current(world)
    artifact_facts = dict(world.facts)
    artifact_facts["printer_1.frontier_ids_json"] = json.dumps(
        [action.action_id for action in world.frontier],
        sort_keys=True,
    )
    artifact_facts["printer_1.explicit_frontier_ids_json"] = json.dumps(
        explicit_frontier_ids,
        sort_keys=True,
    )
    artifact_facts["printer_1.allowed_frontier_ids_json"] = json.dumps(
        visible_frontier_ids,
        sort_keys=True,
    )
    artifact_facts["printer_1.tuning_frontier_ids_json"] = json.dumps(
        visible_tuning_frontier_ids,
        sort_keys=True,
    )
    artifact_facts["printer_1.tuning_action_space_json"] = json.dumps(
        tuning_action_space,
        sort_keys=True,
    )
    artifact_facts["printer_1.shadow_actions_json"] = json.dumps(
        shadow_actions,
        sort_keys=True,
    )
    artifact_facts["printer_1.capability_actions_json"] = json.dumps(
        capability_actions,
        sort_keys=True,
    )
    artifact_facts["printer_1.executable_actions_by_family_json"] = json.dumps(
        executable_actions_by_family,
        sort_keys=True,
    )
    artifact_facts["printer_1.family_blockers_json"] = json.dumps(
        family_blockers,
        sort_keys=True,
    )
    artifact_facts["printer_1.planning_current_json"] = json.dumps(
        planning_current,
        sort_keys=True,
    )
    artifact_facts["printer_1.compact_tuning_space_json"] = artifact_facts["printer_1.tuning_action_space_json"]
    return {
        "live_state_snapshot": compilation,
        "planner_world_packet": planner_world_packet,
        "frontier_ids": executable_frontier_ids,
        "explicit_frontier_ids": explicit_frontier_ids,
        "allowed_frontier_ids": visible_frontier_ids,
        "tuning_frontier_ids": visible_tuning_frontier_ids,
        "tuning_action_space": tuning_action_space,
        "capability_actions": capability_actions,
        "executable_actions_by_family": executable_actions_by_family,
        "family_blockers": family_blockers,
        "shadow_actions": shadow_actions,
        "planning_current": planning_current,
        "compact_tuning_space": tuning_action_space,
        "blockers": list(world.blockers),
        "facts": artifact_facts,
        "pending_human": list(world.pending_human),
    }


def _write_runtime_state(
    config: Config,
    archive: _RuntimeArchive,
    *,
    cycle_index: int,
    phase: str,
    goal: str,
    world,
    plan=None,
    report=None,
    cycle_timing: dict[str, object] | None = None,
) -> Path:
    artifact_payload = _world_artifact_payload(world)
    payload = {
        "cycle_index": cycle_index + 1,
        "phase": phase,
        "goal": goal,
        "ts": _utc_now_iso(),
        "run_scope": world.facts.get("printer_1.job_scope_token"),
        "live_state_snapshot": artifact_payload["live_state_snapshot"],
        "world_summary": {
            "planner_world_packet": artifact_payload["planner_world_packet"],
            "frontier_ids": artifact_payload["frontier_ids"],
            "explicit_frontier_ids": artifact_payload["explicit_frontier_ids"],
            "allowed_frontier_ids": artifact_payload["allowed_frontier_ids"],
            "tuning_frontier_ids": artifact_payload["tuning_frontier_ids"],
            "tuning_action_space": artifact_payload["tuning_action_space"],
            "capability_actions": artifact_payload["capability_actions"],
            "executable_actions_by_family": artifact_payload["executable_actions_by_family"],
            "family_blockers": artifact_payload["family_blockers"],
            "shadow_actions": artifact_payload["shadow_actions"],
            "planning_current": artifact_payload["planning_current"],
            "compact_tuning_space": artifact_payload["compact_tuning_space"],
            "blockers": artifact_payload["blockers"],
            "facts": artifact_payload["facts"],
            "pending_human": artifact_payload["pending_human"],
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
    archive.observe_job_scope(world.facts.get("printer_1.job_scope_token"))
    archive.write_runtime_state_history(cycle_index=cycle_index, phase=phase, payload=payload)
    return out_path


def _write_cycle_artifact_with_worlds(
    archive: _RuntimeArchive,
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
    ts = time.strftime("%Y-%m-%dT%H%M%S")
    out_path = archive.runtime_cycles_dir / f"{ts}-cycle-{cycle_index + 1:04d}.json"
    decision_payload = _world_artifact_payload(decision_world)
    post_execution_payload = _world_artifact_payload(post_execution_world)
    archive.observe_job_scope(post_execution_world.facts.get("printer_1.job_scope_token"))
    captured_artifacts = archive.capture_referenced_artifacts(
        decision_payload=decision_payload,
        post_execution_payload=post_execution_payload,
    )
    payload = {
        "cycle_index": cycle_index + 1,
        "goal": goal,
        "ts": ts,
        "run_scope": post_execution_world.facts.get("printer_1.job_scope_token"),
        "live_state_snapshot": post_execution_payload["live_state_snapshot"],
        "world_summary": {
            "planner_world_packet": post_execution_payload["planner_world_packet"],
            "frontier_ids": post_execution_payload["frontier_ids"],
            "explicit_frontier_ids": post_execution_payload["explicit_frontier_ids"],
            "allowed_frontier_ids": post_execution_payload["allowed_frontier_ids"],
            "tuning_frontier_ids": post_execution_payload["tuning_frontier_ids"],
            "tuning_action_space": post_execution_payload["tuning_action_space"],
            "capability_actions": post_execution_payload["capability_actions"],
            "executable_actions_by_family": post_execution_payload["executable_actions_by_family"],
            "family_blockers": post_execution_payload["family_blockers"],
            "shadow_actions": post_execution_payload["shadow_actions"],
            "planning_current": post_execution_payload["planning_current"],
            "compact_tuning_space": post_execution_payload["compact_tuning_space"],
            "blockers": post_execution_payload["blockers"],
            "facts": post_execution_payload["facts"],
            "pending_human": post_execution_payload["pending_human"],
        },
        "decision_input": decision_payload,
        "post_execution": post_execution_payload,
        "planner_output": plan.model_dump(),
        "planner_provider_metadata": _planner_provider_metadata_for_cycle(planner, cycle_timing=cycle_timing),
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
        "control_lock_path": str(archive.config.control_lock_path),
        "captured_artifacts": captured_artifacts,
    }
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return out_path


def _planner_provider_metadata_for_cycle(planner, *, cycle_timing: dict[str, object]) -> object | None:
    if bool(cycle_timing.get("planner_bypassed")):
        return None
    return getattr(planner, "last_provider_metadata", None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the lean Wallee v6.5 reference runtime")
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
    repo_root = Path(__file__).resolve().parents[1]
    archive = _RuntimeArchive(config, repo_root=repo_root)
    try:
        reset_runtime_start_state(config)
        reconcile_runtime_start_state(config)
        log_path, log_handle, original_stdout, original_stderr = _install_run_log(archive)
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
                "planner_request_timeout_s": config.planner_request_timeout_seconds,
                "planner_retry_attempts": config.planner_retry_attempts,
                "planner_retry_backoff_s": config.planner_retry_backoff_seconds,
                "planner_failures_before_no_action": config.planner_failures_before_no_action,
                "planner_min_interval_s": config.planner_min_interval_seconds,
                "planner_vision_lead_s": config.planner_vision_lead_seconds,
                "monitor_when_inactive": config.monitor_when_inactive,
                "forever": bool(args.forever),
                "once": bool(args.once),
                "cycles": args.cycles,
            },
        )
        poll_interval_s = config.runtime_poll_interval_seconds if args.poll_interval_s is None else float(args.poll_interval_s)
        cycle_index = 0
        consecutive_planner_failures = 0
        consecutive_cycle_failures = 0
        last_planner_attempt_monotonic: float | None = None
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
