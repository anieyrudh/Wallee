"""Builds the system prompt (cached) and user message (dynamic) for each agent cycle.

Architecture: two-part message array for optimal prompt caching. Text only — no images.
Vision is handled by the Gemini Flash Lite sensor; the main LLM reads structured
vision.* keys from the whiteboard.

CACHED PREFIX (system message — static within a job):
  SOUL.md, LEARNED.md, OBSERVATIONS.md, tool list, JOB_CONTEXT.md

DYNAMIC SUFFIX (user message — changes every cycle):
  Pending callout, phase banner, vision status, external changes,
  whiteboard state, human intent, episode, timestamp
"""

import json
import time
from pathlib import Path


MAX_PROMPT_EPISODE_CHARS = 160
MAX_PROMPT_REASON_CHARS = 120
MAX_PROMPT_STATE_JSON_CHARS = 80

_SKIP_KEYS = frozenset({
    "host.usb_devices", "host.network_interfaces", "printer.files",
    "printer.firmware", "printer.serial", "printer.model",
    "printer.nozzle_diameter", "agent.last_decision",
    "agent.heartbeat", "engine.heartbeat",
})


def _trim_text(value, limit: int) -> str:
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _summarize_payload(payload, limit: int = MAX_PROMPT_EPISODE_CHARS) -> str:
    if payload in (None, "", {}):
        return ""

    normalized = payload
    if isinstance(payload, str):
        try:
            normalized = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            normalized = payload

    if isinstance(normalized, (dict, list)):
        text = json.dumps(normalized, default=str, separators=(",", ":"))
    else:
        text = str(normalized)

    return _trim_text(text, limit)


# ── CACHED PREFIX (system message) ──────────────────────────────────


def build_system_prompt(
    knowledge: dict[str, str],
    tools: list[dict],
) -> str:
    """Build the system message — static within a job, cacheable.

    Contains: SOUL.md, LEARNED.md, OBSERVATIONS.md, tool list, JOB_CONTEXT.md.
    """
    sections = []

    # 1. Knowledge files in order
    for name in ["SOUL.md", "LEARNED.md", "OBSERVATIONS.md"]:
        content = knowledge.get(name, "")
        if content:
            sections.append(f"=== {name} ===\n{content}")

    # 2. Available tools
    sections.append("=== AVAILABLE TOOLS ===")
    if tools:
        for t in tools:
            approval = " [REQUIRES APPROVAL]" if t.get("requires_approval") else ""
            sections.append(f"  {t['name']}: {t['description']}{approval}")
    else:
        sections.append("  (no actuator tools available)")

    # 3. JOB_CONTEXT.md — only if a job is active
    job_ctx = knowledge.get("JOB_CONTEXT.md", "")
    if job_ctx:
        sections.append(f"=== JOB_CONTEXT.md ===\n{job_ctx}")

    # Static instruction at end of system prompt (cached with it)
    sections.append(
        "Respond with JSON. One sentence observation, one sentence reasoning."
    )

    return "\n\n".join(sections)


# ── DYNAMIC SUFFIX (user message) ──────────────────────────────────


