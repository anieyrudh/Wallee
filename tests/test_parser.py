"""Tests for LLM output parser. Core safety boundary."""

import json
import pytest
from wallee.agent.parser import (
    parse_llm_output,
    Decision,
    clamp_check_interval,
    configure_check_intervals,
    MIN_CHECK_INTERVAL,
    MAX_CHECK_INTERVAL,
    MAX_CHECK_INTERVAL_IDLE,
    DEFAULT_CHECK_INTERVAL,
)


@pytest.fixture(autouse=True)
def _reset_intervals():
    """Ensure tests use the actual configured defaults (may change between versions)."""
    yield


class TestParseAction:
    def test_valid_action(self):
        raw = json.dumps({
            "type": "ACTION",
            "tool": "resume_print",
            "params": {"speed": 100},
            "reason": "temps nominal",
        })
        d = parse_llm_output(raw)
        assert d.type == "ACTION"
        assert d.tool == "resume_print"
        assert d.params == {"speed": 100}
        assert d.reason == "temps nominal"

    def test_action_no_params(self):
        raw = json.dumps({"type": "ACTION", "tool": "pause_print"})
        d = parse_llm_output(raw)
        assert d.type == "ACTION"
        assert d.params == {}

    def test_action_string_params_are_deserialized(self):
        raw = json.dumps({
            "type": "ACTION",
            "tool": "set_temperature",
            "params": "{\"target\": 210, \"heater\": \"nozzle\"}",
            "reason": "raise temp",
        })
        d = parse_llm_output(raw)
        assert d.type == "ACTION"
        assert d.params == {"target": 210, "heater": "nozzle"}

    def test_action_invalid_string_params_default_empty(self):
        raw = json.dumps({
            "type": "ACTION",
            "tool": "set_temperature",
            "params": "{not-json}",
        })
        d = parse_llm_output(raw)
        assert d.type == "ACTION"
        assert d.params == {}

    def test_action_missing_tool(self):
        raw = json.dumps({"type": "ACTION", "params": {}})
        d = parse_llm_output(raw)
        assert d.type == "WAIT"
        assert "no tool" in d.reason


class TestParseWait:
    def test_valid_wait(self):
        raw = json.dumps({
            "type": "WAIT",
            "reason": "all nominal",
            "check_after_s": 60,
        })
        d = parse_llm_output(raw)
        assert d.type == "WAIT"
        assert d.reason == "all nominal"
        assert d.check_after_s == 60

    def test_wait_defaults(self):
        raw = json.dumps({"type": "WAIT"})
        d = parse_llm_output(raw)
        assert d.type == "WAIT"
        assert d.check_after_s == DEFAULT_CHECK_INTERVAL


class TestParseCallHuman:
    def test_valid_call_human(self):
        raw = json.dumps({
            "type": "CALL_HUMAN",
            "message": "humidity at 72%",
            "severity": "warning",
        })
        d = parse_llm_output(raw)
        assert d.type == "CALL_HUMAN"
        assert d.message == "humidity at 72%"
        assert d.severity == "warning"

    def test_call_human_missing_message(self):
        raw = json.dumps({"type": "CALL_HUMAN", "severity": "info"})
        d = parse_llm_output(raw)
        assert d.type == "WAIT"

    def test_call_human_default_severity(self):
        raw = json.dumps({"type": "CALL_HUMAN", "message": "help"})
        d = parse_llm_output(raw)
        assert d.severity == "info"


