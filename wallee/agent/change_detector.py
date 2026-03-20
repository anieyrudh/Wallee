"""Detects external changes to whiteboard state not caused by Wallee actions.

Compares current whiteboard snapshot to previous one. For tracked keys,
flags changes that have no matching action in the current episode OR
in recently completed ledger actions (within lookback window).
"""

import logging
import time

logger = logging.getLogger(__name__)

# Keys to track for external changes, mapped to tools that would cause them
TRACKED_KEYS = {
    "printer.state": ["pause_print", "resume_print", "cancel_print", "start_print"],
    "printer.job_state": ["pause_print", "resume_print", "cancel_print", "start_print"],
    "printer.target_nozzle": ["set_temperature"],
    "printer.target_bed": ["set_temperature"],
    "printer.target_chamber": ["set_temperature"],
    "printer.speed": ["set_speed_factor"],
    "printer.flow": ["set_flow_factor"],
    # Excluded: job_progress, pos_x/y/z, time_printing/remaining — these are
    # continuously incrementing values, not discrete state changes.
}

# Map from whiteboard key to tools that could cause a change
TOOL_STATE_MAP = {
    "printer.state": ["pause_print", "resume_print", "cancel_print", "start_print"],
    "printer.job_state": ["pause_print", "resume_print", "cancel_print", "start_print"],
    "printer.target_nozzle": ["set_temperature"],
    "printer.target_bed": ["set_temperature"],
    "printer.target_chamber": ["set_temperature"],
    "printer.speed": ["set_speed_factor"],
    "printer.flow": ["set_flow_factor"],
}

# How far back to look in the ledger for agent-caused actions
AGENT_LOOKBACK_S = 30


class ExternalChangeDetector:
    """Tracks whiteboard changes between agent cycles.

    Stores the previous snapshot in memory (Python dict, not Redis).
    On each cycle, compares current to previous and returns a list of
    changes not attributable to Wallee actions in the current episode
    or recent ledger history.
    """

    def __init__(self):
        self._prev_state: dict | None = None

    def _is_agent_caused(self, key: str, episode_tools: set, ledger) -> bool:
        """Check if a whiteboard change was caused by a recent Wallee action.

        Checks both the current episode AND the ledger for recent DONE actions
        matching tools that would affect this key.
        """
        possible_tools = TOOL_STATE_MAP.get(key, [])
        if not possible_tools:
            return False

        # Check 1: current episode has a matching dispatched/done action
        if episode_tools & set(possible_tools):
            return True

        # Check 2: ledger has a recent DONE action matching these tools
        if ledger and hasattr(ledger, "get_by_status"):
            try:
                done_actions = ledger.get_by_status("DONE")
                now = time.time()
                for action in done_actions:
                    tool_name = action.get("tool", "")
                    updated_ts = float(action.get("updated_ts", 0))
                    if tool_name in possible_tools and (now - updated_ts) < AGENT_LOOKBACK_S:
                        logger.debug(f"Change in {key} was agent-caused (recent {tool_name} action)")
                        return True
            except Exception as e:
                logger.debug(f"Ledger check failed for {key}: {e}")

        return False

    def detect(self, current_state: dict, episode: list[dict],
               ledger=None) -> list[str]:
        """Compare current state to previous, return list of external change strings.

        Args:
            current_state: Current whiteboard snapshot.
            episode: Current episode actions from ledger.
            ledger: Ledger instance for checking recent DONE actions beyond current episode.

        Returns:
            List of human-readable change descriptions, or empty list.
        """
        if self._prev_state is None:
            self._prev_state = dict(current_state)
            logger.debug(f"Change detector initialized with {len(current_state)} keys")
            return []

        changes = []
        episode_tools = _episode_tools(episode)

        for key in TRACKED_KEYS:
            prev_val = self._prev_state.get(key)
            curr_val = current_state.get(key)

            if prev_val is None or curr_val is None:
                continue
            if prev_val == curr_val:
                continue

            # Check if Wallee caused this change (episode or recent ledger)
            if self._is_agent_caused(key, episode_tools, ledger):
                continue  # Agent-caused, not external

            changes.append(
                f"{key} changed: {prev_val} -> {curr_val}"
            )

        # Update snapshot
        self._prev_state = dict(current_state)

        if changes:
            logger.info(f"Change detector: {len(changes)} external changes found")
            for c in changes:
                logger.info(f"  EXTERNAL: {c}")

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
