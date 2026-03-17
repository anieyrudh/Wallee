"""Tests for tool registry."""

import time
import fakeredis
import pytest

from wallee.tools.decorator import tool
from wallee.tools.registry import ToolRegistry, RegisteredTool
from wallee.whiteboard.client import Whiteboard


@pytest.fixture
def registry():
    return ToolRegistry()


@pytest.fixture
def wb():
    r = fakeredis.FakeRedis(decode_responses=True)
    return Whiteboard(_redis=r)


# --- Test tool functions ---

@tool(kind="sensor", refresh_hz=1.0, history_depth=5)
def mock_read_temp():
    """Read temperature."""
    return {"env.temperature": 21.5}


@tool(kind="actuator", requires_approval=True)
def mock_resume_print(whiteboard=None):
    """Resume print."""
    return {"status": "success"}


@tool(kind="actuator", requires_approval=False)
def mock_web_search(query="", whiteboard=None):
    """Search the web."""
    return {"status": "success", "results": []}


class TestRegistration:
    def test_register_and_get(self, registry):
        registry.register("read_temp", mock_read_temp, mock_read_temp._tool_meta, "host_pi")
        t = registry.get("read_temp")
        assert t is not None
        assert t.name == "read_temp"
        assert t.kind == "sensor"
        assert t.pack_name == "host_pi"

    def test_contains(self, registry):
        registry.register("read_temp", mock_read_temp, mock_read_temp._tool_meta, "host_pi")
        assert "read_temp" in registry
        assert "nonexistent" not in registry

    def test_get_missing(self, registry):
        assert registry.get("nope") is None

    def test_list_sensors_and_actuators(self, registry):
        registry.register("read_temp", mock_read_temp, mock_read_temp._tool_meta, "host_pi")
        registry.register("resume_print", mock_resume_print, mock_resume_print._tool_meta, "prusa")
        registry.register("web_search", mock_web_search, mock_web_search._tool_meta, "builtin")

        sensors = registry.list_sensors()
        actuators = registry.list_actuators()
        assert len(sensors) == 1
        assert sensors[0].name == "read_temp"
        assert len(actuators) == 2

    def test_list_for_llm(self, registry):
        registry.register("read_temp", mock_read_temp, mock_read_temp._tool_meta, "host_pi")
        registry.register("resume_print", mock_resume_print, mock_resume_print._tool_meta, "prusa")

        llm_tools = registry.list_for_llm()
        # Only actuators shown to LLM
        assert len(llm_tools) == 1
        assert llm_tools[0]["name"] == "resume_print"
        assert llm_tools[0]["requires_approval"] is True


class TestRegisteredTool:
    def test_properties(self):
        t = RegisteredTool("test", mock_resume_print, mock_resume_print._tool_meta, "prusa")
        assert t.kind == "actuator"
        assert t.requires_approval is True
        assert t.max_proposal_age_ms == 30000
        assert t.device_group == "prusa"

    def test_execute(self):
        t = RegisteredTool("test", mock_resume_print, mock_resume_print._tool_meta, "prusa")
        result = t.execute()
        assert result == {"status": "success"}


class TestSensorLoop:
    def test_sensor_publishes_to_whiteboard(self, registry, wb):
        registry.register("read_temp", mock_read_temp, mock_read_temp._tool_meta, "host_pi")
        registry.start_sensors(wb)
        # Give the sensor thread time to run at least once
        time.sleep(0.2)
        registry.stop_sensors()

        val = wb.read("env.temperature")
        assert val == 21.5

    def test_sensor_builds_history(self, registry, wb):
        call_count = 0

        @tool(kind="sensor", refresh_hz=20.0, history_depth=5)
        def counting_sensor():
            nonlocal call_count
            call_count += 1
            return {"test.counter": float(call_count)}

        registry.register("counter", counting_sensor, counting_sensor._tool_meta, "test")
        registry.start_sensors(wb)
        time.sleep(0.3)  # ~6 readings at 20Hz
        registry.stop_sensors()

        history = wb.read_history("test.counter")
        assert len(history) >= 2  # At least a few readings
        assert len(history) <= 5  # Capped by history_depth
