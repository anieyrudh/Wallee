"""OpenRouter LLM client — stateless, one call per agent cycle."""

import logging
import time

import httpx

logger = logging.getLogger(__name__)

_RETRYABLE = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
)

MAX_RETRIES = 3
RETRY_BACKOFF_S = 2.0

# Structured output schema for agent decisions
DECISION_SCHEMA = {
    "name": "agent_decision",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["ACTION", "WAIT", "CALL_HUMAN"]},
            "tool": {"type": "string", "description": "Tool name (ACTION only)"},
            "params": {"type": "object", "description": "Tool parameters (ACTION only)"},
            "reason": {"type": "string", "description": "1-2 sentence explanation"},
            "check_after_s": {"type": "number", "description": "Seconds until next check (WAIT only)"},
            "message": {"type": "string", "description": "Message for human (CALL_HUMAN only)"},
            "severity": {"type": "string", "enum": ["info", "warning", "critical"],
                         "description": "Alert severity (CALL_HUMAN only)"},
        },
        "required": ["type", "reason"],
        "additionalProperties": False,
    },
}


class LLMClient:
    def __init__(self, api_key: str, model: str = "google/gemini-3.1-pro-preview"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"

    def call(self, prompt: str, messages: list | None = None) -> str:
        """Send prompt to LLM, return raw response text.

        Features enabled via OpenRouter:
        - Structured outputs (json_schema) — guarantees valid decision JSON
        - Response healing plugin — fixes malformed JSON automatically
        - Web search plugin (native engine) — real-time web access
        - Prompt caching — 90% discount on repeated system prompts
        """
        if messages is None:
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Decide your next action."},
            ]

        # Prompt caching for system message (SOUL.md + LEARNED.md identical every cycle)
        if messages and messages[0].get("role") == "system":
            if isinstance(messages[0].get("content"), str):
                messages[0]["cache_control"] = {"type": "ephemeral"}

        payload = {
            "model": self.model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": DECISION_SCHEMA,
            },
            "plugins": [
                {"id": "response-healing"},
            ],
            "max_tokens": 2048,
        }

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = httpx.post(
                    self.base_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=60.0,
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"].get("content")
                if content is None:
                    finish = data["choices"][0].get("finish_reason", "")
                    logger.warning(f"LLM returned null content (finish_reason={finish})")
                    return ""
                return content

            except _RETRYABLE as e:
                logger.warning(f"LLM network error (attempt {attempt}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_S * attempt)
                    continue
                logger.error(f"LLM call failed after {MAX_RETRIES} retries: {e}")
                return ""

            except httpx.HTTPStatusError as e:
                logger.error(f"LLM HTTP {e.response.status_code}: {e.response.text[:200]}")
                return ""

            except Exception as e:
                logger.error(f"LLM call unexpected error: {e}")
                return ""

        return ""
