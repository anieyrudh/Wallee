"""Telegram bot for Wallee — remote human interface.

Features:
- Receive text → publish as human.intent to whiteboard
- /urgent → set human.urgent flag
- /estop → trigger safety kernel ESTOP
- /status → dump whiteboard summary
- /snapshot → send camera frames
- /approve <id> / /reject <id> → record approval in ledger
- Inline keyboard for approval requests
- Send alerts and status updates

Uses python-telegram-bot (async). Runs in a background thread with its own
event loop. Exposes a synchronous `send()` method for the call_human chain.
"""

import asyncio
import json
import logging
import os
import threading

from wallee.config import (
    DEFAULT_HUMAN_ESTOP_TTL_S,
    DEFAULT_HUMAN_IMAGE_TTL_S,
    DEFAULT_HUMAN_INTENT_TTL_S,
    DEFAULT_HUMAN_URGENT_TTL_S,
)
from wallee.safety.estop import estop_printer

logger = logging.getLogger(__name__)

# Lazy imports — telegram library may not be installed
_telegram = None
_telegram_ext = None


def _ensure_telegram():
    """Import telegram libraries on first use."""
    global _telegram, _telegram_ext
    if _telegram is None:
        try:
            import telegram
            import telegram.ext
            _telegram = telegram
            _telegram_ext = telegram.ext
        except ImportError:
            raise ImportError("python-telegram-bot not installed. Run: pip install python-telegram-bot")


