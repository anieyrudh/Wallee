"""Detects external changes to whiteboard state not caused by Wallee actions.

Compares current whiteboard snapshot to previous one. For tracked keys,
flags changes that have no matching action in the current episode.
"""

import logging

logger = logging.getLogger(__name__)

# Keys to track for external changes, mapped to the Wallee action that would cause them
TRACKED_KEYS = {
    "printer.state": None,  # Any state change is interesting
    "printer.target_nozzle": "set_temperature",
    "printer.target_bed": "set_temperature",
    "printer.target_chamber": "set_temperature",
    "printer.speed": "set_speed_factor",
    "printer.flow": "set_flow_factor",
    "printer.job_progress": None,  # Watch for unexpected jumps/resets
}


class ExternalChangeDetector:
    """Tracks whiteboard changes between agent cycles.

    Stores the previous snapshot in memory (Python dict, not Redis).
    On each cycle, compares current to previous and returns a list of
    changes not attributable to Wallee actions in the current episode.
    """

    def __init__(self):
        self._prev_state: dict | None = None

    def detect(self, current_state: dict, episode: list[dict]) -> list[str]:
        """Compare current state to previous, return list of external change strings.

        Args:
            current_state: Current whiteboard snapshot.
            episode: Current episode actions from ledger.

        Returns:
            List of human-readable change descriptions, or empty list.
        """
        if self._prev_state is None:
            self._prev_state = dict(current_state)
            logger.debug(f"Change detector initialized with {len(current_state)} keys")
            return []

        changes = []
        wallee_tools = _episode_tools(episode)
        tracked_count = sum(1 for k in TRACKED_KEYS if k in current_state)

        for key, expected_tool in TRACKED_KEYS.items():
            prev_val = self._prev_state.get(key)
            curr_val = current_state.get(key)

            if prev_val is None or curr_val is None:
                continue
            if prev_val == curr_val:
                continue

            # Check if Wallee caused this change
            if expected_tool and expected_tool in wallee_tools:
                continue  # Wallee did this, not external

            changes.append(
                f"{key} changed: {prev_val} -> {curr_val}"
            )

        # Update snapshot
        self._prev_state = dict(current_state)

        if changes:
            logger.info(f"Change detector: {len(changes)} external changes found")
            for c in changes:
                logger.info(f"  EXTERNAL: {c}")
        else:
            logger.debug(f"Change detector: {tracked_count} keys tracked, no external changes")

        return changes

    def format_for_prompt(self, changes: list[str]) -> str:
        """Format changes for insertion at the top of the LLM prompt."""
        if not changes:
            return ""

        lines = ["!!! EXTERNAL CHANGES DETECTED (not caused by Wallee) !!!"]
        for change in changes:
            lines.append(f"  - {change}")
        lines.append("Consider: is someone operating the printer manually? Has the firmware auto-corrected?")
        return "\n".join(lines)


def _episode_tools(episode: list[dict]) -> set[str]:
    """Extract successfully dispatched Wallee tools from the current episode."""
    tools = set()
    for action in episode:
        if action.get("status") not in {"DISPATCHED", "DONE"}:
            continue
        tool_name = action.get("tool")
        if tool_name:
            tools.add(tool_name)
    return tools
