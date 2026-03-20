"""Built-in tool: persist observations to knowledge/OBSERVATIONS.md."""

import logging
import time
from pathlib import Path

from wallee.tools.decorator import tool

logger = logging.getLogger(__name__)

MAX_ENTRIES = 50
_OBSERVATIONS_PATH = Path(__file__).parent.parent.parent / "knowledge" / "OBSERVATIONS.md"


def configure_observations_dir(path: Path):
    """Update the observations file path at runtime (e.g., to WALLEE_DATA_DIR)."""
    global _OBSERVATIONS_PATH
    _OBSERVATIONS_PATH = path / "OBSERVATIONS.md"


@tool(kind="actuator", requires_approval=False, gate_bypass=True)
def remember(observation: str = "", whiteboard=None, **kwargs) -> dict:
    """Persist an observation to OBSERVATIONS.md. Parameter: observation (required string).

    Call as: remember(observation="what you learned")
    Use to record patterns, operator feedback, print outcomes, or lessons learned.
    Capped at 50 most recent entries. Skips near-duplicates of the last entry.
    """
    if not observation:
        return {"error": "observation parameter required"}

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = f"- [{ts}] {observation}"

    try:
        if _OBSERVATIONS_PATH.exists():
            text = _OBSERVATIONS_PATH.read_text()
        else:
            text = "# Wallee — Observations\n\nAuto-recorded by the agent's `remember` tool.\n"

        lines = text.splitlines()

        # Find existing entries (lines starting with "- [")
        header_lines = []
        entry_lines = []
        for line in lines:
            if line.startswith("- ["):
                entry_lines.append(line)
            else:
                if not entry_lines:
                    header_lines.append(line)

        # Dedup: skip if the last entry is substantially similar (same first 50 chars after timestamp)
        if entry_lines:
            # Strip timestamp prefix "- [YYYY-MM-DD HH:MM:SS] " to compare content
            last_content = entry_lines[0].split("] ", 1)[-1] if "] " in entry_lines[0] else entry_lines[0]
            if last_content[:50] == observation[:50]:
                logger.info(f"Remember skipped (duplicate of last entry): {observation[:50]}")
                return {"status": "skipped", "reason": "duplicate of last observation"}

        # Prepend new entry, cap at MAX_ENTRIES
        entry_lines.insert(0, entry)
        entry_lines = entry_lines[:MAX_ENTRIES]

        new_text = "\n".join(header_lines) + "\n" + "\n".join(entry_lines) + "\n"
        _OBSERVATIONS_PATH.write_text(new_text)

        logger.info(f"Remembered: {observation[:80]}")
        return {"status": "success", "entries": len(entry_lines)}

    except Exception as e:
        logger.error(f"remember tool failed: {e}")
        return {"error": str(e)}
