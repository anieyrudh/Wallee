#!/usr/bin/env python3
"""Replay representative planner cases and ask for comparative justification.

This is an offline analysis helper, not a live runtime path. It uses saved
planner inputs from local evidence, augments them with the newer v6.5
pressure-advance and acceleration actions, then:

1. replays the current planner against the reconstructed case
2. asks the same model to explain why the chosen action outranks alternatives

The output is saved under docs/evidence/prusa_core_one_plus/planner_analysis/.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import urllib.request

from wallee_v6.config import Config
from wallee_v6.planner import OpenRouterPlanner, PromptPackage
from wallee_v6.planner_eval import _ReplayWorld, _load_env_defaults, revised_prompt_package


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SESSION = (
    REPO_ROOT
    / "docs"
    / "evidence"
    / "prusa_core_one_plus"
    / "stringing_comparison"
    / "2026-04-15T020043Z-wallee-stringing-rerun"
    / "2026-04-15T100836-phase1-speed-flow-session.json"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _analysis_root() -> Path:
    return (
        REPO_ROOT
        / "docs"
        / "evidence"
        / "prusa_core_one_plus"
        / "planner_analysis"
        / datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    )


def _action(action_id: str, verb: str, description: str, target: int | float) -> dict[str, object]:
    field = {
        "TUNE_FLOW": "printer_1.flow_pct",
        "TUNE_SPEED": "printer_1.speed_pct",
        "TUNE_PRESSURE_ADVANCE": "printer_1.pressure_advance",
        "TUNE_PRINT_ACCEL": "printer_1.print_accel_mm_s2",
    }[verb]
    summary = {
        "TUNE_FLOW": "Primary extrusion-rate trim. Lower values reduce overfeed and ooze.",
        "TUNE_SPEED": "Primary motion-rate trim. Lower values reduce ooze risk and motion energy.",
        "TUNE_PRESSURE_ADVANCE": "Extrusion-dynamics trim around starts, stops, and corners.",
        "TUNE_PRINT_ACCEL": "Motion-dynamics trim that changes how aggressively the toolhead changes velocity.",
    }[verb]
    return {
        "id": action_id,
        "verb": verb,
        "description": description,
        "args": {},
        "target_device": "printer_1",
        "hazard_class": "low",
        "approval_required": False,
        "expected_delta": [
            {
                "field": field,
                "target": target,
                "direction": "decrease",
                "summary": summary,
            }
        ],
    }


def _append_actions(payload: dict[str, object], actions: list[dict[str, object]]) -> dict[str, object]:
    out = deepcopy(payload)
    frontier = list(out.get("frontier") or [])
    allowed = list(((out.get("decision_signals") or {}).get("allowed_frontier_ids") or []))
    existing = {item["id"] for item in frontier if isinstance(item, dict) and "id" in item}
    for action in actions:
        if action["id"] not in existing:
            frontier.append(action)
            allowed.append(action["id"])
    out["frontier"] = frontier
    out.setdefault("decision_signals", {})["allowed_frontier_ids"] = allowed
    family_blockers = out["decision_signals"].setdefault("family_blockers", {})
    family_blockers.setdefault("pressure_advance", [])
    family_blockers.setdefault("accel", [])
    return out


def _build_cases() -> list[dict[str, object]]:
    source = json.loads(SOURCE_SESSION.read_text(encoding="utf-8"))
    step1 = deepcopy(source["step_reports"][0]["planner_input"])
    step2 = deepcopy(source["step_reports"][1]["planner_input"])

    pa_flow = _action(
        "A_PRUSA_TRIM_PRESSURE_ADVANCE_DOWN_SMALL",
        "TUNE_PRESSURE_ADVANCE",
        "Reduce pressure advance by 0.01. This changes extrusion pressure compensation around starts, stops, and corners; lowering it may reduce ooze or stringing around transitions, but too much can underfeed sharp features.",
        0.02,
    )
    accel_flow = _action(
        "A_PRUSA_TRIM_ACCEL_DOWN_SMALL",
        "TUNE_PRINT_ACCEL",
        "Reduce print acceleration by 250 mm/s^2. This makes motion changes gentler and can indirectly stabilize extrusion under dynamic motion, but it also slows the print.",
        2750.0,
    )
    flow_case = _append_actions(step1, [pa_flow, accel_flow])

    flow_again = _action(
        "A_PRUSA_TRIM_FLOW_DOWN_SMALL",
        "TUNE_FLOW",
        "Reduce flow by 5%. This commands less extrusion and can reduce over-extrusion, ooze, blobs, or stringing if the print looks overfed, but too much reduction can cause under-extrusion.",
        95.0,
    )
    pa_speed = _action(
        "A_PRUSA_TRIM_PRESSURE_ADVANCE_DOWN_SMALL",
        "TUNE_PRESSURE_ADVANCE",
        "Reduce pressure advance by 0.01. This changes extrusion pressure compensation around starts, stops, and corners; lowering it may reduce ooze or stringing around transitions, but too much can underfeed sharp features.",
        0.02,
    )
    accel_speed = _action(
        "A_PRUSA_TRIM_ACCEL_DOWN_SMALL",
        "TUNE_PRINT_ACCEL",
        "Reduce print acceleration by 250 mm/s^2. This makes motion changes gentler and can indirectly stabilize extrusion under dynamic motion, but it also slows the print.",
        2750.0,
    )
    speed_case = _append_actions(step2, [flow_again, pa_speed, accel_speed])

    return [
        {
            "case_id": "flow_preferred_over_speed_pa_accel",
            "recorded_action_id": "A_PRUSA_TRIM_FLOW_DOWN_SMALL",
            "source_note": "Reconstructed from 2026-04-15 saved phase session step 1, augmented with v6.5 pressure-advance and accel down-small actions.",
            "planner_input": flow_case,
        },
        {
            "case_id": "speed_followup_over_flow_pa_accel",
            "recorded_action_id": "A_PRUSA_TRIM_SPEED_DOWN_SMALL",
            "source_note": "Reconstructed from 2026-04-15 saved phase session step 2, augmented with v6.5 flow/pressure-advance/accel down-small actions.",
            "planner_input": speed_case,
        },
    ]


def _planner(config: Config, prompt_package: PromptPackage) -> OpenRouterPlanner:
    return OpenRouterPlanner(config, REPO_ROOT / "schemas" / "plan_ir.schema.json", prompt_package=prompt_package)


def _analysis_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "case_id": {"type": "string"},
            "recorded_action_id": {"type": "string"},
            "replayed_action_id": {"type": ["string", "null"]},
            "matches_recorded_choice": {"type": "boolean"},
            "ranking": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "rank": {"type": "integer"},
                        "action_id": {"type": "string"},
                        "support_level": {"type": "string"},
                        "why_this_rank": {"type": "string"},
                    },
                    "required": ["rank", "action_id", "support_level", "why_this_rank"],
                },
            },
            "chosen_over_pressure_advance": {"type": "string"},
            "chosen_over_accel": {"type": "string"},
            "most_decisive_evidence": {"type": "array", "items": {"type": "string"}},
            "weakest_assumptions": {"type": "array", "items": {"type": "string"}},
            "bottom_line": {"type": "string"},
        },
        "required": [
            "case_id",
            "recorded_action_id",
            "replayed_action_id",
            "matches_recorded_choice",
            "ranking",
            "chosen_over_pressure_advance",
            "chosen_over_accel",
            "most_decisive_evidence",
            "weakest_assumptions",
            "bottom_line",
        ],
    }


def _analysis_payload(config: Config, case: dict[str, object], replay_plan: dict[str, object]) -> dict[str, object]:
    planner_input = case["planner_input"]
    return {
        "model": config.openrouter_model,
        "stream": False,
        "temperature": 0.1,
        "plugins": [{"id": "response-healing"}],
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are analyzing a bounded 3D-print planner decision. "
                    "Explain why the selected action outranked the alternatives using only the supplied planner input. "
                    "Do not invent observations that are not present."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": (
                            "Rank the allowed actions by support. Focus especially on why the chosen speed/flow action "
                            "outranks pressure-advance and acceleration trims in this case."
                        ),
                        "case_id": case["case_id"],
                        "recorded_action_id": case["recorded_action_id"],
                        "replayed_plan": replay_plan,
                        "planner_input": planner_input,
                    },
                    sort_keys=True,
                ),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "planner_preference_analysis",
                "strict": True,
                "schema": _analysis_schema(),
            },
        },
        "reasoning": {"effort": config.reasoning_effort},
    }


def _analysis_request(config: Config, case: dict[str, object], replay_plan: dict[str, object]) -> dict[str, object]:
    payload = _analysis_payload(config, case, replay_plan)
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {config.openrouter_api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=max(1.0, config.planner_request_timeout_seconds)) as response:
        raw = json.loads(response.read().decode("utf-8"))
    content = (((raw.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    if isinstance(content, list):
        content = "".join(item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text")
    parsed = json.loads(content)
    parsed["_provider_metadata"] = {
        "model": raw.get("model"),
        "usage": raw.get("usage"),
    }
    return parsed


def main() -> int:
    _load_env_defaults(REPO_ROOT)
    config = Config.from_env(repo_root=REPO_ROOT)
    if not config.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required")

    prompt_package = revised_prompt_package(REPO_ROOT)
    planner = _planner(config, prompt_package)
    out_root = _analysis_root()
    out_root.mkdir(parents=True, exist_ok=True)

    cases = _build_cases()
    output_rows = []
    for case in cases:
        replay_world = _ReplayWorld(case["planner_input"])
        replay_plan = planner.plan(replay_world).model_dump()
        analysis = _analysis_request(config, case, replay_plan)
        row = {
            "case_id": case["case_id"],
            "recorded_action_id": case["recorded_action_id"],
            "source_note": case["source_note"],
            "replay_plan": replay_plan,
            "analysis": analysis,
        }
        output_rows.append(row)
        path = out_root / f"{case['case_id']}.json"
        path.write_text(json.dumps(row, indent=2, sort_keys=True), encoding="utf-8")

    summary = {
        "generated_at": _utc_now(),
        "source_session": str(SOURCE_SESSION),
        "note": (
            "These are reconstructed comparative replay cases from saved local evidence. "
            "The exact 2026-04-19 runtime cycle artifacts were cleared during a later runtime restart, "
            "so this analysis uses representative saved planner inputs augmented with the current v6.5 "
            "pressure-advance and acceleration actions."
        ),
        "rows": output_rows,
    }
    (out_root / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(out_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
