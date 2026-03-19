"""Direct emergency stop — bypasses engine gates entirely."""

import logging

import httpx

logger = logging.getLogger(__name__)


def estop_printer(prusalink_host: str, prusalink_api_key: str) -> bool:
    """Send M25 pause directly to printer. Returns True if printer was stopped."""
    if not prusalink_host:
        logger.warning("ESTOP: PRUSALINK_HOST not set, cannot pause printer")
        return False
    base = prusalink_host if prusalink_host.startswith("http") else f"http://{prusalink_host}"
    headers = {"X-Api-Key": prusalink_api_key} if prusalink_api_key else {}
    try:
        resp = httpx.post(f"{base}/api/v1/gcode", json={"command": "M25"},
                          headers=headers, timeout=5.0)
        if resp.status_code < 300:
            logger.critical("ESTOP: printer paused via M25")
            return True
        logger.warning(f"ESTOP: M25 failed (HTTP {resp.status_code}), trying cancel")
    except Exception as e:
        logger.warning(f"ESTOP: M25 failed ({e}), trying cancel")

    try:
        resp = httpx.delete(f"{base}/api/v1/job", headers=headers, timeout=5.0)
        logger.critical(f"ESTOP: cancel fallback HTTP {resp.status_code}")
        return resp.status_code < 300
    except Exception as e:
        logger.error(f"ESTOP: all stop attempts failed: {e}")
        return False
