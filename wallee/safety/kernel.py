"""Safety kernel — independent watchdog process for heartbeat monitoring."""

import logging
import os
import time
import threading

from wallee.safety.estop import estop_printer
from wallee.whiteboard.client import Whiteboard

logger = logging.getLogger(__name__)


class SafetyKernel:
    """Monitors agent/engine heartbeats and printer safety signals.

    Checks:
    - Agent and engine heartbeats (must publish within timeout)
    - Overcurrent flags from metrics stream (oc_nozz, oc_inp)
    Future: GPIO interlock relay when dangerous actuators are connected.
    """

    def __init__(
        self,
        whiteboard: Whiteboard,
        call_human_fn=None,
        check_interval: float = 0.5,
        heartbeat_timeout: float = 3.0,
    ):
        self.wb = whiteboard
        self.call_human_fn = call_human_fn or self._default_call_human
        self.check_interval = check_interval
        self.heartbeat_timeout = heartbeat_timeout
        self._running = False
        self._agent_alerted = False
        self._engine_alerted = False
        self._oc_nozzle_alerted = False
        self._oc_input_alerted = False
        self._estop_alerted = False
        self._boot_time = time.monotonic()
        self._boot_grace_s = 10.0  # suppress heartbeat alerts for 10s after boot

    @staticmethod
    def _default_call_human(message: str, severity: str = "critical"):
        logger.critical(f"[SAFETY] {severity}: {message}")

    def check_once(self) -> dict:
        """Run one check cycle. Returns status dict for testing."""
        status = {"agent_ok": True, "engine_ok": True, "overcurrent_ok": True, "estop_ok": True}

        # Boot grace period — suppress heartbeat alerts while components start
        in_grace = (time.monotonic() - self._boot_time) < self._boot_grace_s
        now = time.monotonic()

        # Check agent heartbeat (skip during boot grace period)
        agent_hb = self.wb.read("agent.heartbeat")
        if agent_hb is None:
            if not self._agent_alerted and not in_grace:
                self.call_human_fn("Agent heartbeat lost (key missing or expired)", "critical")
                self._agent_alerted = True
            status["agent_ok"] = False
        else:
            age = now - agent_hb
            if age > self.heartbeat_timeout:
                if not self._agent_alerted:
                    self.call_human_fn(
                        f"Agent heartbeat stale ({age:.1f}s old, timeout={self.heartbeat_timeout}s)",
                        "critical",
                    )
                    self._agent_alerted = True
                status["agent_ok"] = False
            else:
                self._agent_alerted = False

        # Check engine heartbeat (skip during boot grace period)
        engine_hb = self.wb.read("engine.heartbeat")
        if engine_hb is None:
            if not self._engine_alerted and not in_grace:
                self.call_human_fn("Engine heartbeat lost (key missing or expired)", "critical")
                self._engine_alerted = True
            status["engine_ok"] = False
        else:
            age = now - engine_hb
            if age > self.heartbeat_timeout:
                if not self._engine_alerted:
                    self.call_human_fn(
                        f"Engine heartbeat stale ({age:.1f}s old, timeout={self.heartbeat_timeout}s)",
                        "critical",
                    )
                    self._engine_alerted = True
                status["engine_ok"] = False
            else:
                self._engine_alerted = False

        # Check overcurrent flags from metrics stream
        oc_nozz = self.wb.read("printer.oc_nozzle")
        if oc_nozz is not None and oc_nozz != 0:
            if not self._oc_nozzle_alerted:
                self.call_human_fn(
                    f"OVERCURRENT DETECTED: nozzle heater (oc_nozz={oc_nozz}). "
                    "Possible heater failure or short circuit.",
                    "critical",
                )
                self._oc_nozzle_alerted = True
            status["overcurrent_ok"] = False
        else:
            self._oc_nozzle_alerted = False

        oc_inp = self.wb.read("printer.oc_input")
        if oc_inp is not None and oc_inp != 0:
            if not self._oc_input_alerted:
                self.call_human_fn(
                    f"OVERCURRENT DETECTED: input power (oc_inp={oc_inp}). "
                    "Possible power supply overload.",
                    "critical",
                )
                self._oc_input_alerted = True
            status["overcurrent_ok"] = False
        else:
            self._oc_input_alerted = False

        estop_active = self.wb.read("safety.estop")
        if estop_active:
            if not self._estop_alerted:
                estop_printer(os.environ.get("PRUSALINK_HOST", ""), os.environ.get("PRUSALINK_API_KEY", ""))
                self.call_human_fn(
                    "ESTOP ACTIVATED — printer paused. Manual intervention required.",
                    "critical",
                )
                self._estop_alerted = True
            status["estop_ok"] = False
        else:
            self._estop_alerted = False

        return status

    def run(self):
        """Main loop. Blocks until stop() is called."""
        self._running = True
        logger.info("Safety kernel started")
        try:
            while self._running:
                try:
                    self.check_once()
                except Exception as e:
                    logger.error(f"Safety kernel check error: {e}")
                time.sleep(self.check_interval)
        finally:
            self._running = False
            logger.info("Safety kernel stopped")

    def stop(self):
        self._running = False
