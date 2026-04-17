"""Core data contracts for Wallee v6.

The models in this file deliberately capture the smallest stable vocabulary that
the rest of the system needs.  The design goal is to keep the abstractions deep:
callers can reason in terms of "world packet", "legal action", or "action run"
without caring how those objects are stored or rendered.
"""

from __future__ import annotations

from enum import Enum
import json
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


Scalar = str | int | float | bool | None
JsonDict = dict[str, Any]


class HazardClass(str, Enum):
    """Human-facing hazard bucket used for approvals and operator triage."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionRunStatus(str, Enum):
    """Durable logical states for an action run.

    These states are intentionally simpler than a large workflow engine.  The
    runtime uses them to answer one question reliably: "What does the official
    notebook say should have happened?"
    """

    PROPOSED = "PROPOSED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    AUTHORIZED = "AUTHORIZED"
    DISPATCHED = "DISPATCHED"
    DONE = "DONE"
    FAILED = "FAILED"
    ABORTED = "ABORTED"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    UNKNOWN = "UNKNOWN"


class ExecJournalStatus(str, Enum):
    """Durable execution-journal states written by the pack runtime."""

    IN_FLIGHT = "IN_FLIGHT"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class DeviceSummary(BaseModel):
    """High-level device state prepared for the planner."""

    model_config = ConfigDict(extra="forbid")

    device_id: str
    display_name: str
    category: str
    mode: str
    health: str
    summary: str


class ResourceState(BaseModel):
    """Planner-visible resource in the current cell.

    Examples include a printer bed, a tray, or a gripper.  Treating these as
    first-class resources lets the frontier builder reason about conflicts and
    legal actions without leaking vendor-specific APIs into the planner.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    owner: str
    state: str
    attributes: dict[str, Scalar] = Field(default_factory=dict)


class Delta(BaseModel):
    """A compact change description used in the world packet."""

    model_config = ConfigDict(extra="forbid")

    device_id: str
    fact: str
    old: Scalar = None
    new: Scalar = None
    note: str


class NormalizedPackState(BaseModel):
    """The semantic view produced by one pack during world compilation."""

    model_config = ConfigDict(extra="forbid")

    summary: DeviceSummary
    facts: dict[str, Scalar] = Field(default_factory=dict)
    resources: list[ResourceState] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


class LegalAction(BaseModel):
    """A legal next action compiled by deterministic code.

    The planner never sees raw device APIs.  It only sees these bounded,
    pre-validated choices.
    """

    model_config = ConfigDict(extra="forbid")

    action_id: str
    verb: str
    description: str
    owner_pack: str
    execute_ref: str
    args: dict[str, Scalar] = Field(default_factory=dict)
    required_locks: list[str] = Field(default_factory=list)
    hazard_class: HazardClass = HazardClass.LOW
    approval_required: bool = False
    preconditions: JsonDict = Field(default_factory=lambda: {"all": []})
    verify: JsonDict | None = None
    expected_delta: list[JsonDict] = Field(default_factory=list)
    target_device: str | None = None
    rank_hint: int = 100

    def prompt_view(self) -> JsonDict:
        """Return the compact, planner-safe view of the action.

        Internal execution details such as `execute_ref` and raw preconditions
        stay out of the prompt to keep the context small and avoid confusing the
        model with implementation noise.
        """
        return {
            "id": self.action_id,
            "verb": self.verb,
            "description": self.description,
            "args": self.args,
            "target_device": self.target_device,
            "hazard_class": self.hazard_class.value,
            "approval_required": self.approval_required,
            "expected_delta": self.expected_delta,
        }


class WorldCompilationContext(BaseModel):
    """Compile-time provenance kept separate from the planner prompt."""

    model_config = ConfigDict(extra="forbid")

    compiled_at: str
    compiled_ts_ms: int
    whiteboard_sequence: int
    live_state_summary: JsonDict = Field(default_factory=dict)
    raw_snapshot: JsonDict = Field(default_factory=dict)


