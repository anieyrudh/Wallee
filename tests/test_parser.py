"""Tests for LLM output parser. Core safety boundary."""

import json
import pytest
from wallee.agent.parser import (
    parse_llm_output,
    Decision,
    clamp_check_interval,
    MIN_CHECK_INTERVAL,
    MAX_CHECK_INTERVAL,
    MAX_CHECK_INTERVAL_IDLE,
    DEFAULT_CHECK_INTERVAL,
)


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

    def test_action_missing_tool(self):
        raw = json.dumps({"type": "ACTION", "params": {}})
        d = parse_llm_output(raw)
        assert d.type == "WAIT"
        assert "missing tool" in d.reason


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
        assert clamp_check_interval(5.0) == MIN_CHECK_INTERVAL
        assert clamp_check_interval(1.0) == MIN_CHECK_INTERVAL
        assert clamp_check_interval(0) == MIN_CHECK_INTERVAL

    def test_too_slow_idle_clamped(self):
        assert clamp_check_interval(400.0, printer_state="IDLE") == MAX_CHECK_INTERVAL_IDLE
        assert clamp_check_interval(400.0, printer_state="FINISHED") == MAX_CHECK_INTERVAL_IDLE

    def test_too_slow_printing_clamped(self):
        assert clamp_check_interval(200.0, printer_state="PRINTING") == MAX_CHECK_INTERVAL
        assert clamp_check_interval(200.0, printer_state="PAUSED") == MAX_CHECK_INTERVAL
        assert clamp_check_interval(200.0, printer_state="ATTENTION") == MAX_CHECK_INTERVAL

    def test_idle_allows_longer_waits(self):
        assert clamp_check_interval(200.0, printer_state="IDLE") == 200.0
        assert clamp_check_interval(200.0, printer_state="PRINTING") == MAX_CHECK_INTERVAL

    def test_no_state_uses_idle_max(self):
        assert clamp_check_interval(250.0, printer_state=None) == 250.0

    def test_case_insensitive_state(self):
        assert clamp_check_interval(200.0, printer_state="printing") == MAX_CHECK_INTERVAL

    def test_exact_boundaries(self):
        assert clamp_check_interval(MIN_CHECK_INTERVAL) == MIN_CHECK_INTERVAL
        assert clamp_check_interval(MAX_CHECK_INTERVAL, "PRINTING") == MAX_CHECK_INTERVAL
        assert clamp_check_interval(MAX_CHECK_INTERVAL_IDLE, "IDLE") == MAX_CHECK_INTERVAL_IDLE


class TestClampInParsedDecision:
    """Clamping is applied during parse_llm_output, not just in isolation."""

    def test_wait_interval_clamped_during_parse(self):
        raw = json.dumps({"type": "WAIT", "reason": "ok", "check_after_s": 5})
        d = parse_llm_output(raw, printer_state="PRINTING")
        assert d.check_after_s == MIN_CHECK_INTERVAL

    def test_wait_large_interval_clamped_printing(self):
        raw = json.dumps({"type": "WAIT", "reason": "ok", "check_after_s": 300})
        d = parse_llm_output(raw, printer_state="PRINTING")
        assert d.check_after_s == MAX_CHECK_INTERVAL

    def test_wait_large_interval_allowed_idle(self):
        raw = json.dumps({"type": "WAIT", "reason": "ok", "check_after_s": 200})
        d = parse_llm_output(raw, printer_state="IDLE")
        assert d.check_after_s == 200.0

    def test_wait_default_60s(self):
        raw = json.dumps({"type": "WAIT", "reason": "ok"})
        d = parse_llm_output(raw)
        assert d.check_after_s == DEFAULT_CHECK_INTERVAL
