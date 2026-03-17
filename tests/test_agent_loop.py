"""Tests for agent loop."""

import json
import time
import threading
import fakeredis
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

from wallee.agent.loop import AgentLoop
from wallee.agent.llm_client import LLMClient
from wallee.whiteboard.client import Whiteboard
from wallee.tools.registry import ToolRegistry
from wallee.tools.decorator import tool


@pytest.fixture
def wb():
    r = fakeredis.FakeRedis(decode_responses=True)
    return Whiteboard(_redis=r)


@pytest.fixture
def mock_llm():
    llm = MagicMock(spec=LLMClient)
    llm.call.return_value = json.dumps({"type": "WAIT", "reason": "all nominal", "check_after_s": 5})
    return llm


@pytest.fixture
def registry():
    reg = ToolRegistry()

    @tool(kind="actuator", requires_approval=False)
    def test_action(whiteboard=None):
        """A test actuator."""
        return {"status": "success"}

    reg.register("test_action", test_action, test_action._tool_meta, "test")
    return reg


@pytest.fixture
def knowledge_dir(tmp_path):
    (tmp_path / "SOUL.md").write_text("You are Wallee.")
    (tmp_path / "HARDWARE.md").write_text("Pi 5, no sensors yet.")
    return tmp_path


@pytest.fixture
def agent(wb, mock_llm, registry, knowledge_dir):
    return AgentLoop(
        whiteboard=wb,
        llm=mock_llm,
        tools=registry,
        knowledge_dir=knowledge_dir,
        poll_interval=0.1,
        heartbeat_interval=0.1,
        heartbeat_ttl=3,
    )


class TestRunOnce:
    def test_calls_llm_with_prompt(self, agent, mock_llm):
        agent.run_once()
        mock_llm.call.assert_called_once()
        prompt = mock_llm.call.call_args[0][0]
        assert "SOUL.md" in prompt
        assert "WHITEBOARD STATE" in prompt
        assert "INSTRUCTIONS" in prompt

    def test_returns_raw_response(self, agent, mock_llm):
        result = agent.run_once()
        assert "WAIT" in result

    def test_includes_whiteboard_state_in_prompt(self, agent, wb, mock_llm):
        wb.publish("env.temperature", 21.5)
        agent.run_once()
        prompt = mock_llm.call.call_args[0][0]
        assert "21.5" in prompt

    def test_includes_human_intent(self, agent, wb, mock_llm):
        wb.publish("human.intent", "check the printer")
        agent.run_once()
        prompt = mock_llm.call.call_args[0][0]
        assert "check the printer" in prompt

    def test_handles_action_decision(self, agent, mock_llm):
        mock_llm.call.return_value = json.dumps({
            "type": "ACTION", "tool": "test_action", "params": {}, "reason": "testing"
        })
        agent.run_once()
        # No crash, action logged (no ledger in Phase 1)

    def test_handles_unknown_tool(self, agent, mock_llm):
        mock_llm.call.return_value = json.dumps({
            "type": "ACTION", "tool": "nonexistent", "params": {}, "reason": "?"
        })
        # Should not crash
        agent.run_once()

    def test_handles_call_human(self, agent, mock_llm):
        mock_llm.call.return_value = json.dumps({
            "type": "CALL_HUMAN", "message": "help!", "severity": "warning"
        })
        agent.run_once()

    def test_handles_empty_llm_response(self, agent, mock_llm):
        mock_llm.call.return_value = ""
        # Parser defaults to WAIT, no crash
        agent.run_once()


class TestHeartbeat:
    def test_heartbeat_publishes(self, agent, wb):
        agent._running = True
        # Run heartbeat in thread briefly
        t = threading.Thread(target=agent._heartbeat, daemon=True)
        t.start()
        time.sleep(0.3)
        agent._running = False
        t.join(timeout=1)

        hb = wb.read("agent.heartbeat")
        assert hb is not None
        assert isinstance(hb, float)


class TestRunLoop:
    def test_loop_runs_and_stops(self, agent, mock_llm):
        # Run loop in background thread
        t = threading.Thread(target=agent.run, daemon=True)
        t.start()
        time.sleep(0.5)
        agent.stop()
        t.join(timeout=2)

        # LLM should have been called at least once
        assert mock_llm.call.call_count >= 1

    def test_loop_survives_llm_error(self, agent, mock_llm):
        call_count = 0

        def flaky_call(prompt, messages=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("network blip")
            return json.dumps({"type": "WAIT", "reason": "ok"})

        mock_llm.call.side_effect = flaky_call

        t = threading.Thread(target=agent.run, daemon=True)
        t.start()
        time.sleep(0.5)
        agent.stop()
        t.join(timeout=2)

        # Should have recovered and called more than once
        assert call_count >= 2


class TestKnowledge:
    def test_loads_knowledge_files(self, agent):
        k = agent._load_knowledge()
        assert "Wallee" in k["SOUL.md"]
        assert "Pi 5" in k["HARDWARE.md"]

    def test_missing_knowledge_file(self, wb, mock_llm, registry, tmp_path):
        # Only SOUL.md exists
        (tmp_path / "SOUL.md").write_text("Mission briefing.")
        agent = AgentLoop(
            whiteboard=wb, llm=mock_llm, tools=registry,
            knowledge_dir=tmp_path,
        )
        k = agent._load_knowledge()
        assert k["SOUL.md"] == "Mission briefing."
        assert k["HARDWARE.md"] == ""
        assert k["LEARNED.md"] == ""
