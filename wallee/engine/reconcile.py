"""Reconcile loop — crash recovery for in-flight actions."""

import logging
from wallee.ledger.db import Ledger
from wallee.ledger.diary import Diary

logger = logging.getLogger(__name__)


def reconcile(ledger: Ledger, get_diary, call_human_fn=None):
    """Check for in-flight actions from pre-crash and resolve them.

    Run on boot and periodically (every 500ms via engine).

    Args:
        ledger: The ledger database.
        get_diary: Callable(device_group) -> Diary.
        call_human_fn: Callable(message, severity) for escalation.
    """
    dispatched = ledger.get_by_status("DISPATCHED")
    for action in dispatched:
        action_id = action["action_id"]
        device_group = action["device_group"]
        idempotency_key = action["idempotency_key"]
        tool_name = action["tool"]

        diary = get_diary(device_group)
        diary_status = diary.lookup(idempotency_key)

        if diary_status == "SUCCESS":
            # Tool completed but ledger wasn't updated (crash after diary write)
            result = diary.get_result(idempotency_key) or {}
            ledger.set_status(action_id, "DONE", result=result)
            logger.info(f"Reconciled {action_id} ({tool_name}): SUCCESS (diary had result)")

        elif diary_status == "FAILED":
            result = diary.get_result(idempotency_key) or {}
            ledger.set_status(action_id, "FAILED", error=result)
            logger.info(f"Reconciled {action_id} ({tool_name}): FAILED (diary had error)")

        elif diary_status == "IN_FLIGHT":
            # DANGEROUS: command was sent to hardware, outcome unknown
            ledger.set_status(action_id, "UNKNOWN")
            msg = f"UNKNOWN state: {tool_name} (action {action_id}) was in-flight when system crashed"
            logger.critical(msg)
            if call_human_fn:
                call_human_fn(msg, "critical")

        elif diary_status is None:
            # Diary has no record — command never reached hardware
            ledger.set_status(action_id, "FAILED", error={"error": "never dispatched (crash before diary write)"})
            logger.warning(f"Reconciled {action_id} ({tool_name}): never dispatched")
