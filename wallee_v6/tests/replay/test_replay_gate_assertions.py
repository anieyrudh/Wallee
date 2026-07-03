"""Model-independent gate assertions over the translated 60-scenario corpus.

The legacy replay harness graded a live model (47/60, see
`docs/internal/REPLAY_BASELINE_2026-03.md`). This suite asserts what the v6
*gates* guarantee for the same worlds, with no model in the loop:

- a legacy tool with no v6 channel cannot be expressed at all — a fabricated
  action id dies at `validate_plan`,
- a translated action outside the scenario's legality envelope dies at
  `validate_plan`,
- a latched ESTOP blocks every EXECUTE regardless of scenario,
- CALL_HUMAN routes to the outbox and never to hardware.

Legal-but-unwise choices (e.g. pausing a healthy print) are decision quality,
not safety: those flow to the live planner-eval lane via `keymap.to_eval_case`.
"""

from __future__ import annotations

import pytest

from wallee_v6.models import PlanIR

from .keymap import (
    TOOL_TRANSLATION,
    build_world,
    expected_decisions,
    load_scenarios,
    to_eval_case,
    tool_action_ids,
    unknown_fact_keys,
)

SCENARIOS = load_scenarios()
SCENARIO_PARAMS = [pytest.param(payload, id=name) for name, payload in SCENARIOS]


def _pin(monkeypatch, engine, world) -> None:
    monkeypatch.setattr(engine.world_compiler, "compile", lambda *a, **k: world)


def test_corpus_is_fully_translated():
    assert len(SCENARIOS) == 60, "the corpus is 60 scenarios; do not drop any silently"
    for name, scenario in SCENARIOS:
        assert unknown_fact_keys(scenario) == [], (
            f"{name}: legacy keys neither translated nor recorded as unmapped"
        )
        for field in ("expected_tool", "acceptable_tools", "unacceptable_tools"):
            raw = scenario.get(field)
            tools = raw if isinstance(raw, list) else ([raw] if raw else [])
            for tool in tools:
                assert tool in TOOL_TRANSLATION, f"{name}: untranslated tool {tool!r}"
        assert expected_decisions(scenario) <= {"NO_ACTION", "EXECUTE", "CALL_HUMAN"}


@pytest.mark.parametrize("scenario", SCENARIO_PARAMS)
def test_untranslatable_and_off_frontier_tools_die_at_the_gates(runtime, scenario):
    engine = runtime["engine"]
    world, _estop = build_world(scenario)
    frontier_ids = {action.action_id for action in world.frontier}

    for tool in scenario.get("unacceptable_tools", []):
        kind, ids = tool_action_ids(tool, frontier_ids)
        if kind == "unrepresentable":
            fabricated = "A_PRUSA_" + tool.upper()
            plan = PlanIR(decision="EXECUTE", sequence=[fabricated], why=f"replay: {tool}")
            with pytest.raises((ValueError, KeyError)):
                engine.validate_plan(world, plan)
        elif kind in {"action", "tuning"}:
            for action_id in ids or (["A_PRUSA_PAUSE"] if kind == "action" else []):
                if action_id in frontier_ids:
                    continue  # legal-but-unwise: decision quality, eval lane below
                plan = PlanIR(decision="EXECUTE", sequence=[action_id], why=f"replay: {tool}")
                with pytest.raises((ValueError, KeyError)):
                    engine.validate_plan(world, plan)


@pytest.mark.parametrize(
    "scenario",
    [pytest.param(p, id=n) for n, p in SCENARIOS if p.get("whiteboard_state", {}).get("safety.estop")],
)
def test_estop_scenarios_block_all_execution(runtime, monkeypatch, scenario):
    engine, safety, db = runtime["engine"], runtime["safety"], runtime["db"]
    world, estop = build_world(scenario)
    assert estop, "parametrization selects only ESTOP scenarios"
    _pin(monkeypatch, engine, world)

    safety.trip("replay scenario: operator ESTOP latched")
    target = world.frontier[0].action_id if world.frontier else "A_PRUSA_PAUSE"
    report = engine.execute_plan(world.goal, world, PlanIR(decision="EXECUTE", sequence=[target] if world.frontier else [], why="replay"))

    assert report.executed_action_ids == []
    assert db._conn.execute("select count(*) from exec_journal").fetchone()[0] == 0


@pytest.mark.parametrize(
    "scenario",
    [pytest.param(p, id=n) for n, p in SCENARIOS if "CALL_HUMAN" in expected_decisions(p)],
)
def test_call_human_scenarios_route_to_outbox_only(runtime, monkeypatch, scenario):
    engine, db = runtime["engine"], runtime["db"]
    world, _estop = build_world(scenario)
    _pin(monkeypatch, engine, world)

    plan = PlanIR(
        decision="CALL_HUMAN",
        sequence=[],
        why="replay: escalation expected",
        call_human_message="replay-translated escalation",
    )
    report = engine.execute_plan(world.goal, world, plan)

    assert report.executed_action_ids == []
    assert report.human_request_ids, "escalation must reach the outbox"
    assert db._conn.execute("select count(*) from exec_journal").fetchone()[0] == 0


def test_eval_cases_emit_for_the_live_lane():
    for name, scenario in SCENARIOS:
        case = to_eval_case(name, scenario)
        assert case["expected_decisions"], name
        assert set(case["expected_decisions"]) <= {"NO_ACTION", "EXECUTE", "CALL_HUMAN"}
        assert isinstance(case["facts"], dict) and case["facts"], name
