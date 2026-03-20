"""Tests for rejection classification and cooldown logic.

The cooldown guard must only activate for genuine human rejections (via Telegram
approve/reject), NOT for engine rejections (TOCTOU, expired, precheck failures).

Root cause of the bug: the `reason` column stores the LLM's proposal reasoning,
and `error_json` stores the engine's rejection reason. The old code used
`reason or error_json` which short-circuited on the proposal text.
"""

import json
import time
import pytest
from unittest.mock import MagicMock

from wallee.agent.loop import AgentLoop


@pytest.fixture
def agent():
    """Create a minimal AgentLoop for testing rejection classification."""
    wb = MagicMock()
    wb.read.return_value = None
    wb.publish = MagicMock()
    wb.r = MagicMock()
    llm = MagicMock()
    tools = MagicMock()
    tools.get_state_effects_map.return_value = {}
    from pathlib import Path
    return AgentLoop(
        whiteboard=wb,
        llm=llm,
        tools=tools,
        knowledge_dir=Path("/tmp"),
    )


class TestIsHumanRejection:
    """Test the _is_human_rejection() classification directly."""

    ENGINE_REASONS = [
        "TOCTOU: percent is required",
        "TOCTOU: Cannot resume: job is PRINTING, not PAUSED",
        "TOCTOU: Cannot pause: job is PAUSED, not PRINTING",
        "TOCTOU: target temperature is required",
        "expired (45123ms > 30000ms)",
        "chain_skipped: predecessor abc123 was FAILED",
        "unknown tool: nonexistent_tool",
        "TOCTOU: Speed factor 5% outside bounds (10-200)",
        "TOCTOU: Cannot move: printer state is PRINTING, must be IDLE",
        "TOCTOU exception: connection timeout",
        "not approved by operator",  # This IS from approval path but is engine-mediated
    ]

    @pytest.mark.parametrize("reason", ENGINE_REASONS)
    def test_engine_rejection_classified_correctly(self, agent, reason):
        """Engine rejections must NOT trigger human-rejection cooldown."""
        assert not agent._is_human_rejection(reason), \
            f"'{reason}' was misclassified as human rejection"

    HUMAN_REASONS = [
        "temperature looks high, reducing speed",
        "nozzle temp rising, adjusting flow",
        "print quality degrading at current settings",
    ]

    @pytest.mark.parametrize("reason", HUMAN_REASONS)
    def test_proposal_reasoning_classified_as_human(self, agent, reason):
        """LLM proposal reasoning (with no engine keywords) is classified as human."""
        # This is technically correct — if only the proposal reasoning reaches
        # the classifier, it looks like a human rejection. The REAL fix is to
        # ensure the rejection reason (from error_json) reaches the classifier,
        # not the proposal reasoning.
        assert agent._is_human_rejection(reason), \
            f"'{reason}' should be classified as human (no engine patterns)"


class TestScanEpisodeReasonExtraction:
    """Test that _scan_episode_for_rejections extracts the REJECTION reason
    from error_json, not the PROPOSAL reason from the reason column.

    This is the actual bug: the reason column holds the LLM's proposal text,
    and error_json holds the engine's rejection reason. The old code used
    `reason or error_json` which short-circuited on the proposal text.
    """

    def test_toctou_rejection_with_proposal_reason_does_not_trigger_cooldown(self, agent):
        """CRITICAL BUG TEST: TOCTOU rejection must not trigger cooldown even when
        the reason column contains the LLM's proposal reasoning."""
        episode = [{
            "status": "REJECTED",
            "action_id": "test-action-001",
            "tool": "set_speed_factor",
            "reason": "temperature looks high, reducing speed",  # LLM's proposal reasoning
            "error_json": json.dumps({"reason": "TOCTOU: percent is required"}),  # Engine rejection
        }]

        agent._scan_episode_for_rejections(episode)

        # Cooldown should NOT have been published (engine rejection, not human)
        agent.wb.publish.assert_not_called()

    def test_toctou_resume_rejection_does_not_trigger_cooldown(self, agent):
        """TOCTOU rejection for resume_print must not trigger cooldown."""
        episode = [{
            "status": "REJECTED",
            "action_id": "test-action-002",
            "tool": "resume_print",
            "reason": "print paused, attempting resume",
            "error_json": json.dumps({"reason": "TOCTOU: Cannot resume: job is PRINTING, not PAUSED"}),
        }]

        agent._scan_episode_for_rejections(episode)
        agent.wb.publish.assert_not_called()

    def test_expired_rejection_does_not_trigger_cooldown(self, agent):
        """Expired proposal must not trigger cooldown."""
        episode = [{
            "status": "REJECTED",
            "action_id": "test-action-003",
            "tool": "set_temperature",
            "reason": "adjusting nozzle temp for quality",
            "error_json": json.dumps({"reason": "expired (45123ms > 30000ms)"}),
        }]

        agent._scan_episode_for_rejections(episode)
        agent.wb.publish.assert_not_called()

    def test_genuine_human_rejection_triggers_cooldown(self, agent):
        """A real human rejection (no engine patterns in error_json) SHOULD trigger cooldown."""
        episode = [{
            "status": "REJECTED",
            "action_id": "test-action-004",
            "tool": "cancel_print",
            "reason": "print looks failed, cancelling",
            "error_json": json.dumps({"reason": "Human rejected: cancel_print"}),
        }]

        agent._scan_episode_for_rejections(episode)

        # Cooldown SHOULD have been published (genuine human rejection)
        agent.wb.publish.assert_called_once()
        call_args = agent.wb.publish.call_args
        assert call_args[0][0] == "agent.cooldown"

    def test_empty_error_json_falls_back_to_reason(self, agent):
        """If error_json is empty, fall back to reason column."""
        episode = [{
            "status": "REJECTED",
            "action_id": "test-action-005",
            "tool": "pause_print",
            "reason": "TOCTOU: Cannot pause during PREPARING",
            "error_json": "",
        }]

        agent._scan_episode_for_rejections(episode)
        # TOCTOU in reason → engine rejection → no cooldown
        agent.wb.publish.assert_not_called()
