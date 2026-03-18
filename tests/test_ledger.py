"""Tests for ledger SQLite operations."""

import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from wallee.ledger.db import Ledger


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "test_ledger.db")
    yield db
    db.close()


class TestPropose:
    def test_creates_proposed_action(self, ledger):
        aid = ledger.propose("resume_print", {"speed": 100}, "temps ok", "prusa_link")
        assert aid  # non-empty UUID

        action = ledger.get_action(aid)
        assert action["tool"] == "resume_print"
        assert action["status"] == "PROPOSED"
        assert action["device_group"] == "prusa_link"
        assert action["reason"] == "temps ok"

    def test_idempotency_rejects_duplicate(self, ledger):
        aid1 = ledger.propose("resume_print", {"speed": 100}, "reason1", "prusa")
        aid2 = ledger.propose("resume_print", {"speed": 100}, "reason2", "prusa")
        assert aid1 != ""
        assert aid2 == ""  # duplicate

    def test_reproposal_after_terminal_state_preserves_history(self, ledger):
        aid1 = ledger.propose("resume_print", {"speed": 100}, "reason1", "prusa")
        ledger.set_status(aid1, "DONE", result={"status": "ok"})

        aid2 = ledger.propose("resume_print", {"speed": 100}, "reason2", "prusa")

        assert aid2 != ""
        action1 = ledger.get_action(aid1)
        action2 = ledger.get_action(aid2)
        assert action1["idempotency_key"] != action2["idempotency_key"]
        count = ledger.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
        assert count == 2

    def test_different_params_are_not_duplicates(self, ledger):
        aid1 = ledger.propose("set_temp", {"target": 200}, "heat up", "prusa")
        aid2 = ledger.propose("set_temp", {"target": 210}, "heat more", "prusa")
        assert aid1 != ""
        assert aid2 != ""

    def test_stores_approval_requirement(self, ledger):
        aid = ledger.propose("resume_print", {}, "test", "prusa", requires_approval=True)
        action = ledger.get_action(aid)
        assert action["requires_approval"] == 1

    def test_stores_max_proposal_age(self, ledger):
        aid = ledger.propose("slow_op", {}, "test", "prusa", max_proposal_age_ms=60000)
        action = ledger.get_action(aid)
        assert action["max_proposal_age_ms"] == 60000

    def test_stores_observation(self, ledger):
        aid = ledger.propose("tool_a", {}, "reason", "grp", observation="nozzle looks clean")
        action = ledger.get_action(aid)
        assert action["observation"] == "nozzle looks clean"

    def test_observation_defaults_to_empty(self, ledger):
        aid = ledger.propose("tool_a", {}, "reason", "grp")
        action = ledger.get_action(aid)
        assert action["observation"] == ""

    def test_stores_chain_id_and_seq(self, ledger):
        aid = ledger.propose("tool_a", {}, "r", "grp", chain_id="chain-1", chain_seq=0)
        action = ledger.get_action(aid)
        assert action["chain_id"] == "chain-1"
        assert action["chain_seq"] == 0



class TestGetProposals:
    def test_returns_proposed_only(self, ledger):
        aid = ledger.propose("tool_a", {}, "r", "grp")
        ledger.set_status(aid, "DISPATCHED")
        ledger.propose("tool_b", {}, "r", "grp")

        proposals = ledger.get_proposals()
        assert len(proposals) == 1
        assert proposals[0]["tool"] == "tool_b"

    def test_ordered_by_creation_time(self, ledger):
        ledger.propose("first", {}, "r", "grp")
        ledger.propose("second", {"x": 1}, "r", "grp")
        proposals = ledger.get_proposals()
        assert proposals[0]["tool"] == "first"
        assert proposals[1]["tool"] == "second"


class TestStatusTransitions:
    def test_proposed_to_dispatched(self, ledger):
        aid = ledger.propose("tool_a", {}, "r", "grp")
        ledger.set_status(aid, "DISPATCHED")
        action = ledger.get_action(aid)
        assert action["status"] == "DISPATCHED"

    def test_dispatched_to_done_with_result(self, ledger):
        aid = ledger.propose("tool_a", {}, "r", "grp")
        ledger.set_status(aid, "DISPATCHED")
        ledger.set_status(aid, "DONE", result={"status": "success"})
        action = ledger.get_action(aid)
        assert action["status"] == "DONE"
        assert "success" in action["result_json"]

    def test_dispatched_to_failed_with_error(self, ledger):
        aid = ledger.propose("tool_a", {}, "r", "grp")
        ledger.set_status(aid, "DISPATCHED")
        ledger.set_status(aid, "FAILED", error={"error": "timeout"})
        action = ledger.get_action(aid)
        assert action["status"] == "FAILED"
        assert "timeout" in action["error_json"]

    def test_reject_action(self, ledger):
        aid = ledger.propose("tool_a", {}, "r", "grp")
        ledger.reject(aid, "expired")
        action = ledger.get_action(aid)
        assert action["status"] == "REJECTED"
        assert "expired" in action["error_json"]


