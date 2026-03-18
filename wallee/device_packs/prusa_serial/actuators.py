"""Actuator tools for Prusa printer via USB serial.

Most sensor data comes from the metrics stream (prusa_metrics pack).
Serial is only used for commands that require reading the response:
- read_endstops (M119) — only available via serial
- send_gcode — generic G-code execution with response capture
"""

import logging
import re

from wallee.bus.serial import SerialBus, find_serial_port
from wallee.tools.decorator import tool

logger = logging.getLogger(__name__)

# Prusa-specific constants — kept in the device pack, not in the generic bus
PRUSA_BY_ID_PATTERN = "Prusa"       # matches /dev/serial/by-id/*Prusa*
PRUSA_BLACKLISTED = frozenset({"M997", "M112", "M502", "M500"})
PRUSA_ALLOWED_DIAGNOSTIC_GCODE = frozenset({"M105", "M114", "M115", "M119", "M503"})
PRUSA_M115_SPAM = frozenset({
    "FIRMWARE_NAME:", "SOURCE_CODE_URL:", "PROTOCOL_VERSION:",
    "MACHINE_TYPE:", "EXTRUDER_COUNT:", "UUID:", "Cap:",
})

_serial: SerialBus | None = None


def _find_prusa_port() -> str | None:
    """Find Prusa serial port via /dev/serial/by-id/ symlinks."""
    return find_serial_port(by_id_pattern=PRUSA_BY_ID_PATTERN, fallback_path="/dev/ttyACM0")


def _get_serial() -> SerialBus:
    """Get or create the shared serial bus with Prusa-specific config."""
    global _serial
    if _serial is None:
        _serial = SerialBus(
            blacklisted_commands=PRUSA_BLACKLISTED,
            spam_patterns=PRUSA_M115_SPAM,
            find_port_fn=_find_prusa_port,
        )
        logger.info("Prusa serial bus initialized")
    return _serial


def _normalize_diagnostic_gcode(command: str) -> tuple[str, str]:
    """Validate that send_gcode only permits a single allowed read-only command."""
    if not command or not command.strip():
        return "", "command is required"

    if any(ch in command for ch in ("\n", "\r", ";")):
        return "", "only a single bare diagnostic G-code command is allowed"

    normalized = " ".join(command.strip().upper().split())
    token = normalized.split()[0]
    if normalized != token:
        return "", "diagnostic G-code must not include parameters or comments"

    if token not in PRUSA_ALLOWED_DIAGNOSTIC_GCODE:
        allowed = ", ".join(sorted(PRUSA_ALLOWED_DIAGNOSTIC_GCODE))
        return "", f"command {token} is not allowed; permitted diagnostic commands: {allowed}"

    return token, ""


@tool(kind="actuator", requires_approval=False, max_proposal_age_ms=10000)
def read_endstops(whiteboard=None, **kwargs) -> dict:
    """Read all endstop states via M119. Only available through serial.

    Returns x_min, x_max, y_min, y_max, z_min, z_max states (open/TRIGGERED).
    """
    serial = _get_serial()
    result = serial.send_command("M119")

    if "error" in result:
        return result

    endstops = {}
    for line in result.get("lines", []):
        # Parse lines like "x_min: open" or "z_max: TRIGGERED"
        m = re.match(r'(\w+_(?:min|max)):\s*(\w+)', line)
        if m:
            endstops[f"printer.endstop_{m.group(1)}"] = m.group(2)

    if not endstops:
        return {"error": "No endstop data in M119 response"}

    return endstops


@tool(kind="actuator", requires_approval=False, max_proposal_age_ms=30000)
def send_gcode(whiteboard=None, command: str = "", **kwargs) -> dict:
    """Send a single allowed diagnostic G-code command and return the response.

    This path is intentionally restricted to a small read-only allowlist.
    State-changing motion, temperature, EEPROM, reset, and emergency commands
    must use dedicated tools instead of raw G-code.

    Args:
        command: One bare allowed command (for example: 'M105' or 'M114').
    """
    normalized, error = _normalize_diagnostic_gcode(command)
    if error:
        return {"error": error}

    serial = _get_serial()
    result = serial.send_command(normalized)

    if "error" in result:
        return result

    return {
        "status": "success",
        "action": "send_gcode",
        "command": normalized,
        "response": result.get("lines", []),
    }
