"""Diary — per-device-group idempotency database for crash recovery."""

import json
import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DIARY_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS idempotency_exec (
    idempotency_key TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('IN_FLIGHT','SUCCESS','FAILED')),
    started_ts REAL NOT NULL,
    completed_ts REAL,
    result_json TEXT
);
"""


class Diary:
    def __init__(self, device_group: str, data_dir: str | Path):
        self.device_group = device_group
        self.db_path = Path(data_dir) / f"diary_{device_group}.db"
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_DIARY_SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def write_inflight(self, idempotency_key: str, action_id: str, tool: str):
        """Record that a command is about to be sent to hardware. fsync."""
        self.conn.execute(
            """insert or replace into idempotency_exec
               (idempotency_key, action_id, tool, status, started_ts)
               VALUES (?, ?, ?, 'IN_FLIGHT', ?)""",
            (idempotency_key, action_id, tool, time.time()),
        )
        self.conn.commit()

    def write_success(self, idempotency_key: str, result: dict):
        """Record successful execution."""
        self.conn.execute(
            """UPDATE idempotency_exec
               SET status = 'SUCCESS', completed_ts = ?, result_json = ?
               WHERE idempotency_key = ?""",
            (time.time(), json.dumps(result), idempotency_key),
        )
        self.conn.commit()

    def write_failed(self, idempotency_key: str, result: dict):
        """Record failed execution."""
        self.conn.execute(
            """UPDATE idempotency_exec
               SET status = 'FAILED', completed_ts = ?, result_json = ?
               WHERE idempotency_key = ?""",
            (time.time(), json.dumps(result), idempotency_key),
        )
        self.conn.commit()

    def lookup(self, idempotency_key: str) -> str | None:
        """Look up the status of an idempotency key. Returns status or None."""
        row = self.conn.execute(
            "SELECT status FROM idempotency_exec WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return row["status"] if row else None

    def get_result(self, idempotency_key: str) -> dict | None:
        """Get the result for an idempotency key."""
        row = self.conn.execute(
            "SELECT result_json FROM idempotency_exec WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row and row["result_json"]:
            return json.loads(row["result_json"])
        return None

    def get_inflight(self) -> list[dict]:
        """Get all IN_FLIGHT entries (for crash recovery)."""
        rows = self.conn.execute(
            "SELECT * FROM idempotency_exec WHERE status = 'IN_FLIGHT'"
        ).fetchall()
        return [dict(r) for r in rows]