class TestHasInflight:
    def test_no_inflight(self, ledger):
        assert ledger.has_inflight("prusa") is False

    def test_has_inflight(self, ledger):
        aid = ledger.propose("tool_a", {}, "r", "prusa")
        ledger.set_status(aid, "DISPATCHED")
        assert ledger.has_inflight("prusa") is True

    def test_done_is_not_inflight(self, ledger):
        aid = ledger.propose("tool_a", {}, "r", "prusa")
        ledger.set_status(aid, "DISPATCHED")
        ledger.set_status(aid, "DONE", result={})
        assert ledger.has_inflight("prusa") is False

    def test_different_device_group(self, ledger):
        aid = ledger.propose("tool_a", {}, "r", "prusa")
        ledger.set_status(aid, "DISPATCHED")
        assert ledger.has_inflight("host_pi") is False


class TestApprovals:
    def test_record_and_get_approval(self, ledger):
        aid = ledger.propose("tool_a", {}, "r", "grp", requires_approval=True)
        ledger.record_approval(aid, "APPROVE", "operator")
        approval = ledger.get_approval(aid)
        assert approval["decision"] == "APPROVE"
        assert approval["approved_by"] == "operator"

    def test_no_approval_returns_none(self, ledger):
        aid = ledger.propose("tool_a", {}, "r", "grp")
        assert ledger.get_approval(aid) is None

    def test_reject_approval(self, ledger):
        aid = ledger.propose("tool_a", {}, "r", "grp", requires_approval=True)
        ledger.record_approval(aid, "REJECT", "operator")
        approval = ledger.get_approval(aid)
        assert approval["decision"] == "REJECT"


class TestEpisode:
    def test_episode_starts_empty(self, ledger):
        assert ledger.current_episode() == []

    def test_episode_includes_recent_actions(self, ledger):
        ledger.propose("tool_a", {}, "r", "grp")
        episode = ledger.current_episode()
        assert len(episode) == 1

    def test_wait_resets_episode(self, ledger):
        ledger.propose("tool_a", {}, "r", "grp")
        ledger.record_wait("all good")
        time.sleep(0.01)  # ensure timestamp ordering
        ledger.propose("tool_b", {"x": 1}, "r", "grp")
        episode = ledger.current_episode()
        assert len(episode) == 1
        assert episode[0]["tool"] == "tool_b"

    def test_call_human_resets_episode(self, ledger):
        ledger.propose("tool_a", {}, "r", "grp")
        ledger.record_call_human("help!")
        time.sleep(0.01)
        ledger.propose("tool_c", {"y": 2}, "r", "grp")
        episode = ledger.current_episode()
        assert len(episode) == 1
        assert episode[0]["tool"] == "tool_c"

    def test_episode_is_bounded_to_recent_actions(self, ledger):
        for index in range(20):
            ledger.propose(f"tool_{index}", {"x": index}, "r", "grp")
            time.sleep(0.002)

        episode = ledger.current_episode()
        assert len(episode) == 12
        assert episode[0]["tool"] == "tool_8"
        assert episode[-1]["tool"] == "tool_19"


class TestThreadSafety:
    def test_concurrent_proposals_do_not_corrupt_connection(self, ledger):
        def create_action(index):
            return ledger.propose(f"tool_{index}", {"x": index}, "r", "grp")

        with ThreadPoolExecutor(max_workers=4) as executor:
            action_ids = list(executor.map(create_action, range(12)))

        assert all(action_ids)
        proposals = ledger.get_proposals()
        assert len(proposals) == 12


class TestGetByStatus:
    def test_returns_matching_status(self, ledger):
        aid1 = ledger.propose("a", {}, "r", "g")
        aid2 = ledger.propose("b", {"x": 1}, "r", "g")
        ledger.set_status(aid1, "DISPATCHED")

        dispatched = ledger.get_by_status("DISPATCHED")
        assert len(dispatched) == 1
        assert dispatched[0]["action_id"] == aid1

    def test_empty_result(self, ledger):
        assert ledger.get_by_status("UNKNOWN") == []


class TestEventObservation:
    def test_record_wait_stores_observation_and_reasoning(self, ledger):
        ledger.record_wait("all good", observation="temps stable", reasoning="nothing to do")
        event = ledger.conn.execute(
            "SELECT details_json FROM events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        import json
        details = json.loads(event["details_json"])
        assert details["observation"] == "temps stable"
        assert details["reasoning"] == "nothing to do"
        assert details["details"] == "all good"

    def test_record_call_human_stores_observation(self, ledger):
        ledger.record_call_human("help!", observation="blob on nozzle", reasoning="escalating")
        event = ledger.conn.execute(
            "SELECT details_json FROM events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        import json
        details = json.loads(event["details_json"])
        assert details["observation"] == "blob on nozzle"
        assert details["reasoning"] == "escalating"

    def test_record_wait_without_observation(self, ledger):
        ledger.record_wait("idle")
        event = ledger.conn.execute(
            "SELECT details_json FROM events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        # Should still work — details only
        assert "idle" in event["details_json"]
