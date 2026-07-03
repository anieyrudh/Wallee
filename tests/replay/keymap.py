"""Translate the legacy 60-scenario replay corpus into v6 world-packet key-space.

The legacy corpus (see `docs/internal/REPLAY_BASELINE_2026-03.md` for its
honest 47/60 baseline) encoded scenarios against the v5 Redis whiteboard keys
and the v5 free-form tool vocabulary. This module is the single source of
truth for how that world maps onto v6:

- fact keys translate to `printer_1.*` world-packet facts (or are recorded in
  `UNMAPPED_FACT_KEYS` with a reason — silence is not an option; a new
  unknown key fails the suite),
- legacy tools translate to v6 frontier action ids, tuning families, the
  CALL_HUMAN decision, or are `unrepresentable` — v6 has no free-form tool
  channel, which is itself the strongest gate,
- each scenario emits (i) a WorldPacket for model-independent gate
  assertions and (ii) an eval case for the live planner-eval lane.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from wallee.models import DeviceSummary, HazardClass, LegalAction, WorldPacket

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"

# Legacy whiteboard keys with no v6 world-packet equivalent. The telemetry
# lane (fan RPMs, voltages, stall detection) is deliberate ROADMAP work; the
# rest are v5 loop internals that v6 replaced structurally.
UNMAPPED_FACT_KEYS: dict[str, str] = {
    "job.phase_detail": "v5 prose phase detail; v6 uses structured lifecycle/printing_phase",
    "job.time_in_phase_s": "v5 loop internal; v6 freshness lane covers staleness",
    "job.phase_changes_30s": "v5 flapping counter; no v6 equivalent yet",
    "printer.state_changes_30s": "v5 flapping counter; no v6 equivalent yet",
    "print.queue": "v5 queue concept; v6 has requested_file only",
    "printer.curr_nozzle": "v5 nozzle hardware id; not planner-relevant in v6",
    "printer.door_sensor": "telemetry lane (ROADMAP)",
    "printer.fan_heatbreak_rpm": "telemetry lane (ROADMAP)",
    "printer.fan_print_pwm": "telemetry lane (ROADMAP)",
    "printer.fan_print_rpm": "telemetry lane (ROADMAP)",
    "printer.fan_print_state": "telemetry lane (ROADMAP)",
    "printer.fsensor_flow": "telemetry lane (ROADMAP)",
    "printer.fsensor_flow:trend": "telemetry lane (ROADMAP)",
    "printer.fsensor_state": "telemetry lane (ROADMAP)",
    "printer.oc_input": "telemetry lane (ROADMAP)",
    "printer.oc_nozzle": "telemetry lane (ROADMAP)",
    "printer.pos_z": "telemetry lane (ROADMAP)",
    "printer.stepper_stall": "telemetry lane (ROADMAP)",
    "printer.stepper_stall:trend": "telemetry lane (ROADMAP)",
    "printer.temp_chamber": "telemetry lane (ROADMAP)",
    "printer.volt_bed": "telemetry lane (ROADMAP)",
    "printer.xbe_fan_1_rpm": "telemetry lane (ROADMAP)",
    "vision.confidence": "folded into evidence_strength on the mapped findings",
    "vision.normal": "implied by an empty finding set",
    "vision.bed_adhesion_ok": "no v6 finding-type yet (vision-vocabulary ROADMAP item)",
}

# Vision defect scores >= this are treated as a present finding.
VISION_SCORE_THRESHOLD = 0.5

# Legacy defect keys with a direct v6 finding-type enum value.
_VISION_ENUM = {
    "vision.spaghetti": "spaghetti",
    "vision.stringing": "stringing",
    "vision.blob": "blob",
}
# Legacy defect keys the v6 enum cannot yet name distinctly: they translate
# to "unknown" (the enum's escape hatch) until the vision-vocabulary
# extension (ROADMAP) gives them first-class types.
_VISION_TO_UNKNOWN = (
    "vision.overextrusion",
    "vision.underextrusion",
    "vision.warping",
    "vision.layer_shift",
    "vision.burn_marks",
)

# Legacy tool -> v6 representation. kinds:
#   action          -> a concrete frontier action id
#   tuning          -> a bounded tuning family (materializes to TRIM_* ids)
#   decision        -> a PlanIR decision, not an action
#   unrepresentable -> no v6 channel exists for this at all
TOOL_TRANSLATION: dict[str, dict[str, Any]] = {
    "pause_print": {"kind": "action", "action_id": "A_PRUSA_PAUSE"},
    "resume_print": {"kind": "action", "action_id": "A_PRUSA_RESUME"},
    "cancel_print": {"kind": "action", "action_id": "A_PRUSA_CANCEL"},
    "call_human": {"kind": "decision", "decision": "CALL_HUMAN"},
    "set_temperature": {"kind": "tuning", "families": ["nozzle", "bed"]},
    "set_speed_factor": {"kind": "tuning", "families": ["speed"]},
    "set_flow_factor": {"kind": "tuning", "families": ["flow"]},
    # v6 exposes no start/motion/G-code/memory/web channel to the planner.
    "start_print": {"kind": "unrepresentable"},
    "home_axes": {"kind": "unrepresentable"},
    "disable_motors": {"kind": "unrepresentable"},
    "set_position": {"kind": "unrepresentable"},
    "extrude": {"kind": "unrepresentable"},
    "retract": {"kind": "unrepresentable"},
    "read_endstops": {"kind": "unrepresentable"},
    "send_gcode": {"kind": "unrepresentable"},
    "discover_hardware": {"kind": "unrepresentable"},
    "remember": {"kind": "unrepresentable"},
    "web_search": {"kind": "unrepresentable"},
    "get_sensor_history": {"kind": "unrepresentable"},
}

_EXPECTED_TYPE_TO_DECISION = {
    "WAIT": "NO_ACTION",
    "ACTION": "EXECUTE",
    "ACTION_CHAIN": "EXECUTE",
    "CALL_HUMAN": "CALL_HUMAN",
}

_TRIM_ID = "A_PRUSA_TRIM_{family}_{direction}_SMALL"
_TUNING_FAMILIES = ("speed", "flow", "nozzle", "bed")


def load_scenarios() -> list[tuple[str, dict[str, Any]]]:
    return [
        (path.stem, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(SCENARIO_DIR.glob("*.json"))
    ]


def unknown_fact_keys(scenario: dict[str, Any]) -> list[str]:
    """Keys neither translated nor explicitly recorded as unmapped."""
    handled = set(UNMAPPED_FACT_KEYS) | {
        "printer.state",
        "printer.job_state",
        "job.phase",
        "printer.temp_nozzle",
        "printer.target_nozzle",
        "printer.temp_bed",
        "printer.target_bed",
        "printer.job_progress",
        "printer.speed",
        "printer.flow",
        "safety.estop",
        "camera.nozzle_status",
        "vision.status",
        *(_VISION_ENUM),
        *(_VISION_TO_UNKNOWN),
    }
    keys = set(scenario.get("whiteboard_state", {})) | set(scenario.get("vision_state", {}))
    return sorted(keys - handled)


def _lifecycle(scenario: dict[str, Any]) -> str:
    state = scenario.get("whiteboard_state", {})
    raw = str(state.get("job.phase") or state.get("printer.state") or "IDLE").upper()
    return "PRINTING" if raw == "PREPARING" else raw


def _printing_phase(scenario: dict[str, Any]) -> str:
    state = scenario.get("whiteboard_state", {})
    raw = str(state.get("job.phase") or state.get("printer.state") or "IDLE").upper()
    if raw == "PREPARING":
        return "preparing"
    return "active_printing" if raw == "PRINTING" else raw.lower()


def _vision_findings(scenario: dict[str, Any]) -> list[str]:
    vision = scenario.get("vision_state", {})
    if str(vision.get("vision.status") or "").upper() == "NORMAL":
        return []
    findings: list[str] = []
    for key, enum_value in _VISION_ENUM.items():
        score = vision.get(key)
        if isinstance(score, (int, float)) and score >= VISION_SCORE_THRESHOLD:
            findings.append(enum_value)
    if not findings:
        for key in _VISION_TO_UNKNOWN:
            score = vision.get(key)
            if isinstance(score, (int, float)) and score >= VISION_SCORE_THRESHOLD:
                findings.append("unknown")
                break
        else:
            if str(vision.get("vision.status") or "").upper() == "DEFECT":
                findings.append("unknown")
    return findings


def translate_facts(scenario: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return (v6 facts, estop_latched) for one legacy scenario."""
    state = scenario.get("whiteboard_state", {})
    vision = scenario.get("vision_state", {})
    lifecycle = _lifecycle(scenario)
    printing_phase = _printing_phase(scenario)
    job_active = lifecycle in {"PRINTING", "PAUSED"}
    estop = bool(state.get("safety.estop"))

    context = str(scenario.get("job_context") or "")
    file_match = re.search(r"File:\s*(\S+)", context)
    material_match = re.search(r"Material:\s*(\w+)", context)

    findings = _vision_findings(scenario)
    cam_usable = (
        str(state.get("camera.nozzle_status") or "").upper() != "OFFLINE"
        and bool(vision)
    )

    facts: dict[str, Any] = {
        "printer_1.lifecycle": lifecycle,
        "printer_1.health": "OK",
        "printer_1.job_active": job_active,
        "printer_1.printing_phase": printing_phase,
        "printer_1.active_printing": printing_phase == "active_printing",
        "printer_1.job_progress_pct": state.get("printer.job_progress"),
        "printer_1.nozzle_temp_c": state.get("printer.temp_nozzle"),
        "printer_1.nozzle_target_c": state.get("printer.target_nozzle"),
        "printer_1.bed_temp_c": state.get("printer.temp_bed"),
        "printer_1.bed_target_c": state.get("printer.target_bed"),
        "printer_1.speed_pct": state.get("printer.speed"),
        "printer_1.flow_pct": state.get("printer.flow"),
        "printer_1.current_file": file_match.group(1) if file_match else None,
        "printer_1.job_material": material_match.group(1) if material_match else None,
        "printer_1.nozzle_cam_usable": cam_usable,
    }
    if cam_usable and findings:
        facts["printer_1.vision_advisory_summary"] = "vision:" + ",".join(findings[:2]) + "; translated from replay corpus."
        facts["printer_1.vision_advisory_finding_types"] = "|".join(findings)
        facts["printer_1.vision_advisory_strength"] = "strong"
    if printing_phase == "active_printing":
        for family in _TUNING_FAMILIES:
            facts[f"printer_1.{family}_shadow_actions"] = "|".join(
                _TRIM_ID.format(family=family.upper(), direction=direction)
                for direction in ("DOWN", "UP")
            )
    return facts, estop


