"""Tests for built-in tools."""

import fakeredis
import pytest

from wallee.whiteboard.client import Whiteboard
from wallee.tools.builtins.trends import trends
from wallee.tools.builtins.differential import differential
from wallee.tools.builtins.sensor_history import get_sensor_history
from wallee.tools.builtins.call_human_tool import call_human
from wallee.tools.builtins.discover import discover_hardware
from wallee.tools.builtins.web_search import web_search
from wallee.tools.builtins.git_pull import git_pull
from wallee.tools.registry import ToolRegistry


@pytest.fixture
def wb():
    r = fakeredis.FakeRedis(decode_responses=True)
    w = Whiteboard(_redis=r)
    # Populate some history
    for val in [20.0, 20.5, 21.0, 21.5, 22.0]:
        w.publish("env.temperature", val, history_depth=10)
    return w


class TestTrends:
    def test_rising_trend(self, wb):
        result = trends(key="env.temperature", whiteboard=wb)
        assert "rising" in result["trend"]
        assert result["readings"] == 5

    def test_no_key(self, wb):
        result = trends(key="", whiteboard=wb)
        assert "error" in result

    def test_no_history(self, wb):
        result = trends(key="nonexistent", whiteboard=wb)
        assert "no history" in result["trend"]

    def test_has_tool_metadata(self):
        assert trends._tool_meta["kind"] == "actuator"
        assert trends._tool_meta["requires_approval"] is False


class TestDifferential:
    def test_positive_rate(self, wb):
        result = differential(key="env.temperature", whiteboard=wb)
        assert "/s" in result["rate"]
        assert result["readings"] == 5

    def test_no_key(self, wb):
        result = differential(key="", whiteboard=wb)
        assert "error" in result

    def test_no_history(self, wb):
        result = differential(key="nonexistent", whiteboard=wb)
        assert "no history" in result["rate"]


class TestGetSensorHistory:
    def test_returns_values(self, wb):
        result = get_sensor_history(key="env.temperature", whiteboard=wb)
        assert result["count"] == 5
        assert result["values"][0] == 22.0  # newest first

    def test_depth_limit(self, wb):
        result = get_sensor_history(key="env.temperature", depth=3, whiteboard=wb)
        assert result["count"] == 3
        assert result["total_available"] == 5

    def test_no_key(self, wb):
        result = get_sensor_history(key="", whiteboard=wb)
        assert "error" in result


class TestCallHumanTool:
    def test_delivers_message(self, wb):
        result = call_human(message="test alert", severity="info", whiteboard=wb)
        assert result["status"] == "delivered"
        assert result["method"] in ("cli", "outbox")

    def test_no_message(self, wb):
        result = call_human(message="", whiteboard=wb)
        assert "error" in result


class TestPlaceholders:
    def test_discover_hardware(self, wb):
        result = discover_hardware(whiteboard=wb)
        assert result["status"] == "success"

    def test_web_search(self, wb):
        result = web_search(query="BME280 datasheet", whiteboard=wb)
        assert result["status"] == "not_implemented"

    def test_web_search_no_query(self, wb):
        result = web_search(query="", whiteboard=wb)
        assert "error" in result

    def test_git_pull(self, wb):
        result = git_pull(repo_url="https://example.com/repo", whiteboard=wb)
        assert result["status"] == "not_implemented"

    def test_git_pull_requires_approval(self):
        assert git_pull._tool_meta["requires_approval"] is True


class TestRegistryLoadBuiltins:
    def test_loads_all_builtins(self):
        reg = ToolRegistry()
        reg.load_builtins()
        assert "trends" in reg
        assert "differential" in reg
        assert "get_sensor_history" in reg
        assert "call_human" in reg
        assert "discover_hardware" in reg
        assert "web_search" in reg
        assert "git_pull" in reg

    def test_builtins_are_actuators(self):
        reg = ToolRegistry()
        reg.load_builtins()
        for t in reg.list_all():
            assert t.kind == "actuator"

    def test_builtins_in_llm_list(self):
        reg = ToolRegistry()
        reg.load_builtins()
        llm_tools = reg.list_for_llm()
        names = [t["name"] for t in llm_tools]
        assert "trends" in names
        assert "git_pull" in names
