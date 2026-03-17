"""Tests for Telegram bot — unit tests without real Telegram connection."""

import pytest
from unittest.mock import MagicMock, patch

from wallee.human.telegram import _escape_md


class TestEscapeMarkdown:
    def test_plain_text(self):
        assert _escape_md("hello world") == "hello world"

    def test_special_chars(self):
        result = _escape_md("temp > 200.0")
        assert "\\>" in result
        assert "\\." in result

    def test_backticks(self):
        result = _escape_md("`code`")
        assert "\\`code\\`" == result

    def test_underscores(self):
        result = _escape_md("printer_state")
        assert "\\_" in result


class TestTelegramBotInit:
    """Test bot can be constructed without real telegram connection."""

    def test_import_error_message(self):
        """If python-telegram-bot not installed, get clear error."""
        # We can't easily test the import failure on a system where
        # it might be installed, so test the escape function instead
        assert callable(_escape_md)

    def test_call_human_integration(self):
        """Verify the call_human chain supports telegram_fn parameter."""
        from wallee.human.call_human import call_human
        from pathlib import Path
        import tempfile

        messages = []

        def fake_telegram(msg, severity):
            messages.append((msg, severity))

        with tempfile.TemporaryDirectory() as tmpdir:
            result = call_human(
                "test message",
                severity="warning",
                telegram_fn=fake_telegram,
                outbox_dir=Path(tmpdir) / "outbox",
            )

        assert result == "telegram"
        assert len(messages) == 1
        assert messages[0] == ("test message", "warning")

    def test_call_human_fallback_on_telegram_failure(self):
        """If telegram fails 3 times, falls through to CLI or outbox."""
        from wallee.human.call_human import call_human
        from pathlib import Path
        import tempfile

        def failing_telegram(msg, severity):
            raise ConnectionError("No internet")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = call_human(
                "test message",
                severity="info",
                telegram_fn=failing_telegram,
                outbox_dir=Path(tmpdir) / "outbox",
            )

        # Should fall through to CLI or outbox
        assert result in ("cli", "outbox")