def build_frontier(facts: dict[str, Any]) -> list[LegalAction]:
    """The legality envelope the real pack would compile for this state."""
    lifecycle = str(facts.get("printer_1.lifecycle") or "")
    active = bool(facts.get("printer_1.active_printing"))

    def action(action_id: str, verb: str, hazard: HazardClass = HazardClass.LOW) -> LegalAction:
        return LegalAction(
            action_id=action_id,
            verb=verb,
            description=f"replay-translated {verb.lower()}",
            owner_pack="prusa_core_one_plus",
            execute_ref=verb.lower(),
            hazard_class=hazard,
        )

    frontier: list[LegalAction] = []
    if lifecycle == "PRINTING":
        frontier.append(action("A_PRUSA_PAUSE", "PAUSE"))
        frontier.append(action("A_PRUSA_CANCEL", "CANCEL"))
        if active:
            for family in _TUNING_FAMILIES:
                for direction in ("DOWN", "UP"):
                    frontier.append(
                        action(
                            _TRIM_ID.format(family=family.upper(), direction=direction),
                            f"TUNE_{family.upper()}",
                        )
                    )
    elif lifecycle == "PAUSED":
        frontier.append(action("A_PRUSA_RESUME", "RESUME"))
        frontier.append(action("A_PRUSA_CANCEL", "CANCEL"))
    return frontier


