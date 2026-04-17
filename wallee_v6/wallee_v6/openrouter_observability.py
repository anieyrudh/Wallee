"""Helpers for normalizing OpenRouter provider metadata."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_openrouter_metadata(
    *,
    payload: dict[str, Any] | None,
    requested_model: str | None,
    latency_ms: float | None,
    response_size_bytes: int | None,
    retry_count: int = 0,
    schema_valid: bool | None,
    request_started_at: str | None,
    response_received_at: str | None,
    headers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = payload or {}
    usage = response.get("usage")
    usage_payload = usage if isinstance(usage, dict) else None
    header_map = {str(key).lower(): value for key, value in (headers or {}).items()}
    provider = response.get("provider")
    choices = response.get("choices") or []
    first_choice = choices[0] if isinstance(choices, list) and choices else {}
    first_choice = first_choice if isinstance(first_choice, dict) else {}
    finish_reason = first_choice.get("finish_reason")

    return {
        "request_id": _coerce_str(response.get("id")) or _coerce_str(header_map.get("x-request-id")),
        "generation_id": _coerce_str(response.get("id")),
        "provider_name": _provider_name(provider) or _coerce_str(header_map.get("x-openrouter-provider")),
        "model": _coerce_str(response.get("model")) or _coerce_str(requested_model),
        "latency_ms": _coerce_float(latency_ms),
        "generation_time_ms": _lookup_float(
            response,
            ("generation_time_ms",),
            ("generation_time",),
            ("provider_latency_ms",),
            ("usage", "generation_time_ms"),
            ("usage", "generation_time"),
        ),
        "tokens_prompt": _lookup_int(response, ("usage", "prompt_tokens"), ("prompt_tokens",)),
        "tokens_completion": _lookup_int(response, ("usage", "completion_tokens"), ("completion_tokens",)),
        "native_tokens_prompt": _lookup_int(response, ("usage", "native_tokens_prompt"), ("native_tokens_prompt",)),
        "native_tokens_completion": _lookup_int(response, ("usage", "native_tokens_completion"), ("native_tokens_completion",)),
        "native_tokens_reasoning": _lookup_int(response, ("usage", "native_tokens_reasoning"), ("native_tokens_reasoning",)),
        "usage": usage_payload,
        "finish_reason": _coerce_str(finish_reason),
        "retry_count": int(retry_count),
        "schema_valid": schema_valid,
        "response_size_bytes": _coerce_int(response_size_bytes),
        "request_started_at": request_started_at,
        "response_received_at": response_received_at,
        "raw_provider_metadata": {
            "id": response.get("id"),
            "provider": response.get("provider"),
            "model": response.get("model"),
            "usage": usage_payload,
            "finish_reason": finish_reason,
            "headers": headers or {},
        },
    }


def _provider_name(provider: Any) -> str | None:
    if isinstance(provider, str):
        return provider.strip() or None
    if isinstance(provider, dict):
        for key in ("name", "provider_name", "slug"):
            value = _coerce_str(provider.get(key))
            if value:
                return value
    return None


def _lookup_value(payload: Any, path: tuple[str, ...]) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _lookup_int(payload: dict[str, Any], *paths: tuple[str, ...]) -> int | None:
    for path in paths:
        value = _lookup_value(payload, path)
        coerced = _coerce_int(value)
        if coerced is not None:
            return coerced
    return None


def _lookup_float(payload: dict[str, Any], *paths: tuple[str, ...]) -> float | None:
    for path in paths:
        value = _lookup_value(payload, path)
        coerced = _coerce_float(value)
        if coerced is not None:
            return coerced
    return None


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None