class WorldPacket(BaseModel):
    """Compact planning context compiled from live state."""

    model_config = ConfigDict(extra="forbid")

    goal: str
    device_summaries: list[DeviceSummary]
    facts: dict[str, Scalar]
    resources: list[ResourceState]
    blockers: list[str] = Field(default_factory=list)
    deltas: list[Delta] = Field(default_factory=list)
    frontier: list[LegalAction] = Field(default_factory=list)
    last_result: JsonDict = Field(default_factory=dict)
    pending_human: list[str] = Field(default_factory=list)
    compilation: WorldCompilationContext | None = None

    def prompt_view(self) -> JsonDict:
        """Return the compact planner-facing representation of this packet."""
        frontier_views = [action.prompt_view() for action in self.frontier]
        allowed_frontier_ids = [action["id"] for action in frontier_views]
        prompt_facts = dict(self.facts)
        prompt_facts.pop("printer_1.active_notes_json", None)
        prompt_facts.pop("printer_1.vision_observation_ref", None)
        prompt_facts.pop("printer_1.vision_frame_ref", None)
        if "printer_1.vision_advisory_summary" in prompt_facts:
            prompt_facts["printer_1.vision_signal_summary"] = prompt_facts.pop("printer_1.vision_advisory_summary")
        if "printer_1.vision_advisory_finding_types" in prompt_facts:
            prompt_facts["printer_1.vision_signal_finding_types"] = prompt_facts.pop(
                "printer_1.vision_advisory_finding_types"
            )
        if "printer_1.vision_advisory_strength" in prompt_facts:
            prompt_facts["printer_1.vision_signal_strength"] = prompt_facts.pop("printer_1.vision_advisory_strength")
        if "printer_1.vision_advisory_issue_level" in prompt_facts:
            prompt_facts["printer_1.vision_signal_issue_level"] = prompt_facts.pop(
                "printer_1.vision_advisory_issue_level"
            )
        return {
            "goal": self.goal,
            "devices": [summary.model_dump() for summary in self.device_summaries],
            "facts": prompt_facts,
            "resources": [resource.model_dump() for resource in self.resources],
            "blockers": self.blockers,
            "deltas": [delta.model_dump() for delta in self.deltas],
            "frontier": frontier_views,
            "last_result": self.last_result,
            "pending_human": self.pending_human,
            "decision_contract": {
                "frontier_only": True,
                "max_actions": 1,
                "prefer_bounded_action_when_supported": True,
                "one_family_at_a_time": True,
                "fresh_supported_signals_are_actionable": True,
                "vision_signals_support_action_selection": True,
                "notebook_signals_support_action_selection": True,
                "blockers_and_legality_dominate": True,
                "no_bundled_family_actions": True,
            },
            "decision_signals": {
                "notebook_notes": _planner_notebook_notes(self.facts),
                "vision_signal": _planner_vision_signal(self.facts),
                "last_result_notes": _planner_last_result_notes(self.last_result),
                "temporal_action_context": _planner_temporal_action_context(self.last_result),
                "freshness": _planner_freshness(self.facts),
                "symptom_feedback": _planner_symptom_feedback(self.facts, self.last_result),
                "family_blockers": _planner_family_blockers(self.facts),
                "allowed_frontier_ids": allowed_frontier_ids,
            },
        }


