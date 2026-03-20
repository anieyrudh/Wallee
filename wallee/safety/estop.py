"""Direct emergency stop transport — bypasses engine gates entirely."""

import logging

import httpx

logger = logging.getLogger(__name__)


def estop_printer(host: str, api_key: str, primary_request: dict | None = None,
                  fallback_request: dict | None = None) -> bool:
    """Send a direct stop request to the configured control endpoint."""
    if not host:
        logger.warning("ESTOP: control host not set, cannot send stop command")
        return False
    base = host if host.startswith("http") else f"http://{host}"
    headers = {"X-Api-Key": api_key} if api_key else {}
    primary = primary_request or {"method": "POST", "path": "/api/v1/gcode", "json": {"command": "M25"}}
    fallback = fallback_request or {"method": "DELETE", "path": "/api/v1/job"}
    try:
        method = str(primary.get("method", "POST")).upper()
        path = primary.get("path", "")
        payload = primary.get("json")
        resp = httpx.request(method, f"{base}{path}", json=payload, headers=headers, timeout=5.0)
        if resp.status_code < 300:
            logger.critical("ESTOP: primary stop request succeeded")
            return True
        logger.warning(f"ESTOP: primary stop request failed (HTTP {resp.status_code}), trying fallback")
    except Exception as e:
        logger.warning(f"ESTOP: primary stop request failed ({e}), trying fallback")

    try:
        method = str(fallback.get("method", "DELETE")).upper()
        path = fallback.get("path", "")
        payload = fallback.get("json")
        resp = httpx.request(method, f"{base}{path}", json=payload, headers=headers, timeout=5.0)
        logger.critical(f"ESTOP: fallback stop request HTTP {resp.status_code}")
        return resp.status_code < 300
    except Exception as e:
        logger.error(f"ESTOP: all stop attempts failed: {e}")
        return False
