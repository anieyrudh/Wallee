from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path


def test_append_stock_experiment_run_appends_row(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    experiment_root = repo_root / "docs" / "evidence" / "prusa_core_one_plus" / "experimentation_data"
    run_dir = repo_root / "docs" / "evidence" / "prusa_core_one_plus" / "stringing_comparison" / "2026-04-14T140902Z-stock-stringing-run"
    ledger = experiment_root / "run_ledger.csv"
    experiment_root.mkdir(parents=True, exist_ok=True)
    (experiment_root / "stock").mkdir()
    (experiment_root / "wallee").mkdir()
    ledger.write_text(
        "run_id,condition,planner_model,planner_reasoning_effort,vision_model,file_name,filament,printer,started_at,finished_at,print_duration_min,success,planner_calls,planner_prompt_tokens,planner_completion_tokens,planner_total_cost_usd,planner_latency_p50_ms,planner_latency_p95_ms,vision_calls,vision_prompt_tokens,vision_completion_tokens,vision_total_cost_usd,vision_latency_p50_ms,vision_latency_p95_ms,total_plans,total_actions,action_families,final_quality_score,final_stringing_score,notes,run_dir\n"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "file_name": "Stringing_Test_latest_PLA_COREONE.bgcode",
                "final_status": {"lifecycle": "FINISHED"},
                "status_polls": [
                    {"ts": "2026-04-16T00:00:00+00:00"},
                    {"ts": "2026-04-16T00:12:00+00:00"},
                ],
            }
        )
    )

    script_path = Path(__file__).resolve().parents[1] / "scripts" / "append_stock_experiment_run.py"
    subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--run-dir",
            str(run_dir),
            "--ledger",
            str(ledger),
            "--experiment-root",
            str(experiment_root),
            "--filament",
            "PLA",
            "--quality-score",
            "3",
            "--stringing-score",
            "2",
            "--notes",
            "stock baseline",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    with ledger.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    row = rows[0]
    assert row["condition"] == "stock"
    assert row["planner_calls"] == "0"
    assert row["vision_calls"] == "0"
    assert row["file_name"] == "Stringing_Test_latest_PLA_COREONE.bgcode"
    assert row["success"] == "true"
    assert row["final_quality_score"] == "3"
    assert row["final_stringing_score"] == "2"
