"""Tests for engine gate sequence."""

import json
import time
import fakeredis
import pytest

from wallee.engine.dispatch import Engine
from wallee.ledger.db import Ledger
from wallee.tools.decorator import tool
from wallee.tools.registry import ToolRegistry
from wallee.whiteboard.client import Whiteboard


@pytest.fixture
def wb():
    return Whiteboard(_redis=fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "ledger.db")
    yield db
    db.close()


@pytest.fixture
def registry():
    reg = ToolRegistry()

    @tool(kind="actuator", requires_approval=False, max_proposal_age_ms=30000)
    def simple_action(whiteboard=None, **kwargs):
        """Simple action that always succeeds."""
        return {"status": "success"}

    @tool(kind="actuator", requires_approval=True, max_proposal_age_ms=30000)
    def approval_action(whiteboard=None, **kwargs):
        """Action that requires approval."""
        return {"status": "success"}

    @tool(kind="actuator", requires_approval=False, max_proposal_age_ms=30000)
    def failing_action(whiteboard=None, **kwargs):
        """Action that returns an error."""
        return {"error": "something broke"}

    @tool(kind="actuator", requires_approval=False, max_proposal_age_ms=30000)
    def precondition_action(whiteboard=None, **kwargs):
        """Action with precondition that checks whiteboard."""
        if whiteboard:
            state = whiteboard.read("printer.state")
            if state != "PAUSED":
                return {"error": f"not paused, state={state}"}
        return {"status": "success"}

    @tool(kind="actuator", requires_approval=False, max_proposal_age_ms=30000)
    def exception_action(whiteboard=None, **kwargs):
        """Action that raises."""
        raise RuntimeError("hardware error")

    reg.register("simple_action", simple_action, simple_action._tool_meta, "test_group")
    reg.register("approval_action", approval_action, approval_action._tool_meta, "test_group")
    reg.register("failing_action", failing_action, failing_action._tool_meta, "test_group")
    reg.register("precondition_action", precondition_action, precondition_action._tool_meta, "test_group")
    reg.register("exception_action", exception_action, exception_action._tool_meta, "test_group")
    return reg


@pytest.fixture
def engine(wb, ledger, registry, tmp_path):
    return Engine(
        whiteboard=wb,
        ledger=ledger,
        tools=registry,
        data_dir=str(tmp_path),
        poll_interval=0.1,
    )


class TestGate1TOCTOU:
    def test_rejects_when_precondition_fails(self, engine, ledger, wb):
        wb.publish("printer.state", "PRINTING")
        aid = ledger.propose("precondition_action", {}, "test", "test_group")
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "REJECTED"
        assert ledger.get_action(aid)["status"] == "REJECTED"
        assert "not paused" in ledger.get_action(aid)["error_json"]

    def test_passes_when_precondition_ok(self, engine, ledger, wb):
        wb.publish("printer.state", "PAUSED")
        aid = ledger.propose("precondition_action", {}, "test", "test_group")
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "DONE"

    def test_rejects_on_exception(self, engine, ledger):
        aid = ledger.propose("exception_action", {}, "test", "test_group")
        result = engine.process_proposal(ledger.get_action(aid))
        # TOCTOU will catch the exception from the first execution
        assert result == "REJECTED"

    def test_rejects_unknown_tool(self, engine, ledger):
        aid = ledger.propose("nonexistent_tool", {}, "test", "test_group")
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "REJECTED"


class TestGate2QueueGuard:
    def test_skips_when_inflight(self, engine, ledger):
        # Create an inflight action
        aid1 = ledger.propose("simple_action", {}, "r1", "test_group")
        ledger.set_status(aid1, "DISPATCHED")

        # New proposal should be skipped
        aid2 = ledger.propose("simple_action", {"x": 1}, "r2", "test_group")
        result = engine.process_proposal(ledger.get_action(aid2))
        assert result == "SKIPPED"
        # Should still be PROPOSED (not rejected)
        assert ledger.get_action(aid2)["status"] == "PROPOSED"

    def test_proceeds_when_no_inflight(self, engine, ledger):
        aid = ledger.propose("simple_action", {}, "test", "test_group")
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "DONE"


class TestGate3Deadline:
    def test_rejects_expired_proposal(self, engine, ledger):
        aid = ledger.propose("simple_action", {}, "test", "test_group", max_proposal_age_ms=1)
        # Sleep to let it expire
        time.sleep(0.01)
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "REJECTED"
        assert "expired" in ledger.get_action(aid)["error_json"]

    def test_passes_fresh_proposal(self, engine, ledger):
        aid = ledger.propose("simple_action", {}, "test", "test_group", max_proposal_age_ms=30000)
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "DONE"


class TestGate4Approval:
    def test_waits_when_no_approval(self, engine, ledger):
        aid = ledger.propose("approval_action", {}, "test", "test_group", requires_approval=True)
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "WAITING_APPROVAL"
        assert ledger.get_action(aid)["status"] == "WAITING_APPROVAL"

    def test_proceeds_when_approved(self, engine, ledger):
        aid = ledger.propose("approval_action", {}, "test", "test_group", requires_approval=True)
        ledger.record_approval(aid, "APPROVE", "operator")
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "DONE"

    def test_rejects_when_not_approved(self, engine, ledger):
        aid = ledger.propose("approval_action", {}, "test", "test_group", requires_approval=True)
        ledger.record_approval(aid, "REJECT", "operator")
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "REJECTED"


class TestGate5Dispatch:
    def test_successful_dispatch(self, engine, ledger, tmp_path):
        aid = ledger.propose("simple_action", {}, "test", "test_group")
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "DONE"
        action = ledger.get_action(aid)
        assert action["status"] == "DONE"
        assert "success" in action["result_json"]

    def test_failed_dispatch(self, engine, ledger):
        aid = ledger.propose("failing_action", {}, "test", "test_group")
        result = engine.process_proposal(ledger.get_action(aid))
        # Gate 1 TOCTOU catches the error return first
        assert result == "REJECTED"

    def test_diary_records_success(self, engine, ledger, tmp_path):
        aid = ledger.propose("simple_action", {}, "test", "test_group")
        engine.process_proposal(ledger.get_action(aid))
        diary = engine._get_diary("test_group")
        action = ledger.get_action(aid)
        status = diary.lookup(action["idempotency_key"])
        assert status == "SUCCESS"


class TestPollOnce:
    def test_processes_all_proposals(self, engine, ledger):
        ledger.propose("simple_action", {}, "r1", "test_group")
        ledger.propose("simple_action", {"x": 1}, "r2", "test_group")
        engine.poll_once()
        proposals = ledger.get_proposals()
        # First should be DONE, second might be SKIPPED (queue guard)
        # since first is dispatched+done atomically
        done = ledger.get_by_status("DONE")
        assert len(done) >= 1

    def test_retries_after_approval(self, engine, ledger):
        aid = ledger.propose("approval_action", {}, "test", "test_group", requires_approval=True)
        engine.poll_once()
        assert ledger.get_action(aid)["status"] == "WAITING_APPROVAL"

        ledger.record_approval(aid, "APPROVE", "operator")
        engine.poll_once()
        assert ledger.get_action(aid)["status"] == "DONE"
