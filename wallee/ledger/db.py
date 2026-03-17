"""Ledger — SQLite WAL database for action state machine."""

import hashlib
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_MIGRATION_DIR = Path(__file__).parent / "migrations"


class Ledger:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._run_migrations()

    def _run_migrations(self):
        """Apply all SQL migration files in order."""
        migration_files = sorted(_MIGRATION_DIR.glob("*.sql"))
        for mf in migration_files:
            sql = mf.read_text()
            self.conn.executescript(sql)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # --- Proposals (Agent writes) ---

    def propose(
        self,
        tool: str,
        params: dict,
        reason: str,
        device_group: str,
        requires_approval: bool = False,
        max_proposal_age_ms: int = 30000,
    ) -> str:
        """Create a new PROPOSED action. Returns action_id."""
        action_id = str(uuid.uuid4())
        params_json = json.dumps(params, sort_keys=True)
        params_hash = hashlib.sha256(params_json.encode()).hexdigest()[:16]
        idempotency_key = f"{tool}:{params_hash}"
        now = time.time()
        mono = time.monotonic()

        try:
            # Clear terminal-state entries so the same tool+params can be re-proposed
            self.conn.execute(
                """DELETE FROM actions
                   WHERE idempotency_key = ? AND status IN ('DONE','FAILED','REJECTED')""",
                (idempotency_key,),
            )
            self.conn.execute(
                """INSERT INTO actions
                   (action_id, tool, params_json, params_hash, idempotency_key,
                    device_group, status, reason, created_ts, created_mono,
                    updated_ts, requires_approval, max_proposal_age_ms)
                   VALUES (?, ?, ?, ?, ?, ?, 'PROPOSED', ?, ?, ?, ?, ?, ?)""",
                (action_id, tool, params_json, params_hash, idempotency_key,
                 device_group, reason, now, mono, now,
                 int(requires_approval), max_proposal_age_ms),
            )
            self.conn.commit()
            logger.info(f"Proposed: {action_id} ({tool})")
            return action_id
        except sqlite3.IntegrityError:
            # Duplicate idempotency key — same tool+params already proposed
            logger.warning(f"Duplicate proposal for {tool} with same params, skipping")
            return ""

    def record_wait(self, reason: str):
        """Record a WAIT event (marks episode boundary)."""
        self._record_event("agent", "INFO", "WAIT", reason)

    def record_call_human(self, message: str):
        """Record a CALL_HUMAN event (marks episode boundary)."""
        self._record_event("agent", "WARN", "CALL_HUMAN", message)

    def _record_event(self, component: str, level: str, message: str, details: str = ""):
        self.conn.execute(
            "INSERT INTO events (ts, component, level, message, details_json) VALUES (?, ?, ?, ?, ?)",
            (time.time(), component, level, message, details),
        )
        self.conn.commit()

    # --- Queries (Engine reads) ---

    def get_proposals(self) -> list[dict]:
        """Get all PROPOSED actions, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM actions WHERE status = 'PROPOSED' ORDER BY created_ts ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_by_status(self, status: str) -> list[dict]:
        """Get all actions with a given status."""
        rows = self.conn.execute(
            "SELECT * FROM actions WHERE status = ? ORDER BY created_ts ASC", (status,)
        ).fetchall()
        return [dict(r) for r in rows]

    def has_inflight(self, device_group: str) -> bool:
        """Check if any action is DISPATCHED for this device group."""
        row = self.conn.execute(
            "SELECT 1 FROM actions WHERE device_group = ? AND status = 'DISPATCHED' LIMIT 1",
            (device_group,),
        ).fetchone()
        return row is not None

    def get_action(self, action_id: str) -> dict | None:
        """Get a single action by ID."""
        row = self.conn.execute(
            "SELECT * FROM actions WHERE action_id = ?", (action_id,)
        ).fetchone()
        return dict(row) if row else None

    # --- State transitions (Engine writes) ---

    def set_status(self, action_id: str, status: str, result: dict | None = None, error: dict | None = None):
        """Update action status. Only the engine should call this."""
        updates = ["status = ?", "updated_ts = ?"]
        params = [status, time.time()]

        if result is not None:
            updates.append("result_json = ?")
            params.append(json.dumps(result))
        if error is not None:
            updates.append("error_json = ?")
            params.append(json.dumps(error))

        params.append(action_id)
        self.conn.execute(
            f"UPDATE actions SET {', '.join(updates)} WHERE action_id = ?",
            params,
        )
        self.conn.commit()

    def reject(self, action_id: str, reason: str):
        """Reject an action with a reason."""
        self.set_status(action_id, "REJECTED", error={"reason": reason})
        self._record_event("engine", "INFO", f"REJECTED {action_id}", reason)

    # --- Approvals ---

    def record_approval(self, action_id: str, decision: str, approved_by: str):
        """Record an approval decision for an action."""
        action = self.get_action(action_id)
        if not action:
            return
        approval_id = str(uuid.uuid4())
        self.conn.execute(
            """INSERT INTO approvals
               (approval_id, action_id, params_hash, decision, approved_by, created_ts)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (approval_id, action_id, action["params_hash"], decision, approved_by, time.time()),
        )
        self.conn.commit()

    def get_approval(self, action_id: str) -> dict | None:
        """Get the approval record for an action."""
        row = self.conn.execute(
            "SELECT * FROM approvals WHERE action_id = ? ORDER BY created_ts DESC LIMIT 1",
            (action_id,),
        ).fetchone()
        return dict(row) if row else None

    # --- Episode ---

    def current_episode(self) -> list[dict]:
        """Return all actions since the last WAIT or CALL_HUMAN event,
        plus any recent terminal actions for context (so the LLM sees rejections)."""
        # Actions since last WAIT/CALL_HUMAN boundary
        rows = self.conn.execute("""
            SELECT * FROM actions
            WHERE created_ts > COALESCE(
                (SELECT MAX(ts) FROM events
                 WHERE message IN ('WAIT', 'CALL_HUMAN')),
                0
            )
            ORDER BY created_ts ASC
        """).fetchall()
        episode = [dict(r) for r in rows]

        # If episode is empty, include recent actions so LLM sees rejections/failures
        if not episode:
            rows = self.conn.execute("""
                SELECT * FROM actions
                ORDER BY created_ts DESC LIMIT 5
            """).fetchall()
            episode = [dict(r) for r in reversed(rows)]

        return episode
