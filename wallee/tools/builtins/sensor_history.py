"""Built-in tool: raw ring buffer values for deeper analysis."""

from wallee.tools.decorator import tool


@tool(kind="actuator", requires_approval=False)
def get_sensor_history(key: str = "", depth: int = 30, whiteboard=None, **kwargs) -> dict:
    """Raw ring buffer values for deeper analysis."""
    if not key:
        return {"error": "key parameter required"}
    if not whiteboard:
        return {"error": "whiteboard not available"}

    history = whiteboard.read_history(key)
    # Trim to requested depth
    values = history[:depth]
    return {
        "key": key,
        "values": values,
        "count": len(values),
        "total_available": len(history),
    }
