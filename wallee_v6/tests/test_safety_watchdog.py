"""Unit tests for the independent safety watchdog's decision logic.

The watchdog's tick() is extracted so its stop-vs-escalate decisions are
testable deterministically with a fake clock and file inputs — no subprocess,
no sleeping. The SIGKILL end-to-end proof lives in the contract suite.
"""

from __future__ import annotations

import json


from wallee_v6.safety_watchdog import SafetyWatchdog, clear_latch


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += s


def _make(tmp_path, clock, **kw):
    safety = tmp_path / "safety"
    safety.mkdir(parents=True, exist_ok=True)
    (safety / "profile.json").write_text(
        json.dumps([{"transport": "file", "primary_request": {"method": "POST", "path": "/sim/stop"}}]),
        encoding="utf-8",
    )
    return SafetyWatchdog(
        data_dir=tmp_path,
        heartbeat_timeout_s=kw.get("heartbeat_timeout_s", 30.0),
        check_interval_s=1.0,
        resend_interval_s=kw.get("resend_interval_s", 30.0),
        boot_grace_s=kw.get("boot_grace_s", 60.0),
        now_fn=clock,
    )


def _beat(tmp_path, clock, *, job_active: bool):
    beacon = tmp_path / "safety" / "heartbeat.json"
    beacon.write_text(json.dumps({"wall_ts": clock(), "job_active": job_active}), encoding="utf-8")
    import os

    os.utime(beacon, (clock(), clock()))


def _stops(tmp_path) -> list:
    log = tmp_path / "safety" / "stop_commands.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_stale_heartbeat_during_active_job_stops_and_latches(tmp_path):
    clock = FakeClock()
    wd = _make(tmp_path, clock, heartbeat_timeout_s=10.0)
    _beat(tmp_path, clock, job_active=True)

    clock.advance(20.0)  # heartbeat now stale
    wd.tick()

    assert (tmp_path / "safety" / "estop.latch.json").exists()
    assert len(_stops(tmp_path)) == 1


def test_stale_heartbeat_while_idle_escalates_without_stopping(tmp_path):
    clock = FakeClock()
    wd = _make(tmp_path, clock, heartbeat_timeout_s=10.0)
    _beat(tmp_path, clock, job_active=False)

    clock.advance(20.0)
    wd.tick()

    assert not (tmp_path / "safety" / "estop.latch.json").exists()
    assert _stops(tmp_path) == []
    assert list((tmp_path / "outbox").glob("watchdog_*.json"))


def test_healthy_heartbeat_does_nothing(tmp_path):
    clock = FakeClock()
    wd = _make(tmp_path, clock, heartbeat_timeout_s=10.0)
    _beat(tmp_path, clock, job_active=True)

    clock.advance(5.0)  # within timeout
    wd.tick()

    assert not (tmp_path / "safety" / "estop.latch.json").exists()
    assert _stops(tmp_path) == []


def test_missing_heartbeat_within_boot_grace_is_silent(tmp_path):
    clock = FakeClock()
    wd = _make(tmp_path, clock, boot_grace_s=60.0)

    clock.advance(30.0)  # still within grace
    wd.tick()

    assert not list((tmp_path / "outbox").glob("watchdog_*.json"))


def test_human_estop_request_trips_immediately(tmp_path):
    clock = FakeClock()
    wd = _make(tmp_path, clock)
    (tmp_path / "safety" / "estop.request.json").write_text("{}", encoding="utf-8")

    wd.tick()

    assert (tmp_path / "safety" / "estop.latch.json").exists()
    assert len(_stops(tmp_path)) == 1


def test_latched_reissues_stop_while_job_active(tmp_path):
    clock = FakeClock()
    wd = _make(tmp_path, clock, resend_interval_s=30.0)
    _beat(tmp_path, clock, job_active=True)
    clock.advance(100.0)
    wd.tick()  # trips (stale + active)
    first = len(_stops(tmp_path))

    _beat(tmp_path, clock, job_active=True)  # refresh payload; latch remains
    clock.advance(40.0)  # past resend interval
    wd.tick()

    assert len(_stops(tmp_path)) == first + 1


def test_clear_latch_removes_latch_and_request(tmp_path):
    safety = tmp_path / "safety"
    safety.mkdir(parents=True)
    (safety / "estop.latch.json").write_text("{}", encoding="utf-8")
    (safety / "estop.request.json").write_text("{}", encoding="utf-8")

    assert clear_latch(tmp_path) is True
    assert not (safety / "estop.latch.json").exists()
    assert not (safety / "estop.request.json").exists()
    assert clear_latch(tmp_path) is False
