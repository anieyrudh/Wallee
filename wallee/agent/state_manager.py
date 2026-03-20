"""Controlled interface for agent whiteboard writes.

The agent ONLY writes through this manager — no direct wb.publish/delete.
Reads from the full whiteboard are unrestricted.
"""

import logging

logger = logging.getLogger(__name__)


class AgentStateManager:
    """Restricted write interface to the whiteboard for the agent loop.

    The agent can read any whiteboard key directly via wb.read/read_all.
    But all writes and deletes go through this manager, which enforces
    an allowlist of keys the agent is permitted to modify.
    """

    ALLOWED_WRITES = frozenset({
        "agent.heartbeat",
        "agent.last_decision",
        "agent.cooldown",
        "agent.external_pause",
        "agent.awaiting_feedback",
        "agent.missing_sensors",
        "human.pending_callout",
        "print.queue",
    })

    ALLOWED_DELETES = frozenset({
        "human.intent",
        "human.image",
        "human.pending_callout",
        "agent.external_pause",
        "agent.awaiting_feedback",
    })

    ALLOWED_LISTS = frozenset({
        "agent.activity_log",
    })

    def __init__(self, wb):
        self._wb = wb

    def publish(self, key: str, value, ttl: int | None = None):
        """Publish a value to the whiteboard. Key must be in ALLOWED_WRITES."""
        if key not in self.ALLOWED_WRITES:
            raise ValueError(f"Agent not permitted to write: {key}")
        self._wb.publish(key, value, ttl=ttl)

    def delete(self, key: str):
        """Delete a key from the whiteboard. Key must be in ALLOWED_DELETES."""
        if key not in self.ALLOWED_DELETES:
            raise ValueError(f"Agent not permitted to delete: {key}")
        self._wb.r.delete(key)

    def list_push(self, key: str, value: str, max_len: int = 20):
        """Push to a Redis list (for activity logs). Key must be in ALLOWED_LISTS."""
        if key not in self.ALLOWED_LISTS:
            raise ValueError(f"Agent not permitted to push to list: {key}")
        self._wb.r.lpush(key, value)
        self._wb.r.ltrim(key, 0, max_len - 1)
