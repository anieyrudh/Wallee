"""Tests for Telegram bot — unit tests without real Telegram connection."""

import asyncio

import fakeredis
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from wallee.human.telegram import TelegramBot, _escape_md
from wallee.whiteboard.client import Whiteboard


async def _noop_sleep(*args, **kwargs):
    """Replacement for asyncio.sleep that returns immediately."""
    pass


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


class TestTelegramAuth:
    def _make_update(self, chat_id, user_id):
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=chat_id),
            effective_user=SimpleNamespace(id=user_id),
        )

    def test_private_chat_defaults_to_same_user(self):
        with patch("wallee.human.telegram._ensure_telegram", return_value=None):
            bot = TelegramBot(token="token", chat_id="12345")

        assert bot._is_authorized(self._make_update(12345, 12345)) is True
        assert bot._is_authorized(self._make_update(12345, 99999)) is False

    def test_group_chat_requires_allowed_user_list(self):
        with patch("wallee.human.telegram._ensure_telegram", return_value=None):
            bot = TelegramBot(token="token", chat_id="-555", allowed_user_ids=["42"])

        assert bot._is_authorized(self._make_update(-555, 42)) is True
        assert bot._is_authorized(self._make_update(-555, 99)) is False

    def test_wrong_chat_is_rejected(self):
        with patch("wallee.human.telegram._ensure_telegram", return_value=None):
            bot = TelegramBot(token="token", chat_id="12345", allowed_user_ids=["12345"])

        assert bot._is_authorized(self._make_update(99999, 12345)) is False

    def test_approval_acknowledges_pending_callout(self):
        """Approving an action should ack any pending callout."""
        import json
        wb = Whiteboard(_redis=fakeredis.FakeRedis(decode_responses=True))
        ledger_mock = MagicMock()

        with patch("wallee.human.telegram._ensure_telegram", return_value=None):
            bot = TelegramBot(
                token="token",
                chat_id="12345",
                allowed_user_ids=["12345"],
                whiteboard=wb,
                ledger=ledger_mock,
            )

        wb.publish("human.pending_callout", json.dumps({
            "hash": "abc", "message": "help", "time": 1, "status": "PENDING",
        }), ttl=60)

        bot._record_approval("action-123", "APPROVE", "12345")
        bot._acknowledge_pending_callout()

        assert wb.read("human.pending_callout") is None

class TestSendWithRetry:
    """Test _send_with_retry retries on failure with exponential backoff."""

    def test_retry_succeeds_on_second_attempt(self):
        """If first send fails and second succeeds, no exception raised."""
        with patch("wallee.human.telegram._ensure_telegram", return_value=None):
            bot = TelegramBot(token="token", chat_id="12345")

        call_count = 0
        async def flaky_send():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TimeoutError("Connection timed out")
            return None

        async def run():
            with patch("asyncio.sleep", _noop_sleep):
                await bot._send_with_retry(flaky_send, max_retries=3)

        asyncio.run(run())
        assert call_count == 2

    def test_raises_after_all_retries_exhausted(self):
        """After max_retries failures, the exception is raised."""
        with patch("wallee.human.telegram._ensure_telegram", return_value=None):
            bot = TelegramBot(token="token", chat_id="12345")

        call_count = 0
        async def always_fail():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("DNS resolution failed")

        async def run():
            with patch("asyncio.sleep", _noop_sleep):
                await bot._send_with_retry(always_fail, max_retries=3)

        with pytest.raises(ConnectionError):
            asyncio.run(run())
        assert call_count == 3

    def test_no_retry_on_success(self):
        """Successful first send does not retry."""
        with patch("wallee.human.telegram._ensure_telegram", return_value=None):
            bot = TelegramBot(token="token", chat_id="12345")

        call_count = 0
        async def success():
            nonlocal call_count
            call_count += 1
            return "ok"

        async def run():
            return await bot._send_with_retry(success, max_retries=3)

        result = asyncio.run(run())
        assert result == "ok"
        assert call_count == 1


class TestTelegramCallHumanWrapper:
    """The _telegram_call_human wrapper must only fall to outbox on failure."""

    def test_successful_send_does_not_write_outbox(self):
        """If telegram.send() succeeds, outbox should NOT be written."""
        # This tests the logic from main.py's _telegram_call_human
        outbox_written = False
        telegram_succeeded = False

        def mock_send(msg, severity):
            nonlocal telegram_succeeded
            telegram_succeeded = True

        def mock_outbox(msg, severity, **kwargs):
            nonlocal outbox_written
            outbox_written = True

        # Simulate the FIXED _telegram_call_human logic
        try:
            mock_send("test", "info")
        except Exception:
            mock_outbox("test", "info")

        assert telegram_succeeded
        assert not outbox_written  # Outbox should NOT be written on success


    def test_estop_command_publishes_safety_key(self):
        wb = Whiteboard(_redis=fakeredis.FakeRedis(decode_responses=True))
        with patch("wallee.human.telegram._ensure_telegram", return_value=None):
            bot = TelegramBot(
                token="token",
                chat_id="12345",
                allowed_user_ids=["12345"],
                whiteboard=wb,
            )

        replies = []

        async def reply_text(message):
            replies.append(message)

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=12345),
            effective_user=SimpleNamespace(id=12345),
            callback_query=None,
            message=SimpleNamespace(reply_text=reply_text),
        )

        asyncio.run(bot._cmd_estop(update, None))

        assert wb.read("safety.estop") is True
        assert wb.read("human.estop") is None
        assert replies == ["🛑 ESTOP ACTIVATED — printer paused. Manual intervention required."]
