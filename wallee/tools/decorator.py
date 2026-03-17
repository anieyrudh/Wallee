"""@tool decorator that attaches metadata to sensor and actuator functions."""

from functools import wraps
from typing import Any


def tool(
    kind: str,
    refresh_hz: float | None = None,
    history_depth: int = 0,
    ttl_ms: int | None = None,
    requires_approval: bool = False,
    max_proposal_age_ms: int = 30000,
    safety_limits: dict | None = None,
    bus: str | None = None,
    address: Any = None,
):
    """Decorator that marks a method as a Wallee tool (sensor or actuator).

    For sensors:
        @tool(kind="sensor", refresh_hz=1.0, history_depth=10)

    For actuators:
        @tool(kind="actuator", requires_approval=True)
    """
    if kind not in ("sensor", "actuator"):
        raise ValueError(f"tool kind must be 'sensor' or 'actuator', got '{kind}'")

    if kind == "sensor" and refresh_hz is None:
        raise ValueError("sensor tools must specify refresh_hz")

    # Auto-calculate TTL for sensors: 2x the refresh period
    computed_ttl_ms = ttl_ms
    if kind == "sensor" and computed_ttl_ms is None and refresh_hz:
        computed_ttl_ms = int((1000 / refresh_hz) * 2)

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)

        # Attach metadata
        wrapper._tool_meta = {
            "kind": kind,
            "name": fn.__name__,
            "doc": fn.__doc__ or "",
            "refresh_hz": refresh_hz,
            "history_depth": history_depth,
            "ttl_ms": computed_ttl_ms,
            "requires_approval": requires_approval,
            "max_proposal_age_ms": max_proposal_age_ms,
            "safety_limits": safety_limits or {},
            "bus": bus,
            "address": address,
        }
        return wrapper

    return decorator
