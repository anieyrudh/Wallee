"""Cycle artifacts: world payloads, runtime-state files, and cycle JSON."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import TYPE_CHECKING

from .config import Config

if TYPE_CHECKING:  # circular at runtime: archive imports _utc_now_iso from here
    from .archive import _RuntimeArchive


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
