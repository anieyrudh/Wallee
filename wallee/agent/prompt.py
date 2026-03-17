"""Builds the system prompt and message list for each agent cycle.

Supports vision: camera frames and human photos are included as
image_url content blocks in the OpenRouter messages format.
"""

import json
import time
from pathlib import Path


def build_prompt(
    state: dict,
    episode: list[dict],
    intent: str | None,
    knowledge: dict[str, str],
    tools: list[dict],
    current_time: float,
    external_changes: list[str] | None = None,
) -> str:
    """Assemble the full system prompt for one LLM call.

    Args:
        state: Whiteboard snapshot with trend annotations.
        episode: Recent actions from ledger (since last WAIT/CALL_HUMAN).
        intent: Current human intent string, or None.
        knowledge: Dict of filename → content (SOUL.md, HARDWARE.md, LEARNED.md).
        tools: Tool descriptions from registry.list_for_llm().
        current_time: time.time() for the prompt.
        external_changes: List of detected external changes (from ExternalChangeDetector).
    """
    sections = []

    # 0. External changes (TOP of prompt, before everything else)
    if external_changes:
        from wallee.agent.change_detector import ExternalChangeDetector
        detector = ExternalChangeDetector()
        sections.append(detector.format_for_prompt(external_changes))

    # 1. Knowledge files (SOUL.md comes first — it's the mission briefing)
    for name in ["SOUL.md", "HARDWARE.md", "LEARNED.md"]:
        content = knowledge.get(name, "")
        if content:
            sections.append(f"=== {name} ===\n{content}")

    # 2. Current whiteboard state (stripped — skip verbose/binary keys)
    _SKIP_KEYS = frozenset({
        "host.usb_devices", "host.network_interfaces", "printer.files",
        "printer.firmware", "printer.serial", "printer.model",
        "printer.nozzle_diameter",
    })
    sections.append("=== WHITEBOARD STATE ===")
    if state:
        for key in sorted(state.keys()):
            if key.startswith("camera.") and key.endswith("_frame"):
                continue  # images sent as vision blocks, not text
            if key == "human.image":
                continue
            if key in _SKIP_KEYS:
                continue
            if key.startswith("camera.") and key.endswith("_frame_size"):
                continue
            val = state[key]
            if isinstance(val, str) and len(val) > 100:
                continue  # skip long strings
            elif isinstance(val, (dict, list)):
                sections.append(f"  {key}: {json.dumps(val, default=str)[:80]}")
            else:
                sections.append(f"  {key}: {val}")
    else:
        sections.append("  (no data)")

    # 3. Human intent
    if intent:
        sections.append(f"=== HUMAN INTENT ===\n{intent}")

    # 4. Episode context (recent actions)
    sections.append("=== RECENT ACTIONS (this episode) ===")
    if episode:
        for action in episode:
            status = action.get("status", "?")
            tool_name = action.get("tool", "?")
            reason = action.get("reason", "")
            error = action.get("error_json", "")
            result = action.get("result_json", "")
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

    # 5. Available tools
    sections.append("=== AVAILABLE TOOLS ===")
    if tools:
        for t in tools:
            approval = " [REQUIRES APPROVAL]" if t.get("requires_approval") else ""
            sections.append(f"  {t['name']}: {t['description']}{approval}")
    else:
        sections.append("  (no actuator tools available)")

    # 6. Instructions
    sections.append("=== INSTRUCTIONS ===")
    sections.append(
        "You are an autonomous agent controlling hardware. "
        "Respond with exactly one JSON object.\n"
        "Options:\n"
        '  {"type": "ACTION", "tool": "<name>", "params": {}, "reason": "<why>"}\n'
        '  {"type": "WAIT", "reason": "<why>", "check_after_s": <seconds>}\n'
        '  {"type": "CALL_HUMAN", "message": "<what>", "severity": "info|warning|critical"}\n'
        "\nRules:\n"
        "- Propose ONE action per response.\n"
        "- Use WAIT when nothing needs to change.\n"
        "- WAIT reason must be 1-2 sentences MAX. Only mention changes or anomalies.\n"
        "- Use CALL_HUMAN when uncertain or when something needs human judgment.\n"
        "- Your proposal will be checked by safety gates before execution.\n"
        "- Respond ONLY with JSON. No commentary. Be concise."
    )

    sections.append(f"=== CURRENT TIME: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(current_time))} ===")

    return "\n\n".join(sections)


def build_messages(system_prompt: str, state: dict) -> list[dict]:
    """Build the full messages list for the LLM, including vision content.

    If camera frames or human images are present on the whiteboard,
    they're included as image_url content blocks in the user message.

    Args:
        system_prompt: The text system prompt from build_prompt().
        state: Whiteboard snapshot (for reading image keys).

    Returns:
        List of message dicts for the LLM client.
    """
    messages = [{"role": "system", "content": system_prompt}]

    # Build user message with optional vision content blocks
    user_content = []

    # Camera frames — include up to 2 to keep token budget manageable.
    # Priority: nozzle (most useful for print inspection) > buddy1 > thermal > buddy2
    camera_sources = [
        ("camera.nozzle_frame", "Nozzle camera (close-up of print head and surface)"),
        ("camera.buddy1_frame", "Buddy camera 1 (wide-angle enclosure view)"),
        ("camera.buddy2_frame", "Buddy camera 2 (wide-angle enclosure view)"),
    ]
    cameras_included = 0
    max_cameras = 2  # Limit to avoid blowing token budget (~200KB base64 per frame)
    for cam_key, cam_label in camera_sources:
        if cameras_included >= max_cameras:
            break
        frame = state.get(cam_key)
        if frame and isinstance(frame, str):
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{frame}"},
            })
            user_content.append({"type": "text", "text": cam_label})
            cameras_included += 1

    # Human photo (if operator sent one via Telegram)
    human_image = state.get("human.image")
    if human_image and isinstance(human_image, str):
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{human_image}"},
        })
        user_content.append({
            "type": "text",
            "text": "The operator sent this image via Telegram. Analyze it in context of the current printer state.",
        })

    # Always include the action prompt
    user_content.append({
        "type": "text",
        "text": "Decide your next action.",
    })

    # If we have images, use content blocks; otherwise simple string
    if len(user_content) > 1:
        messages.append({"role": "user", "content": user_content})
    else:
        messages.append({"role": "user", "content": "Decide your next action."})

    return messages
