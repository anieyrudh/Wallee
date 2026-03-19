"""Built-in tool: rate of change for numerical whiteboard keys."""

from wallee.tools.decorator import tool
from wallee.whiteboard.client import compute_differential


@tool(kind="actuator", requires_approval=False, gate_bypass=True)
def differential(key: str = "", whiteboard=None, **kwargs) -> dict:
    """Rate of change for a numerical whiteboard key (units per second)."""
    if not key:
        return {"error": "key parameter required"}
    if not whiteboard:
        return {"error": "whiteboard not available"}

    history = whiteboard.read_history(key)
    if not history:
        return {"key": key, "rate": "no history available"}

    timestamps = []
    if hasattr(whiteboard, "read_history_timestamps"):
        timestamps = whiteboard.read_history_timestamps(key)

    numeric = []
    numeric_ts = []
    for index, value in enumerate(history):
        if not isinstance(value, (int, float)):
            continue
        numeric.append(value)
        if index < len(timestamps):
            numeric_ts.append(timestamps[index])

    if len(numeric) < 2:
        return {"key": key, "rate": "insufficient numeric data"}

    if len(numeric_ts) >= 2 and numeric_ts[0] > numeric_ts[-1]:
        time_span = numeric_ts[0] - numeric_ts[-1]
        if time_span > 0:
            delta = numeric[0] - numeric[-1]
            rate = f"{delta / time_span:+.3f}/s"
            return {"key": key, "rate": rate, "readings": len(numeric)}

    rate = compute_differential(numeric, 1.0)
    return {"key": key, "rate": rate, "readings": len(numeric)}
