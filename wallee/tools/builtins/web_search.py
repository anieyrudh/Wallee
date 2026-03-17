"""Built-in tool: web search for technical information."""

from wallee.tools.decorator import tool


@tool(kind="actuator", requires_approval=False)
def web_search(query: str = "", whiteboard=None, **kwargs) -> dict:
    """Search the web for technical information, datasheets, troubleshooting.

    v1: Placeholder. Future: integrate with a search API.
    """
    if not query:
        return {"error": "query parameter required"}

    return {
        "status": "not_implemented",
        "message": f"Web search not yet available. Query: {query}",
    }
