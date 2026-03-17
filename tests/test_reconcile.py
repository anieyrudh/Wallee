"""Tests for crash recovery reconcile loop."""

import pytest
from wallee.ledger.db import Ledger
from wallee.ledger.diary import Diary
from wallee.engine.reconcile import reconcile


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "ledger.db")
    yield db
    db.close()


@pytest.fixture
def diary(tmp_path):
    d = Diary("test_group", tmp_path)
    yield d
    d.close()


@pytest.fixture
def get_diary(diary):
    def _get(device_group):
        return diary
    return _get


def _make_dispatched(ledger, tool="tool_a", params=None):
    """Helper: create an action and set it to DISPATCHED."""
    aid = ledger.propose(tool, params or {}, "test", "test_group")
    ledger.set_status(aid, "DISPATCHED")
    action = ledger.get_action(aid)
    return aid, action["idempotency_key"]


class TestReconcileSuccess:
    def test_diary_success_reconciles_to_done(self, ledger, diary, get_diary):
        aid, ikey = _make_dispatched(ledger)
        diary.write_inflight(ikey, aid, "tool_a")
        diary.write_success(ikey, {"status": "ok"})

        reconcile(ledger, get_diary)

        action = ledger.get_action(aid)
        assert action["status"] == "DONE"
        assert "ok" in action["result_json"]


class TestReconcileFailed:
    def test_diary_failed_reconciles_to_failed(self, ledger, diary, get_diary):
        aid, ikey = _make_dispatched(ledger)
        diary.write_inflight(ikey, aid, "tool_a")
        diary.write_failed(ikey, {"error": "hw timeout"})

        reconcile(ledger, get_diary)

        action = ledger.get_action(aid)
        assert action["status"] == "FAILED"
        assert "hw timeout" in action["error_json"]


class TestReconcileInFlight:
    def test_diary_inflight_sets_unknown_and_calls_human(self, ledger, diary, get_diary):
        aid, ikey = _make_dispatched(ledger)
        diary.write_inflight(ikey, aid, "tool_a")

        calls = []
        def mock_call_human(msg, severity):
            calls.append((msg, severity))

        reconcile(ledger, get_diary, call_human_fn=mock_call_human)

        action = ledger.get_action(aid)
        assert action["status"] == "UNKNOWN"
        assert len(calls) == 1
        assert calls[0][1] == "critical"
        assert "UNKNOWN" in calls[0][0]


class TestReconcileNoDiary:
    def test_no_diary_record_means_never_dispatched(self, ledger, get_diary):
        aid, ikey = _make_dispatched(ledger)
        # Diary has no record — crash happened before diary write

        reconcile(ledger, get_diary)

        action = ledger.get_action(aid)
        assert action["status"] == "FAILED"
        assert "never dispatched" in action["error_json"]


class TestReconcileMultiple:
    def test_handles_multiple_dispatched(self, ledger, diary, get_diary):
        aid1, ikey1 = _make_dispatched(ledger, "tool_a")
        aid2, ikey2 = _make_dispatched(ledger, "tool_b")

        diary.write_inflight(ikey1, aid1, "tool_a")
        diary.write_success(ikey1, {"ok": True})
        # aid2 has no diary record

        reconcile(ledger, get_diary)

        assert ledger.get_action(aid1)["status"] == "DONE"
        assert ledger.get_action(aid2)["status"] == "FAILED"


class TestReconcileIdempotent:
    def test_running_twice_is_safe(self, ledger, diary, get_diary):
        aid, ikey = _make_dispatched(ledger)
        diary.write_inflight(ikey, aid, "tool_a")
        diary.write_success(ikey, {"ok": True})

        reconcile(ledger, get_diary)
        reconcile(ledger, get_diary)  # second run should be a no-op

        assert ledger.get_action(aid)["status"] == "DONE"

    def test_no_dispatched_is_noop(self, ledger, get_diary):
        reconcile(ledger, get_diary)  # nothing to reconcile
