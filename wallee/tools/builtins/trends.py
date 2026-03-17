"""Built-in tool: trend analysis for whiteboard keys."""

from wallee.tools.decorator import tool
from wallee.whiteboard.client import compute_trend


@tool(kind="actuator", requires_approval=False)
def trends(key: str = "", whiteboard=None, **kwargs) -> dict:
    """Trend analysis for a whiteboard key: rising/falling/stable + magnitude."""
    if not key:
        return {"error": "key parameter required"}
    if not whiteboard:
        return {"error": "whiteboard not available"}

    history = whiteboard.read_history(key)
    if not history:
        return {"key": key, "trend": "no history available"}

    numeric = [v for v in history if isinstance(v, (int, float))]
    if len(numeric) < 2:
        return {"key": key, "trend": "insufficient numeric data"}

    trend = compute_trend(numeric)
    return {"key": key, "trend": trend, "readings": len(numeric)}
