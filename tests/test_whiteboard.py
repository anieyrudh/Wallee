"""Tests for whiteboard client using fakeredis."""

import time
import fakeredis
import pytest

from wallee.whiteboard.client import (
    Whiteboard,
    compute_trend,
    compute_differential,
)


@pytest.fixture
def wb():
    """Whiteboard backed by fakeredis."""
    r = fakeredis.FakeRedis(decode_responses=True)
    return Whiteboard(_redis=r)


class TestPublishRead:
    def test_publish_and_read(self, wb):
        wb.publish("env.temperature", 21.5)
        assert wb.read("env.temperature") == 21.5

    def test_read_missing_key(self, wb):
        assert wb.read("nonexistent") is None

    def test_publish_dict_value(self, wb):
        wb.publish("printer.state", {"job": "PRINTING", "progress": 42})
        val = wb.read("printer.state")
        assert val == {"job": "PRINTING", "progress": 42}

    def test_publish_string_value(self, wb):
        wb.publish("human.intent", "resume the print")
        assert wb.read("human.intent") == "resume the print"

    def test_ttl_expiry(self, wb):
        """Key should expire after TTL. fakeredis supports time-based expiry."""
        wb.publish("temp.key", "value", ttl=1)
        assert wb.read("temp.key") == "value"
        # fakeredis won't auto-expire in real time without advancing,
        # but we can verify the TTL was set
        ttl = wb.r.ttl("temp.key")
        assert ttl > 0


class TestRingBuffer:
    def test_history_builds_up(self, wb):
        for val in [20.0, 20.5, 21.0]:
            wb.publish("env.temperature", val, history_depth=10)
        history = wb.read_history("env.temperature")
        # Newest first (LPUSH)
        assert history == [21.0, 20.5, 20.0]

    def test_history_trims_to_depth(self, wb):
        for i in range(20):
            wb.publish("env.temperature", float(i), history_depth=5)
        history = wb.read_history("env.temperature")
        assert len(history) == 5
        # Newest first: 19, 18, 17, 16, 15
        assert history == [19.0, 18.0, 17.0, 16.0, 15.0]

    def test_no_history_when_depth_zero(self, wb):
        wb.publish("env.temperature", 21.0, history_depth=0)
        assert wb.read_history("env.temperature") == []

    def test_empty_history_for_unknown_key(self, wb):
        assert wb.read_history("unknown.key") == []


class TestReadAll:
    def test_read_all_excludes_history(self, wb):
        wb.publish("env.temperature", 21.5, history_depth=3)
        wb.publish("env.humidity", 55.0)
        state = wb.read_all()
        assert "env.temperature" in state
        assert "env.humidity" in state
        assert "env.temperature:history" not in state

    def test_read_all_with_trends(self, wb):
        # Publish rising temps: oldest first in ring buffer
        for val in [20.0, 20.5, 21.0, 21.5]:
            wb.publish("env.temperature", val, history_depth=10)
        state = wb.read_all_with_trends()
        assert "env.temperature" in state
        assert "env.temperature:trend" in state
        assert "rising" in state["env.temperature:trend"]

    def test_no_trend_for_non_numeric(self, wb):
        wb.publish("printer.state", "PRINTING", history_depth=5)
        wb.publish("printer.state", "PAUSED", history_depth=5)
        state = wb.read_all_with_trends()
        assert "printer.state:trend" not in state


class TestComputeTrend:
    def test_rising(self):
        # Newest first
        result = compute_trend([22.0, 21.0, 20.0])
        assert "rising" in result
        assert "+2.0" in result

    def test_falling(self):
        result = compute_trend([18.0, 19.0, 20.0])
        assert "falling" in result
        assert "-2.0" in result

    def test_stable(self):
        result = compute_trend([20.05, 20.0, 20.02])
        assert result == "stable"

    def test_insufficient_data(self):
        assert compute_trend([20.0]) == "insufficient data"
        assert compute_trend([]) == "insufficient data"

    def test_custom_threshold(self):
        # With default threshold 0.1, this is rising
        assert "rising" in compute_trend([20.2, 20.0])
        # With high threshold, this is stable
        assert compute_trend([20.2, 20.0], threshold=1.0) == "stable"


class TestComputeDifferential:
    def test_positive_rate(self):
        # 4 readings, 1s apart. Newest=23, oldest=20 → +3 over 3s = +1.0/s
        result = compute_differential([23.0, 22.0, 21.0, 20.0], interval_s=1.0)
        assert "+1.000/s" in result

    def test_negative_rate(self):
        result = compute_differential([17.0, 18.0, 19.0, 20.0], interval_s=1.0)
        assert "-1.000/s" in result

    def test_insufficient_data(self):
        assert compute_differential([20.0], interval_s=1.0) == "insufficient data"

    def test_different_interval(self):
        # 3 readings, 0.5s apart. Newest=22, oldest=20 → +2 over 1s = +2.0/s
        result = compute_differential([22.0, 21.0, 20.0], interval_s=0.5)
        assert "+2.000/s" in result
