"""Tests for safety kernel heartbeat monitoring."""

import time
import threading
import fakeredis
import pytest
from unittest.mock import patch, MagicMock

from wallee.safety.kernel import SafetyKernel
from wallee.safety.estop import estop_printer
from wallee.whiteboard.client import Whiteboard

FAULT_MONITORS = [
    {"key": "printer.oc_nozzle", "label": "heater channel"},
    {"key": "printer.oc_input", "label": "input power"},
]


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


def _kernel(wb, fn, **kwargs):
    return _no_grace(SafetyKernel(wb, call_human_fn=fn, fault_monitors=FAULT_MONITORS, **kwargs))


class TestHeartbeatChecks:
    def test_both_alive(self, wb, alerts):
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)

        kernel = _kernel(wb, fn, heartbeat_timeout=3.0)
        status = kernel.check_once()

        assert status["agent_ok"] is True
        assert status["engine_ok"] is True
        assert len(msgs) == 0

    def test_agent_missing(self, wb, alerts):
        msgs, fn = alerts
        # Only engine heartbeat
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)

        kernel = _kernel(wb, fn)
        status = kernel.check_once()

        assert status["agent_ok"] is False
        assert status["engine_ok"] is True
        assert len(msgs) == 1
        assert "Agent" in msgs[0][0]
        assert msgs[0][1] == "critical"

    def test_engine_missing(self, wb, alerts):
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)

        kernel = _kernel(wb, fn)
        status = kernel.check_once()

        assert status["agent_ok"] is True
        assert status["engine_ok"] is False
        assert len(msgs) == 1
        assert "Engine" in msgs[0][0]

    def test_both_missing(self, wb, alerts):
        msgs, fn = alerts
        kernel = _kernel(wb, fn)
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

        kernel = _kernel(wb, fn, heartbeat_timeout=3.0)
        status = kernel.check_once()

        assert status["agent_ok"] is False
        assert len(msgs) == 1
        assert "stale" in msgs[0][0]


class TestAlertDedup:
    def test_only_alerts_once_per_failure(self, wb, alerts):
        msgs, fn = alerts
        kernel = _kernel(wb, fn)

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
        kernel = _kernel(wb, fn, heartbeat_timeout=3.0)

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

        kernel = _kernel(wb, fn)
        status = kernel.check_once()
        assert status["overcurrent_ok"] is True
        assert len(msgs) == 0

    def test_nozzle_overcurrent(self, wb, alerts):
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)
        wb.publish("printer.oc_nozzle", 1)

        kernel = _kernel(wb, fn)
        status = kernel.check_once()
        assert status["overcurrent_ok"] is False
        assert len(msgs) == 1
        assert "SAFETY FAULT" in msgs[0][0]
        assert "heater channel" in msgs[0][0]

    def test_input_overcurrent(self, wb, alerts):
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)
        wb.publish("printer.oc_input", 1)

        kernel = _kernel(wb, fn)
        status = kernel.check_once()
        assert status["overcurrent_ok"] is False
        assert len(msgs) == 1
        assert "input power" in msgs[0][0]

    def test_overcurrent_dedup(self, wb, alerts):
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)
        wb.publish("printer.oc_nozzle", 1)

        kernel = _kernel(wb, fn)
        kernel.check_once()
        kernel.check_once()
        kernel.check_once()
        oc_msgs = [m for m in msgs if "SAFETY FAULT" in m[0]]
        assert len(oc_msgs) == 1  # only alerts once

    def test_overcurrent_recovery(self, wb, alerts):
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)
        wb.publish("printer.oc_nozzle", 1)

        kernel = _kernel(wb, fn)
        kernel.check_once()
        assert len([m for m in msgs if "SAFETY FAULT" in m[0]]) == 1

        # Overcurrent clears
        wb.publish("printer.oc_nozzle", 0)
        kernel.check_once()

        # Re-triggers
        wb.publish("printer.oc_nozzle", 1)
        kernel.check_once()
        assert len([m for m in msgs if "SAFETY FAULT" in m[0]]) == 2

    def test_no_oc_keys_is_ok(self, wb, alerts):
        """If oc keys haven't been published yet, don't alert."""
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)

        kernel = _kernel(wb, fn)
        status = kernel.check_once()
        assert status["overcurrent_ok"] is True
        assert len(msgs) == 0


