#!/usr/bin/env python3
"""Run the live planner-eval lane over the translated replay corpus.

Feeds each translated scenario (tests/replay/keymap.py — the 60-scenario
legacy corpus in world-packet key-space) to the live configured planner and
scores the decision against the corpus expectations. Results are trend data
for the weekly lane, never a merge gate: the deterministic gates are what
guarantee safety; this measures planner judgment.

Skips cleanly (exit 0) when OPENROUTER_API_KEY is not configured. Budget cap
via WALLEE_LIVE_EVAL_MAX_CASES (default: all 60).

Run from the repo root:  python scripts/run_live_eval.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

OUT_DIR = REPO_ROOT / ".runtime" / "planner_eval"


def main() -> int:
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY not configured; skipping the live eval lane.")
        return 0

    from replay.keymap import build_world, load_scenarios, to_eval_case  # noqa: E402

    from wallee.config import Config  # noqa: E402
    from wallee.planner import OpenRouterPlanner  # noqa: E402

    os.environ.setdefault("WALLEE_PLANNER_BACKEND", "openrouter")
    config = Config.from_env(repo_root=REPO_ROOT)
    planner = OpenRouterPlanner(config, REPO_ROOT / "schemas" / "plan_ir.schema.json")

    scenarios = load_scenarios()
    max_cases = int(os.environ.get("WALLEE_LIVE_EVAL_MAX_CASES", str(len(scenarios))))
    if max_cases < len(scenarios):
        print(f"budget cap: running {max_cases} of {len(scenarios)} cases")
    results: list[dict] = []

    for name, scenario in scenarios[:max_cases]:
        case = to_eval_case(name, scenario)
        world, estop = build_world(scenario)
        if estop:
            # ESTOP scenarios are gate territory, not planner judgment.
            continue
        entry: dict = {"case": name, "title": case["title"], "category": case["category"]}
        try:
            plan = planner.plan(world)
            entry["decision"] = plan.decision
            entry["sequence"] = list(plan.sequence)
            entry["decision_ok"] = plan.decision in case["expected_decisions"]
            entry["chose_unacceptable"] = any(
                action_id in case["unacceptable_action_ids"] for action_id in plan.sequence
            ) or (case["unacceptable_call_human"] and plan.decision == "CALL_HUMAN")
            entry["ok"] = entry["decision_ok"] and not entry["chose_unacceptable"]
        except Exception as exc:  # live network lane: record, keep going
            entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["ok"] = False
        results.append(entry)

    passed = sum(1 for r in results if r.get("ok"))
    summary = {
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": os.environ.get("OPENROUTER_MODEL", "(default)"),
        "cases": len(results),
        "passed": passed,
        "pass_rate": round(passed / len(results), 3) if results else None,
        "results": results,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"live-eval-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(f"live eval: {passed}/{len(results)} passed -> {out_path}")
    # Trend lane, not a gate: a bad model week is signal, not a red build.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
