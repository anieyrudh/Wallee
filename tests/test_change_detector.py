"""Tests for external change detector."""

import pytest
from wallee.agent.change_detector import ExternalChangeDetector


@pytest.fixture
def detector():
    return ExternalChangeDetector()


class TestBasicDetection:
    def test_first_call_returns_empty(self, detector):
        """First call just stores the snapshot, no changes to report."""
        changes = detector.detect({"printer.state": "IDLE"}, [])
        assert changes == []

    def test_no_change(self, detector):
        detector.detect({"printer.state": "IDLE"}, [])
        changes = detector.detect({"printer.state": "IDLE"}, [])
        assert changes == []

    def test_detects_state_change(self, detector):
        detector.detect({"printer.state": "IDLE"}, [])
        changes = detector.detect({"printer.state": "PRINTING"}, [])
        assert len(changes) == 1
        assert "printer.state" in changes[0]
        assert "IDLE" in changes[0]
        assert "PRINTING" in changes[0]

    def test_detects_target_temp_change(self, detector):
        detector.detect({"printer.target_nozzle": 0}, [])
        changes = detector.detect({"printer.target_nozzle": 215}, [])
        assert len(changes) == 1
        assert "printer.target_nozzle" in changes[0]

    def test_detects_speed_change(self, detector):
        detector.detect({"printer.speed": 100}, [])
        changes = detector.detect({"printer.speed": 150}, [])
        assert any("printer.speed" in c for c in changes)


class TestWalleeCausedChanges:
    def test_ignores_wallee_temp_change(self, detector):
        """If Wallee's set_temperature is in the episode, don't flag temp changes."""
        detector.detect({"printer.target_nozzle": 0}, [])
        episode = [{"tool": "set_temperature", "status": "DONE"}]
        changes = detector.detect({"printer.target_nozzle": 215}, episode)
        assert not any("printer.target_nozzle" in c for c in changes)

    def test_ignores_wallee_speed_change(self, detector):
        detector.detect({"printer.speed": 100}, [])
        episode = [{"tool": "set_speed_factor", "status": "DONE"}]
        changes = detector.detect({"printer.speed": 150}, episode)
        assert not any("printer.speed" in c for c in changes)

    def test_ignores_wallee_flow_change(self, detector):
        detector.detect({"printer.flow": 100}, [])
        episode = [{"tool": "set_flow_factor", "status": "DONE"}]
        changes = detector.detect({"printer.flow": 120}, episode)
        assert not any("printer.flow" in c for c in changes)

    def test_state_change_filtered_when_agent_caused(self, detector):
        """printer.state change caused by start_print should NOT be flagged as external."""
        detector.detect({"printer.state": "IDLE"}, [])
        episode = [{"tool": "start_print", "status": "DONE"}]
        changes = detector.detect({"printer.state": "PRINTING"}, episode)
        assert not any("printer.state" in c for c in changes)

    def test_state_change_flagged_when_no_matching_action(self, detector):
        """printer.state change with no matching episode action IS external."""
        detector.detect({"printer.state": "PRINTING"}, [])
        changes = detector.detect({"printer.state": "PAUSED"}, [])
        assert any("printer.state" in c for c in changes)

    def test_failed_action_does_not_mask_external_change(self, detector):
        detector.detect({"printer.target_nozzle": 0}, [])
        episode = [{"tool": "set_temperature", "status": "FAILED"}]
        changes = detector.detect({"printer.target_nozzle": 215}, episode)
        assert any("printer.target_nozzle" in c for c in changes)


class TestUnpublishedMetrics:
    def test_unpublished_gcode_and_cmdcnt_keys_do_not_create_changes(self, detector):
        detector.detect({"printer.state": "IDLE"}, [])
        changes = detector.detect({"printer.state": "IDLE", "printer.last_gcode": "G28", "printer.cmdcnt": 120}, [])
        assert changes == []


class TestFormatForPrompt:

    def test_empty_changes(self, detector):
        assert detector.format_for_prompt([]) == ""

    def test_formatted_output(self, detector):
        result = detector.format_for_prompt(["printer.state changed: IDLE -> PRINTING"])
        assert "EXTERNAL CHANGES DETECTED" in result
        assert "IDLE -> PRINTING" in result
        assert "not caused by Wallee" in result


class TestPromptIntegration:
    def test_external_changes_in_prompt(self):
        from wallee.agent.prompt import build_prompt
        prompt = build_prompt(
            state={"printer.state": "PRINTING"},
            episode=[],
            intent=None,
            knowledge={},
            tools=[],
            current_time=1710000000,
            external_changes=["printer.state changed: IDLE -> PRINTING"],
        )
        assert "EXTERNAL CHANGES" in prompt
        # Should be before WHITEBOARD in the user message portion
        ext_pos = prompt.index("EXTERNAL")
        wb_pos = prompt.index("WHITEBOARD")
        assert ext_pos < wb_pos