class TestFailSafe:
    """Parser NEVER crashes — these all default to WAIT."""

    def test_empty_string(self):
        d = parse_llm_output("")
        assert d.type == "WAIT"

    def test_none_string(self):
        d = parse_llm_output(None)
        assert d.type == "WAIT"

    def test_invalid_json(self):
        d = parse_llm_output("not json at all {{{")
        assert d.type == "WAIT"

    def test_json_array(self):
        d = parse_llm_output("[1, 2, 3]")
        assert d.type == "WAIT"

    def test_json_string(self):
        d = parse_llm_output('"just a string"')
        assert d.type == "WAIT"

    def test_unknown_type(self):
        raw = json.dumps({"type": "EXECUTE_IMMEDIATELY"})
        d = parse_llm_output(raw)
        assert d.type == "WAIT"

    def test_missing_type(self):
        raw = json.dumps({"tool": "resume_print"})
        d = parse_llm_output(raw)
        assert d.type == "WAIT"

    def test_case_insensitive_type(self):
        raw = json.dumps({"type": "action", "tool": "pause_print"})
        d = parse_llm_output(raw)
        assert d.type == "ACTION"

    def test_whitespace_only(self):
        d = parse_llm_output("   \n  ")
        assert d.type == "WAIT"

    def test_partial_json(self):
        d = parse_llm_output('{"type": "ACTION", "tool":')
        assert d.type == "WAIT"

    def test_markdown_json_fence(self):
        raw = '```json\n{"type": "WAIT", "reason": "all good", "check_after_s": 60}\n```'
        d = parse_llm_output(raw)
        assert d.type == "WAIT"
        assert d.reason == "all good"
        assert d.check_after_s == 60

    def test_markdown_fence_no_language(self):
        raw = '```\n{"type": "ACTION", "tool": "resume_print", "params": {}, "reason": "go"}\n```'
        d = parse_llm_output(raw)
        assert d.type == "ACTION"
        assert d.tool == "resume_print"


class TestClampCheckInterval:
    """Interval clamping — enforced in code, not by the LLM."""

    def test_normal_interval_unchanged(self):
        assert clamp_check_interval(60.0) == 60.0

    def test_too_fast_clamped_to_min(self):
        assert clamp_check_interval(1.0) == MIN_CHECK_INTERVAL
        assert clamp_check_interval(0) == MIN_CHECK_INTERVAL

    def test_too_slow_idle_clamped(self):
        assert clamp_check_interval(400.0, printer_state="IDLE") == MAX_CHECK_INTERVAL_IDLE
        assert clamp_check_interval(400.0, printer_state="FINISHED") == MAX_CHECK_INTERVAL_IDLE

    def test_too_slow_printing_clamped(self):
        # Values above MAX_CHECK_INTERVAL are clamped during active printing
        big = MAX_CHECK_INTERVAL + 100
        assert clamp_check_interval(big, printer_state="PRINTING") == MAX_CHECK_INTERVAL
        assert clamp_check_interval(big, printer_state="PAUSED") == MAX_CHECK_INTERVAL
        assert clamp_check_interval(big, printer_state="ATTENTION") == MAX_CHECK_INTERVAL

    def test_idle_allows_longer_waits(self):
        # A value between MAX_CHECK_INTERVAL and MAX_CHECK_INTERVAL_IDLE is allowed when idle
        mid = min(MAX_CHECK_INTERVAL + 10, MAX_CHECK_INTERVAL_IDLE)
        assert clamp_check_interval(mid, printer_state="IDLE") == mid
        assert clamp_check_interval(mid, printer_state="PRINTING") == MAX_CHECK_INTERVAL

    def test_no_state_uses_idle_max(self):
        mid = min(MAX_CHECK_INTERVAL + 10, MAX_CHECK_INTERVAL_IDLE)
        assert clamp_check_interval(mid, printer_state=None) == mid

    def test_case_insensitive_state(self):
        big = MAX_CHECK_INTERVAL + 100
        assert clamp_check_interval(big, printer_state="printing") == MAX_CHECK_INTERVAL

    def test_exact_boundaries(self):
        assert clamp_check_interval(MIN_CHECK_INTERVAL) == MIN_CHECK_INTERVAL
        assert clamp_check_interval(MAX_CHECK_INTERVAL, "PRINTING") == MAX_CHECK_INTERVAL
        assert clamp_check_interval(MAX_CHECK_INTERVAL_IDLE, "IDLE") == MAX_CHECK_INTERVAL_IDLE