def build_user_message(
    state: dict,
    episode: list[dict],
    intent: str | None,
    current_time: float,
    external_changes: list[str] | None = None,
    pending_callout: dict | None = None,
) -> str:
    """Build the dynamic user message text — changes every cycle.

    Contains: phase banner, pending callout, external changes,
    whiteboard state, human intent, episode, timestamp.
    """
    sections = []

    # 0. Pending callout — FIRST, before everything else
    if pending_callout and pending_callout.get("status") == "PENDING":
        msg = pending_callout.get("message", "")[:100]
        sections.append(
            "!!! YOUR LAST ESCALATION IS STILL PENDING — HUMAN HAS NOT RESPONDED YET !!!\n"
            f"Message: {msg}\n"
            "Do NOT claim the human acknowledged this. Do NOT re-escalate the same issue."
        )
    elif pending_callout and pending_callout.get("status") == "ACKNOWLEDGED":
        msg = pending_callout.get("message", "")[:100]
        sections.append(f"Human acknowledged your escalation: {msg}")
    else:
        sections.append("No pending escalation.")

    # 1. Phase banner
    phase = state.get("job.phase", "IDLE")
    detail = state.get("job.phase_detail", "")
    time_in_phase = state.get("job.time_in_phase_s", 0)
    sections.append(f"=== PHASE: {phase} ({detail}) — {time_in_phase}s ===")

    # 1b. Vision analysis (from Gemini Flash Lite sensor)
    vision_status = state.get("vision.status", "NO_DATA")
    vision_desc = state.get("vision.description", "")
    vision_conf = state.get("vision.confidence", "")
    if vision_status != "NO_DATA":
        sections.append(f"=== VISION: {vision_status} (conf: {vision_conf}) — {vision_desc} ===")

    # 2. External changes
    if external_changes:
        lines = ["!!! EXTERNAL CHANGES (not caused by Wallee) !!!"]
        for change in external_changes:
            lines.append(f"  - {change}")
        sections.append("\n".join(lines))

    # 3. Whiteboard sensor data (no images — just numbers and states)
    sections.append("=== WHITEBOARD STATE ===")
    if state:
        for key in sorted(state.keys()):
            if key.startswith("camera.") and key.endswith("_frame"):
                continue
            if key == "human.image":
                continue
            if key in _SKIP_KEYS:
                continue
            if key.startswith("camera.") and key.endswith("_frame_size"):
                continue
            val = state[key]
            if isinstance(val, str) and len(val) > 100:
                continue
            elif isinstance(val, (dict, list)):
                sections.append(f"  {key}: {_summarize_payload(val, MAX_PROMPT_STATE_JSON_CHARS)}")
            else:
                sections.append(f"  {key}: {val}")
    else:
        sections.append("  (no data)")

    # 4. Human intent
    if intent:
        sections.append(f"=== HUMAN INTENT ===\n{intent}")

    # 5. Episode context (recent actions)
    sections.append("=== RECENT ACTIONS (this episode) ===")
    if episode:
        for action in episode:
            status = action.get("status", "?")
            tool_name = action.get("tool", "?")
            reason = _trim_text(action.get("reason", ""), MAX_PROMPT_REASON_CHARS)
            error = _summarize_payload(action.get("error_json", ""))
            result = _summarize_payload(action.get("result_json", ""))
            line = f"  [{status}] {tool_name}"
            if reason:
                line += f" -- {reason}"
            if error:
                line += f" ERROR: {error}"
            if result and status == "DONE":
                line += f" -> {result}"
            sections.append(line)
    else:
        sections.append("  (no actions yet this episode)")

    # 6. Timestamp
    sections.append(f"=== TIME: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(current_time))} ===")

    return "\n\n".join(sections)


# ── MESSAGE BUILDER ─────────────────────────────────────────────────


def build_messages(
    system_prompt: str,
    user_text: str,
    state: dict,
    knowledge_dir: Path | None = None,
    data_dir: Path | None = None,
) -> list[dict]:
    """Build the full messages list for the LLM — text only, no images.

    Vision is handled by the Gemini Flash Lite sensor (vision_analysis.py).
    The main LLM reads structured vision.* keys from the whiteboard instead
    of interpreting raw camera frames directly. This makes calls faster and cheaper.

    Args:
        system_prompt: Cached system message from build_system_prompt().
        user_text: Dynamic user message from build_user_message().
        state: Whiteboard snapshot (unused for images, kept for signature compat).
        knowledge_dir: Path to knowledge/ dir (unused, kept for signature compat).
        data_dir: Path to data directory (unused, kept for signature compat).

    Returns:
        List of message dicts for the LLM client.
    """
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]


# ── BACKWARD COMPAT ─────────────────────────────────────────────────


def build_prompt(
    state: dict,
    episode: list[dict],
    intent: str | None,
    knowledge: dict[str, str],
    tools: list[dict],
    current_time: float,
    external_changes: list[str] | None = None,
    pending_callout: dict | None = None,
) -> str:
    """Legacy single-string prompt builder. Used by tests.

    In production, loop.py calls build_system_prompt + build_user_message
    + build_messages separately for caching.
    """
    sys = build_system_prompt(knowledge, tools)
    usr = build_user_message(state, episode, intent, current_time,
                             external_changes, pending_callout)
    return sys + "\n\n" + usr