def world_packet_prompt_schema() -> JsonDict:
    """Return the JSON schema for the planner-facing world prompt view."""
    scalar_schema: JsonDict = {
        "anyOf": [
            {"type": "string"},
            {"type": "integer"},
            {"type": "number"},
            {"type": "boolean"},
            {"type": "null"},
        ]
    }
    active_note_view = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "note_id": {"type": "string"},
            "scope": {"enum": ["global", "local"], "type": "string"},
            "title": {"type": "string"},
            "text": {"type": "string"},
            "confidence": {"enum": ["low", "medium", "high"], "type": "string"},
            "reason_tags": {"type": "array", "items": {"type": "string"}},
            "suggested_families": {
                "type": "array",
                "items": {"enum": ["speed", "flow", "nozzle", "bed"], "type": "string"},
            },
            "section_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "note_id",
            "scope",
            "title",
            "text",
            "confidence",
            "reason_tags",
            "suggested_families",
            "section_ids",
        ],
    }
    last_result_note = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action_id": {"type": "string"},
            "family": {"enum": ["speed", "flow", "nozzle", "bed", "other"], "type": "string"},
            "outcome": {
                "enum": [
                    "verified_effect",
                    "verified_no_effect",
                    "suppressed",
                    "blocked",
                    "verification_mismatch",
                    "reset_restored",
                    "no_action",
                ],
                "type": "string",
            },
            "summary": {"type": "string"},
            "reason_tags": {"type": "array", "items": {"type": "string"}},
            "observed_at": {"type": "string"},
        },
        "required": ["action_id", "family", "outcome", "summary", "reason_tags", "observed_at"],
    }
    temporal_action_context = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "available": {"type": "boolean"},
            "now_at": {"type": "string"},
            "last_action_id": {"type": ["string", "null"]},
            "last_action_family": {"type": ["string", "null"], "enum": ["speed", "flow", "nozzle", "bed", "other", None]},
            "last_action_status": {"type": ["string", "null"]},
            "last_action_outcome": {
                "type": ["string", "null"],
                "enum": [
                    "verified_effect",
                    "verified_no_effect",
                    "suppressed",
                    "blocked",
                    "verification_mismatch",
                    "reset_restored",
                    "no_action",
                    None,
                ],
            },
            "last_action_summary": {"type": ["string", "null"]},
            "last_action_reason_tags": {"type": "array", "items": {"type": "string"}},
            "last_action_why": {"type": ["string", "null"]},
            "last_action_observed_at": {"type": ["string", "null"]},
            "seconds_since_last_action": {"type": ["number", "null"]},
        },
        "required": [
            "available",
            "now_at",
            "last_action_id",
            "last_action_family",
            "last_action_status",
            "last_action_outcome",
            "last_action_summary",
            "last_action_reason_tags",
            "last_action_why",
            "last_action_observed_at",
            "seconds_since_last_action",
        ],
    }
    freshness = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "now_at": {"type": "string"},
            "raw_observed_at": {"type": ["string", "null"]},
            "seconds_since_raw_observation": {"type": ["number", "null"]},
            "vision_observed_at": {"type": ["string", "null"]},
            "seconds_since_vision_observation": {"type": ["number", "null"]},
            "camera_last_capture_at": {"type": ["string", "null"]},
            "camera_frame_age_s": {"type": ["number", "null"]},
            "vision_usable": {"type": "boolean"},
        },
        "required": [
            "now_at",
            "raw_observed_at",
            "seconds_since_raw_observation",
            "vision_observed_at",
            "seconds_since_vision_observation",
            "camera_last_capture_at",
            "camera_frame_age_s",
            "vision_usable",
        ],
    }
    action_view = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "verb": {"type": "string"},
            "description": {"type": "string"},
            "args": {"type": "object", "additionalProperties": scalar_schema},
            "target_device": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "hazard_class": {"enum": ["low", "medium", "high"], "type": "string"},
            "approval_required": {"type": "boolean"},
            "expected_delta": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        },
        "required": [
            "id",
            "verb",
            "description",
            "args",
            "target_device",
            "hazard_class",
            "approval_required",
            "expected_delta",
        ],
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "WorldPacketPromptView",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "goal": {"type": "string"},
            "devices": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "device_id": {"type": "string"},
                        "display_name": {"type": "string"},
                        "category": {"type": "string"},
                        "mode": {"type": "string"},
                        "health": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                    "required": ["device_id", "display_name", "category", "mode", "health", "summary"],
                },
            },
            "facts": {"type": "object", "additionalProperties": scalar_schema},
            "resources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "kind": {"type": "string"},
                        "owner": {"type": "string"},
                        "state": {"type": "string"},
                        "attributes": {"type": "object", "additionalProperties": scalar_schema},
                    },
                    "required": ["id", "kind", "owner", "state", "attributes"],
                },
            },
            "blockers": {"type": "array", "items": {"type": "string"}},
            "deltas": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "device_id": {"type": "string"},
                        "fact": {"type": "string"},
                        "old": scalar_schema,
                        "new": scalar_schema,
                        "note": {"type": "string"},
                    },
                    "required": ["device_id", "fact", "old", "new", "note"],
                },
            },
            "frontier": {"type": "array", "items": action_view},
            "last_result": {"type": "object", "additionalProperties": True},
            "pending_human": {"type": "array", "items": {"type": "string"}},
            "decision_contract": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "frontier_only": {"const": True},
                    "max_actions": {"const": 1},
                    "prefer_bounded_action_when_supported": {"const": True},
                    "one_family_at_a_time": {"const": True},
                    "fresh_supported_signals_are_actionable": {"const": True},
                    "vision_signals_support_action_selection": {"const": True},
                    "notebook_signals_support_action_selection": {"const": True},
                    "blockers_and_legality_dominate": {"const": True},
                    "no_bundled_family_actions": {"const": True},
                },
                "required": [
                    "frontier_only",
                    "max_actions",
                    "prefer_bounded_action_when_supported",
                    "one_family_at_a_time",
                    "fresh_supported_signals_are_actionable",
                    "vision_signals_support_action_selection",
                    "notebook_signals_support_action_selection",
                    "blockers_and_legality_dominate",
                    "no_bundled_family_actions",
                ],
            },
            "decision_signals": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "notebook_notes": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "global": {"type": "array", "items": active_note_view},
                            "local": {"type": "array", "items": active_note_view},
                            "merged_reason_tags": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["global", "local", "merged_reason_tags"],
                    },
                    "vision_signal": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "usable": {"type": "boolean"},
                            "summary": {"type": ["string", "null"]},
                            "finding_types": {"type": "array", "items": {"type": "string"}},
                            "strength": {
                                "type": ["string", "null"],
                                "enum": ["weak", "moderate", "strong", None],
                            },
                            "issue_level": {
                                "type": ["string", "null"],
                                "enum": ["low", "medium", "high", None],
                            },
                        },
                        "required": ["usable", "summary", "finding_types", "strength", "issue_level"],
                    },
                    "last_result_notes": {"type": "array", "items": last_result_note},
                    "temporal_action_context": temporal_action_context,
                    "freshness": freshness,
                    "symptom_feedback": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "current_finding_types": {"type": "array", "items": {"type": "string"}},
                            "current_strength": {
                                "type": ["string", "null"],
                                "enum": ["weak", "moderate", "strong", None],
                            },
                            "current_issue_level": {
                                "type": ["string", "null"],
                                "enum": ["low", "medium", "high", None],
                            },
                            "last_verified_finding_types": {"type": "array", "items": {"type": "string"}},
                            "last_verified_strength": {
                                "type": ["string", "null"],
                                "enum": ["weak", "moderate", "strong", None],
                            },
                            "last_verified_issue_level": {
                                "type": ["string", "null"],
                                "enum": ["low", "medium", "high", None],
                            },
                            "same_issue_persisted": {"type": "boolean"},
                            "not_improving_after_last_action": {"type": "boolean"},
                        },
                        "required": [
                            "current_finding_types",
                            "current_strength",
                            "current_issue_level",
                            "last_verified_finding_types",
                            "last_verified_strength",
                            "last_verified_issue_level",
                            "same_issue_persisted",
                            "not_improving_after_last_action",
                        ],
                    },
                    "family_blockers": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "speed": {"type": "array", "items": {"type": "string"}},
                            "flow": {"type": "array", "items": {"type": "string"}},
                            "nozzle": {"type": "array", "items": {"type": "string"}},
                            "bed": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["speed", "flow", "nozzle", "bed"],
                    },
                    "allowed_frontier_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "notebook_notes",
                    "vision_signal",
                    "last_result_notes",
                    "temporal_action_context",
                    "freshness",
                    "symptom_feedback",
                    "family_blockers",
                    "allowed_frontier_ids",
                ],
            },
        },
        "required": [
            "goal",
            "devices",
            "facts",
            "resources",
            "blockers",
            "deltas",
            "frontier",
            "last_result",
            "pending_human",
            "decision_contract",
            "decision_signals",
        ],
    }


