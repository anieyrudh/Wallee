"""Independent safety kernel primitives.

The kernel owns one durable fact: the interlock latch. A trip writes a latch
file under the data dir; engagement is derived from that file (plus in-memory
state) so a trip survives a process restart AND is visible to the independent
out-of-process watchdog, which writes the same file when it stops the machine.
The latch clears only on an explicit operator action — never by elapsed time.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class InterlockState:
    """Mutable interlock state used by the reference safety kernel."""
    engaged: bool = False
    reason: str | None = None


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".estop.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_estop_signal(
    path: Path | str,
    *,
    reason: str,
    requested_by: str,
    wall_fn: Callable[[], float] = time.time,
) -> None:
    """Atomically write an operator ESTOP request/clear signal file.

    The signal is a request, not the latch itself: the running control loop
    consumes it and calls ``trip()``/``clear()`` so the physical stop transport
    fires and the durable latch stays owned by the kernel. Writing the latch
    directly from a second process would engage the block but never fire the
    stop callbacks.
    """

    _atomic_write_json(
        Path(path),
        {"reason": reason, "requested_by": requested_by, "wall_ts": wall_fn(), "source": "operator_cli"},
    )


def consume_estop_signal(path: Path | str) -> dict | None:
    """Read and remove a pending ESTOP signal; return its payload or None.

    Consuming (delete-on-read) makes each request act exactly once: a trip or
    clear is applied on the next cycle and does not replay on every later cycle.
    """

    signal_path = Path(path)
    if not signal_path.exists():
        return None
    try:
        payload = json.loads(signal_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        payload = {}
    try:
        signal_path.unlink()
    except OSError:
        pass
    return payload if isinstance(payload, dict) else {}


def remove_estop_latch(path: Path | str) -> bool:
    """Delete the durable latch file if present; return whether it existed.

    Used by the attended ``clear-estop`` path so a latched machine boots clean
    even when no runtime is live to consume a clear signal.
    """

    latch_path = Path(path)
    if not latch_path.exists():
        return False
    try:
        latch_path.unlink()
        return True
    except OSError:
        return False


class SafetyKernel:
    """Heartbeat watchdog and interlock owner.

    The reference implementation keeps safety logic intentionally small.  The
    primary goal is not to model every imaginable fault, but to make sure the
    system has one independent place that can force a safe state when control
    stops responding — and that a stop, once engaged, does not silently expire.
    """

    def __init__(
        self,
        heartbeat_timeout_s: float = 3.0,
        *,
        mono_fn: Callable[[], float] | None = None,
        wall_fn: Callable[[], float] | None = None,
        latch_path: Path | str | None = None,
    ) -> None:
        # Injectable clocks: safety timing must be testable with a fake clock,
        # otherwise TTL/expiry bugs (the legacy ESTOP auto-un-latch class)
        # cannot be ruled out by tests that run in wall-clock milliseconds.
        self._mono = mono_fn or time.monotonic
        self._wall = wall_fn or time.time
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self._last_heartbeat_mono = self._mono()
        self.interlock = InterlockState()
        self._callbacks: list[Callable[[str], None]] = []
        self.latch_path = Path(latch_path) if latch_path is not None else None
        # A latch present at construction means a previous process (or the
        # watchdog) tripped and nobody has cleared it: rediscover it.
        self._load_latch()

    # --- latch persistence -------------------------------------------------

    def _load_latch(self) -> None:
        if self.latch_path is None or not self.latch_path.exists():
            return
        try:
            payload = json.loads(self.latch_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            payload = {}
        self.interlock.engaged = True
        self.interlock.reason = payload.get("reason") or "latched (reason unavailable)"

    def _write_latch(self, reason: str) -> None:
        if self.latch_path is None:
            return
        _atomic_write_json(
            self.latch_path,
            {"reason": reason, "wall_ts": self._wall(), "source": "safety_kernel"},
        )

    # --- interlock lifecycle ----------------------------------------------

    def register_interlock_callback(self, callback: Callable[[str], None]) -> None:
        """Register a side-effect callback invoked on interlock (e.g. a stop)."""
        self._callbacks.append(callback)

    def beat(self) -> None:
        """Record a control-plane heartbeat."""
        self._last_heartbeat_mono = self._mono()

    def trip(self, reason: str) -> None:
        """Engage the interlock. Latches durably; idempotent while engaged."""
        if self.engaged():
            return
        self.interlock.engaged = True
        self.interlock.reason = reason
        self._write_latch(reason)
        for callback in self._callbacks:
            try:
                callback(reason)
            except Exception:
                # A failing stop callback must not prevent the latch from
                # engaging; the watchdog is the backstop for delivery.
                pass

    def clear(self) -> None:
        """Clear the interlock. The ONLY way out — requires an explicit call.

        No elapsed-time path ever reaches here; releasing the latch is a
        deliberate operator action after the machine has been made safe.
        """
        self.interlock.engaged = False
        self.interlock.reason = None
        if self.latch_path is not None and self.latch_path.exists():
            try:
                self.latch_path.unlink()
            except OSError:
                pass
        self._last_heartbeat_mono = self._mono()

    def engaged(self) -> bool:
        """Return True if the interlock is engaged, syncing from the latch.

        Reads the durable latch so a trip written by the out-of-process
        watchdog (or a previous run) is honored even though it never touched
        this in-memory state.
        """
        if not self.interlock.engaged and self.latch_path is not None and self.latch_path.exists():
            self._load_latch()
        return self.interlock.engaged

    def poll(self) -> bool:
        """Check heartbeat age and trip the interlock if stale.

        Returns True if the interlock is now engaged.
        """
        age = self._mono() - self._last_heartbeat_mono
        if age > self.heartbeat_timeout_s:
            self.trip(f"heartbeat stale ({age:.2f}s)")
        return self.engaged()
