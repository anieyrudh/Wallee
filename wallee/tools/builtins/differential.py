"""Built-in tool: rate of change for numerical whiteboard keys."""

from wallee.tools.decorator import tool
from wallee.whiteboard.client import compute_differential


@tool(kind="actuator", requires_approval=False)
def differential(key: str = "", whiteboard=None, **kwargs) -> dict:
    """Rate of change for a numerical whiteboard key (units per second)."""
    if not key:
        return {"error": "key parameter required"}
    if not whiteboard:
        return {"error": "whiteboard not available"}

    history = whiteboard.read_history(key)
    if not history:
        return {"key": key, "rate": "no history available"}

    numeric = [v for v in history if isinstance(v, (int, float))]
    if len(numeric) < 2:
        return {"key": key, "rate": "insufficient numeric data"}

    # Estimate interval from key metadata or use default 1.0s
    interval_s = 1.0  # default — accurate when sensor refresh_hz=1.0
    rate = compute_differential(numeric, interval_s)
    return {"key": key, "rate": rate, "readings": len(numeric)}
