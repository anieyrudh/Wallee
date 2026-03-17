"""Tests for diary per-device idempotency database."""

import pytest
from wallee.ledger.diary import Diary


@pytest.fixture
def diary(tmp_path):
    d = Diary("prusa_link", tmp_path)
    yield d
    d.close()


class TestWriteAndLookup:
    def test_inflight(self, diary):
        diary.write_inflight("key1", "action-123", "resume_print")
        assert diary.lookup("key1") == "IN_FLIGHT"

    def test_success(self, diary):
        diary.write_inflight("key1", "action-123", "resume_print")
        diary.write_success("key1", {"status": "ok"})
        assert diary.lookup("key1") == "SUCCESS"

    def test_failed(self, diary):
        diary.write_inflight("key1", "action-123", "resume_print")
        diary.write_failed("key1", {"error": "timeout"})
        assert diary.lookup("key1") == "FAILED"

    def test_lookup_missing(self, diary):
        assert diary.lookup("nonexistent") is None


class TestGetResult:
    def test_success_result(self, diary):
        diary.write_inflight("key1", "action-123", "resume_print")
        diary.write_success("key1", {"status": "ok", "code": 200})
        result = diary.get_result("key1")
        assert result == {"status": "ok", "code": 200}

    def test_failed_result(self, diary):
        diary.write_inflight("key1", "action-123", "resume_print")
        diary.write_failed("key1", {"error": "connection refused"})
        result = diary.get_result("key1")
        assert result == {"error": "connection refused"}

    def test_inflight_has_no_result(self, diary):
        diary.write_inflight("key1", "action-123", "resume_print")
        assert diary.get_result("key1") is None

    def test_missing_key(self, diary):
        assert diary.get_result("nonexistent") is None


class TestGetInflight:
    def test_returns_inflight_entries(self, diary):
        diary.write_inflight("key1", "a1", "tool_a")
        diary.write_inflight("key2", "a2", "tool_b")
        diary.write_success("key2", {"ok": True})

        inflight = diary.get_inflight()
        assert len(inflight) == 1
        assert inflight[0]["idempotency_key"] == "key1"

    def test_empty_when_none(self, diary):
        assert diary.get_inflight() == []


class TestIdempotency:
    def test_replace_on_duplicate_key(self, diary):
        diary.write_inflight("key1", "a1", "tool_a")
        diary.write_inflight("key1", "a2", "tool_a")  # re-dispatch
        assert diary.lookup("key1") == "IN_FLIGHT"

    def test_db_file_created(self, diary, tmp_path):
        assert (tmp_path / "diary_prusa_link.db").exists()
