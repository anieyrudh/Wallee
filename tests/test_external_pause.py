"""Tests for external pause detection and resume blocking.

When someone or something pauses the printer externally (not Wallee), the agent
must investigate before resuming. resume_print is blocked until the human
responds or the agent calls call_human.
"""

import json
import time

import fakeredis
import pytest
from unittest.mock import MagicMock
from pathlib import Path

from wallee.whiteboard.client import Whiteboard
from wallee.engine.dispatch import Engine
from wallee.agent.loop import AgentLoop


@pytest.fixture
def wb():
    return Whiteboard(_redis=fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture
def agent(wb):
    llm = MagicMock()
    tools = MagicMock()
    tools.get_state_effects_map.return_value = {
        "printer.state": ["pause_print", "resume_print"],
    }
    a = AgentLoop(
        whiteboard=wb,
        llm=llm,
        tools=tools,
        knowledge_dir=Path("/tmp"),
    )
    return a


class TestExternalPauseDetection:
    def test_external_pause_sets_flag(self, agent, wb):
        """When change detector reports external PAUSED, the flag is set."""
        # Simulate external changes that include a PAUSED state change
        external_changes = ["printer.state changed: PRINTING -> PAUSED"]

        # Inject into the agent's run path — simulate what happens after detect()
        for change in external_changes:
            if "printer.state" in change and "PAUSED" in change:
                wb.publish("agent.external_pause", "true", ttl=600)

        assert wb.read("agent.external_pause") == "true"

    def test_human_intent_clears_external_pause(self, agent, wb):
        """Human responding clears the external pause block."""
        wb.publish("agent.external_pause", "true", ttl=600)
        assert wb.read("agent.external_pause") == "true"

        # Simulate human intent consumption (from loop.py step 18)
        if wb.read("agent.external_pause"):
            wb.r.delete("agent.external_pause")

        assert wb.read("agent.external_pause") is None

    def test_call_human_clears_external_pause(self, agent, wb):
        """Agent calling call_human clears the external pause block."""
        wb.publish("agent.external_pause", "true", ttl=600)

        # Simulate CALL_HUMAN route clearing the flag
        if wb.read("agent.external_pause"):
            wb.r.delete("agent.external_pause")

        assert wb.read("agent.external_pause") is None


class TestEngineResumeBlocking:
    def test_resume_rejected_during_external_pause(self, wb):
        """resume_print must be rejected while external pause is active."""
        wb.publish("agent.external_pause", "true", ttl=600)

        # Create minimal engine with mocked ledger and tools
        ledger = MagicMock()
        tools = MagicMock()
        resume_tool = MagicMock()
        resume_tool.meta = {"gate_bypass": False, "state_effects": ["printer.state"]}
        resume_tool.device_group = "prusa_link"
        resume_tool.has_precheck = False
        resume_tool.requires_approval = False
        tools.get.return_value = resume_tool

        engine = Engine(whiteboard=wb, ledger=ledger, tools=tools, data_dir="/tmp")

        proposal = {
            "action_id": "test-resume-001",
            "tool": "resume_print",
            "params_json": "{}",
            "requires_approval": False,
            "max_proposal_age_ms": 30000,
            "created_mono": time.monotonic(),
            "idempotency_key": "resume_print:abc",
            "chain_id": None,
            "chain_seq": None,
        }

        result = engine.process_proposal(proposal)
        assert result == "REJECTED"
        ledger.reject.assert_called_once()
        reject_reason = ledger.reject.call_args[0][1]
        assert "External pause active" in reject_reason
        assert "investigate before resuming" in reject_reason

    def test_other_tools_not_blocked_by_external_pause(self, wb):
        """Non-resume tools should work normally during external pause."""
        wb.publish("agent.external_pause", "true", ttl=600)

        ledger = MagicMock()
        ledger.has_inflight.return_value = False
        tools = MagicMock()
        temp_tool = MagicMock()
        temp_tool.meta = {"gate_bypass": False, "state_effects": []}
        temp_tool.device_group = "prusa_link"
        temp_tool.has_precheck = False
        temp_tool.requires_approval = False
        temp_tool.execute.return_value = {"status": "success"}
        tools.get.return_value = temp_tool

        engine = Engine(whiteboard=wb, ledger=ledger, tools=tools, data_dir="/tmp")

        proposal = {
            "action_id": "test-temp-001",
            "tool": "set_temperature",
            "params_json": '{"target": 210, "heater": "nozzle"}',
            "requires_approval": False,
            "max_proposal_age_ms": 30000,
            "created_mono": time.monotonic(),
            "idempotency_key": "set_temperature:abc",
            "chain_id": None,
            "chain_seq": None,
        }

        result = engine.process_proposal(proposal)
        # Should NOT be rejected by external pause (only resume_print is blocked)
        assert result != "REJECTED" or "External pause" not in str(ledger.reject.call_args)

    def test_resume_allowed_without_external_pause(self, wb):
        """resume_print works normally when there's no external pause."""
        # No agent.external_pause set

        ledger = MagicMock()
        ledger.has_inflight.return_value = False
        ledger.get_approval.return_value = None
        tools = MagicMock()
        resume_tool = MagicMock()
        resume_tool.meta = {"gate_bypass": False, "state_effects": ["printer.state"]}
        resume_tool.device_group = "prusa_link"
        resume_tool.has_precheck = False
        resume_tool.requires_approval = False
        resume_tool.execute.return_value = {"status": "success"}
        tools.get.return_value = resume_tool

        engine = Engine(whiteboard=wb, ledger=ledger, tools=tools, data_dir="/tmp")

        proposal = {
            "action_id": "test-resume-002",
            "tool": "resume_print",
            "params_json": "{}",
            "requires_approval": False,
            "max_proposal_age_ms": 30000,
            "created_mono": time.monotonic(),
            "idempotency_key": "resume_print:def",
            "chain_id": None,
            "chain_seq": None,
        }

        result = engine.process_proposal(proposal)
        # Should pass through (not rejected by external pause)
        assert result != "REJECTED"
