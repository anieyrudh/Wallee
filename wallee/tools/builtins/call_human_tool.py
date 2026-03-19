"""Built-in tool: escalate to human operator."""

from wallee.tools.decorator import tool
from wallee.human.call_human import call_human as _fallback_call_human

_call_human_fn = None


def set_call_human_fn(fn):
    """Inject a Telegram-backed call_human function at boot."""
    global _call_human_fn
    _call_human_fn = fn


@tool(kind="actuator", requires_approval=False)
def call_human(message: str = "", severity: str = "info", whiteboard=None, **kwargs) -> dict:
    """Escalate to human operator. Fallback: Telegram -> CLI -> durable outbox."""
    if not message:
        return {"error": "message parameter required"}

    if _call_human_fn:
        try:
            _call_human_fn(message, severity)
            return {"status": "delivered", "method": "telegram"}
        except Exception:
            pass

    method = _fallback_call_human(message, severity)
    return {"status": "delivered", "method": method}
