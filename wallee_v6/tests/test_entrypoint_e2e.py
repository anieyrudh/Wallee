"""End-to-end tests that drive the SHIPPED entrypoint as a subprocess.

The original v6 safety bugs were all *wiring* omissions in main — components
that existed and passed unit tests but were never called by the real
composition (safety.poll() discarded, interlock callbacks never registered,
approvals never resumed). Tests that drive extracted functions cannot catch
that class; these run `python -m wallee_v6.main` for real and assert
observable outcomes from the database and outbox files.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO_V6 = Path(__file__).resolve().parents[1]
GOAL = "Unload cooled part from printer_1 into tray_A"


def _run_cli(data_dir: Path, *extra_args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        {
            "WALLEE_DATA_DIR": str(data_dir),
            "WALLEE_ENABLED_PACKS": "sim_printer,sim_arm",
            "WALLEE_SIMULATION": "1",
            "WALLEE_PLANNER_BACKEND": "heuristic",
            "WALLEE_RUNTIME_POLL_INTERVAL_S": "0.1",
        }
    )
    env.pop("OPENROUTER_API_KEY", None)
    return subprocess.run(
        [sys.executable, "-m", "wallee_v6.main", "--simulate", "--goal", GOAL, *extra_args],
        cwd=REPO_V6,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _db(data_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(data_dir / "wallee_v6.sqlite3")
    conn.row_factory = sqlite3.Row
    return conn


def _find_db(data_dir: Path) -> Path:
    candidates = list(data_dir.glob("*.sqlite3")) + list(data_dir.glob("*.db"))
    assert candidates, f"no runtime DB created under {data_dir}: {list(data_dir.iterdir())}"
    return candidates[0]


def test_cli_once_runs_a_full_cycle(tmp_path):
    result = _run_cli(tmp_path, "--once")

    assert result.returncode == 0, result.stderr[-2000:] + result.stdout[-2000:]
    db_path = _find_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        events = {row["message"] for row in conn.execute("SELECT message FROM events")}
        assert "runtime_started" in events, sorted(events)
        plan_count = conn.execute("SELECT count(*) FROM plans").fetchone()[0]
        assert plan_count >= 1, "the real entrypoint must persist at least one plan per cycle"
    finally:
        conn.close()


def test_cli_boot_reconcile_is_wired_through_the_real_entrypoint(tmp_path):
    """Seed crash residue, run the real CLI, and observe the sweep.

    This pins that reconcile_runtime_start_state is actually CALLED by main —
    not merely implemented — and that the journal-wipe default stays off.
    """
    # Seed a previous "crashed" run using the runtime's own storage layer.
    env_backup = {k: os.environ.get(k) for k in ("WALLEE_DATA_DIR", "WALLEE_ENABLED_PACKS", "WALLEE_SIMULATION")}
    os.environ["WALLEE_DATA_DIR"] = str(tmp_path)
    try:
        from wallee_v6.config import Config
        from wallee_v6.ids import action_args_hash
        from wallee_v6.models import ActionRun, ActionRunStatus, HazardClass, PlanRecord
        from wallee_v6.runtime_db import RuntimeDB

        config = Config.from_env(repo_root=REPO_V6)
        db = RuntimeDB(config.db_path)
        now_ms = int(time.time() * 1000)
        db.store_plan(
            PlanRecord(
                plan_id="plan_crashed",
                goal="g",
                world_packet={"goal": "g"},
                plan_ir={"decision": "EXECUTE", "sequence": ["A_PRN_PAUSE"], "why": "crashed run"},
                created_ts_ms=now_ms,
            )
        )
        run = ActionRun(
            action_run_id="run_crashed",
            plan_id="plan_crashed",
            action_id="A_PRN_PAUSE",
            verb="PAUSE_PROCESS",
            owner_pack="sim_printer",
            args={},
            args_hash=action_args_hash("PAUSE_PROCESS", {}),
            idempotency_key="idem_crashed",
            required_locks=["printer_1.motion"],
            status=ActionRunStatus.PROPOSED,
            hazard_class=HazardClass.LOW,
            created_ts_ms=now_ms,
            updated_ts_ms=now_ms,
        )
        db.create_action_run(run)
        db.transition_action_run("run_crashed", ActionRunStatus.AUTHORIZED)
        db.transition_action_run("run_crashed", ActionRunStatus.DISPATCHED)
        db.record_event("test", "INFO", "pre_crash_marker")
        db.close()
        db_path = config.db_path
        outbox_dir = config.outbox_dir
    finally:
        for key, value in env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    result = _run_cli(tmp_path, "--once")
    assert result.returncode == 0, result.stderr[-2000:] + result.stdout[-2000:]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # The journal/event history from before the "crash" survived boot...
        events = [row["message"] for row in conn.execute("SELECT message FROM events")]
        assert "pre_crash_marker" in events, "boot wiped durable state — flush default regressed"
        # ...the residue was swept through the REAL entrypoint...
        assert "boot_reconcile_dispatched_outcome_unknown" in events, events
        status = conn.execute(
            "SELECT status FROM action_runs WHERE action_run_id = 'run_crashed'"
        ).fetchone()["status"]
        assert status == "UNKNOWN"
        # ...its locks no longer poison the frontier (a plan was still made)...
        assert conn.execute("SELECT count(*) FROM plans").fetchone()[0] >= 2
    finally:
        conn.close()

    # ...and a human was paged durably.
    payloads = [json.loads(p.read_text()) for p in Path(outbox_dir).glob("human_*.json")]
    assert any("Crash recovery" in p["title"] for p in payloads), (
        "reconcile must escalate unknown-outcome work to the operator"
    )
