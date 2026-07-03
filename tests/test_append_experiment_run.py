from __future__ import annotations

import csv
import json
import sqlite3
import subprocess
import sys
from pathlib import Path


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def test_append_experiment_run_extracts_and_appends(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    experiment_root = repo_root / "docs" / "evidence" / "prusa_core_one_plus" / "experimentation_data"
    vision_root = repo_root / "docs" / "evidence" / "prusa_core_one_plus" / "vision"
    run_dir = repo_root / "docs" / "evidence" / "prusa_core_one_plus" / "stringing_reruns" / "2026-04-16T023047Z-runtime-stringing-rerun"
    ledger = experiment_root / "run_ledger.csv"

    experiment_root.mkdir(parents=True, exist_ok=True)
    vision_root.mkdir(parents=True, exist_ok=True)
    (experiment_root / "stock").mkdir()
    (experiment_root / "wallee").mkdir()
    ledger.write_text(
        "run_id,condition,planner_model,planner_reasoning_effort,vision_model,file_name,filament,printer,started_at,finished_at,print_duration_min,success,planner_calls,planner_prompt_tokens,planner_completion_tokens,planner_total_cost_usd,planner_latency_p50_ms,planner_latency_p95_ms,vision_calls,vision_prompt_tokens,vision_completion_tokens,vision_total_cost_usd,vision_latency_p50_ms,vision_latency_p95_ms,total_plans,total_actions,action_families,final_quality_score,final_stringing_score,notes,run_dir\n"
    )

    _write_json(
        run_dir / "run_meta.json",
        {"file_name": "Stringing_Test_PLA_COREONE.bgcode"},
    )
    _write_json(
        run_dir / "status_polls.json",
        [
            {"ts": "2026-04-16T02:31:23.510304+00:00"},
            {"ts": "2026-04-16T02:43:56.190802+00:00"},
        ],
    )
    _write_json(
        run_dir / "final_status.json",
        {"lifecycle": "FINISHED"},
    )
    _write_json(
        run_dir / "runtime_cycles" / "cycle-0001.json",
        {
            "decision_input": {
                "facts": {
                    "printer_1.vision_observation_ref": "/opt/wallee/src/docs/evidence/prusa_core_one_plus/vision/observations/frame-001.json"
                }
            },
            "planner_provider_metadata": {
                "model": "openai/gpt-5.4",
                "latency_ms": 5000,
                "tokens_prompt": 100,
                "tokens_completion": 20,
                "usage": {"cost": 0.12},
            },
        },
    )
    _write_json(
        run_dir / "runtime_cycles" / "cycle-0002.json",
        {
            "decision_input": {
                "facts": {
                    "printer_1.vision_observation_ref": "/opt/wallee/src/docs/evidence/prusa_core_one_plus/vision/observations/frame-002.json"
                }
            },
            "planner_provider_metadata": {
                "model": "openai/gpt-5.4",
                "latency_ms": 9000,
                "tokens_prompt": 120,
                "tokens_completion": 30,
                "usage": {"cost": 0.18},
            },
        },
    )

    replay_dir = vision_root / "replays"
    _write_json(
        replay_dir / "frame-001.json",
        {
            "provider_metadata": {
                "model": "google/gemini-3.1-flash-lite-preview",
                "latency_ms": 2000,
                "tokens_prompt": 11,
                "tokens_completion": 3,
                "usage": {"cost": 0.02},
            }
        },
    )
    _write_json(
        replay_dir / "frame-002.json",
        {
            "provider_metadata": {
                "model": "google/gemini-3.1-flash-lite-preview",
                "latency_ms": 3000,
                "tokens_prompt": 13,
                "tokens_completion": 4,
                "usage": {"cost": 0.03},
            }
        },
    )

    db_path = run_dir / "runtime.db.snapshot"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE action_runs (action_id TEXT NOT NULL, created_ts_ms INTEGER NOT NULL)")
    conn.execute("INSERT INTO action_runs (action_id, created_ts_ms) VALUES (?, ?)", ("A_PRUSA_TRIM_FLOW_DOWN_SMALL", 1))
    conn.execute("INSERT INTO action_runs (action_id, created_ts_ms) VALUES (?, ?)", ("A_PRUSA_TRIM_SPEED_DOWN_SMALL", 2))
    conn.commit()
    conn.close()

    script_path = Path(__file__).resolve().parents[1] / "scripts" / "append_experiment_run.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--run-dir",
            str(run_dir),
            "--ledger",
            str(ledger),
            "--experiment-root",
            str(experiment_root),
            "--vision-root",
            str(vision_root),
            "--quality-score",
            "5",
            "--stringing-score",
            "4",
            "--notes",
            "best run",
            "--planner-reasoning-effort",
            "medium",
            "--filament",
            "PLA",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload["run_id"] == "2026-04-16T023047Z-runtime-stringing-rerun"

    with ledger.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    row = rows[0]
    assert row["planner_model"] == "openai/gpt-5.4"
    assert row["planner_prompt_tokens"] == "220"
    assert row["planner_completion_tokens"] == "50"
    assert row["planner_total_cost_usd"] == "0.3"
    assert row["planner_latency_p50_ms"] == "5000"
    assert row["planner_latency_p95_ms"] == "9000"
    assert row["vision_model"] == "google/gemini-3.1-flash-lite-preview"
    assert row["vision_calls"] == "2"
    assert row["vision_prompt_tokens"] == "24"
    assert row["vision_completion_tokens"] == "7"
    assert row["vision_total_cost_usd"] == "0.05"
    assert row["total_plans"] == "2"
    assert row["total_actions"] == "2"
    assert row["action_families"] == "flow:1|speed:1"
    assert row["success"] == "true"
    assert row["final_quality_score"] == "5"
    assert row["final_stringing_score"] == "4"

    metrics_json = experiment_root / "wallee" / "2026-04-16T023047Z-runtime-stringing-rerun" / "run_metrics.json"
    assert metrics_json.exists()


def test_append_experiment_run_reads_local_remote_bundle_replay(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    experiment_root = repo_root / "docs" / "evidence" / "prusa_core_one_plus" / "experimentation_data"
    vision_root = repo_root / "docs" / "evidence" / "prusa_core_one_plus" / "vision"
    run_dir = repo_root / "docs" / "evidence" / "prusa_core_one_plus" / "stringing_reruns" / "run-1"
    ledger = experiment_root / "run_ledger.csv"
    experiment_root.mkdir(parents=True, exist_ok=True)
    (experiment_root / "stock").mkdir()
    (experiment_root / "wallee").mkdir()
    vision_root.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        "run_id,condition,planner_model,planner_reasoning_effort,vision_model,file_name,filament,printer,started_at,finished_at,print_duration_min,success,planner_calls,planner_prompt_tokens,planner_completion_tokens,planner_total_cost_usd,planner_latency_p50_ms,planner_latency_p95_ms,vision_calls,vision_prompt_tokens,vision_completion_tokens,vision_total_cost_usd,vision_latency_p50_ms,vision_latency_p95_ms,total_plans,total_actions,action_families,final_quality_score,final_stringing_score,notes,run_dir\n"
    )
    _write_json(run_dir / "run_meta.json", {"file_name": "Stringing.bgcode"})
    _write_json(run_dir / "status_polls.json", [{"ts": "2026-04-16T00:00:00+00:00"}, {"ts": "2026-04-16T00:10:00+00:00"}])
    _write_json(run_dir / "final_status.json", {"lifecycle": "FINISHED"})
    _write_json(
        run_dir / "runtime_cycles" / "cycle.json",
        {
            "decision_input": {"facts": {"printer_1.vision_observation_ref": "/opt/wallee/src/docs/evidence/prusa_core_one_plus/vision/observations/frame.json"}},
            "planner_provider_metadata": {"model": "openai/gpt-5.4", "latency_ms": 1000, "tokens_prompt": 10, "tokens_completion": 2},
        },
    )
    remote_bundle_replay = run_dir / "remote_run_bundle" / "probe_mid" / "vision" / "replays" / "frame.json"
    _write_json(
        remote_bundle_replay,
        {"provider_metadata": {"model": "google/gemini-3.1-flash-lite-preview", "latency_ms": 2100, "tokens_prompt": 9, "tokens_completion": 2, "usage": {"cost": 0.01}}},
    )
    db_path = run_dir / "runtime.db.snapshot"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE action_runs (action_id TEXT NOT NULL, created_ts_ms INTEGER NOT NULL)")
    conn.commit()
    conn.close()

    script_path = Path(__file__).resolve().parents[1] / "scripts" / "append_experiment_run.py"
    subprocess.run(
        [sys.executable, str(script_path), "--run-dir", str(run_dir), "--ledger", str(ledger), "--experiment-root", str(experiment_root), "--vision-root", str(vision_root)],
        check=True,
        capture_output=True,
        text=True,
    )
    with ledger.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["vision_calls"] == "1"
    assert rows[0]["vision_model"] == "google/gemini-3.1-flash-lite-preview"
    assert rows[0]["vision_total_cost_usd"] == "0.01"
