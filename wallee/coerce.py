"""Shared scalar coercion and fact-text helpers.

These existed as five-plus per-module clones (`_float_or_none` and friends);
this module is the single definition. Behavior notes that matter:

- ``float_or_none``/``int_or_none`` treat ``None``, ``""``, and unparseable
  input as ``None`` — predicates and prompt fields fail closed, never raise.
- ``string_or_none`` collapses whitespace-only to ``None``.
- ``split_pipe`` is the one pipe-joined multi-value fact codec (empty and
  ``None`` decode to ``[]``; empty segments are dropped).
- the ``normalize_*`` helpers admit only their closed vocabularies; anything
  else is ``None``, never passed through.
"""

from __future__ import annotations

from typing import Any


def float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def split_pipe(value: Any) -> list[str]:
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    return [item for item in text.split("|") if item]


def normalize_strength(value: Any) -> str | None:
    text = string_or_none(value)
    return text if text in {"weak", "moderate", "strong"} else None


def normalize_issue_level(value: Any) -> str | None:
    text = string_or_none(value)
    return text if text in {"low", "medium", "high"} else None


def normalize_comparison_delta(value: Any) -> str | None:
    text = string_or_none(value)
    return text if text in {"better", "same", "worse", "unknown"} else None
