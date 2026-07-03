"""Independent out-of-process safety watchdog.

This is the real replacement for the shipped `sleep(10**9)` placeholder. It
runs as its own OS process (its own systemd unit, started before and required
by the main runtime) and does one job: if the control loop stops proving
liveness while a job is active, force the machine to a safe state.

Independence is the point, so this module imports only the stdlib plus the
stdlib-only `stop_transport`. It reads its inputs from files and env — never
from the runtime's in-memory objects — so it keeps working even if the runtime
never started or has wedged:

  {data_dir}/safety/heartbeat.json   runtime liveness beacon (mtime is truth)
  {data_dir}/safety/profile.json     pack stop profiles (persisted by build_runtime)
  {data_dir}/safety/estop.latch.json the shared latch (present == engaged)
  {data_dir}/safety/stop_commands.jsonl   file-transport stop log (sim/tests)

On a trip it writes the latch (so the in-process gate honors it too), issues
every profile's stop, and re-issues while the job stays active — it never
gives up while latched.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from .stop_transport import execute_stop


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".wd.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class SafetyWatchdog:
    def __init__(
        self,
        *,
        data_dir: Path,
        heartbeat_timeout_s: float,
        check_interval_s: float,
        resend_interval_s: float,
        boot_grace_s: float,
        now_fn=time.time,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.safety_dir = self.data_dir / "safety"
        self.heartbeat_path = self.safety_dir / "heartbeat.json"
        self.profile_path = self.safety_dir / "profile.json"
        self.latch_path = self.safety_dir / "estop.latch.json"
        self.request_path = self.safety_dir / "estop.request.json"
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.check_interval_s = check_interval_s
        self.resend_interval_s = resend_interval_s
        self.boot_grace_s = boot_grace_s
        self._now = now_fn
        self._started_at = now_fn()
        self._last_stop_at = 0.0

    # --- inputs ------------------------------------------------------------

    def _profiles(self) -> list[dict]:
        if not self.profile_path.exists():
            return []
        try:
            data = json.loads(self.profile_path.read_text(encoding="utf-8"))
            return [p for p in data if isinstance(p, dict)]
        except (ValueError, OSError):
            return []

    def _heartbeat_age(self) -> float | None:
        try:
            mtime = self.heartbeat_path.stat().st_mtime
        except OSError:
            return None
        return self._now() - mtime

    def _heartbeat_payload(self) -> dict:
        try:
            return json.loads(self.heartbeat_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def _latched(self) -> bool:
        return self.latch_path.exists()

    def _human_estop_requested(self) -> bool:
        return self.request_path.exists()

    # --- actions -----------------------------------------------------------

    def trip(self, reason: str, *, job_active: bool) -> None:
        if not self._latched():
            _atomic_write_json(
                self.latch_path,
                {"reason": reason, "wall_ts": self._now(), "source": "safety_watchdog"},
            )
        self._issue_stop(reason, job_active=job_active)

    def _issue_stop(self, reason: str, *, job_active: bool) -> None:
        self._last_stop_at = self._now()
        for profile in self._profiles():
            outcome = execute_stop(profile, data_dir=self.data_dir)
            self._escalate(
                title="Safety watchdog issued a machine stop",
                body=f"reason: {reason}\ntransport: {profile.get('transport')}\nresult: {outcome.detail}",
                ok=outcome.ok,
            )

    def _escalate(self, *, title: str, body: str, ok: bool) -> None:
        outbox = self.data_dir / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        request_id = f"watchdog_{int(self._now() * 1000)}_{os.getpid()}"
        _atomic_write_json(
            outbox / f"{request_id}.json",
            {
                "request_id": request_id,
                "severity": "critical",
                "require_ack": True,
                "title": title,
                "body": body,
                "stop_ok": ok,
                "created_ts_ms": int(self._now() * 1000),
            },
        )

    # --- loop --------------------------------------------------------------

    def tick(self) -> None:
        """One evaluation pass. Extracted for deterministic testing."""
        if self._human_estop_requested():
            self.trip("human estop request", job_active=True)
            return

        if self._latched():
            payload = self._heartbeat_payload()
            if payload.get("job_active") and self._now() - self._last_stop_at >= self.resend_interval_s:
                self._issue_stop("re-issue while latched during active job", job_active=True)
            return

        age = self._heartbeat_age()
        if age is None:
            # No beacon yet. Only alarm after the boot grace, and only escalate
            # (do not stop) — a machine that never started needs attention, not
            # a stop it cannot receive.
            if self._now() - self._started_at > self.boot_grace_s:
                self._escalate(
                    title="Safety watchdog: no runtime heartbeat",
                    body="No heartbeat beacon after boot grace; the runtime may not be running.",
                    ok=False,
                )
            return

        if age > self.heartbeat_timeout_s:
            payload = self._heartbeat_payload()
            if payload.get("job_active"):
                self.trip(f"heartbeat stale {age:.1f}s during active job", job_active=True)
            else:
                self._escalate(
                    title="Safety watchdog: runtime stale while idle",
                    body=f"Heartbeat stale {age:.1f}s but no job is active; not stopping.",
                    ok=True,
                )

    def run_forever(self) -> None:  # pragma: no cover - exercised via subprocess tests
        while True:
            try:
                self.tick()
            except Exception:
                pass
            time.sleep(self.check_interval_s)


def _watchdog_from_env() -> SafetyWatchdog:
    data_dir = Path(os.environ.get("WALLEE_SAFETY_DATA_DIR") or os.environ.get("WALLEE_DATA_DIR", ".runtime"))
    return SafetyWatchdog(
        data_dir=data_dir,
        heartbeat_timeout_s=float(os.environ.get("WALLEE_SAFETY_HEARTBEAT_TIMEOUT_S", "30")),
        check_interval_s=float(os.environ.get("WALLEE_SAFETY_CHECK_INTERVAL_S", "2")),
        resend_interval_s=float(os.environ.get("WALLEE_SAFETY_RESEND_INTERVAL_S", "30")),
        boot_grace_s=float(os.environ.get("WALLEE_SAFETY_BOOT_GRACE_S", "60")),
    )


def clear_latch(data_dir: Path) -> bool:
    """Operator-only latch clear. Returns True if a latch was removed."""
    latch = Path(data_dir) / "safety" / "estop.latch.json"
    request = Path(data_dir) / "safety" / "estop.request.json"
    removed = False
    for path in (latch, request):
        if path.exists():
            path.unlink()
            removed = True
    return removed


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - entry point
    parser = argparse.ArgumentParser(description="Wallee independent safety watchdog")
    parser.add_argument("--clear", action="store_true", help="Clear the ESTOP latch (operator action) and exit")
    parser.add_argument("--confirm", action="store_true", help="Required alongside --clear")
    parser.add_argument("--once", action="store_true", help="Run a single evaluation tick and exit")
    args = parser.parse_args(argv)

    watchdog = _watchdog_from_env()
    if args.clear:
        if not args.confirm:
            print("refusing to clear ESTOP latch without --confirm")
            return 2
        removed = clear_latch(watchdog.data_dir)
        print("ESTOP latch cleared" if removed else "no ESTOP latch present")
        return 0
    if args.once:
        watchdog.tick()
        return 0
    watchdog.run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