class PlanIR(BaseModel):
    """Canonical planner output contract.

    The planner is allowed to choose frontier action IDs and explain why.
    Anything more expressive than that belongs in deterministic code or in a new
    ADR, not in a casual prompt tweak.
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["NO_ACTION", "EXECUTE", "CALL_HUMAN"]
    sequence: list[str] = Field(default_factory=list)
    why: str
    call_human_message: str | None = None

    @field_validator("sequence")
    @classmethod
    def _validate_sequence(cls, sequence: list[str]) -> list[str]:
        """Enforce the short-horizon rule.

        Wallee deliberately uses short horizons plus replan instead of long
        planner-authored programs.  This keeps verification local and makes
        failures easier to reason about.
        """
        if len(sequence) > 3:
            raise ValueError("plan horizon exceeds 3 actions")
        if len(set(sequence)) != len(sequence):
            raise ValueError("sequence contains duplicate action IDs")
        return sequence


class PlanRecord(BaseModel):
    """Durable plan record stored in SQLite."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    run_scope: str | None = None
    goal: str
    world_packet: JsonDict
    world_compilation: JsonDict = Field(default_factory=dict)
    plan_ir: JsonDict
    created_ts_ms: int


class ActionRun(BaseModel):
    """Logical execution attempt persisted in `action_runs`."""

    model_config = ConfigDict(extra="forbid")

    action_run_id: str
    plan_id: str
    run_scope: str | None = None
    action_id: str
    verb: str
    owner_pack: str
    args: dict[str, Scalar]
    args_hash: str
    idempotency_key: str
    required_locks: list[str] = Field(default_factory=list)
    status: ActionRunStatus
    hazard_class: HazardClass
    verify: JsonDict | None = None
    expected_delta: list[JsonDict] = Field(default_factory=list)
    target_device: str | None = None
    created_ts_ms: int
    updated_ts_ms: int
    result: JsonDict | None = None
    error: JsonDict | None = None


