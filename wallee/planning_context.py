"""World packet compilation and legal-frontier construction."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .coerce import string_or_none as _string_or_none
from .config import Config
from .models import ActionRunStatus, Delta, LegalAction, WorldCompilationContext, WorldPacket
from .predicates import PredicateEvaluator
from .registry import PackRegistry
from .runtime_db import RuntimeDB
from .whiteboard import BaseWhiteboard


def run_scope_from_facts(facts: dict[str, Any]) -> str | None:
    """The one run-scope builder (previously duplicated verbatim here and in
    the engine)."""
    job_scope_token = _string_or_none(facts.get("printer_1.job_scope_token"))
    if not job_scope_token:
        job_scope_token = _string_or_none(facts.get("printer_1.current_file")) or _string_or_none(
            facts.get("printer_1.requested_file")
        )
    if not job_scope_token:
        return None
    job_id = facts.get("printer_1.job_id")
    lifecycle = _string_or_none(facts.get("printer_1.lifecycle")) or "unknown"
    if job_id is not None:
        return f"job:{job_id}:{job_scope_token}"
    return f"state:{lifecycle}:{job_scope_token}"


_run_scope_from_facts = run_scope_from_facts


def _utc_now_context() -> tuple[int, str]:
    ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return ts_ms, datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _build_live_state_summary(raw_snapshot: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in sorted(raw_snapshot):
        if "." not in key:
            continue
        device_id, remainder = key.split(".", 1)
        device_summary = summary.setdefault(device_id, {})
        if remainder == "raw.observed_at":
            device_summary["raw_observed_at"] = raw_snapshot.get(key)
        elif remainder == "raw.lifecycle":
            device_summary["lifecycle"] = raw_snapshot.get(key)
        elif remainder == "raw.health":
            device_summary["health"] = raw_snapshot.get(key)
        elif remainder == "raw.job_active":
            device_summary["job_active"] = raw_snapshot.get(key)
        elif remainder == "raw.job_progress_pct":
            device_summary["job_progress_pct"] = raw_snapshot.get(key)
        elif remainder == "raw.job_id":
            device_summary["job_id"] = raw_snapshot.get(key)
        elif remainder == "raw.current_file":
            device_summary["current_file"] = raw_snapshot.get(key)
        elif remainder == "vision_observed_at":
            device_summary["vision_observed_at"] = raw_snapshot.get(key)
        elif remainder == "nozzle_cam_last_capture_at":
            device_summary["camera_last_capture_at"] = raw_snapshot.get(key)
        elif remainder == "nozzle_cam_frame_age_s":
            device_summary["camera_frame_age_s"] = raw_snapshot.get(key)
    return {device_id: data for device_id, data in summary.items() if data}


def _is_tuning_action(action: LegalAction) -> bool:
    return action.verb.startswith("TUNE_")


class WorldCompiler:
    """Compile raw state into a compact planner-facing world packet.

    The compiler is the heart of the "prompting as physical grounding" idea.
    Instead of dumping Redis into the model, it produces a small, typed planning
    projection of the world.
    """

    def __init__(
        self,
        *,
        config: Config,
        registry: PackRegistry,
        whiteboard: BaseWhiteboard,
        runtime_db: RuntimeDB,
        predicate_evaluator: PredicateEvaluator | None = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.whiteboard = whiteboard
        self.runtime_db = runtime_db
        self.predicate_evaluator = predicate_evaluator or PredicateEvaluator()
        self._last_facts: dict[str, Any] = {}
        self._last_snapshot_seq = 0

    def compile(self, goal: str, pending_human: list[str] | None = None) -> WorldPacket:
        """Return a compact world packet for the current cycle."""
        snapshot = self.whiteboard.snapshot()
        compiled_ts_ms, compiled_at = _utc_now_context()
        normalized = [pack.normalize(snapshot.values) for pack in self.registry.all_packs()]

        facts: dict[str, Any] = {}
        resources = []
        device_summaries = []
        blockers: list[str] = []

        for item in normalized:
            facts.update(item.facts)
            resources.extend(item.resources)
            device_summaries.append(item.summary)
            blockers.extend(item.blockers)

        deltas = self._compute_deltas(snapshot.values, facts)
        run_scope = _run_scope_from_facts(facts)
        recent_results = self.runtime_db.recent_completed_actions(run_scope, limit=3)
        preliminary = WorldPacket(
            goal=goal,
            device_summaries=device_summaries,
            facts=facts,
            resources=resources,
            blockers=blockers,
            deltas=deltas,
            frontier=[],
            last_result=recent_results[0] if recent_results else {},
            recent_results=recent_results,
            pending_human=pending_human or [],
            prompt_frontier_limit=self.config.frontier_limit,
            compilation=WorldCompilationContext(
                compiled_at=compiled_at,
                compiled_ts_ms=compiled_ts_ms,
                whiteboard_sequence=snapshot.sequence,
                live_state_summary=_build_live_state_summary(snapshot.values),
                raw_snapshot=dict(snapshot.values),
            ),
        )
        frontier = self._build_frontier(preliminary)
        packet = preliminary.model_copy(update={"frontier": frontier})

        self._last_facts = dict(facts)
        self._last_snapshot_seq = snapshot.sequence
        return packet

    def _compute_deltas(self, raw_snapshot: dict[str, Any], facts: dict[str, Any]) -> list[Delta]:
        """Return a truncated list of semantically meaningful deltas.

        We compare normalized facts, not raw keys, because the planner should be
        told that a part has become "SAFE" rather than drowning in every sensor
        tick that happened to contribute to that conclusion.
        """
        deltas_by_device: dict[str, list[Delta]] = defaultdict(list)
        for fact_name, new_value in facts.items():
            old_value = self._last_facts.get(fact_name)
            if old_value == new_value:
                continue
            device_id = fact_name.split(".", 1)[0]
            deltas_by_device[device_id].append(
                Delta(
                    device_id=device_id,
                    fact=fact_name,
                    old=old_value,
                    new=new_value,
                    note=f"{fact_name} changed from {old_value!r} to {new_value!r}",
                )
            )

        truncated: list[Delta] = []
        for device_id, items in sorted(deltas_by_device.items()):
            truncated.extend(items[: self.config.delta_limit_per_device])
        return truncated

    def _build_frontier(self, world: WorldPacket) -> list[LegalAction]:
        """Build the legal action frontier for *world*."""
        candidates: list[LegalAction] = []
        for pack in self.registry.all_packs():
            candidates.extend(pack.candidate_actions(world))

        locked = self._active_locks()
        legal: list[LegalAction] = []
        for action in candidates:
            if any(lock in locked for lock in action.required_locks):
                continue
            if not self.predicate_evaluator.evaluate(action.preconditions, world.facts):
                continue
            legal.append(action)

        legal.sort(key=lambda action: (action.rank_hint, action.action_id))
        control_actions = [action for action in legal if not _is_tuning_action(action)]
        tuning_actions = [action for action in legal if _is_tuning_action(action)]
        return control_actions + tuning_actions

    def _active_locks(self) -> set[str]:
        """Return currently held lock keys.

        Because the reference runtime uses one main process, lock contention is
        rare.  Still, deriving the active locks from durable `DISPATCHED`
        actions keeps the safety rule obvious and testable.
        """
        locked: set[str] = set()
        for action_run in self.runtime_db.list_action_runs_by_status(ActionRunStatus.DISPATCHED):
            locked.update(action_run.required_locks)
        return locked
