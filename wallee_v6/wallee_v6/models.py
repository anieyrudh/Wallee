"""Core data contracts for Wallee v6.5.

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
    recent_results: list[JsonDict] = Field(default_factory=list)
    pending_human: list[str] = Field(default_factory=list)
    compilation: WorldCompilationContext | None = None
    prompt_frontier_limit: int | None = None

    def prompt_view(self) -> JsonDict:
        """Return the compact planner-facing representation of this packet."""
        explicit_frontier = [action.prompt_view() for action in self.frontier if not _is_tuning_action(action)]
        if self.prompt_frontier_limit is not None:
            explicit_frontier = explicit_frontier[: max(0, self.prompt_frontier_limit)]
        allowed_frontier_ids = [action["id"] for action in explicit_frontier]
        tuning_action_space = _planner_tuning_action_space(self.frontier)
        prompt_facts = _planner_prompt_facts(self.facts)
        return {
            "goal": self.goal,
            "devices": [summary.model_dump() for summary in self.device_summaries],
            "facts": prompt_facts,
            "resources": [resource.model_dump() for resource in self.resources],
            "blockers": self.blockers,
            "deltas": [delta.model_dump() for delta in self.deltas],
            "frontier": explicit_frontier,
            "last_result": self.last_result,
            "pending_human": self.pending_human,
            "decision_contract": {
                "explicit_frontier_non_tuning_only": True,
                "tuning_action_space_independently_legal": True,
                "do_not_infer_tuning_illegality_from_explicit_frontier": True,
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
                "recent_family_actions": _planner_recent_family_actions(self.recent_results),
                "temporal_action_context": _planner_temporal_action_context(self.last_result),
                "freshness": _planner_freshness(self.facts),
                "runtime_trends": _planner_runtime_trends(self.facts),
                "symptom_feedback": _planner_symptom_feedback(self.facts, self.last_result),
                "action_consequence": _planner_action_consequence(self.facts, self.last_result),
                "tuning_settle_context": _planner_tuning_settle_context(self.facts, self.last_result),
                "cumulative_family_deltas": _planner_cumulative_family_deltas(self.facts),
                "family_blockers": _planner_family_blockers(self.facts, self.last_result),
                "allowed_frontier_ids": allowed_frontier_ids,
                "tuning_action_space": tuning_action_space,
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
                "items": {"enum": ["speed", "flow", "nozzle", "bed", "pressure_advance"], "type": "string"},
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
            "family": {"enum": ["speed", "flow", "nozzle", "bed", "pressure_advance", "accel", "other"], "type": "string"},
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
    recent_family_action = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action_id": {"type": "string"},
            "family": {"enum": ["speed", "flow", "nozzle", "bed", "pressure_advance", "accel", "other"], "type": "string"},
            "status": {"type": "string"},
            "outcome": {
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
            "direction": {"type": ["string", "null"], "enum": ["up", "down", "restore", None]},
            "size": {"type": ["string", "null"], "enum": ["small", "big", "restore", None]},
            "summary": {"type": "string"},
            "observed_at": {"type": ["string", "null"]},
            "seconds_since_action": {"type": ["number", "null"]},
        },
        "required": [
            "action_id",
            "family",
            "status",
            "outcome",
            "direction",
            "size",
            "summary",
            "observed_at",
            "seconds_since_action",
        ],
    }
    cumulative_family_deltas = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "speed": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "current": {"type": ["number", "null"]},
                    "default": {"type": ["number", "null"]},
                    "delta": {"type": ["number", "null"]},
                    "unit": {"const": "pct"},
                },
                "required": ["current", "default", "delta", "unit"],
            },
            "flow": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "current": {"type": ["number", "null"]},
                    "default": {"type": ["number", "null"]},
                    "delta": {"type": ["number", "null"]},
                    "unit": {"const": "pct"},
                },
                "required": ["current", "default", "delta", "unit"],
            },
            "nozzle": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "current": {"type": ["number", "null"]},
                    "default": {"type": ["number", "null"]},
                    "delta": {"type": ["number", "null"]},
                    "unit": {"const": "c"},
                },
                "required": ["current", "default", "delta", "unit"],
            },
            "bed": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "current": {"type": ["number", "null"]},
                    "default": {"type": ["number", "null"]},
                    "delta": {"type": ["number", "null"]},
                    "unit": {"const": "c"},
                },
                "required": ["current", "default", "delta", "unit"],
            },
            "pressure_advance": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "current": {"type": ["number", "null"]},
                    "default": {"type": ["number", "null"]},
                    "delta": {"type": ["number", "null"]},
                    "unit": {"const": "ratio"},
                },
                "required": ["current", "default", "delta", "unit"],
            },
        },
        "required": ["speed", "flow", "nozzle", "bed", "pressure_advance"],
    }
    tuning_family_option = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "family": {"enum": ["speed", "flow", "nozzle", "bed", "pressure_advance"], "type": "string"},
            "summary": {"type": "string"},
            "allowed_directions": {"type": "array", "items": {"enum": ["up", "down"], "type": "string"}},
            "allowed_magnitudes": {"type": "array", "items": {"enum": ["small", "big"], "type": "string"}},
        },
        "required": ["family", "summary", "allowed_directions", "allowed_magnitudes"],
    }
    tuning_action_space = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "speed": tuning_family_option,
            "flow": tuning_family_option,
            "nozzle": tuning_family_option,
            "bed": tuning_family_option,
            "pressure_advance": tuning_family_option,
        },
    }
    temporal_action_context = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "available": {"type": "boolean"},
            "now_at": {"type": "string"},
            "last_action_id": {"type": ["string", "null"]},
            "last_action_family": {"type": ["string", "null"], "enum": ["speed", "flow", "nozzle", "bed", "pressure_advance", "accel", "other", None]},
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
    action_consequence = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "available": {"type": "boolean"},
            "action_id": {"type": ["string", "null"]},
            "family": {"type": ["string", "null"], "enum": ["speed", "flow", "nozzle", "bed", "pressure_advance", "accel", "other", None]},
            "status": {"type": ["string", "null"]},
            "time_since_action_s": {"type": ["number", "null"]},
            "post_action_issue_types": {"type": "array", "items": {"type": "string"}},
            "current_issue_types": {"type": "array", "items": {"type": "string"}},
            "issue": {"type": "string", "enum": ["better", "same", "worse", "unknown"]},
            "confidence": {"type": ["string", "null"], "enum": ["weak", "moderate", "strong", None]},
            "summary": {"type": ["string", "null"]},
            "compared_against_action_id": {"type": ["string", "null"]},
            "valid_for_escalation": {"type": "boolean"},
        },
        "required": [
            "available",
            "action_id",
            "family",
            "status",
            "time_since_action_s",
            "post_action_issue_types",
            "current_issue_types",
            "issue",
            "confidence",
            "summary",
            "compared_against_action_id",
            "valid_for_escalation",
        ],
    }
    tuning_settle_context = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "active": {"type": "boolean"},
            "family": {"type": ["string", "null"], "enum": ["speed", "flow", "nozzle", "bed", "pressure_advance", "accel", None]},
            "action_id": {"type": ["string", "null"]},
            "window_s": {"type": ["number", "null"]},
            "remaining_s": {"type": ["number", "null"]},
            "blocks_same_family": {"type": "boolean"},
            "cross_family_requires_fresh_strong_signal": {"type": "boolean"},
            "cross_family_signal_allows_action": {"type": "boolean"},
        },
        "required": [
            "active",
            "family",
            "action_id",
            "window_s",
            "remaining_s",
            "blocks_same_family",
            "cross_family_requires_fresh_strong_signal",
            "cross_family_signal_allows_action",
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
                    "explicit_frontier_non_tuning_only": {"const": True},
                    "tuning_action_space_independently_legal": {"const": True},
                    "do_not_infer_tuning_illegality_from_explicit_frontier": {"const": True},
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
                    "explicit_frontier_non_tuning_only",
                    "tuning_action_space_independently_legal",
                    "do_not_infer_tuning_illegality_from_explicit_frontier",
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
                            "planner_facts": {"type": "object", "additionalProperties": True},
                        },
                        "required": ["global", "local", "merged_reason_tags", "planner_facts"],
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
                    "recent_family_actions": {"type": "array", "items": recent_family_action},
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
                    "action_consequence": action_consequence,
                    "tuning_settle_context": tuning_settle_context,
                    "cumulative_family_deltas": cumulative_family_deltas,
                    "family_blockers": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "speed": {"type": "array", "items": {"type": "string"}},
                            "flow": {"type": "array", "items": {"type": "string"}},
                            "nozzle": {"type": "array", "items": {"type": "string"}},
                            "bed": {"type": "array", "items": {"type": "string"}},
                            "pressure_advance": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["speed", "flow", "nozzle", "bed", "pressure_advance"],
                    },
                    "allowed_frontier_ids": {"type": "array", "items": {"type": "string"}},
                    "tuning_action_space": tuning_action_space,
                },
                "required": [
                    "notebook_notes",
                    "vision_signal",
                    "last_result_notes",
                    "recent_family_actions",
                    "temporal_action_context",
                    "freshness",
                    "symptom_feedback",
                    "action_consequence",
                    "tuning_settle_context",
                    "cumulative_family_deltas",
                    "family_blockers",
                    "allowed_frontier_ids",
                    "tuning_action_space",
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

    The planner may either choose an explicit non-tuning frontier action ID or a
    compact tuning choice that deterministic code resolves into one exact legal
    action ID.
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["NO_ACTION", "EXECUTE", "CALL_HUMAN"]
    sequence: list[str] = Field(default_factory=list)
    why: str
    call_human_message: str | None = None
    tuning_choice: "TuningChoice | None" = None

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


class TuningChoice(BaseModel):
    """Compact tuning selector returned by the planner."""

    model_config = ConfigDict(extra="forbid")

    family: Literal["speed", "flow", "nozzle", "bed", "pressure_advance", "accel"]
    direction: Literal["up", "down"]
    magnitude: Literal["small", "big"]


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
        return {"global": [], "local": [], "merged_reason_tags": [], "planner_facts": {}}
    planner_facts = payload.get("planner_facts", {}) or {}
    if isinstance(planner_facts, dict):
        planner_facts = dict(planner_facts)
        active_section = planner_facts.get("active_section")
        if isinstance(active_section, dict):
            active_section = dict(active_section)
            gcode_facts = active_section.get("gcode_facts")
            if isinstance(gcode_facts, dict):
                gcode_facts = {
                    key: value for key, value in gcode_facts.items() if "m204" not in str(key).lower()
                }
                active_section["gcode_facts"] = gcode_facts
            planner_facts["active_section"] = active_section
    return {
        "global": [_planner_sanitize_active_note_view(note) for note in payload.get("global_notes", []) or []],
        "local": [_planner_sanitize_active_note_view(note) for note in payload.get("local_notes", []) or []],
        "merged_reason_tags": payload.get("merged_reason_tags", []) or [],
        "planner_facts": planner_facts if isinstance(planner_facts, dict) else {},
    }


def _planner_family_blockers(facts: dict[str, Scalar], last_result: JsonDict | None = None) -> JsonDict:
    blockers = {
        "speed": _split_blockers(facts.get("printer_1.speed_autonomy_blockers")),
        "flow": _split_blockers(facts.get("printer_1.flow_shadow_blockers")),
        "nozzle": _split_blockers(facts.get("printer_1.nozzle_shadow_blockers")),
        "bed": _split_blockers(facts.get("printer_1.bed_shadow_blockers")),
        "pressure_advance": _split_blockers(facts.get("printer_1.pressure_advance_shadow_blockers")),
        "accel": _split_blockers(facts.get("printer_1.accel_shadow_blockers")),
    }
    settle = _planner_tuning_settle_context(facts, last_result or {})
    if settle.get("active"):
        settling_family = settle.get("family")
        cross_family_allowed = bool(settle.get("cross_family_signal_allows_action"))
        for family, family_blockers in blockers.items():
            if family == settling_family:
                family_blockers.append("same_family_settle_active")
            elif not cross_family_allowed:
                family_blockers.append("settle_waiting_for_fresh_strong_signal")
    for family in _PLANNER_HIDDEN_TUNING_FAMILIES:
        blockers.pop(family, None)
    return blockers


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


def _fresh_strong_vision_for_cross_family_settle(facts: dict[str, Scalar]) -> bool:
    signal = _planner_vision_signal(facts)
    freshness = _planner_freshness(facts)
    frame_age_s = _float_or_none(freshness.get("camera_frame_age_s"))
    interval_s = _float_or_none(facts.get("printer_1.vision_advisory_interval_s")) or 10.0
    fresh_window_s = max(15.0, interval_s * 1.5)
    return (
        bool(signal.get("usable"))
        and signal.get("issue_level") == "high"
        and signal.get("strength") in {"moderate", "strong"}
        and frame_age_s is not None
        and frame_age_s <= fresh_window_s
    )


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


def _planner_tuning_settle_context(facts: dict[str, Scalar], last_result: JsonDict) -> JsonDict:
    now_ms = int(time.time() * 1000)
    action_id = str((last_result or {}).get("action_id") or "").strip()
    family = _action_family(action_id, str((last_result or {}).get("verb") or "")) if action_id else None
    status = str((last_result or {}).get("status") or "").strip().upper()
    updated_ts_ms = (last_result or {}).get("updated_ts_ms")
    window_s = _settle_window_for_family(facts, family)
    seconds_since_action: float | None = None
    remaining_s: float | None = None
    active = False
    if family in {"speed", "flow", "nozzle", "bed", "pressure_advance", "accel"} and status == "DONE" and isinstance(updated_ts_ms, int):
        seconds_since_action = max(0.0, round((now_ms - updated_ts_ms) / 1000.0, 3))
        remaining_s = max(0.0, round(window_s - seconds_since_action, 3))
        active = remaining_s > 0.0
    return {
        "active": active,
        "family": family if active else None,
        "action_id": action_id if active else None,
        "window_s": window_s if active else None,
        "remaining_s": remaining_s if active else None,
        "blocks_same_family": active,
        "cross_family_requires_fresh_strong_signal": active,
        "cross_family_signal_allows_action": active and _fresh_strong_vision_for_cross_family_settle(facts),
    }


def _settle_window_for_family(facts: dict[str, Scalar], family: str | None) -> float:
    if family in {"nozzle", "bed"}:
        return _float_or_none(facts.get("printer_1.thermal_tuning_settle_window_s")) or 60.0
    if family == "pressure_advance":
        return _float_or_none(facts.get("printer_1.pressure_advance_tuning_settle_window_s")) or 45.0
    if family == "accel":
        return _float_or_none(facts.get("printer_1.accel_tuning_settle_window_s")) or 45.0
    return _float_or_none(facts.get("printer_1.tuning_settle_window_s")) or 25.0


def _planner_action_consequence(facts: dict[str, Scalar], last_result: JsonDict) -> JsonDict:
    temporal = _planner_temporal_action_context(last_result)
    action_id = temporal.get("last_action_id")
    result = (last_result or {}).get("result") or {}
    signal = _result_vision_signal(result if isinstance(result, dict) else {})
    comparison_delta = _normalize_comparison_delta(signal.get("comparison_delta"))
    comparison_confidence = _normalize_strength(signal.get("comparison_confidence"))
    current_signal = _planner_vision_signal(facts)
    post_issue_types = signal.get("finding_types") or []
    current_issue_types = current_signal.get("finding_types") or []
    comparable_material_issues = _material_issue_types(post_issue_types) & _material_issue_types(current_issue_types)
    repeated_non_threat_only = comparison_delta in {"same", "worse"} and not comparable_material_issues
    valid_for_escalation = (
        comparison_delta in {"same", "worse"}
        and comparison_confidence in {"moderate", "strong"}
        and bool(comparable_material_issues)
    )
    if repeated_non_threat_only:
        comparison_delta = "unknown"
        comparison_summary = "Only non-threatening repeated residue was comparable; ignore it for escalation."
    else:
        comparison_summary = signal.get("comparison_summary") or temporal.get("last_action_summary")
    return {
        "available": bool(action_id),
        "action_id": action_id,
        "family": temporal.get("last_action_family"),
        "status": temporal.get("last_action_status"),
        "time_since_action_s": temporal.get("seconds_since_last_action"),
        "post_action_issue_types": post_issue_types,
        "current_issue_types": current_issue_types,
        "issue": comparison_delta or "unknown",
        "confidence": comparison_confidence,
        "summary": comparison_summary,
        "compared_against_action_id": action_id if comparison_delta in {"better", "same", "worse"} else None,
        "valid_for_escalation": valid_for_escalation,
    }


def _material_issue_types(issue_types: list[str]) -> set[str]:
    return {str(item) for item in issue_types if str(item) not in {"residue"}}


def _planner_recent_family_actions(recent_results: list[JsonDict]) -> list[JsonDict]:
    now_ms = int(time.time() * 1000)
    payload: list[JsonDict] = []
    for item in recent_results[:3]:
        if not isinstance(item, dict):
            continue
        action_id = str(item.get("action_id") or "").strip()
        if not action_id:
            continue
        notes = _planner_last_result_notes(item)
        note = notes[0] if notes else {}
        updated_ts_ms = item.get("updated_ts_ms")
        seconds_since_action: float | None = None
        if isinstance(updated_ts_ms, int):
            seconds_since_action = max(0.0, round((now_ms - updated_ts_ms) / 1000.0, 3))
        payload.append(
            {
                "action_id": action_id,
                "family": note.get("family") or _action_family(action_id, str(item.get("verb") or "")),
                "status": str(item.get("status") or "").strip().upper(),
                "outcome": note.get("outcome"),
                "direction": _action_direction(action_id),
                "size": _action_size(action_id),
                "summary": note.get("summary") or _last_result_summary(
                    action_id=action_id,
                    outcome=str(note.get("outcome") or "blocked"),
                    result=item.get("result") or {},
                    error=item.get("error") or {},
                ),
                "observed_at": note.get("observed_at") or None,
                "seconds_since_action": seconds_since_action,
            }
        )
    return payload


def _planner_cumulative_family_deltas(facts: dict[str, Scalar]) -> JsonDict:
    payload = {
        "speed": _cumulative_delta_entry(
            current=facts.get("printer_1.speed_pct"),
            default=facts.get("printer_1.speed_default_pct"),
            unit="pct",
        ),
        "flow": _cumulative_delta_entry(
            current=facts.get("printer_1.flow_pct"),
            default=facts.get("printer_1.flow_default_pct"),
            unit="pct",
        ),
        "nozzle": _cumulative_delta_entry(
            current=facts.get("printer_1.nozzle_target_c"),
            default=facts.get("printer_1.job_nozzle_target_c_default"),
            unit="c",
        ),
        "bed": _cumulative_delta_entry(
            current=facts.get("printer_1.bed_target_c"),
            default=facts.get("printer_1.job_bed_target_c_default"),
            unit="c",
        ),
        "pressure_advance": _cumulative_delta_entry(
            current=facts.get("printer_1.pressure_advance"),
            default=facts.get("printer_1.active_pressure_advance_baseline"),
            unit="ratio",
        ),
        "accel": _cumulative_delta_entry(
            current=facts.get("printer_1.print_accel_mm_s2"),
            default=facts.get("printer_1.active_print_accel_baseline_mm_s2"),
            unit="mm_s2",
        ),
    }
    for family in _PLANNER_HIDDEN_TUNING_FAMILIES:
        payload.pop(family, None)
    return payload


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


def _planner_runtime_trends(facts: dict[str, Scalar]) -> JsonDict:
    return {
        "sample_elapsed_s": _float_or_none(facts.get("printer_1.trend_sample_elapsed_s")),
        "job_time_delta_s": _float_or_none(facts.get("printer_1.job_time_delta_s")),
        "progress_delta_pct": _float_or_none(facts.get("printer_1.progress_delta_pct")),
        "nozzle_temp_delta_c": _float_or_none(facts.get("printer_1.nozzle_temp_delta_c")),
        "bed_temp_delta_c": _float_or_none(facts.get("printer_1.bed_temp_delta_c")),
        "nozzle_temp_trend": _string_or_none(facts.get("printer_1.nozzle_temp_trend")) or "unknown",
        "bed_temp_trend": _string_or_none(facts.get("printer_1.bed_temp_trend")) or "unknown",
        "job_time_advancing": bool(facts.get("printer_1.job_time_advancing", False)),
        "progress_advancing": bool(facts.get("printer_1.progress_advancing", False)),
        "targets_nonzero": bool(facts.get("printer_1.targets_nonzero", False)),
        "thermal_ramp_active": bool(facts.get("printer_1.thermal_ramp_active", False)),
        "pre_tuning_window": bool(facts.get("printer_1.pre_tuning_window", False)),
        "active_print_evidence": _string_or_none(facts.get("printer_1.active_print_evidence")) or "unknown",
        "motion_confirmed": facts.get("printer_1.motion_confirmed")
        if isinstance(facts.get("printer_1.motion_confirmed"), bool)
        else None,
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
    if "PRESSURE_ADVANCE" in text:
        return "pressure_advance"
    if "_ACCEL_" in text or "PRINT_ACCEL" in text or "ACCELERATION" in text:
        return "accel"
    if "SPEED" in text:
        return "speed"
    if "FLOW" in text:
        return "flow"
    if "NOZZLE" in text:
        return "nozzle"
    if "BED" in text:
        return "bed"
    return "other"


def _action_direction(action_id: str) -> str | None:
    text = action_id.upper()
    if "RESTORE" in text:
        return "restore"
    if "_DOWN_" in text:
        return "down"
    if "_UP_" in text:
        return "up"
    return None


def _action_size(action_id: str) -> str | None:
    text = action_id.upper()
    if "RESTORE" in text:
        return "restore"
    if "_BIG" in text:
        return "big"
    if "_SMALL" in text:
        return "small"
    return None


def _cumulative_delta_entry(*, current: Scalar, default: Scalar, unit: str) -> JsonDict:
    current_value = _float_or_none(current)
    default_value = _float_or_none(default)
    delta = None
    if current_value is not None and default_value is not None:
        delta = round(current_value - default_value, 4)
    return {
        "current": current_value,
        "default": default_value,
        "delta": delta,
        "unit": unit,
    }


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
        "comparison_delta": _normalize_comparison_delta(payload.get("comparison_delta")),
        "comparison_confidence": _normalize_strength(payload.get("comparison_confidence")),
        "comparison_summary": _string_or_none(payload.get("comparison_summary")),
    }


def _normalize_comparison_delta(value: Any) -> str | None:
    text = _string_or_none(value)
    if text in {"better", "same", "worse", "unknown"}:
        return text
    return None


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
        return "low"
    return "low"


def _issue_level_rank(value: str | None) -> int:
    return {
        None: -1,
        "low": 0,
        "medium": 1,
        "high": 2,
    }.get(value, -1)


_TUNING_FAMILY_SUMMARIES: dict[str, str] = {
    "speed": "Adjust print motion rate.",
    "flow": "Adjust commanded extrusion amount.",
    "nozzle": "Adjust nozzle target temperature.",
    "bed": "Adjust bed target temperature.",
    "pressure_advance": "Adjust how aggressively the printer bleeds off leftover nozzle pressure during starts, stops, and direction changes.",
    "accel": "Adjust how gently or aggressively the printer pulls away from features and changes speed.",
}


def _action_family_from_id(action_id: str) -> str | None:
    text = action_id.upper()
    if "_TRIM_SPEED_" in text:
        return "speed"
    if "_TRIM_FLOW_" in text:
        return "flow"
    if "_TRIM_NOZZLE_" in text:
        return "nozzle"
    if "_TRIM_BED_" in text:
        return "bed"
    if "_TRIM_PRESSURE_ADVANCE_" in text:
        return "pressure_advance"
    if "_TRIM_ACCEL_" in text:
        return "accel"
    return None


def _action_direction_from_id(action_id: str) -> str | None:
    text = action_id.upper()
    if "_DOWN_" in text:
        return "down"
    if "_UP_" in text:
        return "up"
    return None


def _action_magnitude_from_id(action_id: str) -> str | None:
    text = action_id.upper()
    if "_BIG" in text:
        return "big"
    if "_SMALL" in text:
        return "small"
    return None


def _is_tuning_action(action: LegalAction) -> bool:
    return _action_family_from_id(action.action_id) is not None


def _planner_tuning_action_space(frontier: list[LegalAction]) -> JsonDict:
    space: JsonDict = {}
    for action in frontier:
        family = _action_family_from_id(action.action_id)
        direction = _action_direction_from_id(action.action_id)
        magnitude = _action_magnitude_from_id(action.action_id)
        if family is None or family in _PLANNER_HIDDEN_TUNING_FAMILIES or direction is None or magnitude is None:
            continue
        family_entry = space.setdefault(
            family,
            {
                "family": family,
                "summary": _TUNING_FAMILY_SUMMARIES[family],
                "allowed_directions": [],
                "allowed_magnitudes": [],
            },
        )
        if direction not in family_entry["allowed_directions"]:
            family_entry["allowed_directions"].append(direction)
        if magnitude not in family_entry["allowed_magnitudes"]:
            family_entry["allowed_magnitudes"].append(magnitude)
    return space


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


_PLANNER_HIDDEN_TUNING_FAMILIES = frozenset({"accel"})
_PLANNER_PROMPT_HIDDEN_FACT_SUFFIXES = (
    "_shadow_eligible",
    "_shadow_actions",
    "_shadow_blockers",
    "_autonomy_eligible",
    "_autonomy_actions",
    "_autonomy_blockers",
    "_autonomy_boundary",
)
_PLANNER_PROMPT_HIDDEN_FACT_KEYS = frozenset(
    {
        "printer_1.active_notes_json",
        "printer_1.vision_observation_ref",
        "printer_1.vision_frame_ref",
    }
)


def _planner_prompt_hides_fact_key(key: str) -> bool:
    if key in _PLANNER_PROMPT_HIDDEN_FACT_KEYS:
        return True
    if key.endswith(_PLANNER_PROMPT_HIDDEN_FACT_SUFFIXES):
        return True
    if key.startswith("printer_1.") and ("accel" in key or "m204" in key.lower()):
        return True
    return False


def _planner_prompt_facts(facts: dict[str, Scalar]) -> JsonDict:
    prompt_facts: JsonDict = {
        key: value for key, value in facts.items() if not _planner_prompt_hides_fact_key(key)
    }
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
    return prompt_facts


def _planner_sanitize_active_note_view(note: object) -> JsonDict:
    if not isinstance(note, dict):
        return {}
    sanitized = dict(note)
    suggested = sanitized.get("suggested_families")
    if isinstance(suggested, list):
        sanitized["suggested_families"] = [
            str(family) for family in suggested if str(family) not in _PLANNER_HIDDEN_TUNING_FAMILIES
        ]
    return sanitized


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


class SafetyProfile(BaseModel):
    """Declarative data a pack contributes so a generic, non-AI transport can
    stop its machine.

    The profile carries no code and no secrets: hosts and API keys are named
    by environment variable, so the out-of-process watchdog can read the same
    profile from disk and issue a stop even if the runtime never started.
    """

    model_config = ConfigDict(extra="forbid")

    transport: Literal["http", "file", "none"] = "none"
    host_env: str | None = None
    api_key_env: str | None = None
    api_key_header: str = "X-Api-Key"
    primary_request: dict[str, Any] | None = None
    fallback_request: dict[str, Any] | None = None


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
    safety_profile: SafetyProfile | None = None
    notes: str | None = None