class ExecJournalEntry(BaseModel):
    """Durable journal entry written before hardware side effects."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str
    action_run_id: str
    verb: str
    args_hash: str
    exec_state: ExecJournalStatus
    started_mono_ms: int
    ended_mono_ms: int | None = None
    result: JsonDict | None = None
    error: JsonDict | None = None


def _planner_notebook_notes(facts: dict[str, Scalar]) -> JsonDict:
    payload = _json_from_fact(facts.get("printer_1.active_notes_json"))
    if not isinstance(payload, dict):
        return {"global": [], "local": [], "merged_reason_tags": []}
    return {
        "global": payload.get("global_notes", []) or [],
        "local": payload.get("local_notes", []) or [],
        "merged_reason_tags": payload.get("merged_reason_tags", []) or [],
    }


def _planner_family_blockers(facts: dict[str, Scalar]) -> JsonDict:
    return {
        "speed": _split_blockers(facts.get("printer_1.speed_autonomy_blockers")),
        "flow": _split_blockers(facts.get("printer_1.flow_shadow_blockers")),
        "nozzle": _split_blockers(facts.get("printer_1.nozzle_shadow_blockers")),
        "bed": _split_blockers(facts.get("printer_1.bed_shadow_blockers")),
    }


def _planner_vision_signal(facts: dict[str, Scalar]) -> JsonDict:
    summary = _string_or_none(facts.get("printer_1.vision_signal_summary") or facts.get("printer_1.vision_advisory_summary"))
    finding_types = _split_blockers(
        facts.get("printer_1.vision_signal_finding_types") or facts.get("printer_1.vision_advisory_finding_types")
    )
    strength = _normalize_strength(
        facts.get("printer_1.vision_signal_strength") or facts.get("printer_1.vision_advisory_strength")
    )
    usable = bool(facts.get("printer_1.nozzle_cam_usable", False)) and bool(summary)
    issue_level = _normalize_issue_level(
        facts.get("printer_1.vision_signal_issue_level") or facts.get("printer_1.vision_advisory_issue_level")
    )
    if issue_level is None and usable:
        issue_level = _vision_issue_level(finding_types, strength)
    return {
        "usable": usable,
        "summary": summary if usable else None,
        "finding_types": finding_types if usable else [],
        "strength": strength if usable else None,
        "issue_level": issue_level if usable else None,
    }


def _planner_last_result_notes(last_result: JsonDict) -> list[JsonDict]:
    if not last_result:
        return []
    action_id = str(last_result.get("action_id") or "").strip()
    verb = str(last_result.get("verb") or "").strip()
    status = str(last_result.get("status") or "").strip().upper()
    result = last_result.get("result") or {}
    error = last_result.get("error") or {}
    updated_ts_ms = last_result.get("updated_ts_ms")
    if not action_id:
        return []
    outcome = "blocked"
    reason_tags: list[str] = []
    if status == "DONE":
        effect = str(result.get("effect") or "")
        if effect.startswith("noop"):
            outcome = "no_action"
            reason_tags.append("noop")
        elif "no_effect" in effect:
            outcome = "verified_no_effect"
            reason_tags.append("verified_no_effect")
        elif "RESTORE" in action_id:
            outcome = "reset_restored"
            reason_tags.append("reset")
        else:
            outcome = "verified_effect"
            reason_tags.append("verified")
        post_signal = _result_vision_signal(result)
        if post_signal.get("usable") and post_signal.get("finding_types"):
            reason_tags.append("post_action_symptom_present")
            if post_signal.get("issue_level") in {"medium", "high"}:
                reason_tags.append("post_action_not_cleared")
    elif status == "FAILED":
        message = json.dumps(error, sort_keys=True) if error else ""
        if "expected post-state" in message or "verify" in message.lower():
            outcome = "verification_mismatch"
            reason_tags.append("verification")
        else:
            outcome = "blocked"
            reason_tags.append("failed")
    elif status == "REPLAN_REQUIRED":
        outcome = "blocked"
        reason_tags.append("replan")
    elif status == "ABORTED":
        outcome = "suppressed"
        reason_tags.append("aborted")
    family = _action_family(action_id, verb)
    summary = _last_result_summary(action_id=action_id, outcome=outcome, result=result, error=error)
    observed_at = _ts_ms_to_iso(updated_ts_ms)
    return [
        {
            "action_id": action_id,
            "family": family,
            "outcome": outcome,
            "summary": summary,
            "reason_tags": reason_tags,
            "observed_at": observed_at,
        }
    ]


def _planner_temporal_action_context(last_result: JsonDict) -> JsonDict:
    now_ms = int(time.time() * 1000)
    now_at = _ts_ms_to_iso(now_ms)
    if not last_result:
        return {
            "available": False,
            "now_at": now_at,
            "last_action_id": None,
            "last_action_family": None,
            "last_action_status": None,
            "last_action_outcome": None,
            "last_action_summary": None,
            "last_action_reason_tags": [],
            "last_action_why": None,
            "last_action_observed_at": None,
            "seconds_since_last_action": None,
        }

    notes = _planner_last_result_notes(last_result)
    note = notes[0] if notes else {}
    updated_ts_ms = last_result.get("updated_ts_ms")
    seconds_since_last_action: float | None = None
    if isinstance(updated_ts_ms, int):
        seconds_since_last_action = max(0.0, round((now_ms - updated_ts_ms) / 1000.0, 3))

    return {
        "available": bool(note),
        "now_at": now_at,
        "last_action_id": str(last_result.get("action_id") or "").strip() or None,
        "last_action_family": note.get("family"),
        "last_action_status": str(last_result.get("status") or "").strip().upper() or None,
        "last_action_outcome": note.get("outcome"),
        "last_action_summary": note.get("summary"),
        "last_action_reason_tags": list(note.get("reason_tags") or []),
        "last_action_why": str(last_result.get("plan_why") or "").strip() or None,
        "last_action_observed_at": note.get("observed_at") or None,
        "seconds_since_last_action": seconds_since_last_action,
    }


def _planner_freshness(facts: dict[str, Scalar]) -> JsonDict:
    now_ms = int(time.time() * 1000)
    now_at = _ts_ms_to_iso(now_ms)
    raw_observed_at = _string_or_none(
        facts.get("printer_1.raw_observed_at") or facts.get("printer_1.raw.observed_at")
    )
    vision_observed_at = _string_or_none(facts.get("printer_1.vision_observed_at"))
    camera_last_capture_at = _string_or_none(facts.get("printer_1.nozzle_cam_last_capture_at"))
    camera_frame_age_s = _float_or_none(facts.get("printer_1.nozzle_cam_frame_age_s"))
    vision_usable = bool(facts.get("printer_1.nozzle_cam_usable", False)) and bool(
        _string_or_none(facts.get("printer_1.vision_signal_summary") or facts.get("printer_1.vision_advisory_summary"))
    )
    return {
        "now_at": now_at,
        "raw_observed_at": raw_observed_at,
        "seconds_since_raw_observation": _seconds_since_iso(raw_observed_at, now_ms=now_ms),
        "vision_observed_at": vision_observed_at,
        "seconds_since_vision_observation": _seconds_since_iso(vision_observed_at, now_ms=now_ms),
        "camera_last_capture_at": camera_last_capture_at,
        "camera_frame_age_s": round(camera_frame_age_s, 3) if camera_frame_age_s is not None else None,
        "vision_usable": vision_usable,
    }


def _planner_symptom_feedback(facts: dict[str, Scalar], last_result: JsonDict) -> JsonDict:
    current_signal = _planner_vision_signal(facts)
    last_signal = _result_vision_signal(last_result.get("result") or {})
    current_findings = list(current_signal.get("finding_types") or [])
    last_findings = list(last_signal.get("finding_types") or [])
    overlap = set(current_findings) & set(last_findings)
    current_issue_level = _normalize_issue_level(current_signal.get("issue_level"))
    last_issue_level = _normalize_issue_level(last_signal.get("issue_level"))
    same_issue_persisted = bool(
        current_signal.get("usable")
        and last_signal.get("usable")
        and overlap
    )
    not_improving_after_last_action = same_issue_persisted and (
        _issue_level_rank(current_issue_level) >= _issue_level_rank(last_issue_level)
    )
    return {
        "current_finding_types": current_findings,
        "current_strength": _normalize_strength(current_signal.get("strength")),
        "current_issue_level": current_issue_level,
        "last_verified_finding_types": last_findings,
        "last_verified_strength": _normalize_strength(last_signal.get("strength")),
        "last_verified_issue_level": last_issue_level,
        "same_issue_persisted": same_issue_persisted,
        "not_improving_after_last_action": not_improving_after_last_action,
    }


def _action_family(action_id: str, verb: str) -> str:
    text = f"{action_id} {verb}".upper()
    if "SPEED" in text:
        return "speed"
    if "FLOW" in text:
        return "flow"
    if "NOZZLE" in text:
        return "nozzle"
    if "BED" in text:
        return "bed"
    return "other"


def _last_result_summary(*, action_id: str, outcome: str, result: JsonDict, error: JsonDict) -> str:
    post_signal = _result_vision_signal(result)
    if outcome == "verified_effect":
        if post_signal.get("usable") and post_signal.get("finding_types"):
            findings = ",".join(post_signal["finding_types"])
            issue_level = post_signal.get("issue_level") or "unknown"
            return (
                f"{action_id} produced a verified actuator change, but the post-action symptom signal still showed "
                f"{findings} ({issue_level})."
            )
        return f"{action_id} produced a verified effect."
    if outcome == "reset_restored":
        return f"{action_id} restored the baseline."
    if outcome == "verification_mismatch":
        return f"{action_id} did not verify on supported surfaces."
    if outcome == "suppressed":
        return f"{action_id} was suppressed or aborted."
    if outcome == "no_action":
        return f"{action_id} resulted in no state change."
    if error:
        return f"{action_id} was blocked: {json.dumps(error, sort_keys=True)[:160]}"
    if result:
        return f"{action_id} completed with result {json.dumps(result, sort_keys=True)[:160]}"
    return f"{action_id} completed with outcome {outcome}."


def _result_vision_signal(result: JsonDict) -> JsonDict:
    if not isinstance(result, dict):
        return {"usable": False, "finding_types": [], "strength": None, "issue_level": None}
    payload = result.get("post_action_vision_signal") or {}
    if not isinstance(payload, dict):
        return {"usable": False, "finding_types": [], "strength": None, "issue_level": None}
    finding_types = payload.get("finding_types")
    if not isinstance(finding_types, list):
        finding_types = []
    return {
        "usable": bool(payload.get("usable")) and bool(finding_types),
        "finding_types": [str(item) for item in finding_types if str(item).strip()],
        "strength": _normalize_strength(payload.get("strength")),
        "issue_level": _normalize_issue_level(payload.get("issue_level")),
    }


def _normalize_strength(value: Any) -> str | None:
    text = _string_or_none(value)
    if text in {"weak", "moderate", "strong"}:
        return text
    return None


def _normalize_issue_level(value: Any) -> str | None:
    text = _string_or_none(value)
    if text in {"low", "medium", "high"}:
        return text
    return None


def _vision_issue_level(finding_types: list[str], strength: str | None) -> str | None:
    if not finding_types:
        return None
    findings = set(finding_types)
    if "spaghetti" in findings or "stringing" in findings or "blob" in findings:
        return "high"
    if "residue" in findings:
        return "high" if strength == "strong" else "medium"
    return "low"


def _issue_level_rank(value: str | None) -> int:
    return {
        None: -1,
        "low": 0,
        "medium": 1,
        "high": 2,
    }.get(value, -1)


def _split_blockers(value: Scalar) -> list[str]:
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    return [item for item in text.split("|") if item]


def _string_or_none(value: Scalar) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float_or_none(value: Scalar) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_from_fact(value: Scalar) -> Any:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _ts_ms_to_iso(value: Any) -> str:
    if not isinstance(value, int):
        return ""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _seconds_since_iso(value: str | None, *, now_ms: int) -> float | None:
    if not value:
        return None
    from datetime import datetime, timezone

    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    observed_ms = int(observed.timestamp() * 1000.0)
    return max(0.0, round((now_ms - observed_ms) / 1000.0, 3))


class ExecutionResult(BaseModel):
    """Outcome returned by a pack or builtin executor."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "failed", "expired", "in_flight"]
    result: JsonDict = Field(default_factory=dict)
    error: JsonDict | None = None


class ApprovalRecord(BaseModel):
    """Bound, expiring human approval."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str
    action_run_id: str
    args_hash: str
    approved_by: str
    decision: Literal["APPROVE", "REJECT"]
    created_ts_ms: int
    expires_ts_ms: int


class PackManifest(BaseModel):
    """Minimal manifest read from `manifest.yaml`.

    The manifest intentionally carries just enough information for discovery,
    documentation, and operator visibility.  Higher-order logic stays in code,
    because once behavior gets truly dynamic, deeply embedding it in YAML creates
    more accidental complexity than it removes.
    """

    model_config = ConfigDict(extra="forbid")

    pack_id: str
    display_name: str
    category: str
    python_entrypoint: str
    capabilities: list[str] = Field(default_factory=list)
    detection: dict[str, Any] = Field(default_factory=dict)
    resources: list[dict[str, Any]] = Field(default_factory=list)
    notes: str | None = None
