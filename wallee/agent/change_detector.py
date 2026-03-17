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

        # Check for G-code commands Wallee didn't send
        gcode_change = self._check_external_gcode(current_state, wallee_tools)
        if gcode_change:
            changes.append(gcode_change)

        # Check command count acceleration
        cmdcnt_change = self._check_cmdcnt(current_state)
        if cmdcnt_change:
            changes.append(cmdcnt_change)

        # Update snapshot
        self._prev_state = dict(current_state)

        if changes:
            logger.info(f"Change detector: {len(changes)} external changes found")
            for c in changes:
                logger.info(f"  EXTERNAL: {c}")
        else:
            logger.debug(f"Change detector: {tracked_count} keys tracked, no external changes")

        return changes

    def _check_external_gcode(self, state: dict, wallee_tools: set) -> str | None:
        """Check if firmware executed a G-code command Wallee didn't send."""
        # The metrics stream publishes the last G-code via printer.last_gcode
        # (mapped from the 'gcode' metric field)
        curr_gcode = state.get("printer.last_gcode")
        prev_gcode = self._prev_state.get("printer.last_gcode")

        if curr_gcode and curr_gcode != prev_gcode:
            # If Wallee has any gcode-sending tool in the episode, it's likely ours
            gcode_tools = {"set_temperature", "home_axes", "disable_motors",
                           "set_speed_factor", "set_flow_factor", "set_position",
                           "extrude", "retract", "send_gcode"}
            if not (wallee_tools & gcode_tools):
                return f"External G-code detected: {curr_gcode}"

        return None

    def _check_cmdcnt(self, state: dict) -> str | None:
        """Check if command count is incrementing faster than expected."""
        curr_cnt = state.get("printer.cmdcnt")
        prev_cnt = self._prev_state.get("printer.cmdcnt")

        if curr_cnt is None or prev_cnt is None:
            return None

        try:
            delta = int(curr_cnt) - int(prev_cnt)
        except (ValueError, TypeError):
            return None

        # More than 10 commands per cycle is suspicious (Wallee sends ~1 per cycle max)
        if delta > 10:
            return f"Command count jumped by {delta} (expected ~1, possible external control)"

        return None

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
    """Extract the set of tool names used in the current episode."""
    tools = set()
    for action in episode:
        tool_name = action.get("tool")
        if tool_name:
            tools.add(tool_name)
    return tools
