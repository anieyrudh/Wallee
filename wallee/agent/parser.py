"""Parse LLM JSON output into typed decisions. NEVER crashes."""

import json
import logging
from dataclasses import dataclass

from wallee.config import (
    DEFAULT_CHECK_INTERVAL_S,
    DEFAULT_MAX_CHECK_INTERVAL_IDLE_S,
    DEFAULT_MAX_CHECK_INTERVAL_S,
    DEFAULT_MIN_CHECK_INTERVAL_S,
)

logger = logging.getLogger(__name__)

VALID_TYPES = {"ACTION", "WAIT", "CALL_HUMAN"}

# Interval clamping — enforced in code, not by the LLM
MIN_CHECK_INTERVAL = DEFAULT_MIN_CHECK_INTERVAL_S
MAX_CHECK_INTERVAL = DEFAULT_MAX_CHECK_INTERVAL_S
MAX_CHECK_INTERVAL_IDLE = DEFAULT_MAX_CHECK_INTERVAL_IDLE_S
DEFAULT_CHECK_INTERVAL = DEFAULT_CHECK_INTERVAL_S


def configure_check_intervals(
    min_check_interval: int,
    max_check_interval: int,
    max_check_interval_idle: int,
    default_check_interval: int,
):
    """Update parser interval bounds from central runtime config."""
    global MIN_CHECK_INTERVAL, MAX_CHECK_INTERVAL, MAX_CHECK_INTERVAL_IDLE, DEFAULT_CHECK_INTERVAL
    MIN_CHECK_INTERVAL = int(min_check_interval)
    MAX_CHECK_INTERVAL = int(max_check_interval)
    MAX_CHECK_INTERVAL_IDLE = int(max_check_interval_idle)
    DEFAULT_CHECK_INTERVAL = int(default_check_interval)


@dataclass
class Decision:
    type: str  # ACTION, WAIT, CALL_HUMAN
    tool: str = ""
    params: dict = None
    reason: str = ""
    check_after_s: float = 60.0
    message: str = ""
    severity: str = "info"

    def __post_init__(self):
        if self.params is None:
            self.params = {}


def _default_wait(reason: str) -> Decision:
    """Return a safe WAIT decision with the given reason."""
    return Decision(type="WAIT", reason=reason, check_after_s=DEFAULT_CHECK_INTERVAL)


def _strip_markdown_fences(raw: str) -> str:
    """Strip markdown code fences if present (```json ... ```)."""
    stripped = raw.strip()
    if stripped.startswith("```"):
        first_newline = stripped.index("\n") if "\n" in stripped else len(stripped)
        stripped = stripped[first_newline + 1:]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3].rstrip()
    return stripped


def clamp_check_interval(raw_interval: float, printer_state: str | None = None) -> float:
    """Clamp check_after_s to a safe range based on printer state.

    Args:
        raw_interval: The LLM's requested interval.
        printer_state: Current printer.state from whiteboard (or None).

    Returns:
        Clamped interval in seconds.
    """
    active_states = {"PRINTING", "PAUSED", "ATTENTION"}
    if printer_state and printer_state.upper() in active_states:
        max_interval = MAX_CHECK_INTERVAL
    else:
        max_interval = MAX_CHECK_INTERVAL_IDLE

    clamped = max(MIN_CHECK_INTERVAL, min(float(raw_interval), max_interval))

    if clamped != raw_interval:
        logger.info(f"LLM requested check_after_s={raw_interval}, clamped to {clamped}")

    return clamped


def parse_llm_output(raw: str, printer_state: str | None = None) -> Decision:
    """Parse LLM response JSON into a Decision. Never raises.

    Args:
        raw: Raw LLM response string.
        printer_state: Current printer.state for interval clamping.

    Rules from spec:
    - Invalid JSON → WAIT
    - Unknown type → WAIT
    - Missing required fields → WAIT
    - Empty string → WAIT
    """
    if not raw or not raw.strip():
        logger.warning("Empty LLM response, defaulting to WAIT")
        return _default_wait("empty LLM response")

    cleaned = _strip_markdown_fences(raw)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning(f"Invalid JSON from LLM: {e}")
        return _default_wait(f"invalid JSON: {e}")

    if not isinstance(data, dict):
        logger.warning(f"LLM returned non-dict: {type(data)}")
        return _default_wait("LLM response is not a JSON object")

    decision_type = data.get("type", "").upper()

    if decision_type not in VALID_TYPES:
        logger.warning(f"Unknown decision type: {data.get('type')}")
        return _default_wait(f"unknown type: {data.get('type')}")

    if decision_type == "ACTION":
        tool = data.get("tool", "")
        if not tool:
            logger.warning("ACTION missing tool field")
            return _default_wait("ACTION missing tool")
        return Decision(
            type="ACTION",
            tool=tool,
            params=data.get("params", {}),
            reason=data.get("reason", ""),
        )

    if decision_type == "WAIT":
        raw_interval = data.get("check_after_s", DEFAULT_CHECK_INTERVAL)
        clamped = clamp_check_interval(raw_interval, printer_state)
        return Decision(
            type="WAIT",
            reason=data.get("reason", ""),
            check_after_s=clamped,
        )

    if decision_type == "CALL_HUMAN":
        message = data.get("message", "")
        if not message:
            logger.warning("CALL_HUMAN missing message")
            return _default_wait("CALL_HUMAN missing message")
        return Decision(
            type="CALL_HUMAN",
            message=message,
            severity=data.get("severity", "info"),
            reason=data.get("reason", ""),
        )

    return _default_wait("parser fallthrough")