def build_world(scenario: dict[str, Any]) -> tuple[WorldPacket, bool]:
    facts, estop = translate_facts(scenario)
    frontier = build_frontier(facts)
    world = WorldPacket(
        goal="Improve the active print conservatively.",
        device_summaries=[
            DeviceSummary(
                device_id="printer_1",
                display_name="Prusa (replay)",
                category="additive",
                mode=str(facts.get("printer_1.lifecycle") or "IDLE"),
                health="OK",
                summary="replay-corpus translated world",
            )
        ],
        facts=facts,
        resources=[],
        blockers=[],
        deltas=[],
        frontier=frontier,
        last_result={},
        pending_human=[],
    )
    return world, estop


def tool_action_ids(tool: str, frontier_ids: set[str]) -> tuple[str, list[str]]:
    """Return (kind, concrete v6 action ids) for a legacy tool in this world."""
    spec = TOOL_TRANSLATION[tool]
    if spec["kind"] == "action":
        return "action", [spec["action_id"]]
    if spec["kind"] == "tuning":
        ids = [
            fid
            for fid in sorted(frontier_ids)
            for family in spec["families"]
            if f"_TRIM_{family.upper()}_" in fid
        ]
        return "tuning", ids
    if spec["kind"] == "decision":
        return "decision", []
    return "unrepresentable", []


def expected_decisions(scenario: dict[str, Any]) -> set[str]:
    raw = scenario.get("expected_type")
    values = raw if isinstance(raw, list) else [raw]
    return {_EXPECTED_TYPE_TO_DECISION[str(value)] for value in values}


def to_eval_case(name: str, scenario: dict[str, Any]) -> dict[str, Any]:
    """Emit one live planner-eval case (lane ii) from a legacy scenario."""
    facts, estop = translate_facts(scenario)
    frontier_ids = {a.action_id for a in build_frontier(facts)}
    unacceptable: list[str] = []
    for tool in scenario.get("unacceptable_tools", []):
        _, ids = tool_action_ids(tool, frontier_ids)
        unacceptable.extend(i for i in ids if i in frontier_ids)
    return {
        "name": name,
        "title": scenario.get("name"),
        "category": scenario.get("category"),
        "facts": facts,
        "estop": estop,
        "frontier_ids": sorted(frontier_ids),
        "expected_decisions": sorted(expected_decisions(scenario)),
        "unacceptable_action_ids": sorted(set(unacceptable)),
        "unacceptable_call_human": "call_human" in scenario.get("unacceptable_tools", []),
    }
