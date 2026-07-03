from __future__ import annotations

import json
from pathlib import Path

from wallee.config import Config
from wallee.models import DeviceSummary, HazardClass, LegalAction, PlanIR, TuningChoice, WorldPacket, world_packet_prompt_schema
from wallee.planner import OpenRouterPlanner, PromptPackage


def _world() -> WorldPacket:
    notes_payload = {
        "schema_version": "1.0",
        "job_hash": "job123",
        "selected_at": "2026-04-13T00:00:00Z",
        "selection_context": {
            "lifecycle": "PRINTING",
            "job_active": True,
            "job_progress_pct": 12.0,
            "current_layer": 10,
            "current_z_mm": 2.0,
            "current_section_ids": ["sec_0010"],
            "lookahead_section_ids": ["sec_0011"],
        },
        "global_notes": [
            {
                "note_id": "global_bridge_watch",
                "scope": "global",
                "title": "Bridge watch",
                "text": "Bridge sections exist.",
                "confidence": "high",
                "reason_tags": ["bridge"],
                "suggested_families": ["speed"],
                "section_ids": [],
            }
        ],
        "local_notes": [
            {
                "note_id": "local_flow_watch",
                "scope": "local",
                "title": "Flow watch",
                "text": "Flow-heavy region near current layer.",
                "confidence": "medium",
                "reason_tags": ["high_flow"],
                "suggested_families": ["flow"],
                "section_ids": ["sec_0010"],
            }
        ],
        "merged_reason_tags": ["bridge", "high_flow"],
        "planner_facts": {
            "active_section": {
                "section_id": "sec_0010",
                "progress_start_pct": 10.0,
                "progress_end_pct": 20.0,
                "remaining_progress_pct": 8.0,
                "remaining_time_s": 48.0,
                "gcode_facts": {
                    "long_travel_count": 2,
                    "travel_to_extrusion_ratio": 12.5,
                    "retraction_count": 1,
                    "active_m204_p_mm_s2": 800.0,
                    "active_m204_t_mm_s2": 1500.0,
                    "active_m204_r_mm_s2": 300.0,
                },
            },
            "geometry": {"repeated_tower_or_stringing_test": True},
        },
        "family_hints": {
            "speed": ["global_bridge_watch"],
            "flow": ["local_flow_watch"],
            "nozzle": [],
            "bed": [],
        },
    }
    return WorldPacket(
        goal="Improve the active print conservatively.",
        device_summaries=[
            DeviceSummary(
                device_id="printer_1",
                display_name="Prusa",
                category="additive",
                mode="PRINTING",
                health="OK",
                summary="Printing benchy",
            )
        ],
        facts={
            "printer_1.speed_autonomy_blockers": "speed_progress_not_ready|startup_thermal_not_ready",
            "printer_1.flow_shadow_blockers": "",
            "printer_1.nozzle_shadow_blockers": None,
            "printer_1.bed_shadow_blockers": "bed_progress_not_ready",
            "printer_1.accel_shadow_blockers": "accel_verification_unavailable",
            "printer_1.speed_autonomy_eligible": False,
            "printer_1.flow_shadow_actions": "A_PRUSA_TRIM_FLOW_DOWN_SMALL",
            "printer_1.print_accel_mm_s2": 2500.0,
            "printer_1.active_print_accel_baseline_mm_s2": 3000.0,
            "printer_1.active_notes_json": json.dumps(notes_payload, sort_keys=True),
            "printer_1.raw_observed_at": "2026-04-13T00:00:05Z",
            "printer_1.vision_observed_at": "2026-04-13T00:00:04Z",
            "printer_1.nozzle_cam_last_capture_at": "2026-04-13T00:00:04Z",
            "printer_1.nozzle_cam_frame_age_s": 0.4,
            "printer_1.nozzle_cam_usable": True,
            "printer_1.vision_advisory_summary": "vision:stringing; A thin strand is visible.",
            "printer_1.vision_advisory_finding_types": "stringing",
            "printer_1.vision_advisory_strength": "strong",
            "printer_1.vision_advisory_issue_level": "high",
        },
        resources=[],
        blockers=[],
        deltas=[],
        frontier=[
            LegalAction(
                action_id="A_PRUSA_TRIM_SPEED_DOWN_SMALL",
                verb="TUNE_SPEED",
                description="Reduce print speed a little while keeping the current print running.",
                owner_pack="prusa_core_one_plus",
                execute_ref="trim_speed_down_small",
                hazard_class=HazardClass.LOW,
            ),
            LegalAction(
                action_id="A_PRUSA_TRIM_FLOW_DOWN_SMALL",
                verb="TUNE_FLOW",
                description="Reduce flow a little while keeping the current print running.",
                owner_pack="prusa_core_one_plus",
                execute_ref="trim_flow_down_small",
                hazard_class=HazardClass.LOW,
            ),
        ],
        last_result={
            "action_id": "A_PRUSA_OPERATOR_RESTORE_FLOW_DEFAULT",
            "verb": "TUNE_FLOW",
            "status": "DONE",
            "result": {
                "effect": "restored_default",
                "post_action_vision_signal": {
                    "usable": True,
                    "summary": "vision:stringing; A thin strand is still visible.",
                    "finding_types": ["stringing"],
                    "strength": "strong",
                    "issue_level": "high",
                    "comparison_delta": "same",
                    "comparison_confidence": "strong",
                    "comparison_summary": "The visible stringing looks the same as the prior frame.",
                },
            },
            "updated_ts_ms": 1776038400000,
        },
        pending_human=[],
    )


