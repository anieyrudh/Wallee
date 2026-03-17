"""Tests for safety kernel heartbeat monitoring."""

import time
import threading
import fakeredis
import pytest

from wallee.safety.kernel import SafetyKernel
from wallee.whiteboard.client import Whiteboard


@pytest.fixture
def wb():
    return Whiteboard(_redis=fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture
def alerts():
    """Collects alert messages."""
    messages = []
    def call_human(msg, severity="critical"):
        messages.append((msg, severity))
    return messages, call_human


def _no_grace(kernel):
    """Disable boot grace period for testing."""
    kernel._boot_grace_s = 0
    return kernel


class TestHeartbeatChecks:
    def test_both_alive(self, wb, alerts):
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)

        kernel = _no_grace(SafetyKernel(wb, call_human_fn=fn, heartbeat_timeout=3.0))
        status = kernel.check_once()

        assert status["agent_ok"] is True
        assert status["engine_ok"] is True
        assert len(msgs) == 0

    def test_agent_missing(self, wb, alerts):
        msgs, fn = alerts
        # Only engine heartbeat
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)

        kernel = _no_grace(SafetyKernel(wb, call_human_fn=fn))
        status = kernel.check_once()

        assert status["agent_ok"] is False
        assert status["engine_ok"] is True
        assert len(msgs) == 1
        assert "Agent" in msgs[0][0]
        assert msgs[0][1] == "critical"

    def test_engine_missing(self, wb, alerts):
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)

        kernel = _no_grace(SafetyKernel(wb, call_human_fn=fn))
        status = kernel.check_once()

        assert status["agent_ok"] is True
        assert status["engine_ok"] is False
        assert len(msgs) == 1
        assert "Engine" in msgs[0][0]

    def test_both_missing(self, wb, alerts):
        msgs, fn = alerts
        kernel = _no_grace(SafetyKernel(wb, call_human_fn=fn))
        status = kernel.check_once()

        assert status["agent_ok"] is False
        assert status["engine_ok"] is False
        assert len(msgs) == 2

    def test_stale_heartbeat(self, wb, alerts):
        msgs, fn = alerts
        # Heartbeat from 10 seconds ago
        stale = time.monotonic() - 10.0
        wb.publish("agent.heartbeat", stale, ttl=30)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)

        kernel = _no_grace(SafetyKernel(wb, call_human_fn=fn, heartbeat_timeout=3.0))
        status = kernel.check_once()

        assert status["agent_ok"] is False
        assert len(msgs) == 1
        assert "stale" in msgs[0][0]


class TestAlertDedup:
    def test_only_alerts_once_per_failure(self, wb, alerts):
        msgs, fn = alerts
        kernel = _no_grace(SafetyKernel(wb, call_human_fn=fn))

        kernel.check_once()
        kernel.check_once()
        kernel.check_once()

        # Should only alert once per component, not every check
        agent_alerts = [m for m in msgs if "Agent" in m[0]]
        engine_alerts = [m for m in msgs if "Engine" in m[0]]
        assert len(agent_alerts) == 1
        assert len(engine_alerts) == 1

    def test_re_alerts_after_recovery_and_failure(self, wb, alerts):
        msgs, fn = alerts
        kernel = _no_grace(SafetyKernel(wb, call_human_fn=fn, heartbeat_timeout=3.0))

        # First failure
        kernel.check_once()
        assert len([m for m in msgs if "Agent" in m[0]]) == 1

        # Recovery
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)
        kernel.check_once()

        # Second failure
        wb.r.delete("agent.heartbeat")
        kernel.check_once()
        assert len([m for m in msgs if "Agent" in m[0]]) == 2


class TestOvercurrentMonitoring:
    def test_no_overcurrent(self, wb, alerts):
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)
        wb.publish("printer.oc_nozzle", 0)
        wb.publish("printer.oc_input", 0)

        kernel = _no_grace(SafetyKernel(wb, call_human_fn=fn))
        status = kernel.check_once()
        assert status["overcurrent_ok"] is True
        assert len(msgs) == 0

    def test_nozzle_overcurrent(self, wb, alerts):
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)
        wb.publish("printer.oc_nozzle", 1)

        kernel = _no_grace(SafetyKernel(wb, call_human_fn=fn))
        status = kernel.check_once()
        assert status["overcurrent_ok"] is False
        assert len(msgs) == 1
        assert "OVERCURRENT" in msgs[0][0]
        assert "nozzle" in msgs[0][0]

    def test_input_overcurrent(self, wb, alerts):
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)
        wb.publish("printer.oc_input", 1)

        kernel = _no_grace(SafetyKernel(wb, call_human_fn=fn))
        status = kernel.check_once()
        assert status["overcurrent_ok"] is False
        assert len(msgs) == 1
        assert "input power" in msgs[0][0]

    def test_overcurrent_dedup(self, wb, alerts):
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)
        wb.publish("printer.oc_nozzle", 1)

        kernel = _no_grace(SafetyKernel(wb, call_human_fn=fn))
        kernel.check_once()
        kernel.check_once()
        kernel.check_once()
        oc_msgs = [m for m in msgs if "OVERCURRENT" in m[0]]
        assert len(oc_msgs) == 1  # only alerts once

    def test_overcurrent_recovery(self, wb, alerts):
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)
        wb.publish("printer.oc_nozzle", 1)

        kernel = _no_grace(SafetyKernel(wb, call_human_fn=fn))
        kernel.check_once()
        assert len([m for m in msgs if "OVERCURRENT" in m[0]]) == 1

        # Overcurrent clears
        wb.publish("printer.oc_nozzle", 0)
        kernel.check_once()

        # Re-triggers
        wb.publish("printer.oc_nozzle", 1)
        kernel.check_once()
        assert len([m for m in msgs if "OVERCURRENT" in m[0]]) == 2

    def test_no_oc_keys_is_ok(self, wb, alerts):
        """If oc keys haven't been published yet, don't alert."""
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)

        kernel = _no_grace(SafetyKernel(wb, call_human_fn=fn))
        status = kernel.check_once()
        assert status["overcurrent_ok"] is True
        assert len(msgs) == 0


class TestEstopMonitoring:
    def test_estop_alerts_once(self, wb, alerts):
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)
        wb.publish("safety.estop", True, ttl=30)

        kernel = _no_grace(SafetyKernel(wb, call_human_fn=fn))
        status = kernel.check_once()
        assert status["estop_ok"] is False
        assert len(msgs) == 1
        assert "ESTOP ACTIVE" in msgs[0][0]

        kernel.check_once()
        assert len(msgs) == 1

    def test_estop_recovery_realerts(self, wb, alerts):
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)

        kernel = _no_grace(SafetyKernel(wb, call_human_fn=fn))
        wb.publish("safety.estop", True, ttl=30)
        kernel.check_once()
        assert len(msgs) == 1

        wb.publish("safety.estop", False, ttl=30)
        status = kernel.check_once()
        assert status["estop_ok"] is True

        wb.publish("safety.estop", True, ttl=30)
        kernel.check_once()
        assert len(msgs) == 2


class TestRunLoop:
    def test_loop_runs_and_stops(self, wb, alerts):
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=30)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=30)

        kernel = SafetyKernel(wb, call_human_fn=fn, check_interval=0.05)
        t = threading.Thread(target=kernel.run, daemon=True)
        t.start()
        time.sleep(0.2)
        kernel.stop()
        t.join(timeout=1)

        assert len(msgs) == 0  # both healthy, no alerts
