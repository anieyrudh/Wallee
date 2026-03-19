"""Tests for built-in tools."""

import fakeredis
import pytest
from unittest.mock import patch, MagicMock

from wallee.whiteboard.client import Whiteboard
from wallee.tools.builtins.trends import trends
from wallee.tools.builtins.differential import differential
from wallee.tools.builtins.sensor_history import get_sensor_history
from wallee.tools.builtins.call_human_tool import call_human
from wallee.tools.builtins.discover import discover_hardware
from wallee.tools.builtins.web_search import web_search
from wallee.tools.registry import ToolRegistry


@pytest.fixture
def wb():
    r = fakeredis.FakeRedis(decode_responses=True)
    w = Whiteboard(_redis=r)
    # Populate some history with known timestamps: newest-oldest span = 4s
    values = iter([100.0, 101.0, 102.0, 103.0, 104.0])

    def fake_now():
        try:
            return next(values)
        except StopIteration:
            return 104.0

    w._now = fake_now
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
        assert result["rate"] == "+0.500/s"
        assert result["readings"] == 5

    def test_falls_back_without_timestamps(self, wb):
        wb.r.delete("env.temperature:history_ts")
        result = differential(key="env.temperature", whiteboard=wb)
        assert result["rate"] == "+0.500/s"

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
    @patch("wallee.device_packs.pi_cameras.sensors.discover_buddy_cameras", return_value=["192.168.0.194"])
    @patch("wallee.device_packs.pi_cameras.sensors.discover_nozzle_camera_port", return_value="8083")
    @patch("wallee.device_packs.prusa_serial.actuators._find_prusa_port", return_value="/dev/ttyACM0")
    def test_discover_hardware(self, mock_serial, mock_nozzle, mock_buddies, wb):
        result = discover_hardware(whiteboard=wb)
        assert result["status"] == "success"
        assert result["findings"]["camera.nozzle_port"] == "8083"
        assert result["findings"]["camera.buddy_ips"] == ["192.168.0.194"]
        assert result["findings"]["printer.serial_port"] == "/dev/ttyACM0"
        assert wb.read("camera.nozzle_port") == "8083"


class TestWebSearch:
    def test_no_query(self):
        result = web_search(query="")
        assert "error" in result

    def test_no_api_key(self):
        from wallee.tools.builtins.web_search import configure_web_search
        configure_web_search("", "model")
        result = web_search(query="test query")
        assert "error" in result
        assert "not configured" in result["error"]

    @patch("wallee.tools.builtins.web_search.httpx.post")
    def test_successful_search(self, mock_post):
        from wallee.tools.builtins.web_search import configure_web_search
        configure_web_search("test-key", "test-model")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "PLA prints best at 200-215C"}}]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = web_search(query="PLA temperature")
        assert result["status"] == "success"
        assert "PLA" in result["answer"]

    def test_has_tool_metadata(self):
        assert web_search._tool_meta["kind"] == "actuator"
        assert web_search._tool_meta["requires_approval"] is False


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
        assert "discover_hardware" in names
