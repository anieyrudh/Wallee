from __future__ import annotations

import json

from wallee_v6.planner_eval import load_eval_case, score_plan


def _make_repo(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / "knowledge").mkdir(parents=True)
    (repo_root / "schemas").mkdir(parents=True)
    return repo_root


def test_eval_loader_supports_old_artifact_without_planner_input(tmp_path):
    repo_root = _make_repo(tmp_path)
    artifact = repo_root / "docs" / "evidence" / "prusa_core_one_plus" / "phase1" / "runs" / "old.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        json.dumps(
            {
                "goal": "Reduce flow a little while keeping the current print running.",
                "world": {
                    "lifecycle": "PRINTING",
                    "job_active": True,
                    "job_progress_pct": 0.0,
                    "flow_shadow_blockers": "flow_progress_not_ready",
                    "frontier_ids": ["A_PRUSA_TRIM_FLOW_DOWN_SMALL"],
                },
                "shadow_frontier_ids": ["A_PRUSA_TRIM_FLOW_DOWN_SMALL"],
            }
        ),
        encoding="utf-8",
    )

    case = load_eval_case(artifact)

    assert case.planner_input["decision_signals"]["allowed_frontier_ids"] == ["A_PRUSA_TRIM_FLOW_DOWN_SMALL"]
    assert case.planner_input["decision_signals"]["family_blockers"]["flow"] == ["flow_progress_not_ready"]


def test_eval_loader_supports_new_artifact_with_exact_planner_input(tmp_path):
    repo_root = _make_repo(tmp_path)
    artifact = repo_root / "docs" / "evidence" / "prusa_core_one_plus" / "phase1" / "runs" / "new.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        json.dumps(
            {
                "goal": "Reduce print speed a little while keeping the current print running.",
                "planner_input": {
                    "goal": "Reduce print speed a little while keeping the current print running.",
                    "devices": [],
                    "facts": {},
                    "resources": [],
                    "blockers": [],
                    "deltas": [],
                    "frontier": [],
                    "last_result": {},
                    "pending_human": [],
                    "decision_contract": {
                        "frontier_only": True,
                        "max_actions": 1,
                        "prefer_bounded_action_when_supported": True,
                        "one_family_at_a_time": True,
                        "fresh_supported_signals_are_actionable": True,
                    },
                    "decision_signals": {
                        "notebook_notes": {"global": [], "local": [], "merged_reason_tags": []},
                        "last_result_notes": [],
                        "family_blockers": {"speed": [], "flow": [], "nozzle": [], "bed": []},
                        "allowed_frontier_ids": ["A_PRUSA_TRIM_SPEED_DOWN_SMALL"],
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    case = load_eval_case(artifact)

    assert case.planner_input["decision_signals"]["allowed_frontier_ids"] == ["A_PRUSA_TRIM_SPEED_DOWN_SMALL"]


def test_score_plan_prefers_no_action_and_rejects_multi_action_sequences():
    case_payload = {
        "case_id": "startup",
        "category": "startup_suppression",
        "source_artifact": "artifact.json",
        "goal": "Reduce print speed a little while keeping the current print running.",
        "planner_input": {
            "goal": "Reduce print speed a little while keeping the current print running.",
            "devices": [],
            "facts": {},
            "resources": [],
            "blockers": [],
            "deltas": [],
            "frontier": [],
            "last_result": {},
            "pending_human": [],
            "decision_contract": {
                "frontier_only": True,
                "max_actions": 1,
                "prefer_bounded_action_when_supported": True,
                "one_family_at_a_time": True,
                "fresh_supported_signals_are_actionable": True,
            },
            "decision_signals": {
                "notebook_notes": {"global": [], "local": [], "merged_reason_tags": []},
                "last_result_notes": [],
                "family_blockers": {"speed": ["startup_printing"], "flow": [], "nozzle": [], "bed": []},
                "allowed_frontier_ids": [],
            },
        },
        "expected_decision": {"decision": "NO_ACTION", "sequence": []},
        "allowed_sequences": [[]],
        "expected_reason_tags": [],
    }
    case_file = json.loads(json.dumps(case_payload))

    class _Case:
        pass

    case = _Case()
    for key, value in case_file.items():
        setattr(case, key, value)

    good = score_plan(case, {"decision": "NO_ACTION", "sequence": [], "why": "No legal action is available."})
    bad = score_plan(
        case,
        {
            "decision": "EXECUTE",
            "sequence": ["A_PRUSA_TRIM_SPEED_DOWN_SMALL", "A_PRUSA_TRIM_FLOW_DOWN_SMALL"],
            "why": "Try two actions.",
        },
    )

    assert good.passed is True
    assert bad.one_action_compliance is False
    assert bad.frontier_only_compliance is False
