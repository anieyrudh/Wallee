"""Durable runtime database for Wallee v6.5.

This module is intentionally one of the deepest in the repository.  Callers get
simple semantic methods such as `create_action_run()` or `start_exec_journal()`
while all SQLite details, transition checks, and serialization stay hidden here.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from .ids import canonical_json
from .models import (
    ActionRun,
    ActionRunStatus,
    ApprovalRecord,
    ExecJournalEntry,
    ExecJournalStatus,
    PlanRecord,
)


_ALLOWED_TRANSITIONS: dict[ActionRunStatus, set[ActionRunStatus]] = {
    ActionRunStatus.PROPOSED: {
        ActionRunStatus.WAITING_APPROVAL,
        ActionRunStatus.AUTHORIZED,
        ActionRunStatus.ABORTED,
        # The engine parks a just-created run when its locks are already held
        # (engine lock-conflict path); this must be a safe refusal, not a crash.
        ActionRunStatus.REPLAN_REQUIRED,
    },
    ActionRunStatus.WAITING_APPROVAL: {
        ActionRunStatus.AUTHORIZED,
        ActionRunStatus.ABORTED,
    },
    ActionRunStatus.AUTHORIZED: {
        ActionRunStatus.DISPATCHED,
        ActionRunStatus.ABORTED,
        # TOCTOU re-check failure after authorization (engine precondition
        # re-validation path); same rationale as PROPOSED -> REPLAN_REQUIRED.
        ActionRunStatus.REPLAN_REQUIRED,
    },
    ActionRunStatus.DISPATCHED: {
        ActionRunStatus.DONE,
        ActionRunStatus.FAILED,
        ActionRunStatus.REPLAN_REQUIRED,
        ActionRunStatus.UNKNOWN,
    },
    ActionRunStatus.DONE: set(),
    ActionRunStatus.FAILED: set(),
    ActionRunStatus.ABORTED: set(),
    ActionRunStatus.REPLAN_REQUIRED: set(),
    ActionRunStatus.UNKNOWN: {ActionRunStatus.FAILED, ActionRunStatus.REPLAN_REQUIRED},
}


class RuntimeDB:
    """SQLite WAL-backed runtime database.

    The database stores two distinct truths:

    - `action_runs`: the official logical notebook
    - `exec_journal`: the hardware boundary journal

    Keeping both in one SQLite file is fine.  Collapsing them into one status is
    not.  The whole "did a move start before power died?" question depends on
    this separation.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._init_schema()

    def _configure(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=FULL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._conn.commit()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS plans (
              plan_id TEXT PRIMARY KEY,
              run_scope TEXT,
              goal TEXT NOT NULL,
              world_packet_json TEXT NOT NULL,
              world_compilation_json TEXT NOT NULL DEFAULT '{}',
              plan_ir_json TEXT NOT NULL,
              created_ts_ms INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS action_runs (
              action_run_id TEXT PRIMARY KEY,
              plan_id TEXT NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
              run_scope TEXT,
              action_id TEXT NOT NULL,
              verb TEXT NOT NULL,
              owner_pack TEXT NOT NULL,
              args_json TEXT NOT NULL,
              args_hash TEXT NOT NULL,
              idempotency_key TEXT NOT NULL,
              required_locks_json TEXT NOT NULL,
              status TEXT NOT NULL,
              hazard_class TEXT NOT NULL,
              verify_json TEXT,
              expected_delta_json TEXT NOT NULL,
              target_device TEXT,
              created_ts_ms INTEGER NOT NULL,
              updated_ts_ms INTEGER NOT NULL,
              result_json TEXT,
              error_json TEXT
            );

            CREATE TABLE IF NOT EXISTS approvals (
              approval_id TEXT PRIMARY KEY,
              action_run_id TEXT NOT NULL REFERENCES action_runs(action_run_id) ON DELETE CASCADE,
              args_hash TEXT NOT NULL,
              approved_by TEXT NOT NULL,
              decision TEXT NOT NULL,
              created_ts_ms INTEGER NOT NULL,
              expires_ts_ms INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS exec_journal (
              idempotency_key TEXT PRIMARY KEY,
              action_run_id TEXT NOT NULL,
              verb TEXT NOT NULL,
              args_hash TEXT NOT NULL,
              exec_state TEXT NOT NULL,
              started_mono_ms INTEGER NOT NULL,
              ended_mono_ms INTEGER,
              result_json TEXT,
              error_json TEXT
            );

            CREATE TABLE IF NOT EXISTS events (
              event_id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts_ms INTEGER NOT NULL,
              component TEXT NOT NULL,
              level TEXT NOT NULL,
              message TEXT NOT NULL,
              context_json TEXT NOT NULL
            );
            """
        )
        self._ensure_plan_schema()
        self._conn.commit()

    def _ensure_plan_schema(self) -> None:
        plan_columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(plans)").fetchall()
        }
        if "world_compilation_json" not in plan_columns:
            self._conn.execute(
                "ALTER TABLE plans ADD COLUMN world_compilation_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "run_scope" not in plan_columns:
            self._conn.execute("ALTER TABLE plans ADD COLUMN run_scope TEXT")
        action_columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(action_runs)").fetchall()
        }
        if "run_scope" not in action_columns:
            self._conn.execute("ALTER TABLE action_runs ADD COLUMN run_scope TEXT")

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()

    def clear_runtime_state(self) -> None:
        """Drop all persisted runtime state for a clean process restart."""
        with self._lock:
            self._conn.executescript(
                """
                DELETE FROM approvals;
                DELETE FROM exec_journal;
                DELETE FROM action_runs;
                DELETE FROM plans;
                DELETE FROM events;
                """
            )
            self._conn.commit()

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def store_plan(self, record: PlanRecord) -> None:
        """Persist a planner decision for audit and later replay."""
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO plans(
                  plan_id, run_scope, goal, world_packet_json, world_compilation_json, plan_ir_json, created_ts_ms
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.plan_id,
                    record.run_scope,
                    record.goal,
                    canonical_json(record.world_packet),
                    canonical_json(record.world_compilation),
                    canonical_json(record.plan_ir),
                    record.created_ts_ms,
                ),
            )
            self._conn.commit()

    def get_plan_run_scope(self, plan_id: str) -> str | None:
        """Return the logical run scope recorded for one plan."""
        row = self._conn.execute(
            "SELECT run_scope FROM plans WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row["run_scope"]).strip() or None if row["run_scope"] is not None else None

    def create_action_run(self, action_run: ActionRun) -> None:
        """Insert a new logical action run."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO action_runs(
                  action_run_id, plan_id, run_scope, action_id, verb, owner_pack, args_json,
                  args_hash, idempotency_key, required_locks_json, status,
                  hazard_class, verify_json, expected_delta_json, target_device,
                  created_ts_ms, updated_ts_ms, result_json, error_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_run.action_run_id,
                    action_run.plan_id,
                    action_run.run_scope,
                    action_run.action_id,
                    action_run.verb,
                    action_run.owner_pack,
                    canonical_json(action_run.args),
                    action_run.args_hash,
                    action_run.idempotency_key,
                    canonical_json(action_run.required_locks),
                    action_run.status.value,
                    action_run.hazard_class.value,
                    canonical_json(action_run.verify) if action_run.verify is not None else None,
                    canonical_json(action_run.expected_delta),
                    action_run.target_device,
                    action_run.created_ts_ms,
                    action_run.updated_ts_ms,
                    canonical_json(action_run.result) if action_run.result is not None else None,
                    canonical_json(action_run.error) if action_run.error is not None else None,
                ),
            )
            self._conn.commit()

    def get_action_run(self, action_run_id: str) -> ActionRun | None:
        """Return one action run or ``None`` if it does not exist."""
        row = self._conn.execute(
            "SELECT * FROM action_runs WHERE action_run_id = ?",
            (action_run_id,),
        ).fetchone()
        return self._row_to_action_run(row) if row else None

    def transition_action_run(
        self,
        action_run_id: str,
        new_status: ActionRunStatus,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> ActionRun:
        """Move an action run to a new logical state.

        Raises
        ------
        ValueError
            If the requested transition is not allowed.
        """
        with self._lock:
            current = self.get_action_run(action_run_id)
            if current is None:
                raise KeyError(f"unknown action_run_id={action_run_id}")

            if new_status not in _ALLOWED_TRANSITIONS[current.status]:
                raise ValueError(f"illegal transition {current.status.value} -> {new_status.value}")

            updated_ts_ms = self._now_ms()
            self._conn.execute(
                """
                UPDATE action_runs
                SET status = ?, updated_ts_ms = ?, result_json = ?, error_json = ?
                WHERE action_run_id = ?
                """,
                (
                    new_status.value,
                    updated_ts_ms,
                    canonical_json(result) if result is not None else current.result and canonical_json(current.result),
                    canonical_json(error) if error is not None else current.error and canonical_json(current.error),
                    action_run_id,
                ),
            )
            self._conn.commit()

            return self.get_action_run(action_run_id)  # type: ignore[return-value]

    def list_action_runs_by_status(self, *statuses: ActionRunStatus) -> list[ActionRun]:
        """Return all action runs currently in one of *statuses*."""
        placeholders = ",".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"SELECT * FROM action_runs WHERE status IN ({placeholders}) ORDER BY created_ts_ms ASC",
            tuple(status.value for status in statuses),
        ).fetchall()
        return [self._row_to_action_run(row) for row in rows]

    def list_action_runs_for_scope(self, run_scope: str) -> list[ActionRun]:
        """Return all action runs for one logical print/run scope."""
        rows = self._conn.execute(
            "SELECT * FROM action_runs WHERE run_scope = ? ORDER BY created_ts_ms ASC",
            (run_scope,),
        ).fetchall()
        return [self._row_to_action_run(row) for row in rows]

    def record_approval(self, approval: ApprovalRecord) -> None:
        """Persist a human approval or rejection."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO approvals(
                  approval_id, action_run_id, args_hash, approved_by, decision, created_ts_ms, expires_ts_ms
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.approval_id,
                    approval.action_run_id,
                    approval.args_hash,
                    approval.approved_by,
                    approval.decision,
                    approval.created_ts_ms,
                    approval.expires_ts_ms,
                ),
            )
            self._conn.commit()

    def has_valid_approval(self, action_run_id: str, args_hash: str, now_ts_ms: int | None = None) -> bool:
        """Return ``True`` only if a matching, unexpired approval exists."""
        ts_ms = now_ts_ms or self._now_ms()
        row = self._conn.execute(
            """
            SELECT 1
            FROM approvals
            WHERE action_run_id = ?
              AND args_hash = ?
              AND decision = 'APPROVE'
              AND expires_ts_ms >= ?
            ORDER BY created_ts_ms DESC
            LIMIT 1
            """,
            (action_run_id, args_hash, ts_ms),
        ).fetchone()
        return row is not None

    def record_event(
        self,
        component: str,
        level: str,
        message: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Append an audit event."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO events(ts_ms, component, level, message, context_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    self._now_ms(),
                    component,
                    level,
                    message,
                    canonical_json(context or {}),
                ),
            )
            self._conn.commit()

    def latest_completed_action(self) -> dict[str, Any]:
        """Return the latest terminal action result for prompt grounding."""
        row = self._conn.execute(
            """
            SELECT
              ar.action_id,
              ar.run_scope,
              ar.verb,
              ar.status,
              ar.result_json,
              ar.error_json,
              ar.updated_ts_ms,
              p.plan_ir_json
            FROM action_runs ar
            JOIN plans p ON p.plan_id = ar.plan_id
            WHERE status IN ('DONE','FAILED','REPLAN_REQUIRED','ABORTED')
            ORDER BY ar.updated_ts_ms DESC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return {}
        plan_ir = json.loads(row["plan_ir_json"]) if row["plan_ir_json"] else {}
        return {
            "action_id": row["action_id"],
            "run_scope": row["run_scope"],
            "verb": row["verb"],
            "status": row["status"],
            "result": json.loads(row["result_json"]) if row["result_json"] else {},
            "error": json.loads(row["error_json"]) if row["error_json"] else {},
            "updated_ts_ms": row["updated_ts_ms"],
            "plan_why": str(plan_ir.get("why") or "").strip() or None,
            "plan_decision": str(plan_ir.get("decision") or "").strip() or None,
            "plan_sequence": list(plan_ir.get("sequence") or []),
        }

    def recent_completed_actions(self, run_scope: str | None, *, limit: int = 3) -> list[dict[str, Any]]:
        """Return recent terminal action results, preferring the current logical run scope."""
        statuses = ("DONE", "FAILED", "REPLAN_REQUIRED", "ABORTED")

        def _fetch(scope: str | None) -> list[dict[str, Any]]:
            if scope:
                rows = self._conn.execute(
                    """
                    SELECT
                      ar.action_id,
                      ar.run_scope,
                      ar.verb,
                      ar.status,
                      ar.result_json,
                      ar.error_json,
                      ar.updated_ts_ms,
                      p.plan_ir_json
                    FROM action_runs ar
                    JOIN plans p ON p.plan_id = ar.plan_id
                    WHERE ar.run_scope = ?
                      AND ar.status IN (?, ?, ?, ?)
                    ORDER BY ar.updated_ts_ms DESC
                    LIMIT ?
                    """,
                    (scope, *statuses, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT
                      ar.action_id,
                      ar.run_scope,
                      ar.verb,
                      ar.status,
                      ar.result_json,
                      ar.error_json,
                      ar.updated_ts_ms,
                      p.plan_ir_json
                    FROM action_runs ar
                    JOIN plans p ON p.plan_id = ar.plan_id
                    WHERE ar.status IN (?, ?, ?, ?)
                    ORDER BY ar.updated_ts_ms DESC
                    LIMIT ?
                    """,
                    (*statuses, limit),
                ).fetchall()
            payload: list[dict[str, Any]] = []
            for row in rows:
                plan_ir = json.loads(row["plan_ir_json"]) if row["plan_ir_json"] else {}
                payload.append(
                    {
                        "action_id": row["action_id"],
                        "run_scope": row["run_scope"],
                        "verb": row["verb"],
                        "status": row["status"],
                        "result": json.loads(row["result_json"]) if row["result_json"] else {},
                        "error": json.loads(row["error_json"]) if row["error_json"] else {},
                        "updated_ts_ms": row["updated_ts_ms"],
                        "plan_why": str(plan_ir.get("why") or "").strip() or None,
                        "plan_decision": str(plan_ir.get("decision") or "").strip() or None,
                        "plan_sequence": list(plan_ir.get("sequence") or []),
                    }
                )
            return payload

        scoped = _fetch(run_scope)
        if scoped or run_scope is None:
            return scoped
        return _fetch(None)

    def get_exec_journal(self, idempotency_key: str) -> ExecJournalEntry | None:
        """Return the execution journal entry for one idempotency key."""
        row = self._conn.execute(
            "SELECT * FROM exec_journal WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return self._row_to_exec_journal(row) if row else None

    def start_exec_journal(
        self,
        *,
        idempotency_key: str,
        action_run_id: str,
        verb: str,
        args_hash: str,
        started_mono_ms: int,
    ) -> ExecJournalEntry:
        """Create or return the durable `IN_FLIGHT` journal entry.

        This method is one of the most safety-sensitive places in the codebase.
        The caller is expected to invoke it and then, only after the commit
        returns, perform any hardware side effect.
        """
        with self._lock:
            existing = self.get_exec_journal(idempotency_key)
            if existing is not None:
                return existing

            self._conn.execute(
                """
                INSERT INTO exec_journal(
                  idempotency_key, action_run_id, verb, args_hash, exec_state,
                  started_mono_ms, ended_mono_ms, result_json, error_json
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                """,
                (
                    idempotency_key,
                    action_run_id,
                    verb,
                    args_hash,
                    ExecJournalStatus.IN_FLIGHT.value,
                    started_mono_ms,
                ),
            )
            self._conn.commit()
            return self.get_exec_journal(idempotency_key)  # type: ignore[return-value]

    def finish_exec_journal(
        self,
        idempotency_key: str,
        new_status: ExecJournalStatus,
        *,
        ended_mono_ms: int,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> ExecJournalEntry:
        """Mark an execution journal entry terminal."""
        with self._lock:
            self._conn.execute(
                """
                UPDATE exec_journal
                SET exec_state = ?, ended_mono_ms = ?, result_json = ?, error_json = ?
                WHERE idempotency_key = ?
                """,
                (
                    new_status.value,
                    ended_mono_ms,
                    canonical_json(result) if result is not None else None,
                    canonical_json(error) if error is not None else None,
                    idempotency_key,
                ),
            )
            self._conn.commit()
            return self.get_exec_journal(idempotency_key)  # type: ignore[return-value]

    def _row_to_action_run(self, row: sqlite3.Row) -> ActionRun:
        return ActionRun(
            action_run_id=row["action_run_id"],
            plan_id=row["plan_id"],
            run_scope=row["run_scope"] if "run_scope" in row.keys() else None,
            action_id=row["action_id"],
            verb=row["verb"],
            owner_pack=row["owner_pack"],
            args=json.loads(row["args_json"]),
            args_hash=row["args_hash"],
            idempotency_key=row["idempotency_key"],
            required_locks=json.loads(row["required_locks_json"]),
            status=ActionRunStatus(row["status"]),
            hazard_class=row["hazard_class"],
            verify=json.loads(row["verify_json"]) if row["verify_json"] else None,
            expected_delta=json.loads(row["expected_delta_json"]),
            target_device=row["target_device"],
            created_ts_ms=row["created_ts_ms"],
            updated_ts_ms=row["updated_ts_ms"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=json.loads(row["error_json"]) if row["error_json"] else None,
        )

    def _row_to_exec_journal(self, row: sqlite3.Row) -> ExecJournalEntry:
        return ExecJournalEntry(
            idempotency_key=row["idempotency_key"],
            action_run_id=row["action_run_id"],
            verb=row["verb"],
            args_hash=row["args_hash"],
            exec_state=ExecJournalStatus(row["exec_state"]),
            started_mono_ms=row["started_mono_ms"],
            ended_mono_ms=row["ended_mono_ms"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=json.loads(row["error_json"]) if row["error_json"] else None,
        )
