"""Tests for @tool decorator."""

import pytest
from wallee.tools.decorator import tool


class TestSensorDecorator:
    def test_basic_sensor(self):
        @tool(kind="sensor", refresh_hz=1.0, history_depth=10)
        def read_temp(self):
            """Read temperature."""
            return {"env.temperature": 21.5}

        meta = read_temp._tool_meta
        assert meta["kind"] == "sensor"
        assert meta["name"] == "read_temp"
        assert meta["refresh_hz"] == 1.0
        assert meta["history_depth"] == 10
        assert meta["doc"] == "Read temperature."

    def test_auto_ttl_calculation(self):
        @tool(kind="sensor", refresh_hz=1.0)
        def read_temp(self):
            return {}

        # TTL = (1000 / 1.0) * 2 = 2000ms
        assert read_temp._tool_meta["ttl_ms"] == 2000

    def test_auto_ttl_fast_sensor(self):
        @tool(kind="sensor", refresh_hz=10.0)
        def read_fast(self):
            return {}

        # TTL = (1000 / 10.0) * 2 = 200ms
        assert read_fast._tool_meta["ttl_ms"] == 200

    def test_ttl_override(self):
        @tool(kind="sensor", refresh_hz=1.0, ttl_ms=5000)
        def read_temp(self):
            return {}

        assert read_temp._tool_meta["ttl_ms"] == 5000

    def test_sensor_requires_refresh_hz(self):
        with pytest.raises(ValueError, match="refresh_hz"):
            @tool(kind="sensor")
            def bad_sensor(self):
                return {}

    def test_sensor_callable(self):
        @tool(kind="sensor", refresh_hz=1.0)
        def read_temp(self):
            return {"env.temperature": 21.5}

        # Should still be callable
        result = read_temp(None)
        assert result == {"env.temperature": 21.5}


class TestActuatorDecorator:
    def test_basic_actuator(self):
        @tool(kind="actuator", requires_approval=True)
        def resume_print(self, whiteboard):
            """Resume a paused print."""
            return {"status": "success"}

        meta = resume_print._tool_meta
        assert meta["kind"] == "actuator"
        assert meta["requires_approval"] is True
        assert meta["max_proposal_age_ms"] == 30000

    def test_actuator_defaults(self):
        @tool(kind="actuator")
        def simple_action(self):
            return {}

        meta = simple_action._tool_meta
        assert meta["requires_approval"] is False
        assert meta["max_proposal_age_ms"] == 30000

    def test_custom_proposal_age(self):
        @tool(kind="actuator", max_proposal_age_ms=60000)
        def slow_action(self):
            return {}

        assert slow_action._tool_meta["max_proposal_age_ms"] == 60000


class TestValidation:
    def test_invalid_kind(self):
        with pytest.raises(ValueError, match="kind"):
            @tool(kind="invalid")
            def bad_tool(self):
                return {}

    def test_preserves_function_name(self):
        @tool(kind="actuator")
        def my_special_tool(self):
            """Special doc."""
            return {}

        assert my_special_tool.__name__ == "my_special_tool"
        assert my_special_tool.__doc__ == "Special doc."

    def test_safety_limits(self):
        limits = {"max_temp": 85, "min_temp": -40}

        @tool(kind="sensor", refresh_hz=1.0, safety_limits=limits)
        def read_temp(self):
            return {}

        assert read_temp._tool_meta["safety_limits"] == limits