class TestEstopMonitoring:
    def test_estop_alerts_once(self, wb, alerts):
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)
        wb.publish("safety.estop", True, ttl=30)

        kernel = _kernel(wb, fn)
        status = kernel.check_once()
        assert status["estop_ok"] is False
        assert len(msgs) == 1
        assert "ESTOP ACTIVATED" in msgs[0][0]

        kernel.check_once()
        assert len(msgs) == 1

    def test_estop_recovery_realerts(self, wb, alerts):
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=5)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=5)

        kernel = _kernel(wb, fn)
        wb.publish("safety.estop", True, ttl=30)
        kernel.check_once()
        assert len(msgs) == 1

        wb.publish("safety.estop", False, ttl=30)
        status = kernel.check_once()
        assert status["estop_ok"] is True

        wb.publish("safety.estop", True, ttl=30)
        kernel.check_once()
        assert len(msgs) == 2


class TestEstopPrinter:
    """Tests for wallee.safety.estop.estop_printer — direct stop bypass."""

    @patch("wallee.safety.estop.httpx.request")
    def test_estop_sends_primary_request(self, mock_request):
        """ESTOP sends M25 to printer via direct HTTP, bypassing engine."""
        mock_request.return_value = MagicMock(status_code=204)
        result = estop_printer("192.0.2.50", "test-key")
        assert result is True
        mock_request.assert_called_once()
        call_args = mock_request.call_args
        assert "/api/v1/gcode" in call_args[0][1]
        assert call_args[1]["json"] == {"command": "M25"}

    @patch("wallee.safety.estop.httpx.request")
    def test_estop_falls_back_to_cancel(self, mock_request):
        """If M25 fails, ESTOP tries DELETE /api/v1/job."""
        mock_request.side_effect = [Exception("connection refused"), MagicMock(status_code=204)]
        result = estop_printer("192.0.2.50", "test-key")
        assert result is True
        assert mock_request.call_count == 2
        assert "/api/v1/job" in mock_request.call_args_list[1][0][1]

    @patch("wallee.safety.estop.httpx.request")
    def test_estop_all_fail(self, mock_request):
        """If both M25 and cancel fail, returns False."""
        mock_request.side_effect = [Exception("post failed"), Exception("delete failed")]
        result = estop_printer("192.0.2.50", "test-key")
        assert result is False

    def test_estop_no_host(self):
        """No host configured returns False without HTTP calls."""
        result = estop_printer("", "key")
        assert result is False


class TestRunLoop:
    def test_loop_runs_and_stops(self, wb, alerts):
        msgs, fn = alerts
        wb.publish("agent.heartbeat", time.monotonic(), ttl=30)
        wb.publish("engine.heartbeat", time.monotonic(), ttl=30)

        kernel = SafetyKernel(wb, call_human_fn=fn, check_interval=0.05, fault_monitors=FAULT_MONITORS)
        t = threading.Thread(target=kernel.run, daemon=True)
        t.start()
        time.sleep(0.2)
        kernel.stop()
        t.join(timeout=1)

        assert len(msgs) == 0  # both healthy, no alerts


class TestSafetyKernelSubprocess:
    """Verify the safety kernel runs as an independent OS process."""

    def test_kernel_main_is_importable(self):
        """kernel_main.py must be importable as a module."""
        from wallee.safety import kernel_main
        assert hasattr(kernel_main, "run_kernel")
        assert hasattr(kernel_main, "_estop_printer")

    def test_kernel_main_runs_as_script(self):
        """Safety kernel must be launchable as a separate process."""
        import subprocess, sys
        proc = subprocess.Popen(
            [sys.executable, "-m", "wallee.safety.kernel_main", "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, _ = proc.communicate(timeout=10)
        assert proc.returncode == 0
        assert b"redis-url" in stdout

    def test_main_py_uses_subprocess_not_thread(self):
        """main.py must launch safety kernel via subprocess, not threading."""
        import os
        main_path = os.path.join(os.path.dirname(__file__), "..", "wallee", "main.py")
        with open(main_path) as f:
            content = f.read()

        assert "subprocess.Popen" in content
        assert "kernel_main" in content
        # Thread-based safety launch must be gone
        assert "Thread(target=safety.run" not in content
