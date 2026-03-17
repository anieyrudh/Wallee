"""OpenRouter LLM client — stateless, one call per agent cycle."""

import logging
import time

import httpx

logger = logging.getLogger(__name__)

# Network errors worth retrying (DNS, connection refused, connection reset)
_RETRYABLE = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
)

MAX_RETRIES = 3
RETRY_BACKOFF_S = 2.0


class LLMClient:
    def __init__(self, api_key: str, model: str = "google/gemini-3.1-pro-preview"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"

    def call(self, prompt: str, messages: list | None = None) -> str:
        """Send prompt to LLM, return raw response text.

        Args:
            prompt: System prompt content.
            messages: Optional full message list (for vision content blocks).
                      If provided, overrides the default system+user messages.

        Returns empty string on any failure. The agent parser handles this safely.
        Retries up to 3 times on network errors (DNS, connection, timeout).
        Does NOT retry on HTTP 4xx/5xx (those are server-side, not transient).
        """
        if messages is None:
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Decide your next action."},
            ]

        # Mark system message for prompt caching (SOUL.md + LEARNED.md are
        # identical every cycle — 90% cost reduction on cached tokens)
        if messages and len(messages) > 0 and messages[0].get("role") == "system":
            if isinstance(messages[0].get("content"), str):
                messages[0]["cache_control"] = {"type": "ephemeral"}

        payload = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "max_tokens": 2048,  # Gemini 3.1 Pro uses reasoning tokens — needs headroom
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
                    # Gemini reasoning models may exhaust tokens on thinking
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
                # Do NOT retry on 4xx/5xx — these are server-side issues
                logger.error(f"LLM HTTP {e.response.status_code}: {e.response.text[:200]}")
                return ""

            except Exception as e:
                logger.error(f"LLM call unexpected error: {e}")
                return ""

        return ""
