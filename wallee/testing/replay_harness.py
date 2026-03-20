"""Offline scenario replay harness.

Tests agent decisions against known scenarios without hardware.
Usage: python -m wallee.testing.replay_harness wallee/testing/scenarios/
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx

from wallee.agent.llm_client import LLMClient
from wallee.agent.parser import parse_llm_output
from wallee.agent.prompt import (
    build_messages,
    build_system_prompt,
    build_user_message,
)
from wallee.config import load_config
from wallee.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

# Compatibility alias used by the replay harness design doc.
parse_decision = parse_llm_output

_HARDCODED_TOOL_NAMES = [
    "pause_print",
    "resume_print",
    "cancel_print",
    "start_print",
    "set_temperature",
    "home_axes",
    "disable_motors",
    "set_speed_factor",
    "set_flow_factor",
    "set_position",
    "extrude",
    "retract",
    "read_endstops",
    "send_gcode",
    "call_human",
    "discover_hardware",
    "remember",
    "web_search",
    "trends",
    "differential",
    "get_sensor_history",
]

MAX_API_RETRIES = 3
API_BACKOFF_S = 2.0


def _load_knowledge(job_context_text: str = "") -> dict[str, str]:
    """Load static knowledge files plus scenario-specific job context."""
    knowledge_dir = Path(__file__).resolve().parents[1] / "knowledge"
    knowledge = {}
    for name in ("SOUL.md", "LEARNED.md", "OBSERVATIONS.md"):
        path = knowledge_dir / name
        if path.exists():
            knowledge[name] = path.read_text()
    if job_context_text:
        knowledge["JOB_CONTEXT.md"] = job_context_text
    return knowledge


def _load_tools_for_prompt() -> list[dict]:
    """Load actuator tools for the prompt without starting any hardware threads."""
    registry = ToolRegistry()
    registry.load_builtins()
    registry.load_pack("wallee.device_packs.prusa_link")
    registry.load_pack("wallee.device_packs.prusa_serial")

    tools = registry.list_for_llm()
    if tools:
        return tools

    return [
        {"name": name, "description": "", "requires_approval": False}
        for name in _HARDCODED_TOOL_NAMES
    ]


def _tool_names(tools: list[dict]) -> list[str]:
    names = {tool["name"] for tool in tools}
    names.update(_HARDCODED_TOOL_NAMES)
    return sorted(names)


def _parse_or_none(raw: str, printer_state: str | None):
    """Best-effort validity check using the real parser."""
    decision = parse_decision(raw, printer_state=printer_state)
    invalid_reasons = (
        "invalid JSON",
        "empty LLM response",
        "LLM response is not a JSON object",
        "unknown type",
        "CALL_HUMAN missing message",
        "ACTION had no tool",
        "ACTION_CHAIN had no actions",
    )
    if decision.type == "WAIT" and any(token in (decision.reasoning or "") for token in invalid_reasons):
        return None
    return decision


def _call_model(cfg, messages: list[dict], available_tools: list[str], printer_state: str | None) -> tuple[str, object]:
    """Call OpenRouter directly without the shared strict schema.

    The main LLM client uses json_schema structured outputs, but the configured
    Anthropic model rejects that schema. The harness still uses the real prompt
    and real parser; it only swaps the transport layer so replay works with the
    repo's current provider settings.
    """
    if not cfg.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")

    base_messages = list(messages)

    for attempt in range(1, MAX_API_RETRIES + 1):
        request_messages = list(base_messages)
        if attempt > 1:
            request_messages = request_messages + [
                {
                    "role": "user",
                    "content": (
                        "Your prior answer was not valid JSON for this harness. "
                        "Return exactly one JSON object matching the documented schema. "
                        f"Valid tool names: {', '.join(available_tools)}."
                    ),
                }
            ]

        try:
            response = httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {cfg.openrouter_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": cfg.openrouter_model,
                    "messages": request_messages,
                    "stream": False,
                    "max_tokens": 512,
                    "temperature": cfg.llm_temperature,
                    "top_p": 0.9,
                },
                timeout=60.0,
            )
            response.raise_for_status()
            data = response.json()
            raw = data["choices"][0]["message"].get("content", "")
            decision = _parse_or_none(raw, printer_state)
            if decision is not None:
                return raw, decision
            logger.warning(f"Scenario replay received invalid JSON decision (attempt {attempt}/{MAX_API_RETRIES})")
        except httpx.HTTPStatusError as e:
            logger.warning(
                f"Scenario replay HTTP {e.response.status_code} "
                f"(attempt {attempt}/{MAX_API_RETRIES}): {e.response.text[:200]}"
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as e:
            logger.warning(f"Scenario replay network error (attempt {attempt}/{MAX_API_RETRIES}): {e}")
        except Exception as e:
            logger.warning(f"Scenario replay unexpected error (attempt {attempt}/{MAX_API_RETRIES}): {e}")

        if attempt < MAX_API_RETRIES:
            time.sleep(API_BACKOFF_S * attempt)

    raise RuntimeError("LLM call failed after 3 attempts")


def _build_replay_messages(scenario: dict, tools: list[dict]) -> tuple[list[dict], dict]:
    state = dict(scenario.get("whiteboard_state", {}))
    state.update(scenario.get("vision_state", {}))

    human_intent = scenario.get("human_intent")
    if human_intent:
        state["human.intent"] = human_intent

    pending = scenario.get("pending_callout")
    if pending:
        state["human.pending_callout"] = json.dumps(pending)

    knowledge = _load_knowledge(scenario.get("job_context", ""))
    system_prompt = build_system_prompt(knowledge=knowledge, tools=tools)
    user_text = build_user_message(
        state=state,
        episode=scenario.get("episode", []),
        intent=human_intent,
        current_time=time.time(),
        pending_callout=pending,
    )
    messages = build_messages(system_prompt, user_text, state)
    return messages, state


def _evaluate_scenario(scenario: dict, decision) -> tuple[bool, list[str], list[str]]:
    expected_type = scenario.get("expected_type")
    expected_tool = scenario.get("expected_tool")
    acceptable = scenario.get("acceptable_tools", [])
    unacceptable = scenario.get("unacceptable_tools", [])
    if isinstance(expected_type, list):
        expected_types = expected_type
    elif expected_type:
        expected_types = [expected_type]
    else:
        expected_types = []

    tools_proposed = []
    if getattr(decision, "tool", None):
        tools_proposed.append(decision.tool)
    for action in getattr(decision, "actions", []) or []:
        tool = action.get("tool")
        if tool:
            tools_proposed.append(tool)

    passed = True
    reasons = []

    if expected_types and decision.type not in expected_types:
        allow_alt_call_human = (
            "WAIT" in expected_types
            and decision.type == "CALL_HUMAN"
            and "call_human" in acceptable
        )
        allow_action_equivalence = (
            decision.type in {"ACTION", "ACTION_CHAIN"}
            and any(t in {"ACTION", "ACTION_CHAIN"} for t in expected_types)
        )
        if not allow_alt_call_human and not allow_action_equivalence:
            passed = False
            reasons.append(f"type: expected {expected_type}, got {decision.type}")

    if expected_tool and expected_tool not in tools_proposed:
        passed = False
        reasons.append(f"expected tool: {expected_tool}, got {tools_proposed or ['<none>']}")

    for tool in tools_proposed:
        if tool in unacceptable:
            passed = False
            reasons.append(f"unacceptable tool: {tool}")

    if not expected_tool and acceptable:
        if "ACTION_CHAIN" in expected_types and decision.type == "ACTION_CHAIN":
            missing = [tool for tool in acceptable if tool not in tools_proposed]
            if missing:
                passed = False
                reasons.append(f"missing chain tools: {missing}")
        elif decision.type in {"ACTION", "ACTION_CHAIN"} and not any(tool in acceptable for tool in tools_proposed):
            passed = False
            reasons.append(f"acceptable tools not used: {acceptable}")

    return passed, reasons, tools_proposed


def run_scenario(scenario_path, cfg, llm_client, tools):
    with open(scenario_path) as f:
        scenario = json.load(f)

    messages, state = _build_replay_messages(scenario, tools)
    tool_names = _tool_names(tools)

    raw, decision = _call_model(cfg, messages, tool_names, state.get("printer.state"))

    passed, reasons, tools_proposed = _evaluate_scenario(scenario, decision)

    return {
        "name": scenario["name"],
        "passed": passed,
        "reason": "; ".join(reasons) if reasons else "OK",
        "decision_type": decision.type,
        "decision_tool": getattr(decision, "tool", None),
        "decision_actions": tools_proposed,
        "observation": getattr(decision, "observation", ""),
        "reasoning": getattr(decision, "reasoning", ""),
    }


def run_all(scenario_dir):
    cfg = load_config()
    llm_client = LLMClient(
        api_key=cfg.openrouter_api_key,
        model=cfg.openrouter_model,
        temperature=cfg.llm_temperature,
    )
    tools = _load_tools_for_prompt()

    results = []
    files = sorted(Path(scenario_dir).glob("*.json"))
    passed_count = 0

    for path in files:
        label = path.stem
        try:
            result = run_scenario(str(path), cfg, llm_client, tools)
            status = "PASS" if result["passed"] else "FAIL"
            if result["passed"]:
                passed_count += 1
            print(f"  {status}  {label}: {result['name']}")
            print(
                f"        -> {result['decision_type']}:{result['decision_tool'] or ''} "
                f"{result['decision_actions']} - {result['reason']}"
            )
            if not result["passed"]:
                print(f"        observation: {result['observation'][:100]}")
                print(f"        reasoning:   {result['reasoning'][:100]}")
            results.append(result)
        except Exception as e:
            print(f"  ERR   {label}: {e}")
            results.append({"name": label, "passed": False, "reason": str(e)})

        time.sleep(1)

    print(f"\n{'=' * 60}")
    print(f"Results: {passed_count}/{len(results)} passed")

    if passed_count < len(results):
        print("\nFailed:")
        for result in results:
            if not result.get("passed"):
                print(f"  {result['name']}: {result.get('reason', 'unknown')}")

    out_path = Path(__file__).resolve().parent / "results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull results saved to {out_path}")

    return passed_count, len(results)


if __name__ == "__main__":
    scenario_dir = sys.argv[1] if len(sys.argv) > 1 else "wallee/testing/scenarios"
    run_all(scenario_dir)
