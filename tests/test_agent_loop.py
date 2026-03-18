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
from wallee.ledger.db import Ledger


@pytest.fixture
def wb():
    r = fakeredis.FakeRedis(decode_responses=True)
    return Whiteboard(_redis=r)


@pytest.fixture
def mock_llm():
    llm = MagicMock(spec=LLMClient)
    llm.call.return_value = json.dumps({
        "type": "WAIT", "observation": "all clear", "reasoning": "nothing to do",
        "check_after_s": 5,
    })
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
def ledger(tmp_path):
    db = Ledger(tmp_path / "agent_loop.db")
    yield db
    db.close()


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


def _get_messages(mock_llm):
    """Extract the messages kwarg from the last llm.call()."""
    return mock_llm.call.call_args.kwargs.get("messages", [])


def _get_system_text(mock_llm):
    """Extract system message text from last call."""
    msgs = _get_messages(mock_llm)
    for m in msgs:
        if m.get("role") == "system":
            c = m.get("content", "")
            return c if isinstance(c, str) else ""
    return ""


def _get_user_text(mock_llm):
    """Extract user message text from last call."""
    msgs = _get_messages(mock_llm)
    for m in msgs:
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, str):
                return c
            # Vision content blocks — find text blocks
            texts = [b["text"] for b in c if b.get("type") == "text"]
            return "\n".join(texts)
    return ""