def test_prompt_view_surfaces_decision_contract_and_signals():
    world = _world()

    view = world.prompt_view()

    assert view["decision_contract"] == {
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
    }
    assert view["decision_signals"]["allowed_frontier_ids"] == []
    assert view["decision_signals"]["tuning_action_space"] == {
        "speed": {
            "family": "speed",
            "summary": "Adjust print motion rate.",
            "allowed_directions": ["down"],
            "allowed_magnitudes": ["small"],
        },
        "flow": {
            "family": "flow",
            "summary": "Adjust commanded extrusion amount.",
            "allowed_directions": ["down"],
            "allowed_magnitudes": ["small"],
        },
    }
    assert view["frontier"] == []
    assert view["decision_signals"]["family_blockers"] == {
        "speed": ["speed_progress_not_ready", "startup_thermal_not_ready"],
        "flow": [],
        "nozzle": [],
        "bed": ["bed_progress_not_ready"],
        "pressure_advance": [],
    }
    assert view["decision_signals"]["vision_signal"] == {
        "usable": True,
        "summary": "vision:stringing; A thin strand is visible.",
        "finding_types": ["stringing"],
        "strength": "strong",
        "issue_level": "high",
    }
    assert view["decision_signals"]["symptom_feedback"] == {
        "current_finding_types": ["stringing"],
        "current_strength": "strong",
        "current_issue_level": "high",
        "last_verified_finding_types": ["stringing"],
        "last_verified_strength": "strong",
        "last_verified_issue_level": "high",
        "same_issue_persisted": True,
        "not_improving_after_last_action": True,
    }
    consequence = view["decision_signals"]["action_consequence"]
    assert consequence["action_id"] == "A_PRUSA_OPERATOR_RESTORE_FLOW_DEFAULT"
    assert consequence["issue"] == "same"
    assert consequence["confidence"] == "strong"
    assert consequence["compared_against_action_id"] == "A_PRUSA_OPERATOR_RESTORE_FLOW_DEFAULT"
    assert consequence["valid_for_escalation"] is True
    assert view["decision_signals"]["notebook_notes"]["merged_reason_tags"] == ["bridge", "high_flow"]
    assert view["decision_signals"]["notebook_notes"]["planner_facts"]["active_section"]["section_id"] == "sec_0010"
    assert view["decision_signals"]["last_result_notes"][0]["outcome"] == "reset_restored"
    temporal = view["decision_signals"]["temporal_action_context"]
    assert temporal["available"] is True
    assert temporal["last_action_id"] == "A_PRUSA_OPERATOR_RESTORE_FLOW_DEFAULT"
    assert temporal["last_action_family"] == "flow"
    assert temporal["last_action_outcome"] == "reset_restored"
    assert temporal["last_action_why"] is None
    assert temporal["seconds_since_last_action"] is not None
    assert isinstance(temporal["seconds_since_last_action"], float)
    assert temporal["now_at"].endswith("Z")
    freshness = view["decision_signals"]["freshness"]
    assert freshness["raw_observed_at"] == "2026-04-13T00:00:05Z"
    assert freshness["vision_observed_at"] == "2026-04-13T00:00:04Z"
    assert freshness["camera_last_capture_at"] == "2026-04-13T00:00:04Z"
    assert freshness["camera_frame_age_s"] == 0.4
    assert freshness["vision_usable"] is True
    assert freshness["seconds_since_raw_observation"] is not None
    assert freshness["seconds_since_vision_observation"] is not None
    assert "printer_1.active_notes_json" not in view["facts"]
    assert "printer_1.speed_autonomy_blockers" not in view["facts"]
    assert "printer_1.flow_shadow_actions" not in view["facts"]
    assert "printer_1.print_accel_mm_s2" not in view["facts"]
    assert "printer_1.active_print_accel_baseline_mm_s2" not in view["facts"]
    assert view["facts"]["printer_1.vision_signal_summary"] == "vision:stringing; A thin strand is visible."
    assert view["facts"]["printer_1.vision_signal_finding_types"] == "stringing"
    assert view["facts"]["printer_1.vision_signal_strength"] == "strong"
    assert view["facts"]["printer_1.vision_signal_issue_level"] == "high"
    assert "printer_1.vision_advisory_summary" not in view["facts"]
    assert "printer_1.vision_advisory_finding_types" not in view["facts"]
    assert "active_m204_p_mm_s2" not in json.dumps(view["decision_signals"]["notebook_notes"], sort_keys=True)


