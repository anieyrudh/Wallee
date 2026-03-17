"""Tests for CLI command processing."""

import fakeredis
import pytest

from wallee.human.cli import CLI
from wallee.whiteboard.client import Whiteboard
from wallee.ledger.db import Ledger


@pytest.fixture
def wb():
    return Whiteboard(_redis=fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "cli_test.db")
    yield db
    db.close()


@pytest.fixture
def cli(wb, ledger):
    return CLI(wb, ledger)


class TestIntent:
    def test_set_intent(self, cli, wb):
        cli.process_command("intent resume the print")
        assert wb.read("human.intent") == "resume the print"

    def test_intent_no_args(self, cli, capsys):
        cli.process_command("intent")
        assert "Usage" in capsys.readouterr().out


class TestUrgent:
    def test_set_urgent(self, cli, wb):
        cli.process_command("urgent")
        assert wb.read("human.urgent") is True


class TestApproval:
    def test_approve_action(self, cli, ledger):
        aid = ledger.propose("tool_a", {}, "test", "grp", requires_approval=True)
        ledger.set_status(aid, "WAITING_APPROVAL")
        cli.process_command(f"approve {aid}")
        approval = ledger.get_approval(aid)
        assert approval["decision"] == "APPROVE"

    def test_reject_action(self, cli, ledger):
        aid = ledger.propose("tool_a", {}, "test", "grp", requires_approval=True)
        ledger.set_status(aid, "WAITING_APPROVAL")
        cli.process_command(f"reject {aid}")
        approval = ledger.get_approval(aid)
        assert approval["decision"] == "REJECT"

    def test_approve_nonexistent(self, cli, capsys):
        cli.process_command("approve nonexistent-id")
        assert "not found" in capsys.readouterr().out

    def test_approve_wrong_status(self, cli, ledger, capsys):
        aid = ledger.propose("tool_a", {}, "test", "grp")
        cli.process_command(f"approve {aid}")
        assert "not WAITING_APPROVAL" in capsys.readouterr().out


class TestStatus:
    def test_shows_whiteboard(self, cli, wb, capsys):
        wb.publish("env.temperature", 21.5)
        cli.process_command("status")
        output = capsys.readouterr().out
        assert "env.temperature" in output
        assert "21.5" in output

    def test_empty_whiteboard(self, cli, capsys):
        cli.process_command("status")
        assert "empty" in capsys.readouterr().out


class TestPending:
    def test_shows_pending(self, cli, ledger, capsys):
        aid = ledger.propose("resume_print", {}, "temps ok", "prusa", requires_approval=True)
        ledger.set_status(aid, "WAITING_APPROVAL")
        cli.process_command("pending")
        output = capsys.readouterr().out
        assert "resume_print" in output

    def test_no_pending(self, cli, capsys):
        cli.process_command("pending")
        assert "No actions" in capsys.readouterr().out


class TestESTOP:
    def test_estop_sets_flag(self, cli, wb):
        cli.process_command("estop")
        assert wb.read("safety.estop") is True


class TestMisc:
    def test_quit_returns_false(self, cli):
        assert cli.process_command("quit") is False

    def test_help_runs(self, cli, capsys):
        cli.process_command("help")
        assert "Commands" in capsys.readouterr().out

    def test_unknown_command(self, cli, capsys):
        cli.process_command("foobar")
        assert "Unknown" in capsys.readouterr().out

    def test_empty_input(self, cli):
        assert cli.process_command("") is True
