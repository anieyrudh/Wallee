#!/usr/bin/env python3
"""Generate the planner cassette corpus under tests/fixtures/cassettes/.

Two modes:

  --synthetic (default)  Build each case's request payload through the REAL
                         OpenRouterPlanner.build_payload over a deterministic
                         world, pair it with a hand-authored valid response,
                         run the response through the real parse+normalize
                         path, and store the resulting PlanIR as the expected
                         outcome. No network, no key.

  --live                 Same cases, but the response comes from a live
                         OpenRouter call (requires OPENROUTER_API_KEY).

Cassette fingerprints hash {model, messages, response_format}: any change to
the prompt stack (CONTRACT.md/RUBRIC.md/EXAMPLES, developer prompt, world
shaping, transport schema) changes the fingerprint and turns the
cassette-replay CI job red until this script is deliberately re-run and the
diff committed in a separate [re-record] commit. That is the drift detector
working as designed.

Run from wallee/:  python scripts/record_cassettes.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_V6 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_V6))

# Deterministic planner environment: cassettes must not depend on the
# invoking user's env.
os.environ.setdefault("OPENROUTER_MODEL", "synthetic/planner-cassette")
os.environ.setdefault("WALLEE_REASONING_EFFORT", "low")
os.environ.setdefault("WALLEE_OPENROUTER_RESPONSE_HEALING", "1")

from wallee.config import Config  # noqa: E402
from wallee.llm_transport import Cassette, request_fingerprint, write_cassette  # noqa: E402
from wallee.models import HazardClass, LegalAction, WorldPacket  # noqa: E402
from wallee.planner import OpenRouterPlanner  # noqa: E402

CASSETTE_DIR = REPO_V6 / "tests" / "fixtures" / "cassettes" / "planner"


def _sim_wait_cool_world() -> WorldPacket:
    return WorldPacket(
        goal="Unload cooled part from printer_1 into tray_A",
        device_summaries=[],
        facts={
            "printer_1.mode": "PAUSED",
            "printer_1.health": "OK",
            "printer_1.part_present": True,
            "printer_1.part_temp_c": 39.5,
            "printer_1.safe_to_unload": False,
        },
        resources=[],
        blockers=["printer_1: part too hot to unload"],
        deltas=[],
        frontier=[
            LegalAction(
                action_id="A_PRN_WAIT_COOL",
                verb="WAIT_UNTIL",
                description="Wait until the printed part is cool enough to unload",
                owner_pack="builtin",
                execute_ref="builtin.wait_until",
                args={"timeout_s": 300, "slice_s": 2.0, "predicate": "printer_1.safe_to_unload == true"},
                hazard_class=HazardClass.LOW,
            ),
            LegalAction(
                action_id="A_ARM_CALL_HUMAN",
                verb="CALL_HUMAN",
                description="Ask the operator to intervene",
                owner_pack="builtin",
                execute_ref="builtin.call_human",
                hazard_class=HazardClass.LOW,
            ),
        ],
        last_result={},
        pending_human=[],
    )


def _prusa_tuning_world() -> WorldPacket:
    return WorldPacket(
        goal="Improve the active print conservatively.",
        device_summaries=[],
        facts={
            "printer_1.lifecycle": "PRINTING",
            "printer_1.printing_phase": "active_printing",
            "printer_1.health": "OK",
            "printer_1.job_progress_pct": 42.0,
        },
        resources=[],
        blockers=[],
        deltas=[],
        frontier=[
            LegalAction(
                action_id="A_PRUSA_PAUSE",
                verb="PAUSE_PROCESS",
                description="Pause the active print.",
                owner_pack="prusa_core_one_plus",
                execute_ref="pause_process",
                hazard_class=HazardClass.LOW,
            ),
            LegalAction(
                action_id="A_PRUSA_TRIM_SPEED_DOWN_SMALL",
                verb="TUNE_SPEED",
                description="Reduce print speed a little while keeping the current print running.",
                owner_pack="prusa_core_one_plus",
                execute_ref="trim_speed_down_small",
                hazard_class=HazardClass.LOW,
            ),
        ],
        last_result={},
        pending_human=[],
    )


def _response(case_id: str, model: str, plan_json: dict) -> dict:
    return {
        "id": f"gen-synthetic-{case_id}",
        "model": model,
        "provider": "synthetic",
        "choices": [
            {
                "message": {"role": "assistant", "content": json.dumps(plan_json)},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 4200, "completion_tokens": 60},
    }


CASES = [
    {
        "case_id": "sim_wait_cool_execute",
        "world": _sim_wait_cool_world,
        "plan_json": {
            "decision": "EXECUTE",
            "sequence": ["A_PRN_WAIT_COOL"],
            "why": "The part is too hot; waiting is the only productive legal action.",
        },
    },
    {
        "case_id": "sim_no_action",
        "world": _sim_wait_cool_world,
        "plan_json": {
            "decision": "NO_ACTION",
            "sequence": [],
            "why": "Evidence is stale; wait for a fresh observation before acting.",
        },
    },
    {
        "case_id": "prusa_tuning_choice",
        "world": _prusa_tuning_world,
        "plan_json": {
            "decision": "EXECUTE",
            "sequence": [],
            "tuning_choice": {"family": "speed", "direction": "down", "magnitude": "small"},
            "why": "Fresh vision evidence supports one small bounded speed reduction.",
        },
    },
    {
        "case_id": "prusa_residue_pause_suppressed",
        "world": _prusa_tuning_world,
        # A pause on a healthy active print with bounded tuning available is
        # rewritten to NO_ACTION by the deterministic suppression policy; the
        # cassette pins that normalization behavior.
        "plan_json": {
            "decision": "EXECUTE",
            "sequence": ["A_PRUSA_PAUSE"],
            "why": "Small nozzle residue observed.",
        },
    },
]

ERROR_CASE = {
    "case_id": "provider_429_rate_limited",
    "world": _sim_wait_cool_world,
    "status": 429,
    "body": {"error": {"message": "rate limited", "code": 429}},
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="record from a live OpenRouter call")
    args = parser.parse_args()
    if args.live:
        raise SystemExit("--live recording is not implemented yet; use --synthetic mode (default)")

    tmp_data = REPO_V6 / ".cassette-recording-tmp"
    os.environ["WALLEE_DATA_DIR"] = str(tmp_data)
    config = Config.from_env(repo_root=REPO_V6)
    planner = OpenRouterPlanner(config, REPO_V6 / "schemas" / "plan_ir.schema.json")

    written = []
    for case in CASES:
        world = case["world"]()
        payload = planner.build_payload(world)
        response_body = _response(case["case_id"], config.openrouter_model, case["plan_json"])

        # Compute the expected FINAL plan through the real parse+normalize
        # path, so replay pins schema validation and suppression policies.
        parsed = planner._parse_response(response_body)
        expected = planner._normalize_plan(parsed, world)

        cassette = Cassette(
            fingerprint=request_fingerprint(payload),
            request_payload=payload,
            response_status=200,
            response_headers={"content-type": "application/json", "x-request-id": f"synthetic-{case['case_id']}"},
            response_body=response_body,
            meta={
                "case_id": case["case_id"],
                "recorded": "synthetic",
                "world": world.model_dump(mode="json"),
                "expected_plan": expected.model_dump(mode="json"),
            },
        )
        path = CASSETTE_DIR / f"{case['case_id']}.json"
        write_cassette(path, cassette)
        written.append(path)

    world = ERROR_CASE["world"]()
    payload = planner.build_payload(world)
    cassette = Cassette(
        fingerprint=request_fingerprint(payload),
        request_payload=payload,
        response_status=ERROR_CASE["status"],
        response_headers={"content-type": "application/json"},
        response_body=ERROR_CASE["body"],
        meta={
            "case_id": ERROR_CASE["case_id"],
            "recorded": "synthetic",
            "world": world.model_dump(mode="json"),
            "expected_error_status": ERROR_CASE["status"],
        },
    )
    error_dir = CASSETTE_DIR / "errors"
    write_cassette(error_dir / f"{ERROR_CASE['case_id']}.json", cassette)
    written.append(error_dir / f"{ERROR_CASE['case_id']}.json")

    for path in written:
        print(f"wrote {path.relative_to(REPO_V6)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
