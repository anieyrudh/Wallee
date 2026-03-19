"""Built-in tool: web search via OpenRouter."""

import logging

import httpx

from wallee.tools.decorator import tool

logger = logging.getLogger(__name__)

_MAX_RESULT_CHARS = 2000
_api_key = ""
_model = "google/gemini-3.1-pro-preview"


def configure_web_search(api_key: str, model: str):
    """Set API key and model at boot from central config."""
    global _api_key, _model
    _api_key = api_key
    _model = model


@tool(kind="actuator", requires_approval=False, gate_bypass=True)
def web_search(query: str = "", whiteboard=None, **kwargs) -> dict:
    """Search the web for 3D printing troubleshooting, datasheets, or technical info.

    Makes a separate OpenRouter call with the web search plugin enabled.
    Returns a summarized answer capped at 2000 characters.
    """
    if not query:
        return {"error": "query parameter required"}

    if not _api_key:
        return {"error": "OPENROUTER_API_KEY not configured"}

    payload = {
        "model": _model,
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
                "Authorization": f"Bearer {_api_key}",
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
