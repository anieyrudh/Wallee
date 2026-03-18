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
    call_counts = {
        "simple_action": 0,
        "approval_action": 0,
        "failing_action": 0,
        "precondition_action": 0,
        "exception_action": 0,
    }

    @tool(kind="actuator", requires_approval=False, max_proposal_age_ms=30000)
    def simple_action(whiteboard=None, **kwargs):
        """Simple action that always succeeds."""
        call_counts["simple_action"] += 1
        return {"status": "success"}

    @tool(kind="actuator", requires_approval=True, max_proposal_age_ms=30000)
    def approval_action(whiteboard=None, **kwargs):
        """Action that requires approval."""
        call_counts["approval_action"] += 1
        return {"status": "success"}

    @tool(kind="actuator", requires_approval=False, max_proposal_age_ms=30000)
    def failing_action(whiteboard=None, **kwargs):
        """Action that returns an error."""
        call_counts["failing_action"] += 1
        return {"error": "something broke"}

    def precondition_check(whiteboard=None, **kwargs):
        if whiteboard:
            state = whiteboard.read("printer.state")
            if state != "PAUSED":
                return {"error": f"not paused, state={state}"}
        return {"status": "ok"}

    @tool(
        kind="actuator",
        requires_approval=False,
        max_proposal_age_ms=30000,
        precheck_fn=precondition_check,
    )
    def precondition_action(whiteboard=None, **kwargs):
        """Action with side-effect-free precondition check."""
        call_counts["precondition_action"] += 1
        return {"status": "success"}

    @tool(kind="actuator", requires_approval=False, max_proposal_age_ms=30000)
    def exception_action(whiteboard=None, **kwargs):
        """Action that raises."""
        call_counts["exception_action"] += 1
        raise RuntimeError("hardware error")

    reg.register("simple_action", simple_action, simple_action._tool_meta, "test_group")
    reg.register("approval_action", approval_action, approval_action._tool_meta, "test_group")
    reg.register("failing_action", failing_action, failing_action._tool_meta, "test_group")
    reg.register("precondition_action", precondition_action, precondition_action._tool_meta, "test_group")
    reg.register("exception_action", exception_action, exception_action._tool_meta, "test_group")
    reg._call_counts = call_counts
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
    def test_rejects_when_precondition_fails(self, engine, ledger, wb, registry):
        wb.publish("printer.state", "PRINTING")
        aid = ledger.propose("precondition_action", {}, "test", "test_group")
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "REJECTED"
        assert ledger.get_action(aid)["status"] == "REJECTED"
        assert "not paused" in ledger.get_action(aid)["error_json"]
        assert registry._call_counts["precondition_action"] == 0

    def test_passes_when_precondition_ok(self, engine, ledger, wb, registry):
        wb.publish("printer.state", "PAUSED")
        aid = ledger.propose("precondition_action", {}, "test", "test_group")
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "DONE"
        assert registry._call_counts["precondition_action"] == 1

    def test_rejects_on_exception(self, engine, ledger, registry):
        aid = ledger.propose("exception_action", {}, "test", "test_group")
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "FAILED"
        assert registry._call_counts["exception_action"] == 1

    def test_rejects_unknown_tool(self, engine, ledger):
        aid = ledger.propose("nonexistent_tool", {}, "test", "test_group")
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "REJECTED"


