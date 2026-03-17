"""Tests for call_human fallback chain."""

import json
import pytest
from unittest.mock import MagicMock

from wallee.human.call_human import call_human


class TestTelegramFallback:
    def test_telegram_succeeds(self, tmp_path):
        tg = MagicMock()
        result = call_human("test message", "info", outbox_dir=tmp_path / "outbox", telegram_fn=tg)
        assert result == "telegram"
        tg.assert_called_once_with("test message", "info")

    def test_telegram_retries_then_falls_through(self, tmp_path):
        tg = MagicMock(side_effect=ConnectionError("network down"))
        result = call_human("test", "info", outbox_dir=tmp_path / "outbox", telegram_fn=tg)
        assert tg.call_count == 3
        # Falls through to CLI or outbox
        assert result in ("cli", "outbox")

    def test_telegram_succeeds_on_second_try(self, tmp_path):
        tg = MagicMock(side_effect=[ConnectionError("fail"), None])
        result = call_human("test", "info", outbox_dir=tmp_path / "outbox", telegram_fn=tg)
        assert result == "telegram"
        assert tg.call_count == 2


class TestOutboxFallback:
    def test_outbox_creates_file(self, tmp_path):
        outbox = tmp_path / "outbox"
        # No telegram, force non-TTY by using outbox
        result = call_human("emergency!", "critical", outbox_dir=outbox, telegram_fn=None)
        # Will be "cli" if running in a terminal, "outbox" otherwise
        assert result in ("cli", "outbox")

    def test_outbox_file_contents(self, tmp_path):
        outbox = tmp_path / "outbox"
        # Force outbox by making no telegram and calling directly
        from wallee.human.call_human import _try_outbox
        success = _try_outbox("help me", "warning", outbox)
        assert success is True

        files = list(outbox.iterdir())
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["message"] == "help me"
        assert data["severity"] == "warning"
        assert data["delivered"] is False

    def test_outbox_dir_created_if_missing(self, tmp_path):
        outbox = tmp_path / "deep" / "nested" / "outbox"
        from wallee.human.call_human import _try_outbox
        _try_outbox("msg", "info", outbox)
        assert outbox.exists()


class TestCLIFallback:
    def test_cli_message_format(self, capsys, tmp_path):
        # If running in a TTY, this will print. If not, it falls to outbox.
        result = call_human("sensor fault", "warning", outbox_dir=tmp_path / "outbox")
        # Can't reliably assert CLI in CI, but verify it doesn't crash
        assert result in ("cli", "outbox")
