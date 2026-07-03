"""Characterization goldens: exact plan→gate→dispatch→journal traces.

The safety-invariant suite pins *properties* (never/always); these goldens
pin *exact shapes* over the sim packs, so refactors (god-file splits,
dedupe, dispatch-table rewrites) can prove they changed nothing.

Re-blessing is deliberate: run

    WALLEE_BLESS_GOLDENS=1 pytest tests/test_golden_traces.py

and commit the diff in a separate commit titled `[re-bless] <reason>`.
Never mix a re-bless with production-code changes.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from wallee.models import HazardClass, PlanIR

GOLDEN_DIR = Path(__file__).resolve().parent / "goldens"
GOAL = "Unload cooled part from printer_1 into tray_A"

_ID = re.compile(r"\b(plan|run|human|idem)_[0-9a-f]{6,}\b")
_ISO_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
_TS_KEY = re.compile(r"(_ts_ms|_mono_ms|_ts|_mono)$")


def _normalize(value, id_map: dict[str, str]):
    """Deterministic trace: stable placeholder ids, zeroed clocks.

    idempotency_key values are derived from the (random) plan id, so they
    get placeholders too; args_hash stays literal — it is a deterministic
    hash of verb+args and pins argument drift.
    """
    if isinstance(value, dict):
        normalized = {}
        for k, v in sorted(value.items()):
            if isinstance(v, (int, float)) and _TS_KEY.search(k):
                normalized[k] = 0
            elif k == "idempotency_key" and isinstance(v, str):
                if v not in id_map:
                    id_map[v] = f"IDEM_{len(id_map) + 1}"
                normalized[k] = id_map[v]
            else:
                normalized[k] = _normalize(v, id_map)
        return normalized
    if isinstance(value, list):
        return [_normalize(v, id_map) for v in value]
    if isinstance(value, str):
        def sub_id(match: re.Match) -> str:
            token = match.group(0)
            if token not in id_map:
                id_map[token] = f"{match.group(1).upper()}_{len(id_map) + 1}"
            return id_map[token]

        value = _ID.sub(sub_id, value)
        return _ISO_TS.sub("<TS>", value)
    return value


def _capture_trace(runtime, report) -> dict:
    db = runtime["db"]
    id_map: dict[str, str] = {}

    def rows(query: str) -> list[dict]:
        return [dict(row) for row in db._conn.execute(query).fetchall()]

    trace = {
        "report": {
            "executed_action_ids": report.executed_action_ids,
            "replan_required": report.replan_required,
            "blocked_action_run_id": report.blocked_action_run_id,
            "failed_action_run_id": report.failed_action_run_id,
            "notes": report.notes,
            "human_request_ids": report.human_request_ids,
        },
        "plans": rows("SELECT plan_id, goal, plan_ir_json FROM plans ORDER BY created_ts_ms, plan_id"),
        "action_runs": rows(
            "SELECT action_run_id, plan_id, action_id, verb, owner_pack, status, hazard_class, args_hash "
            "FROM action_runs ORDER BY created_ts_ms, action_run_id"
        ),
        "exec_journal": rows(
            "SELECT idempotency_key, action_run_id, verb, args_hash, exec_state "
            "FROM exec_journal ORDER BY started_mono_ms, idempotency_key"
        ),
        "events": rows("SELECT component, level, message FROM events ORDER BY ts_ms, message"),
    }
    return _normalize(trace, id_map)


def _scenario_noop(runtime, monkeypatch):
    world = runtime["compiler"].compile(GOAL)
    plan = PlanIR(decision="NO_ACTION", sequence=[], why="golden: nothing to do")
    return runtime["engine"].execute_plan(GOAL, world, plan)


def _scenario_wait_cool(runtime, monkeypatch):
    # The builtin WAIT_UNTIL returns promptly once its predicate holds; cool
    # the part first so the golden run is fast and deterministic.
    printer = runtime["registry"].get("sim_printer")
    printer.state["printer_1.part_temp_c"] = 30.0
    runtime["registry"].publish_all_raw_state(runtime["whiteboard"])
    world = runtime["compiler"].compile(GOAL)
    action = next(a for a in world.frontier if a.owner_pack != "builtin")
    plan = PlanIR(decision="EXECUTE", sequence=[action.action_id], why="golden: pack-owned unload")
    return runtime["engine"].execute_plan(GOAL, world, plan)


def _scenario_approval_blocked(runtime, monkeypatch):
    engine = runtime["engine"]
    world = runtime["compiler"].compile(GOAL)
    action = world.frontier[0]
    action.approval_required = True
    action.hazard_class = HazardClass.MEDIUM
    monkeypatch.setattr(engine.world_compiler, "compile", lambda *a, **k: world)
    plan = PlanIR(decision="EXECUTE", sequence=[action.action_id], why="golden: approval gate")
    return engine.execute_plan(GOAL, world, plan)


def _scenario_lock_conflict(runtime, monkeypatch):
    engine = runtime["engine"]
    world = runtime["compiler"].compile(GOAL)
    action = world.frontier[0]
    monkeypatch.setattr(engine.lock_manager, "acquire", lambda keys: False)
    plan = PlanIR(decision="EXECUTE", sequence=[action.action_id], why="golden: lock conflict")
    return engine.execute_plan(GOAL, world, plan)


def _scenario_call_human(runtime, monkeypatch):
    world = runtime["compiler"].compile(GOAL)
    plan = PlanIR(
        decision="CALL_HUMAN",
        sequence=[],
        why="golden: escalation",
        call_human_message="golden escalation message",
    )
    return runtime["engine"].execute_plan(GOAL, world, plan)


SCENARIOS = {
    "noop": _scenario_noop,
    "pack_unload_success": _scenario_wait_cool,
    "approval_blocked": _scenario_approval_blocked,
    "lock_conflict_replan": _scenario_lock_conflict,
    "call_human_escalation": _scenario_call_human,
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_golden_trace(runtime, monkeypatch, name):
    report = SCENARIOS[name](runtime, monkeypatch)
    trace = _capture_trace(runtime, report)

    golden_path = GOLDEN_DIR / f"{name}.json"
    if os.environ.get("WALLEE_BLESS_GOLDENS") == "1":
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(json.dumps(trace, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert golden_path.exists(), (
        f"missing golden {golden_path.name} — generate with WALLEE_BLESS_GOLDENS=1 "
        "and commit it in a separate [re-bless] commit"
    )
    expected = json.loads(golden_path.read_text(encoding="utf-8"))
    assert trace == expected, (
        f"trace for {name!r} drifted from its golden. If the change is intentional, "
        "re-bless with WALLEE_BLESS_GOLDENS=1 in a separate [re-bless] commit."
    )