class TelegramBot:
    """Wallee Telegram bot — bridges async telegram library with sync Wallee."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        allowed_user_ids: list[str] | None = None,
        intent_ttl: int = DEFAULT_HUMAN_INTENT_TTL_S,
        urgent_ttl: int = DEFAULT_HUMAN_URGENT_TTL_S,
        image_ttl: int = DEFAULT_HUMAN_IMAGE_TTL_S,
        estop_ttl: int = DEFAULT_HUMAN_ESTOP_TTL_S,
        whiteboard=None,
        ledger=None,
        safety_kernel=None,
        wake_agent_fn=None,
    ):
        _ensure_telegram()
        self.token = token
        self.chat_id = str(chat_id)
        self.allowed_user_ids = {
            str(user_id).strip()
            for user_id in (allowed_user_ids or self._load_allowed_user_ids())
            if str(user_id).strip()
        }
        if not self.allowed_user_ids and self.chat_id.isdigit():
            self.allowed_user_ids = {self.chat_id}
        if not self.allowed_user_ids:
            # A group/channel chat id (or an empty one) with no explicit
            # allowlist would authorize EVERY member of the chat to approve
            # actions, inject intents, and trigger or clear ESTOP. Refuse to
            # start the bot in that configuration.
            raise ValueError(
                "TELEGRAM_ALLOWED_USER_IDS must be set explicitly when "
                "TELEGRAM_CHAT_ID is not a single numeric user id "
                "(group chats would otherwise authorize every member)"
            )
        self.intent_ttl = intent_ttl
        self.urgent_ttl = urgent_ttl
        self.image_ttl = image_ttl
        self.estop_ttl = estop_ttl
        self.wb = whiteboard
        self.ledger = ledger
        self.safety_kernel = safety_kernel
        self._wake_agent = wake_agent_fn
        self._app = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self):
        """Start the bot in a background thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="telegram-bot")
        self._thread.start()
        logger.info("Telegram bot starting in background thread")

    def _run_loop(self):
        """Run the async event loop in this thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        builder = _telegram_ext.ApplicationBuilder().token(self.token)
        self._app = builder.build()

        # Register handlers
        self._app.add_handler(_telegram_ext.CommandHandler("start", self._cmd_start))
        self._app.add_handler(_telegram_ext.CommandHandler("status", self._cmd_status))
        self._app.add_handler(_telegram_ext.CommandHandler("urgent", self._cmd_urgent))
        self._app.add_handler(_telegram_ext.CommandHandler("estop", self._cmd_estop))
        self._app.add_handler(_telegram_ext.CommandHandler("estop_clear", self._cmd_estop_clear))
        self._app.add_handler(_telegram_ext.CommandHandler("snapshot", self._cmd_snapshot))
        self._app.add_handler(_telegram_ext.CommandHandler("approve", self._cmd_approve))
        self._app.add_handler(_telegram_ext.CommandHandler("reject", self._cmd_reject))
        self._app.add_handler(_telegram_ext.CommandHandler("help", self._cmd_help))
        self._app.add_handler(_telegram_ext.CommandHandler("queue", self._cmd_queue))
        self._app.add_handler(_telegram_ext.CallbackQueryHandler(self._callback_handler))
        self._app.add_handler(_telegram_ext.MessageHandler(
            _telegram_ext.filters.PHOTO,
            self._handle_photo,
        ))
        self._app.add_handler(_telegram_ext.MessageHandler(
            _telegram_ext.filters.TEXT & ~_telegram_ext.filters.COMMAND,
            self._handle_intent,
        ))

        try:
            self._loop.run_until_complete(self._app.initialize())
            # Register command menu (shows in Telegram's "/" menu)
            self._loop.run_until_complete(self._register_commands())
            self._loop.run_until_complete(self._app.start())
            self._loop.run_until_complete(self._app.updater.start_polling())
            logger.info("Telegram bot polling started")
            # Keep running until stopped
            while self._running:
                self._loop.run_until_complete(asyncio.sleep(1))
        except Exception as e:
            logger.error(f"Telegram bot error: {e}")
        finally:
            try:
                self._loop.run_until_complete(self._app.updater.stop())
                self._loop.run_until_complete(self._app.stop())
                self._loop.run_until_complete(self._app.shutdown())
            except Exception:
                pass
            self._loop.close()

    def stop(self):
        """Stop the bot."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("Telegram bot stopped")

    async def _send_with_retry(self, coro_fn, max_retries=3):
        """Retry an async send operation with exponential backoff."""
        for attempt in range(max_retries):
            try:
                return await coro_fn()
            except Exception as e:
                if attempt < max_retries - 1:
                    delay = 2 ** attempt
                    logger.warning(f"Telegram send attempt {attempt+1}/{max_retries} failed: {e}. Retrying in {delay}s...")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"Telegram send failed after {max_retries} attempts: {e}")
                    raise

    def send(self, message: str, severity: str = "info"):
        """Synchronous send — for use in call_human chain. Retries 3x with backoff."""
        if not self._loop or not self._running:
            raise RuntimeError("Telegram bot not running")

        prefix = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(severity, "📌")
        text = f"{prefix} {severity.upper()}\n\n{message}"

        # Telegram limit is 4096 chars
        if len(text) > 4000:
            text = text[:4000] + "\n... (truncated)"

        future = asyncio.run_coroutine_threadsafe(
            self._send_with_retry(lambda: self._app.bot.send_message(
                chat_id=self.chat_id,
                text=text,
            )),
            self._loop,
        )
        future.result(timeout=60)

    def send_approval_request(self, action_id: str, tool: str, params: dict,
                              reason: str = "", observation: str = ""):
        """Send an approval request with inline keyboard buttons. Retries 3x."""
        if not self._loop or not self._running:
            return

        obs_line = f"\nObservation: {_escape_md(observation[:200])}" if observation else ""
        reason_line = f"\nReasoning: {_escape_md(reason[:200])}" if reason else ""
        text = (
            f"🔧 *{_escape_md(tool.upper().replace('_', ' '))} requested*\n"
            f"Tool: `{_escape_md(tool)}`\n"
            f"Params: `{_escape_md(json.dumps(params, default=str))}`"
            f"{obs_line}{reason_line}\n"
            f"Reply APPROVE or REJECT\\."
        )
        keyboard = _telegram.InlineKeyboardMarkup([
            [
                _telegram.InlineKeyboardButton("✅ Approve", callback_data=f"approve:{action_id}"),
                _telegram.InlineKeyboardButton("❌ Reject", callback_data=f"reject:{action_id}"),
            ]
        ])

        future = asyncio.run_coroutine_threadsafe(
            self._send_with_retry(lambda: self._app.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
            )),
            self._loop,
        )
        try:
            future.result(timeout=60)
        except Exception as e:
            logger.error(f"Failed to send approval request after retries: {e}")

    @staticmethod
    def _load_allowed_user_ids() -> list[str]:
        raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
        return [user_id.strip() for user_id in raw.split(",") if user_id.strip()]

    def _is_authorized(self, update) -> bool:
        chat = getattr(update, "effective_chat", None)
        user = getattr(update, "effective_user", None)

        if self.chat_id and (chat is None or str(chat.id) != self.chat_id):
            return False
        if self.allowed_user_ids and (user is None or str(user.id) not in self.allowed_user_ids):
            return False
        return True

    async def _reject_unauthorized(self, update):
        chat = getattr(update, "effective_chat", None)
        user = getattr(update, "effective_user", None)
        logger.warning(
            "Rejected unauthorized Telegram update: chat=%s user=%s",
            getattr(chat, "id", None),
            getattr(user, "id", None),
        )

        query = getattr(update, "callback_query", None)
        if query is not None:
            try:
                await query.answer("Unauthorized", show_alert=True)
            except Exception:
                pass
            return False

        message = getattr(update, "message", None)
        if message is not None:
            try:
                await message.reply_text("Unauthorized")
            except Exception:
                pass
        return False

    async def _ensure_authorized(self, update) -> bool:
        if self._is_authorized(update):
            return True
        return await self._reject_unauthorized(update)

    # --- Setup ---

    async def _register_commands(self):
        """Register bot commands so they show in Telegram's / menu."""
        commands = [
            _telegram.BotCommand("status", "Printer status & temps"),
            _telegram.BotCommand("snapshot", "Camera snapshots"),
            _telegram.BotCommand("urgent", "Flag as urgent"),
            _telegram.BotCommand("estop", "Emergency stop (latches)"),
            _telegram.BotCommand("estop_clear", "Clear the ESTOP latch"),
            _telegram.BotCommand("approve", "Approve pending action"),
            _telegram.BotCommand("reject", "Reject pending action"),
            _telegram.BotCommand("queue", "Manage print queue"),
            _telegram.BotCommand("help", "Show all commands"),
        ]
        await self._app.bot.set_my_commands(commands)
        logger.info("Telegram command menu registered")

    def _quick_keyboard(self):
        """Persistent reply keyboard with common actions."""
        return _telegram.ReplyKeyboardMarkup(
            [
                ["📊 Status", "📷 Snapshot"],
                ["🚨 Urgent", "🛑 ESTOP"],
            ],
            resize_keyboard=True,
            input_field_placeholder="Type intent or tap a button...",
        )

    # --- Command handlers ---

    async def _cmd_start(self, update, context):
        if not await self._ensure_authorized(update):
            return

        await update.message.reply_text(
            "🤖 *Wallee* is connected\\.\n\n"
            "I monitor your Prusa Core One\\+ and can:\n"
            "• Show printer status and temps\n"
            "• Send camera snapshots\n"
            "• Relay your instructions to the agent\n"
            "• Request approval for risky actions\n\n"
            "Tap a button below or type what you want the printer to do\\.",
            parse_mode="MarkdownV2",
            reply_markup=self._quick_keyboard(),
        )

    async def _cmd_help(self, update, context):
        if not await self._ensure_authorized(update):
            return

        await update.message.reply_text(
            "Commands:\n"
            "/status — Printer state, temps, safety\n"
            "/snapshot — Camera photos (nozzle + buddy)\n"
            "/queue — View/add to print queue\n"
            "/urgent — Flag next cycle as urgent\n"
            "/estop — Emergency stop (latches until cleared)\n"
            "/estop_clear — Clear the ESTOP latch\n"
            "/approve <id> — Approve a pending action\n"
            "/reject <id> — Reject a pending action\n\n"
            "Or just type what you want:\n"
            '  "resume the print"\n'
            '  "set nozzle to 215"\n'
            '  "what\'s the bed temp?"\n\n'
            "Send a photo and I'll show it to the LLM for analysis.",
            reply_markup=self._quick_keyboard(),
        )

    async def _cmd_status(self, update, context):
        if not await self._ensure_authorized(update):
            return

        if not self.wb:
            await update.message.reply_text("Whiteboard not connected")
            return

        s = self.wb.read_all()
        def g(k, default="-"):
            return s.get(k, default)

        # Printer state
        lines = ["PRINTER"]
        state = g("printer.state")
        job = g("printer.job_state")
        lines.append(f"  State: {state} | Job: {job}")
        progress = g("printer.job_progress")
        if progress not in ("-", None) and job not in ("IDLE", "-"):
            remaining = g("printer.job_time_remaining_s")
            r_str = f" | {int(remaining)//60}m left" if remaining not in ("-", None) else ""
            lines.append(f"  Progress: {progress}%{r_str}")
        fname = g("printer.print_filename")
        if fname and fname != "" and fname != "-":
            lines.append(f"  File: {fname}")

        # Temperatures
        lines.append("\nTEMPS")
        noz = g("printer.temp_nozzle")
        noz_t = g("printer.target_nozzle")
        bed = g("printer.temp_bed")
        bed_t = g("printer.target_bed")
        lines.append(f"  Nozzle: {noz}/{noz_t}C")
        lines.append(f"  Bed: {bed}/{bed_t}C")
        chamber = g("printer.temp_chamber")
        if chamber not in ("-", None):
            lines.append(f"  Chamber: {chamber}C")
        hbr = g("printer.temp_heatbreak")
        if hbr not in ("-", None):
            lines.append(f"  Heatbreak: {hbr}C")

        # Safety
        oc_n = g("printer.oc_nozzle")
        oc_i = g("printer.oc_input")
        if oc_n not in ("-", None, 0) or oc_i not in ("-", None, 0):
            lines.append(f"\n!! OVERCURRENT  nozzle={oc_n} input={oc_i}")
        else:
            lines.append("\nSAFETY: OK")

        # Cameras
        cams = []
        for name, key in [("Nozzle", "camera.nozzle_status"),
                          ("Buddy1", "camera.buddy1_status"), ("Buddy2", "camera.buddy2_status")]:
            st = g(key)
            if st not in ("-", None):
                cams.append(f"{name}:{st}")
        if cams:
            lines.append(f"\nCAMS: {' | '.join(cams)}")

        # Host
        cpu = g("host.cpu_temp")
        mem = g("host.memory_percent")
        if cpu not in ("-", None):
            lines.append(f"\nHOST: CPU {cpu}C | Mem {mem}%")

        await update.message.reply_text("\n".join(lines))

    async def _cmd_urgent(self, update, context):
        if not await self._ensure_authorized(update):
            return

        if self.wb:
            self.wb.publish("human.urgent", True, ttl=self.urgent_ttl)
        if self._wake_agent:
            self._wake_agent()
        await update.message.reply_text(f"🚨 Urgent flag set ({self.urgent_ttl}s TTL)")

    async def _cmd_estop(self, update, context):
        if not await self._ensure_authorized(update):
            return

        if self.wb:
            # No TTL: ESTOP latches until a human explicitly clears it with
            # /estop_clear. An emergency stop that silently expires is unsafe.
            self.wb.publish("safety.estop", True)
        estop_printer(os.environ.get("PRUSALINK_HOST", ""), os.environ.get("PRUSALINK_API_KEY", ""))
        if self._wake_agent:
            self._wake_agent()
        logger.critical("ESTOP triggered via Telegram")
        await update.message.reply_text(
            "🛑 ESTOP ACTIVATED — printer paused. Latched until /estop_clear."
        )

    async def _cmd_estop_clear(self, update, context):
        if not await self._ensure_authorized(update):
            return

        if not self.wb:
            await update.message.reply_text("Whiteboard not connected")
            return
        if not self.wb.read("safety.estop"):
            await update.message.reply_text("No ESTOP latch is set.")
            return
        self.wb.delete("safety.estop")
        if self._wake_agent:
            self._wake_agent()
        user = getattr(update, "effective_user", None)
        logger.critical("ESTOP latch cleared via Telegram by user %s", getattr(user, "id", None))
        await update.message.reply_text(
            "✅ ESTOP latch cleared. Verify machine state before resuming operation."
        )

    async def _cmd_queue(self, update, context):
        """Manage print queue. Usage: /queue [file1.bgcode file2.bgcode ...]"""
        if not await self._ensure_authorized(update):
            return

        if not self.wb:
            await update.message.reply_text("Whiteboard not connected")
            return

        args = context.args
        if not args:
            # Show current queue
            queue_raw = self.wb.read("print.queue") or []
            try:
                items = json.loads(queue_raw) if isinstance(queue_raw, str) else queue_raw
            except (json.JSONDecodeError, TypeError):
                items = []
            if items:
                text = "Print queue:\n" + "\n".join(f"{i+1}. {f}" for i, f in enumerate(items))
            else:
                text = "Queue is empty. Usage: /queue file1.bgcode file2.bgcode"
            await update.message.reply_text(text)
            return

        # Add files to queue
        queue_raw = self.wb.read("print.queue") or []
        try:
            items = json.loads(queue_raw) if isinstance(queue_raw, str) else queue_raw
        except (json.JSONDecodeError, TypeError):
            items = []
        items.extend(args)
        self.wb.publish("print.queue", items, ttl=86400)  # 24h TTL
        await update.message.reply_text(f"Added {len(args)} file(s). Queue: {len(items)} total.")
        if self._wake_agent:
            self._wake_agent()

    async def _cmd_snapshot(self, update, context):
        if not await self._ensure_authorized(update):
            return

        """Send camera snapshots — all live cameras."""
        if not self.wb:
            await update.message.reply_text("Whiteboard not connected")
            return

        import base64
        from io import BytesIO
        cameras = [
            ("camera.nozzle_frame", "camera.nozzle_status", "🔍 Nozzle (3DO endoscope)"),
            ("camera.buddy1_frame", "camera.buddy1_status", "📷 Buddy camera 1"),
            ("camera.buddy2_frame", "camera.buddy2_status", "📷 Buddy camera 2"),
        ]
        sent = 0
        for frame_key, status_key, label in cameras:
            status = self.wb.read(status_key)
            if status != "live":
                continue
            b64 = self.wb.read(frame_key)
            if not b64:
                continue
            try:
                jpeg = base64.b64decode(b64)
                await update.message.reply_photo(photo=BytesIO(jpeg), caption=label)
                sent += 1
            except Exception as e:
                await update.message.reply_text(f"{label}: error {e}")
        if sent == 0:
            await update.message.reply_text("No live cameras available")

    async def _cmd_approve(self, update, context):
        if not await self._ensure_authorized(update):
            return

        if not context.args:
            await update.message.reply_text("Usage: /approve <action_id>")
            return
        action_id = context.args[0]
        self._record_approval(action_id, "APPROVE", str(update.effective_user.id))
        self._acknowledge_pending_callout()
        await update.message.reply_text(f"✅ Approved {action_id[:8]}")

    async def _cmd_reject(self, update, context):
        if not await self._ensure_authorized(update):
            return

        if not context.args:
            await update.message.reply_text("Usage: /reject <action_id>")
            return
        action_id = context.args[0]
        self._record_approval(action_id, "REJECT", str(update.effective_user.id))
        self._acknowledge_pending_callout()
        await update.message.reply_text(f"❌ Rejected {action_id[:8]}")

    async def _callback_handler(self, update, context):
        if not await self._ensure_authorized(update):
            return

        """Handle inline keyboard button presses (approve/reject)."""
        query = update.callback_query
        await query.answer()

        data = query.data
        if ":" not in data:
            return

        action, action_id = data.split(":", 1)
        decision = "APPROVE" if action == "approve" else "REJECT"
        user = str(query.from_user.id)

        self._record_approval(action_id, decision, user)
        self._acknowledge_pending_callout()

        emoji = "✅" if decision == "APPROVE" else "❌"
        await query.edit_message_text(f"{emoji} {decision.title()}d: {action_id[:8]}")

    async def _handle_photo(self, update, context):
        if not await self._ensure_authorized(update):
            return

        """Download photo, resize, base64 encode, publish to whiteboard."""
        if not self.wb:
            await update.message.reply_text("Whiteboard not connected")
            return

        try:
            # Get highest resolution version
            photo = update.message.photo[-1]  # Last = largest
            file = await context.bot.get_file(photo.file_id)
            data = await file.download_as_bytearray()

            # Resize to max 1024x1024 and re-encode as JPEG
            import base64
            from io import BytesIO
            try:
                from PIL import Image
                img = Image.open(BytesIO(bytes(data)))
                img.thumbnail((1024, 1024))
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=85)
                jpeg_data = buf.getvalue()
            except ImportError:
                # No PIL — use raw data
                jpeg_data = bytes(data)

            b64 = base64.b64encode(jpeg_data).decode("ascii")
            self.wb.publish("human.image", b64, ttl=self.image_ttl)
            logger.info(f"Published human.image ({len(jpeg_data)} bytes, TTL={self.image_ttl}s)")

            # If photo has a caption, publish as intent too
            caption = update.message.caption
            if caption:
                self.wb.publish("human.intent", caption.strip(), ttl=self.intent_ttl)
                await update.message.reply_text(f"📸 Image + intent received: {caption.strip()}")
            else:
                await update.message.reply_text("📸 Image received — LLM will analyze on next cycle")

            if self._wake_agent:
                self._wake_agent()

        except Exception as e:
            logger.error(f"Failed to process photo: {e}")
            await update.message.reply_text(f"Failed to process photo: {e}")

    async def _handle_intent(self, update, context):
        if not await self._ensure_authorized(update):
            return

        """Route quick keyboard buttons or set as human.intent."""
        text = update.message.text.strip()
        if not text:
            return

        # Route quick keyboard button taps to commands
        button_map = {
            "📊 Status": self._cmd_status,
            "📷 Snapshot": self._cmd_snapshot,
            "🚨 Urgent": self._cmd_urgent,
            "🛑 ESTOP": self._cmd_estop,
        }
        handler = button_map.get(text)
        if handler:
            await handler(update, context)
            return

        # Everything else is a human intent for the agent
        if self.wb:
            self.wb.publish("human.intent", text, ttl=self.intent_ttl)
            # Also store in intent history for dashboard persistence
            import json as _json
            import time as _time
            entry = _json.dumps({"ts": _time.strftime("%H:%M:%S"), "text": text})
            self.wb.r.lpush("human.intent_log", entry)
            self.wb.r.ltrim("human.intent_log", 0, 9)
            # Clear pending callout when human responds with any intent
            if self.wb.read("human.pending_callout"):
                self.wb.r.delete("human.pending_callout")
                logger.info("Pending callout cleared after human intent")
        if self._wake_agent:
            self._wake_agent()
        await update.message.reply_text(f"📝 Intent set: {text}")

    def _acknowledge_pending_callout(self):
        """Clear pending callout from whiteboard when human responds."""
        if not self.wb:
            return
        pending = self.wb.read("human.pending_callout")
        if pending:
            self.wb.r.delete("human.pending_callout")
            logger.info("Pending callout cleared after human response")

    # --- Helpers ---

    def _record_approval(self, action_id: str, decision: str, approved_by: str):
        """Record an approval/rejection in the ledger."""
        if not self.ledger:
            logger.warning("Cannot record approval — no ledger connected")
            return
        try:
            self.ledger.record_approval(action_id, decision, approved_by)
            logger.info(f"Approval recorded: {action_id[:8]} → {decision} by {approved_by}")
        except Exception as e:
            logger.error(f"Failed to record approval: {e}")


def _escape_md(text: str) -> str:
    """Escape special characters for MarkdownV2."""
    special = r'_*[]()~`>#+-=|{}.!'
    result = []
    for c in text:
        if c in special:
            result.append('\\')
        result.append(c)
    return ''.join(result)
