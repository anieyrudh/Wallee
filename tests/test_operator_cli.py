"""Operator CLI: remote ESTOP request and attended clear."""

from __future__ import annotations

from wallee.config import Config
from wallee.operator_cli import main as operator_main
from wallee.safety import consume_estop_signal


def _config(tmp_path, monkeypatch) -> Config:
    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WALLEE_ENABLED_PACKS", "sim_printer")
    monkeypatch.setenv("WALLEE_SIMULATION", "1")
    return Config.from_env()


def test_operator_cli_estop_writes_a_consumable_request(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path, monkeypatch)

    assert operator_main(["estop", "--reason", "lid open", "--by", "alice"]) == 0

    payload = consume_estop_signal(config.estop_request_path)
    assert payload is not None
    assert payload["reason"] == "lid open"
    assert payload["requested_by"] == "alice"
    assert "remote ESTOP requested" in capsys.readouterr().out


def test_operator_cli_clear_estop_removes_latch_and_signals_a_live_loop(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path, monkeypatch)
    config.estop_latch_path.parent.mkdir(parents=True, exist_ok=True)
    config.estop_latch_path.write_text('{"reason": "prior trip"}', encoding="utf-8")

    assert operator_main(["clear-estop", "--by", "bob"]) == 0

    assert not config.estop_latch_path.exists(), "clear-estop removes the durable latch"
    clear = consume_estop_signal(config.estop_clear_request_path)
    assert clear is not None and clear["requested_by"] == "bob"
    assert "ATTENDED OPERATION ONLY" in capsys.readouterr().out
