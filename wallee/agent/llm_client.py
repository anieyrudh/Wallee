"""OpenRouter LLM client — stateless, one call per agent cycle."""

import json
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
MAX_VALIDATION_RETRIES = 1  # One retry only — don't burn tokens
_FALLBACK_WAIT = ('{"type": "WAIT", "observation": "LLM response error", '
                  '"reasoning": "Upstream failure, defaulting to WAIT", "check_after_s": 30}')

# Structured output schema for agent decisions (OpenAI strict mode)
DECISION_SCHEMA = {
    "name": "agent_decision",
    "strict": True,
    "schema": {
        "type": "object",
        "required": ["type", "observation", "reasoning"],
        "additionalProperties": False,
        "properties": {
            "type": {"type": "string", "enum": ["ACTION", "ACTION_CHAIN", "WAIT", "CALL_HUMAN"]},
            "observation": {"type": "string", "description": "One sentence: what you see right now"},
            "reasoning": {"type": "string", "description": "One sentence: why this decision"},
            "tool": {"type": ["string", "null"], "description": "Tool name for ACTION"},
            "params": {"type": ["object", "null"], "description": "Tool params for ACTION"},
            "actions": {
                "type": ["array", "null"],
                "description": "Action steps for ACTION_CHAIN",
                "items": {
                    "type": "object",
                    "required": ["tool", "params", "reasoning"],
                    "additionalProperties": False,
                    "properties": {
                        "tool": {"type": "string"},
                        "params": {"type": "object"},
                        "reasoning": {"type": "string"},
                    },
                },
            },
            "message": {"type": ["string", "null"], "description": "Message for CALL_HUMAN"},
            "severity": {"type": ["string", "null"], "enum": ["info", "warning", "critical", None]},
            "check_after_s": {"type": ["number", "null"], "description": "Seconds until next check for WAIT"},
        },
    },
}


class LLMClient:
    def __init__(self, api_key: str, model: str = "openai/gpt-5.4", **kwargs):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"

    def _validate_decision(self, raw_json: str, available_tools: list[str]) -> tuple[bool, str]:
        """Validate LLM output structure. Returns (is_valid, error_message)."""
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as e:
            # Try to extract JSON from surrounding text
            start = raw_json.find('{')
            end = raw_json.rfind('}')
            if start >= 0 and end > start:
                try:
                    data = json.loads(raw_json[start:end + 1])
                except json.JSONDecodeError:
                    return False, f"Invalid JSON: {e}"
            else:
                return False, "No JSON found in response"

        dtype = data.get("type")
        if dtype not in ("ACTION", "ACTION_CHAIN", "WAIT", "CALL_HUMAN"):
            return False, f"Invalid type: {dtype}"

        if not data.get("observation"):
            return False, "Missing observation"
        if not data.get("reasoning"):
            return False, "Missing reasoning"

        if dtype == "ACTION":
            tool = data.get("tool")
            if not tool:
                return False, "ACTION missing tool field"
            if available_tools and tool not in available_tools:
                return False, f"Unknown tool '{tool}'. Available: {available_tools}"

        if dtype == "ACTION_CHAIN":
            actions = data.get("actions")
            if not actions or not isinstance(actions, list):
                return False, "ACTION_CHAIN missing actions array"
            for i, action in enumerate(actions):
                tool = action.get("tool")
                if not tool:
                    return False, f"Chain step {i} missing tool"
                if available_tools and tool not in available_tools:
                    return False, f"Chain step {i} unknown tool '{tool}'. Available: {available_tools}"

        if dtype == "CALL_HUMAN":
            if not data.get("message"):
                return False, "CALL_HUMAN missing message"

        return True, ""

    def call(self, prompt: str, messages: list | None = None,
             available_tools: list[str] | None = None) -> str:
        """Send prompt to LLM, return raw response text.

        Features enabled via OpenRouter:
        - Structured outputs (json_schema) — guarantees valid decision JSON
        - Response healing plugin — fixes malformed JSON automatically
        - Prompt caching — 90% discount on repeated system prompts
        - Output validation with self-healing retry on malformed responses
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

        raw_content = self._send_request(payload)
        if raw_content is None:
            return _FALLBACK_WAIT

        # Validate output and retry once if invalid
        if available_tools is not None:
            is_valid, error = self._validate_decision(raw_content, available_tools)
            if not is_valid:
                logger.warning(f"LLM output invalid: {error}. Retrying with correction.")
                # Append correction and retry
                messages = list(messages)  # don't mutate caller's list
                messages.append({"role": "assistant", "content": raw_content})
                messages.append({"role": "user", "content": (
                    f"Your response was invalid: {error}. "
                    "Please fix and respond with valid JSON only."
                )})
                payload["messages"] = messages
                retry_content = self._send_request(payload)
                if retry_content is not None:
                    is_valid2, error2 = self._validate_decision(retry_content, available_tools)
                    if is_valid2:
                        return retry_content
                    logger.warning(f"LLM retry also invalid: {error2}. Giving up.")
                return '{"type": "WAIT", "observation": "LLM output invalid after retry", "reasoning": "Defaulting to WAIT for safety", "check_after_s": 30}'

        return raw_content

    def _send_request(self, payload: dict) -> str | None:
        """Send HTTP request to OpenRouter. Returns content string or None on failure."""
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
                    return None
                content = choices[0]["message"].get("content")
                if content is None:
                    finish = choices[0].get("finish_reason", "")
                    logger.warning(f"LLM returned null content (finish_reason={finish})")
                    return None
                return content

            except _RETRYABLE as e:
                logger.warning(f"LLM network error (attempt {attempt}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_S * attempt)
                    continue
                logger.error(f"LLM call failed after {MAX_RETRIES} retries: {e}")
                return None

            except httpx.HTTPStatusError as e:
                logger.error(f"LLM HTTP {e.response.status_code}: {e.response.text[:200]}")
                return None

            except Exception as e:
                logger.error(f"LLM call unexpected error: {e}")
                return None

        return None
