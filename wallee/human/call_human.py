"""call_human fallback chain: Telegram -> CLI/TTY -> durable outbox."""

import json
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _try_cli(message: str, severity: str) -> bool:
    """Try to write to the TTY/terminal."""
    try:
        if sys.stdout.isatty():
            prefix = {"info": "[INFO]", "warning": "[WARN]", "critical": "[CRIT]"}.get(severity, "[????]")
            print(f"\n{'='*60}")
            print(f"  WALLEE {prefix} {message}")
            print(f"{'='*60}\n", flush=True)
            return True
    except Exception:
        pass
    return False


def _try_outbox(message: str, severity: str, outbox_dir: Path) -> bool:
    """Write to durable outbox as last resort."""
    try:
        outbox_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{int(time.time())}_{severity}.json"
        payload = {
            "message": message,
            "severity": severity,
            "timestamp": time.time(),
            "delivered": False,
        }
        (outbox_dir / filename).write_text(json.dumps(payload, indent=2))
        logger.info(f"Message written to outbox: {filename}")
        return True
    except Exception as e:
        logger.error(f"Outbox write failed: {e}")
        return False


def call_human(
    message: str,
    severity: str = "info",
    outbox_dir: Path | None = None,
    telegram_fn=None,
) -> str:
    """Send a message to the human operator via the fallback chain.

    Chain: Telegram (x3 backoff) -> CLI/TTY -> durable outbox.
    Returns the delivery method used, or "outbox" as final fallback.
    """
    if outbox_dir is None:
        outbox_dir = Path(os.environ.get("WALLEE_DATA_DIR", "/var/lib/wallee")) / "outbox"

    # 1. Try Telegram (Phase 4 — pass callable)
    if telegram_fn:
        for attempt in range(3):
            try:
                telegram_fn(message, severity)
                logger.info(f"call_human delivered via Telegram (attempt {attempt+1})")
                return "telegram"
            except Exception as e:
                logger.warning(f"Telegram attempt {attempt+1} failed: {e}")
                if attempt < 2:
                    time.sleep(2 ** attempt)  # exponential backoff: 1s, 2s

    # 2. Try CLI/TTY
    if _try_cli(message, severity):
        logger.info("call_human delivered via CLI")
        return "cli"

    # 3. Durable outbox (never silently gives up)
    _try_outbox(message, severity, outbox_dir)
    logger.warning("call_human fell back to outbox")
    return "outbox"