class TestGate0Estop:
    def test_rejects_when_estop_active(self, engine, ledger, wb, registry):
        wb.publish("safety.estop", True, ttl=30)
        aid = ledger.propose("simple_action", {}, "test", "test_group")

        result = engine.process_proposal(ledger.get_action(aid))

        assert result == "REJECTED"
        assert ledger.get_action(aid)["status"] == "REJECTED"
        assert "safety.estop" in ledger.get_action(aid)["error_json"]
        assert registry._call_counts["simple_action"] == 0


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
    def test_waits_when_no_approval(self, engine, ledger, registry):
        aid = ledger.propose("approval_action", {}, "test", "test_group", requires_approval=True)
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "WAITING_APPROVAL"
        assert ledger.get_action(aid)["status"] == "WAITING_APPROVAL"
        assert registry._call_counts["approval_action"] == 0

    def test_proceeds_when_approved(self, engine, ledger, registry):
        aid = ledger.propose("approval_action", {}, "test", "test_group", requires_approval=True)
        ledger.record_approval(aid, "APPROVE", "operator")
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "DONE"
        assert registry._call_counts["approval_action"] == 1

    def test_sends_approval_notification_once(self, wb, ledger, registry, tmp_path):
        notifications = []

        engine = Engine(
            whiteboard=wb,
            ledger=ledger,
            tools=registry,
            data_dir=str(tmp_path),
            poll_interval=0.1,
            approval_notifier=lambda action_id, tool, params, reason="", observation="": notifications.append(
                (action_id, tool, params, reason, observation)),
        )

        aid = ledger.propose("approval_action", {"speed": 42}, "test reason", "test_group",
                             requires_approval=True, observation="test obs")
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "WAITING_APPROVAL"
        assert len(notifications) == 1
        assert notifications[0][0] == aid
        assert notifications[0][1] == "approval_action"
        assert notifications[0][2] == {"speed": 42}
        assert notifications[0][3] == "test reason"
        assert notifications[0][4] == "test obs"

        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "WAITING_APPROVAL"
        assert len(notifications) == 1  # not sent again

    def test_waiting_approval_survives_notifier_failure(self, wb, ledger, registry, tmp_path):
        engine = Engine(
            whiteboard=wb,
            ledger=ledger,
            tools=registry,
            data_dir=str(tmp_path),
            poll_interval=0.1,
            approval_notifier=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("telegram down")),
        )

        aid = ledger.propose("approval_action", {}, "test", "test_group", requires_approval=True)
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "WAITING_APPROVAL"
        assert ledger.get_action(aid)["status"] == "WAITING_APPROVAL"
        assert registry._call_counts["approval_action"] == 0


class TestGate5Dispatch:
    def test_successful_dispatch(self, engine, ledger, tmp_path):
        aid = ledger.propose("simple_action", {}, "test", "test_group")
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "DONE"
        action = ledger.get_action(aid)
        assert action["status"] == "DONE"
        assert "success" in action["result_json"]

    def test_failed_dispatch(self, engine, ledger, registry):
        aid = ledger.propose("failing_action", {}, "test", "test_group")
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "FAILED"
        assert ledger.get_action(aid)["status"] == "FAILED"
        assert registry._call_counts["failing_action"] == 1

    def test_diary_records_success(self, engine, ledger, tmp_path):
        aid = ledger.propose("simple_action", {}, "test", "test_group")
        engine.process_proposal(ledger.get_action(aid))
        diary = engine._get_diary("test_group")
        action = ledger.get_action(aid)
        status = diary.lookup(action["idempotency_key"])
        assert status == "SUCCESS"


class TestChainExecution:
    def test_chain_second_step_waits_for_first(self, engine, ledger, registry):
        """Second step in chain should SKIP while first is still PROPOSED."""
        aid1 = ledger.propose("simple_action", {}, "step 1", "test_group",
                              chain_id="chain-1", chain_seq=0)
        aid2 = ledger.propose("simple_action", {"x": 1}, "step 2", "test_group",
                              chain_id="chain-1", chain_seq=1)
        # Process step 2 first — should skip
        result = engine.process_proposal(ledger.get_action(aid2))
        assert result == "SKIPPED"

    def test_chain_second_step_proceeds_after_first_done(self, engine, ledger, registry):
        aid1 = ledger.propose("simple_action", {}, "step 1", "test_group",
                              chain_id="chain-2", chain_seq=0)
        aid2 = ledger.propose("simple_action", {"x": 1}, "step 2", "test_group",
                              chain_id="chain-2", chain_seq=1)
        # Process step 1 — DONE
        engine.process_proposal(ledger.get_action(aid1))
        assert ledger.get_action(aid1)["status"] == "DONE"
        # Now step 2 should proceed
        result = engine.process_proposal(ledger.get_action(aid2))
        assert result == "DONE"

    def test_chain_second_step_rejected_on_predecessor_failure(self, engine, ledger, registry):
        aid1 = ledger.propose("failing_action", {}, "step 1", "test_group",
                              chain_id="chain-3", chain_seq=0)
        aid2 = ledger.propose("simple_action", {"x": 1}, "step 2", "test_group",
                              chain_id="chain-3", chain_seq=1)
        # Process step 1 — FAILED
        engine.process_proposal(ledger.get_action(aid1))
        assert ledger.get_action(aid1)["status"] == "FAILED"
        # Step 2 should be rejected
        result = engine.process_proposal(ledger.get_action(aid2))
        assert result == "REJECTED"
        assert "chain_predecessor_failed" in ledger.get_action(aid2)["error_json"]

    def test_first_step_no_chain_check(self, engine, ledger, registry):
        """First step (seq=0) should not check for predecessors."""
        aid = ledger.propose("simple_action", {}, "step 0", "test_group",
                             chain_id="chain-4", chain_seq=0)
        result = engine.process_proposal(ledger.get_action(aid))
        assert result == "DONE"


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
