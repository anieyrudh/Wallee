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
import time
from typing import Callable

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
        whiteboard=None,
        ledger=None,
        safety_kernel=None,
    ):
        _ensure_telegram()
        self.token = token
        self.chat_id = str(chat_id)
        self.wb = whiteboard
        self.ledger = ledger
        self.safety_kernel = safety_kernel
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
        self._app.add_handler(_telegram_ext.CommandHandler("snapshot", self._cmd_snapshot))
        self._app.add_handler(_telegram_ext.CommandHandler("approve", self._cmd_approve))
        self._app.add_handler(_telegram_ext.CommandHandler("reject", self._cmd_reject))
        self._app.add_handler(_telegram_ext.CommandHandler("help", self._cmd_help))
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

    def send(self, message: str, severity: str = "info"):
        """Synchronous send — for use in call_human chain."""
        if not self._loop or not self._running:
            raise RuntimeError("Telegram bot not running")

        prefix = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(severity, "📌")
        text = f"{prefix} {severity.upper()}\n\n{message}"

        # Telegram limit is 4096 chars
        if len(text) > 4000:
            text = text[:4000] + "\n... (truncated)"

        future = asyncio.run_coroutine_threadsafe(
            self._app.bot.send_message(
                chat_id=self.chat_id,
                text=text,
            ),
            self._loop,
        )
        future.result(timeout=10)

    def send_approval_request(self, action_id: str, tool: str, params: dict):
        """Send an approval request with inline keyboard buttons."""
        if not self._loop or not self._running:
            return

        text = (
            f"🔧 *Approval Required*\n"
            f"Tool: `{_escape_md(tool)}`\n"
            f"Params: `{_escape_md(json.dumps(params, default=str))}`\n"
            f"Action ID: `{_escape_md(action_id[:8])}`"
        )
        keyboard = _telegram.InlineKeyboardMarkup([
            [
                _telegram.InlineKeyboardButton("✅ Approve", callback_data=f"approve:{action_id}"),
                _telegram.InlineKeyboardButton("❌ Reject", callback_data=f"reject:{action_id}"),
            ]
        ])

        future = asyncio.run_coroutine_threadsafe(
            self._app.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
            ),
            self._loop,
        )
        try:
            future.result(timeout=10)
        except Exception as e:
            logger.error(f"Failed to send approval request: {e}")

    # --- Setup ---

    async def _register_commands(self):
        """Register bot commands so they show in Telegram's / menu."""
        commands = [
            _telegram.BotCommand("status", "Printer status & temps"),
            _telegram.BotCommand("snapshot", "Camera snapshots"),
            _telegram.BotCommand("urgent", "Flag as urgent"),
            _telegram.BotCommand("estop", "Emergency stop"),
            _telegram.BotCommand("approve", "Approve pending action"),
            _telegram.BotCommand("reject", "Reject pending action"),
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
        await update.message.reply_text(
            "Commands:\n"
            "/status — Printer state, temps, safety\n"
            "/snapshot — Camera photos (nozzle + buddy)\n"
            "/urgent — Flag next cycle as urgent\n"
            "/estop — Emergency stop\n"
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
        if not self.wb:
            await update.message.reply_text("Whiteboard not connected")
            return

        s = self.wb.read_all()
        g = lambda k, default="-": s.get(k, default)

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
        if self.wb:
            self.wb.publish("human.urgent", True, ttl=10)
        await update.message.reply_text("🚨 Urgent flag set (10s TTL)")

    async def _cmd_estop(self, update, context):
        if self.wb:
            self.wb.publish("human.estop", True, ttl=30)
        logger.critical("ESTOP triggered via Telegram")
        await update.message.reply_text("🛑 ESTOP triggered. All actions paused.")

    async def _cmd_snapshot(self, update, context):
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
        if not context.args:
            await update.message.reply_text("Usage: /approve <action_id>")
            return
        action_id = context.args[0]
        self._record_approval(action_id, "APPROVE", str(update.effective_user.id))
        await update.message.reply_text(f"✅ Approved {action_id[:8]}")

    async def _cmd_reject(self, update, context):
        if not context.args:
            await update.message.reply_text("Usage: /reject <action_id>")
            return
        action_id = context.args[0]
        self._record_approval(action_id, "REJECT", str(update.effective_user.id))
        await update.message.reply_text(f"❌ Rejected {action_id[:8]}")

    async def _callback_handler(self, update, context):
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

        emoji = "✅" if decision == "APPROVE" else "❌"
        await query.edit_message_text(f"{emoji} {decision.title()}d: {action_id[:8]}")

    async def _handle_photo(self, update, context):
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
            self.wb.publish("human.image", b64, ttl=60)
            logger.info(f"Published human.image ({len(jpeg_data)} bytes, TTL=60s)")

            # If photo has a caption, publish as intent too
            caption = update.message.caption
            if caption:
                self.wb.publish("human.intent", caption.strip(), ttl=60)
                await update.message.reply_text(f"📸 Image + intent received: {caption.strip()}")
            else:
                await update.message.reply_text("📸 Image received — LLM will analyze on next cycle")

        except Exception as e:
            logger.error(f"Failed to process photo: {e}")
            await update.message.reply_text(f"Failed to process photo: {e}")

    async def _handle_intent(self, update, context):
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
            self.wb.publish("human.intent", text, ttl=60)
        await update.message.reply_text(f"📝 Intent set: {text}")

    # --- Helpers ---

    def _record_approval(self, action_id: str, decision: str, approved_by: str):
        """Record an approval/rejection in the ledger."""
        if not self.ledger:
            logger.warning(f"Cannot record approval — no ledger connected")
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