class TestRunOnce:
    def test_calls_llm_with_prompt(self, agent, mock_llm):
        agent.run_once()
        mock_llm.call.assert_called_once()
        system = _get_system_text(mock_llm)
        user = _get_user_text(mock_llm)
        assert "SOUL.md" in system
        assert "WHITEBOARD STATE" in user
        assert "PHASE:" in user

    def test_returns_raw_response(self, agent, mock_llm):
        result = agent.run_once()
        assert "WAIT" in result

    def test_includes_whiteboard_state_in_prompt(self, agent, wb, mock_llm):
        wb.publish("env.temperature", 21.5)
        agent.run_once()
        user = _get_user_text(mock_llm)
        assert "21.5" in user

    def test_includes_human_intent(self, agent, wb, mock_llm):
        wb.publish("human.intent", "check the printer")
        agent.run_once()
        user = _get_user_text(mock_llm)
        assert "check the printer" in user

    def test_handles_action_decision(self, agent, mock_llm):
        mock_llm.call.return_value = json.dumps({
            "type": "ACTION", "tool": "test_action", "params": {},
            "observation": "test", "reasoning": "testing",
        })
        agent.run_once()

    def test_handles_unknown_tool(self, agent, mock_llm):
        mock_llm.call.return_value = json.dumps({
            "type": "ACTION", "tool": "nonexistent", "params": {},
            "observation": "?", "reasoning": "?",
        })
        agent.run_once()

    def test_handles_call_human(self, agent, mock_llm):
        mock_llm.call.return_value = json.dumps({
            "type": "CALL_HUMAN", "message": "help!",
            "severity": "warning", "observation": "issue", "reasoning": "need help",
        })
        agent.run_once()

    def test_call_human_records_episode_boundary(self, wb, mock_llm, registry, knowledge_dir, ledger):
        agent = AgentLoop(
            whiteboard=wb,
            llm=mock_llm,
            tools=registry,
            knowledge_dir=knowledge_dir,
            ledger=ledger,
        )
        mock_llm.call.return_value = json.dumps({
            "type": "CALL_HUMAN", "message": "need operator",
            "severity": "warning", "observation": "issue", "reasoning": "escalating",
        })

        agent.run_once()

        event = ledger.conn.execute(
            "SELECT message, details_json FROM events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        assert event["message"] == "CALL_HUMAN"
        assert event["details_json"] == "need operator"

    def test_handles_empty_llm_response(self, agent, mock_llm):
        mock_llm.call.return_value = ""
        agent.run_once()

    def test_wait_updates_next_cycle_delay(self, agent, mock_llm):
        mock_llm.call.return_value = json.dumps({
            "type": "WAIT", "observation": "ok", "reasoning": "watching",
            "check_after_s": 45,
        })

        agent.run_once()

        assert agent._next_cycle_delay_s == 45

    def test_action_resets_next_cycle_delay_to_poll_interval(self, agent, mock_llm):
        agent._next_cycle_delay_s = 90
        mock_llm.call.return_value = json.dumps({
            "type": "ACTION", "tool": "test_action", "params": {},
            "observation": "test", "reasoning": "testing",
        })

        agent.run_once()

        assert agent._next_cycle_delay_s == agent.poll_interval


class TestCallHumanDedup:
    def test_suppresses_duplicate_callout(self, agent, wb, mock_llm):
        """Second CALL_HUMAN with same message should be suppressed."""
        msg = json.dumps({
            "type": "CALL_HUMAN", "message": "nozzle blob detected",
            "severity": "warning", "observation": "blob", "reasoning": "escalating",
        })
        mock_llm.call.return_value = msg

        agent.run_once()  # First call — should publish
        pending = wb.read("human.pending_callout")
        assert pending is not None

        agent.run_once()  # Second call — should be suppressed
        decision_text = wb.read("agent.last_decision")
        assert "suppressed" in decision_text

    def test_allows_different_callout(self, agent, wb, mock_llm):
        """Different CALL_HUMAN message should NOT be suppressed."""
        mock_llm.call.return_value = json.dumps({
            "type": "CALL_HUMAN", "message": "nozzle blob detected",
            "severity": "warning", "observation": "blob", "reasoning": "escalating",
        })
        agent.run_once()

        mock_llm.call.return_value = json.dumps({
            "type": "CALL_HUMAN", "message": "filament runout detected",
            "severity": "critical", "observation": "runout", "reasoning": "no filament",
        })
        agent.run_once()
        decision_text = wb.read("agent.last_decision")
        assert "filament runout" in decision_text


class TestWake:
    def test_wake_interrupts_sleep(self, agent, mock_llm):
        """wake() should interrupt the sleep between cycles."""
        mock_llm.call.return_value = json.dumps({
            "type": "WAIT", "observation": "ok", "reasoning": "idle",
            "check_after_s": 300,
        })

        t = threading.Thread(target=agent.run, daemon=True)
        t.start()
        time.sleep(0.3)

        # Agent should be sleeping for 300s. Wake it.
        agent.wake()
        time.sleep(0.5)

        # Should have run at least 2 cycles (initial + after wake)
        assert mock_llm.call.call_count >= 2

        agent.stop()
        t.join(timeout=2)


class TestHeartbeat:
    def test_heartbeat_publishes(self, agent, wb):
        agent._running = True
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
        t = threading.Thread(target=agent.run, daemon=True)
        t.start()
        time.sleep(0.5)
        agent.stop()
        t.join(timeout=2)

        assert mock_llm.call.call_count >= 1

    def test_loop_survives_llm_error(self, agent, mock_llm):
        call_count = 0

        def flaky_call(prompt, messages=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("network blip")
            return json.dumps({"type": "WAIT", "observation": "ok", "reasoning": "ok"})

        mock_llm.call.side_effect = flaky_call

        t = threading.Thread(target=agent.run, daemon=True)
        t.start()
        time.sleep(0.5)
        agent.stop()
        t.join(timeout=2)

        assert call_count >= 2

    def test_stop_interrupts_long_wait_sleep(self, agent, mock_llm):
        mock_llm.call.return_value = json.dumps({
            "type": "WAIT", "observation": "idle", "reasoning": "idle",
            "check_after_s": 120,
        })

        t = threading.Thread(target=agent.run, daemon=True)
        t.start()
        time.sleep(0.2)
        agent.stop()
        t.join(timeout=1)

        assert not t.is_alive()


class TestKnowledge:
    def test_loads_knowledge_files(self, agent):
        k = agent._load_knowledge()
        assert "Wallee" in k["SOUL.md"]
        assert "Pi 5" in k["HARDWARE.md"]

    def test_missing_knowledge_file(self, wb, mock_llm, registry, tmp_path):
        (tmp_path / "SOUL.md").write_text("Mission briefing.")
        agent = AgentLoop(
            whiteboard=wb, llm=mock_llm, tools=registry,
            knowledge_dir=tmp_path,
        )
        k = agent._load_knowledge()
        assert k["SOUL.md"] == "Mission briefing."
        assert k["HARDWARE.md"] == ""
        assert k["LEARNED.md"] == ""
