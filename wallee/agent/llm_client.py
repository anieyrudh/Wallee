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
_FALLBACK_WAIT = ('{"type": "WAIT", "observation": "LLM response error", '
                  '"reasoning": "Upstream failure, defaulting to WAIT", "check_after_s": 30}')

# Structured output schema for agent decisions
DECISION_SCHEMA = {
    "name": "agent_decision",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["ACTION", "WAIT", "CALL_HUMAN", "ACTION_CHAIN"]},
            "observation": {"type": "string", "description": "One sentence: what you see right now"},
            "reasoning": {"type": "string", "description": "One sentence: why you chose this decision"},
            "tool": {"type": "string", "description": "Tool name. Required if type=ACTION"},
            "params": {"type": "object", "description": "Tool parameters. Required if type=ACTION"},
            "message": {"type": "string", "description": "Message to human. Required if type=CALL_HUMAN"},
            "severity": {"type": "string", "enum": ["info", "warning", "critical"],
                         "description": "Alert severity. Required if type=CALL_HUMAN"},
            "check_after_s": {"type": "number", "description": "Seconds until next check. Required if type=WAIT"},
            "actions": {"type": "array", "items": {"type": "object"},
                        "description": "Ordered list of {tool, params} for ACTION_CHAIN"},
        },
        "required": ["type", "observation", "reasoning"],
        "additionalProperties": False,
    },
}


class LLMClient:
    def __init__(self, api_key: str, model: str = "google/gemini-3.1-pro-preview", **kwargs):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"

    def call(self, prompt: str, messages: list | None = None) -> str:
        """Send prompt to LLM, return raw response text.

        Features enabled via OpenRouter:
        - Structured outputs (json_schema) — guarantees valid decision JSON
        - Response healing plugin — fixes malformed JSON automatically
        - Prompt caching — 90% discount on repeated system prompts
        """
        if messages is None:
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Decide your next action."},
            ]

        # Prompt caching for system message (static knowledge — identical between cycles)
        if messages and messages[0].get("role") == "system":
            content = messages[0].get("content")
            if isinstance(content, str):
                messages[0]["cache_control"] = {"type": "ephemeral"}
            elif isinstance(content, list):
                # List content (with vision blocks) — add cache_control to last text block
                for block in reversed(content):
                    if isinstance(block, dict) and block.get("type") == "text":
                        block["cache_control"] = {"type": "ephemeral"}
                        break

        payload = {
            "model": self.model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": DECISION_SCHEMA,
            },
            "plugins": [{"id": "response-healing"}],
            "stream": False,
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
                choices = data.get("choices")
                if not choices or not isinstance(choices, list) or len(choices) == 0:
                    logger.warning(f"LLM response missing choices: {list(data.keys())}")
                    return ('{"type": "WAIT", "observation": "LLM response malformed", '
                            '"reasoning": "Defaulting to WAIT", "check_after_s": 30}')
                content = choices[0]["message"].get("content")
                if content is None:
                    finish = choices[0].get("finish_reason", "")
                    logger.warning(f"LLM returned null content (finish_reason={finish})")
                    return _FALLBACK_WAIT
                return content

            except _RETRYABLE as e:
                logger.warning(f"LLM network error (attempt {attempt}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_S * attempt)
                    continue
                logger.error(f"LLM call failed after {MAX_RETRIES} retries: {e}")
                return _FALLBACK_WAIT

            except httpx.HTTPStatusError as e:
                logger.error(f"LLM HTTP {e.response.status_code}: {e.response.text[:200]}")
                return _FALLBACK_WAIT

            except Exception as e:
                logger.error(f"LLM call unexpected error: {e}")
                return _FALLBACK_WAIT

        return _FALLBACK_WAIT
