"""Built-in tool: escalate to human operator."""

from wallee.tools.decorator import tool
from wallee.human.call_human import call_human as _call_human


@tool(kind="actuator", requires_approval=False)
def call_human(message: str = "", severity: str = "info", whiteboard=None, **kwargs) -> dict:
    """Escalate to human operator. Fallback: Telegram -> CLI -> durable outbox."""
    if not message:
        return {"error": "message parameter required"}

    method = _call_human(message, severity)
    return {"status": "delivered", "method": method}