def test_action_consequence_ignores_repeated_residue_for_escalation():
    world = _world()
    world.facts["printer_1.vision_advisory_summary"] = "vision:residue,stringing; Thin strands are visible."
    world.facts["printer_1.vision_advisory_finding_types"] = "residue|stringing"
    world.last_result["result"]["post_action_vision_signal"] = {
        "usable": True,
        "summary": "vision:residue; Residue is still visible.",
        "finding_types": ["residue"],
        "strength": "strong",
        "issue_level": "low",
        "comparison_delta": "same",
        "comparison_confidence": "strong",
        "comparison_summary": "The nozzle residue appears to be in the same state as the previous frame.",
    }

    consequence = world.prompt_view()["decision_signals"]["action_consequence"]

    assert consequence["post_action_issue_types"] == ["residue"]
    assert consequence["current_issue_types"] == ["residue", "stringing"]
    assert consequence["issue"] == "unknown"
    assert consequence["compared_against_action_id"] is None
    assert consequence["valid_for_escalation"] is False
    assert "non-threatening repeated residue" in consequence["summary"]


def test_world_packet_prompt_schema_file_matches_helper():
    repo_root = Path(__file__).resolve().parents[1]
    on_disk = json.loads((repo_root / "schemas" / "world_packet.schema.json").read_text(encoding="utf-8"))
    assert on_disk == world_packet_prompt_schema()


