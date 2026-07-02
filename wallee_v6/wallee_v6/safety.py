"""Independent safety kernel primitives."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable


@dataclass
class InterlockState:
    """Mutable interlock state used by the reference safety kernel."""
    engaged: bool = False
    reason: str | None = None


class SafetyKernel:
    """Heartbeat watchdog and interlock owner.

    The reference implementation keeps safety logic intentionally small.  The
    primary goal is not to model every imaginable fault, but to make sure the
    system has one independent place that can force a safe state when control
    stops responding.
    """

    def __init__(
        self,
        heartbeat_timeout_s: float = 3.0,
        *,
        mono_fn: Callable[[], float] | None = None,
    ) -> None:
        # Injectable clock: safety timing must be testable with a fake clock,
        # otherwise TTL/expiry bugs (the legacy ESTOP auto-un-latch class)
        # cannot be ruled out by tests that run in wall-clock milliseconds.
        self._mono = mono_fn or time.monotonic
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self._last_heartbeat_mono = self._mono()
        self.interlock = InterlockState()
        self._callbacks: list[Callable[[str], None]] = []

    def register_interlock_callback(self, callback: Callable[[str], None]) -> None:
        """Register a side-effect callback invoked on interlock."""
        self._callbacks.append(callback)

    def beat(self) -> None:
        """Record a control-plane heartbeat."""
        self._last_heartbeat_mono = self._mono()

    def trip(self, reason: str) -> None:
        """Engage the interlock if it is not already engaged."""
        if self.interlock.engaged:
            return
        self.interlock.engaged = True
        self.interlock.reason = reason
        for callback in self._callbacks:
            callback(reason)

    def clear(self) -> None:
        """Clear the interlock for tests or controlled reset flows."""
        self.interlock.engaged = False
        self.interlock.reason = None
        self._last_heartbeat_mono = self._mono()

    def poll(self) -> bool:
        """Check heartbeat age and trip the interlock if stale.

        Returns
        -------
        bool
            ``True`` if the interlock is now engaged.
        """
        age = self._mono() - self._last_heartbeat_mono
        if age > self.heartbeat_timeout_s:
            self.trip(f"heartbeat stale ({age:.2f}s)")
        return self.interlock.engaged