class TestClampInParsedDecision:
    """Clamping is applied during parse_llm_output, not just in isolation."""

    def test_wait_interval_clamped_during_parse(self):
        raw = json.dumps({"type": "WAIT", "reason": "ok", "check_after_s": 1})
        d = parse_llm_output(raw, printer_state="PRINTING")
        assert d.check_after_s == MIN_CHECK_INTERVAL

    def test_wait_large_interval_clamped_printing(self):
        big = MAX_CHECK_INTERVAL + 100
        raw = json.dumps({"type": "WAIT", "reason": "ok", "check_after_s": big})
        d = parse_llm_output(raw, printer_state="PRINTING")
        assert d.check_after_s == MAX_CHECK_INTERVAL

    def test_wait_large_interval_allowed_idle(self):
        mid = min(MAX_CHECK_INTERVAL + 10, MAX_CHECK_INTERVAL_IDLE)
        raw = json.dumps({"type": "WAIT", "reason": "ok", "check_after_s": mid})
        d = parse_llm_output(raw, printer_state="IDLE")
        assert d.check_after_s == mid

    def test_wait_default_interval(self):
        raw = json.dumps({"type": "WAIT", "reason": "ok"})
        d = parse_llm_output(raw)
        assert d.check_after_s == DEFAULT_CHECK_INTERVAL


class TestParseActionChain:
    def test_valid_action_chain(self):
        raw = json.dumps({
            "type": "ACTION_CHAIN",
            "observation": "need two steps",
            "reasoning": "first pause, then set temp",
            "actions": [
                {"tool": "pause_print", "params": {}},
                {"tool": "set_temperature", "params": {"target": 200, "heater": "nozzle"}},
            ],
        })
        d = parse_llm_output(raw)
        assert d.type == "ACTION_CHAIN"
        assert len(d.actions) == 2
        assert d.actions[0]["tool"] == "pause_print"
        assert d.actions[1]["tool"] == "set_temperature"

    def test_action_chain_string_params_are_deserialized(self):
        raw = json.dumps({
            "type": "ACTION_CHAIN",
            "observation": "need two steps",
            "reasoning": "pause then tune",
            "actions": [
                {"tool": "pause_print", "params": "{}"},
                {"tool": "set_temperature", "params": "{\"target\": 200, \"heater\": \"nozzle\"}"},
            ],
        })
        d = parse_llm_output(raw)
        assert d.type == "ACTION_CHAIN"
        assert d.actions[0]["params"] == {}
        assert d.actions[1]["params"] == {"target": 200, "heater": "nozzle"}

    def test_action_chain_invalid_string_params_default_empty(self):
        raw = json.dumps({
            "type": "ACTION_CHAIN",
            "observation": "test",
            "reasoning": "test",
            "actions": [{"tool": "pause_print", "params": "{bad-json}"}],
        })
        d = parse_llm_output(raw)
        assert d.type == "ACTION_CHAIN"
        assert d.actions[0]["params"] == {}

    def test_empty_actions_defaults_to_wait(self):
        raw = json.dumps({
            "type": "ACTION_CHAIN",
            "observation": "test",
            "reasoning": "test",
            "actions": [],
        })
        d = parse_llm_output(raw)
        assert d.type == "WAIT"

    def test_missing_actions_defaults_to_wait(self):
        raw = json.dumps({
            "type": "ACTION_CHAIN",
            "observation": "test",
            "reasoning": "test",
        })
        d = parse_llm_output(raw)
        assert d.type == "WAIT"

    def test_action_missing_tool_defaults_to_wait(self):
        raw = json.dumps({
            "type": "ACTION_CHAIN",
            "observation": "test",
            "reasoning": "test",
            "actions": [{"params": {}}],
        })
        d = parse_llm_output(raw)
        assert d.type == "WAIT"

    def test_chain_truncated_to_max_length(self):
        from wallee.agent.parser import MAX_CHAIN_LENGTH
        actions = [{"tool": f"tool_{i}", "params": {}} for i in range(8)]
        raw = json.dumps({
            "type": "ACTION_CHAIN",
            "observation": "lots of steps",
            "reasoning": "trying everything",
            "actions": actions,
        })
        d = parse_llm_output(raw)
        assert d.type == "ACTION_CHAIN"
        assert len(d.actions) == MAX_CHAIN_LENGTH
        assert d.actions[-1]["tool"] == f"tool_{MAX_CHAIN_LENGTH - 1}"