def test_openrouter_payload_repeats_bounded_runtime_contract(monkeypatch, tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    config = Config.from_env(repo_root=repo_root)
    planner = OpenRouterPlanner(
        config,
        repo_root / "schemas" / "plan_ir.schema.json",
        prompt_package=PromptPackage(
            contract_text="contract",
            rubric_text="rubric",
            examples_text="examples",
        ),
    )

    payload = planner.build_payload(_world())
    developer_text = payload["messages"][1]["content"]
    user_text = payload["messages"][2]["content"]

    assert payload["messages"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["messages"][1]["cache_control"] == {"type": "ephemeral"}
    assert payload["messages"][2]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in payload["messages"][3]
    assert payload["plugins"] == [{"id": "response-healing"}]
    assert payload["reasoning"] == {"effort": "low"}
    assert "<task>" in developer_text
    assert "Return only valid PlanIR JSON for one bounded planner decision." in developer_text
    assert "<decision_principles>" in developer_text
    assert "Choose exactly one legal bounded action when fresh grounded evidence supports it." in developer_text
    assert "`decision_signals.allowed_frontier_ids` lists only legal non-tuning explicit safety or operator actions." in developer_text
    assert "Legal tuning choices are listed separately in `decision_signals.tuning_action_space` and are independently legal." in developer_text
    assert "legal bounded tuning remains available even when `decision_signals.allowed_frontier_ids` is empty or contains only `A_PRUSA_PAUSE` or `A_PRUSA_CANCEL`." in developer_text
    assert "Do not infer tuning illegality from `decision_signals.allowed_frontier_ids` being empty or safety-only." in developer_text
    assert "Use `action_consequence`, `vision_signal`, `freshness`, `recent_family_actions`, and `cumulative_family_deltas`" in developer_text
    assert "Prefer `NO_ACTION` when evidence is stale, still settling, weak, conflicting" in developer_text
    assert "Treat nozzle residue alone as observational." in developer_text
    assert "Do not use `CALL_HUMAN` for residue-only observations during a healthy advancing print." in developer_text
    assert "If bounded tuning remains legal during a healthy active print, prefer tuning or `NO_ACTION` over `CALL_HUMAN`." in developer_text
    assert "Emit exactly one of:" in developer_text
    assert "Planning rubric and examples." in user_text
    assert "When fresh grounded evidence supports one legal bounded action, take it." in user_text
    assert "choose one additional legal follow-up action instead of waiting indefinitely" in user_text
    assert "Treat `decision_signals.allowed_frontier_ids` as non-tuning explicit safety/operator actions only" in user_text
    assert "legal bounded tuning remains available even when `decision_signals.allowed_frontier_ids` is empty or only contains pause/cancel" in user_text
    assert "rubric" in user_text
    assert "examples" in user_text
    transport_schema = payload["response_format"]["json_schema"]["schema"]
    assert sorted(transport_schema["required"]) == [
        "call_human_message",
        "decision",
        "sequence",
        "tuning_choice",
        "why",
    ]


def test_prompt_view_can_carry_compact_vision_debug_summary_without_raw_image_data():
    world = _world().model_copy(
        update={
            "facts": {
                **_world().facts,
                "printer_1.nozzle_cam_usable": True,
                "printer_1.vision_advisory_summary": "vision:blob; Small buildup is visible.",
                "printer_1.vision_advisory_finding_types": "blob",
                "printer_1.vision_advisory_strength": "strong",
                "printer_1.vision_advisory_issue_level": "high",
                "printer_1.vision_observation_ref": "docs/evidence/prusa_core_one_plus/vision/observations/frame.json",
                "printer_1.vision_frame_ref": "docs/evidence/prusa_core_one_plus/vision/frames/frame.jpg",
                "printer_1.vision_debug_summary": "vision:blob; Small buildup is visible.",
            }
        }
    )

    view = world.prompt_view()

    assert view["facts"]["printer_1.vision_debug_summary"] == "vision:blob; Small buildup is visible."
    assert view["facts"]["printer_1.vision_signal_summary"] == "vision:blob; Small buildup is visible."
    assert view["facts"]["printer_1.vision_signal_finding_types"] == "blob"
    assert view["facts"]["printer_1.vision_signal_strength"] == "strong"
    assert view["facts"]["printer_1.vision_signal_issue_level"] == "high"
    assert "printer_1.vision_observation_ref" not in view["facts"]
    assert "printer_1.vision_frame_ref" not in view["facts"]
    assert view["decision_signals"]["vision_signal"] == {
        "usable": True,
        "summary": "vision:blob; Small buildup is visible.",
        "finding_types": ["blob"],
        "strength": "strong",
        "issue_level": "high",
    }
    assert "data:image" not in json.dumps(view["facts"], sort_keys=True)


def test_openrouter_planner_suppresses_startup_only_call_human():
    repo_root = Path(__file__).resolve().parents[1]
    config = Config.from_env(repo_root=repo_root)
    planner = OpenRouterPlanner(
        config,
        repo_root / "schemas" / "plan_ir.schema.json",
        prompt_package=PromptPackage(contract_text="contract", rubric_text="rubric", examples_text="examples"),
    )
    startup_world = _world().model_copy(
        update={
            "facts": {
                **_world().facts,
                "printer_1.lifecycle": "PRINTING",
                "printer_1.health": "OK",
                "printer_1.job_active": True,
                "printer_1.job_progress_pct": 1.0,
                "printer_1.speed_autonomy_blockers": "speed_progress_not_ready|startup_thermal_not_ready",
                "printer_1.flow_shadow_blockers": "flow_progress_not_ready",
                "printer_1.nozzle_shadow_blockers": "nozzle_progress_not_ready",
                "printer_1.bed_shadow_blockers": "bed_progress_not_ready",
            },
            "frontier": [
                LegalAction(
                    action_id="A_PRUSA_PAUSE",
                    verb="PAUSE",
                    description="Pause the print.",
                    owner_pack="prusa_core_one_plus",
                    execute_ref="pause",
                    hazard_class=HazardClass.LOW,
                ),
                LegalAction(
                    action_id="A_PRUSA_CANCEL",
                    verb="CANCEL",
                    description="Cancel the print.",
                    owner_pack="prusa_core_one_plus",
                    execute_ref="cancel",
                    hazard_class=HazardClass.LOW,
                ),
            ],
        }
    )

    normalized = planner._normalize_plan(
        PlanIR(decision="CALL_HUMAN", sequence=[], why="Only pause/cancel remain."),
        startup_world,
    )

    assert normalized.decision == "NO_ACTION"
    assert normalized.sequence == []
    assert "Healthy print startup" in normalized.why


def test_openrouter_planner_suppresses_pause_during_healthy_active_print_when_tuning_exists():
    repo_root = Path(__file__).resolve().parents[1]
    config = Config.from_env(repo_root=repo_root)
    planner = OpenRouterPlanner(
        config,
        repo_root / "schemas" / "plan_ir.schema.json",
        prompt_package=PromptPackage(contract_text="contract", rubric_text="rubric", examples_text="examples"),
    )
    active_print_world = _world().model_copy(
        update={
            "facts": {
                **_world().facts,
                "printer_1.lifecycle": "PRINTING",
                "printer_1.health": "OK",
                "printer_1.job_active": True,
                "printer_1.printing_phase": "active_printing",
            },
            "frontier": [
                LegalAction(
                    action_id="A_PRUSA_PAUSE",
                    verb="PAUSE",
                    description="Pause the print.",
                    owner_pack="prusa_core_one_plus",
                    execute_ref="pause",
                    hazard_class=HazardClass.LOW,
                ),
                LegalAction(
                    action_id="A_PRUSA_CANCEL",
                    verb="CANCEL",
                    description="Cancel the print.",
                    owner_pack="prusa_core_one_plus",
                    execute_ref="cancel",
                    hazard_class=HazardClass.LOW,
                ),
                LegalAction(
                    action_id="A_PRUSA_TRIM_FLOW_DOWN_SMALL",
                    verb="TUNE_FLOW",
                    description="Reduce flow slightly.",
                    owner_pack="prusa_core_one_plus",
                    execute_ref="trim_flow",
                    hazard_class=HazardClass.LOW,
                ),
            ],
        }
    )

    normalized = planner._normalize_plan(
        PlanIR(decision="EXECUTE", sequence=["A_PRUSA_PAUSE"], why="Vision sees residue."),
        active_print_world,
    )

    assert normalized.decision == "NO_ACTION"
    assert normalized.sequence == []
    assert "bounded tuning available" in normalized.why


def test_openrouter_planner_suppresses_residue_only_call_human_during_healthy_active_print():
    repo_root = Path(__file__).resolve().parents[1]
    config = Config.from_env(repo_root=repo_root)
    planner = OpenRouterPlanner(
        config,
        repo_root / "schemas" / "plan_ir.schema.json",
        prompt_package=PromptPackage(contract_text="contract", rubric_text="rubric", examples_text="examples"),
    )
    active_print_world = _world().model_copy(
        update={
            "facts": {
                **_world().facts,
                "printer_1.lifecycle": "PRINTING",
                "printer_1.health": "OK",
                "printer_1.job_active": True,
                "printer_1.printing_phase": "active_printing",
                "printer_1.nozzle_cam_usable": True,
                "printer_1.vision_advisory_summary": "vision:residue; Dark residue is visible on the nozzle exterior.",
                "printer_1.vision_advisory_finding_types": "residue",
                "printer_1.vision_advisory_strength": "strong",
                "printer_1.vision_advisory_issue_level": "low",
            },
            "frontier": [
                LegalAction(
                    action_id="A_PRUSA_CANCEL",
                    verb="CANCEL",
                    description="Cancel the print.",
                    owner_pack="prusa_core_one_plus",
                    execute_ref="cancel",
                    hazard_class=HazardClass.LOW,
                ),
                LegalAction(
                    action_id="A_PRUSA_TRIM_FLOW_DOWN_SMALL",
                    verb="TUNE_FLOW",
                    description="Reduce flow slightly.",
                    owner_pack="prusa_core_one_plus",
                    execute_ref="trim_flow",
                    hazard_class=HazardClass.LOW,
                ),
            ],
        }
    )

    normalized = planner._normalize_plan(
        PlanIR(decision="CALL_HUMAN", sequence=[], why="Fresh strong residue."),
        active_print_world,
    )

    assert normalized.decision == "NO_ACTION"
    assert normalized.sequence == []
    assert "only a residue observation" in normalized.why


def test_openrouter_planner_suppresses_call_human_when_healthy_active_print_still_has_tuning():
    repo_root = Path(__file__).resolve().parents[1]
    config = Config.from_env(repo_root=repo_root)
    planner = OpenRouterPlanner(
        config,
        repo_root / "schemas" / "plan_ir.schema.json",
        prompt_package=PromptPackage(contract_text="contract", rubric_text="rubric", examples_text="examples"),
    )
    active_print_world = _world().model_copy(
        update={
            "facts": {
                **_world().facts,
                "printer_1.lifecycle": "PRINTING",
                "printer_1.health": "OK",
                "printer_1.job_active": True,
                "printer_1.printing_phase": "active_printing",
                "printer_1.nozzle_cam_usable": True,
                "printer_1.vision_advisory_summary": "vision:stringing; Fine strings are visible between towers.",
                "printer_1.vision_advisory_finding_types": "stringing",
                "printer_1.vision_advisory_strength": "strong",
                "printer_1.vision_advisory_issue_level": "high",
            },
            "frontier": [
                LegalAction(
                    action_id="A_PRUSA_CANCEL",
                    verb="CANCEL",
                    description="Cancel the print.",
                    owner_pack="prusa_core_one_plus",
                    execute_ref="cancel",
                    hazard_class=HazardClass.LOW,
                ),
                LegalAction(
                    action_id="A_PRUSA_TRIM_FLOW_DOWN_SMALL",
                    verb="TUNE_FLOW",
                    description="Reduce flow slightly.",
                    owner_pack="prusa_core_one_plus",
                    execute_ref="trim_flow",
                    hazard_class=HazardClass.LOW,
                ),
            ],
        }
    )

    normalized = planner._normalize_plan(
        PlanIR(decision="CALL_HUMAN", sequence=[], why="Stringing is worsening."),
        active_print_world,
    )

    assert normalized.decision == "NO_ACTION"
    assert normalized.sequence == []
    assert "bounded tuning available" in normalized.why


def test_openrouter_planner_keeps_call_human_for_real_fault_even_when_tuning_exists():
    repo_root = Path(__file__).resolve().parents[1]
    config = Config.from_env(repo_root=repo_root)
    planner = OpenRouterPlanner(
        config,
        repo_root / "schemas" / "plan_ir.schema.json",
        prompt_package=PromptPackage(contract_text="contract", rubric_text="rubric", examples_text="examples"),
    )
    fault_world = _world().model_copy(
        update={
            "facts": {
                **_world().facts,
                "printer_1.lifecycle": "PRINTING",
                "printer_1.health": "OK",
                "printer_1.job_active": True,
                "printer_1.printing_phase": "active_printing",
                "printer_1.nozzle_cam_usable": True,
                "printer_1.vision_advisory_summary": "vision:spaghetti; Loose filament is contacting the part and causing spaghetti.",
                "printer_1.vision_advisory_finding_types": "spaghetti",
                "printer_1.vision_advisory_strength": "strong",
                "printer_1.vision_advisory_issue_level": "high",
            },
            "frontier": [
                LegalAction(
                    action_id="A_PRUSA_CANCEL",
                    verb="CANCEL",
                    description="Cancel the print.",
                    owner_pack="prusa_core_one_plus",
                    execute_ref="cancel",
                    hazard_class=HazardClass.LOW,
                ),
                LegalAction(
                    action_id="A_PRUSA_TRIM_FLOW_DOWN_SMALL",
                    verb="TUNE_FLOW",
                    description="Reduce flow slightly.",
                    owner_pack="prusa_core_one_plus",
                    execute_ref="trim_flow",
                    hazard_class=HazardClass.LOW,
                ),
            ],
        }
    )

    normalized = planner._normalize_plan(
        PlanIR(decision="CALL_HUMAN", sequence=[], why="Spaghetti requires intervention."),
        fault_world,
    )

    assert normalized.decision == "CALL_HUMAN"


def _tuning_world(frontier: list[LegalAction]) -> WorldPacket:
    return WorldPacket(
        goal="Improve the active print conservatively.",
        device_summaries=[],
        facts={},
        resources=[],
        blockers=[],
        deltas=[],
        frontier=frontier,
        last_result={},
        pending_human=[],
    )


def _trim_action(action_id: str, verb: str, description: str) -> LegalAction:
    return LegalAction(
        action_id=action_id,
        verb=verb,
        description=description,
        owner_pack="prusa_core_one_plus",
        execute_ref=action_id.lower(),
        hazard_class=HazardClass.LOW,
    )


def test_malformed_execute_plan_with_clean_tuning_choice_collapses_to_tuning_only(monkeypatch, tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    config = Config.from_env(repo_root=repo_root)
    planner = OpenRouterPlanner(
        config,
        repo_root / "schemas" / "plan_ir.schema.json",
        prompt_package=PromptPackage(contract_text="contract", rubric_text="rubric", examples_text="examples"),
    )
    world = _tuning_world(
        [
            _trim_action("A_PRUSA_TRIM_SPEED_DOWN_SMALL", "TUNE_SPEED", "Reduce print speed a little."),
            _trim_action("A_PRUSA_TRIM_FLOW_UP_SMALL", "TUNE_FLOW", "Raise flow a little."),
        ]
    )
    plan = PlanIR(
        decision="EXECUTE",
        sequence=["A_PRUSA_TRIM_SPEED_DOWN_SMALL"],
        tuning_choice=TuningChoice(family="speed", direction="down", magnitude="small"),
        why="malformed: both explicit sequence and tuning choice",
    )

    # Regression: this path crashed with NameError (planner.py used three
    # models helpers without importing them) before the Phase-0 hotfix.
    normalized = planner._normalize_malformed_execute_plan(plan, world)

    assert normalized.decision == "EXECUTE"
    assert normalized.sequence == []
    assert normalized.tuning_choice == plan.tuning_choice


def test_malformed_execute_plan_with_unresolvable_tuning_choice_becomes_no_action(monkeypatch, tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    config = Config.from_env(repo_root=repo_root)
    planner = OpenRouterPlanner(
        config,
        repo_root / "schemas" / "plan_ir.schema.json",
        prompt_package=PromptPackage(contract_text="contract", rubric_text="rubric", examples_text="examples"),
    )
    world = _tuning_world(
        [_trim_action("A_PRUSA_TRIM_FLOW_UP_SMALL", "TUNE_FLOW", "Raise flow a little.")]
    )
    plan = PlanIR(
        decision="EXECUTE",
        sequence=["A_PRUSA_TRIM_FLOW_UP_SMALL"],
        tuning_choice=TuningChoice(family="speed", direction="down", magnitude="small"),
        why="malformed: tuning choice matches nothing in the frontier",
    )

    normalized = planner._normalize_malformed_execute_plan(plan, world)

    assert normalized.decision == "NO_ACTION"
    assert normalized.sequence == []
    assert normalized.tuning_choice is None
