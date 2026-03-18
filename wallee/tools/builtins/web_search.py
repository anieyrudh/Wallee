"""Built-in tool: web search via OpenRouter with Exa plugin."""

import logging
import os

import httpx

from wallee.tools.decorator import tool

logger = logging.getLogger(__name__)

_MAX_RESULT_CHARS = 2000


@tool(kind="actuator", requires_approval=False)
def web_search(query: str = "", whiteboard=None, **kwargs) -> dict:
    """Search the web for 3D printing troubleshooting, datasheets, or technical info.

    Makes a separate OpenRouter call with the Exa web search plugin enabled.
    Returns a summarized answer capped at 2000 characters.
    """
    if not query:
        return {"error": "query parameter required"}

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return {"error": "OPENROUTER_API_KEY not configured"}

    model = os.environ.get("OPENROUTER_MODEL", "google/gemini-3.1-pro-preview")

    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": f"Search the web and answer concisely: {query}"},
        ],
        "plugins": [
            {"id": "web", "max_results": 3},
        ],
        "stream": False,
        "max_tokens": 1024,
    }

    try:
        response = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"].get("content", "")
        if not content:
            return {"error": "empty response from web search"}
        # Cap result length
        if len(content) > _MAX_RESULT_CHARS:
            content = content[:_MAX_RESULT_CHARS] + "... (truncated)"
        return {"status": "success", "query": query, "answer": content}
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        return {"error": f"web search failed: {e}"}
