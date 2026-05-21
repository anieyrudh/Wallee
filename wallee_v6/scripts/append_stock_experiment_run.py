#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from append_experiment_run import _csv_value, _default_experiment_root, _default_ledger_path


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _read_ledger_rows(ledger_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not ledger_path.exists():
        raise FileNotFoundError(f"Ledger not found: {ledger_path}")
    with ledger_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def _write_ledger_rows(ledger_path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with ledger_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _parse_stock_run(run_dir: Path) -> dict[str, Any]:
    payload = _read_json(run_dir / "run.json")
    status_polls = payload.get("status_polls") or []
    started_at = status_polls[0]["ts"] if status_polls else None
    finished_at = status_polls[-1]["ts"] if status_polls else None
    duration_min = None
    if started_at and finished_at:
        start_dt = datetime.fromisoformat(started_at)
        finish_dt = datetime.fromisoformat(finished_at)
        duration_min = round((finish_dt - start_dt).total_seconds() / 60.0, 3)
    final_status = payload.get("final_status") or {}
    lifecycle = final_status.get("lifecycle")
    success = lifecycle == "FINISHED" if lifecycle is not None else None
    return {
        "run_id": run_dir.name,
        "file_name": payload.get("file_name"),
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_min": duration_min,
        "success": success,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Append one stock run to the experimentation ledger.")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--condition", default="stock", choices=["stock"])
    parser.add_argument("--ledger", type=Path, default=_default_ledger_path())
    parser.add_argument("--experiment-root", type=Path, default=_default_experiment_root())
    parser.add_argument("--planner-reasoning-effort")
    parser.add_argument("--planner-model")
    parser.add_argument("--vision-model")
    parser.add_argument("--filament")
    parser.add_argument("--printer", default="Prusa CORE One/+")
    parser.add_argument("--quality-score", type=int)
    parser.add_argument("--stringing-score", type=int)
    parser.add_argument("--notes")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    parsed = _parse_stock_run(args.run_dir)
    rows, fieldnames = _read_ledger_rows(args.ledger)
    existing_index = next((idx for idx, row in enumerate(rows) if row.get("run_id") == parsed["run_id"]), None)
    if existing_index is not None and not args.replace:
        raise SystemExit(f"Run id already exists in ledger: {parsed['run_id']}. Use --replace to update it.")

    row = {
        "run_id": parsed["run_id"],
        "condition": "stock",
        "planner_model": args.planner_model,
        "planner_reasoning_effort": args.planner_reasoning_effort,
        "vision_model": args.vision_model,
        "file_name": parsed["file_name"],
        "filament": args.filament,
        "printer": args.printer,
        "started_at": parsed["started_at"],
        "finished_at": parsed["finished_at"],
        "print_duration_min": parsed["duration_min"],
        "success": parsed["success"],
        "planner_calls": 0,
        "planner_prompt_tokens": None,
        "planner_completion_tokens": None,
        "planner_total_cost_usd": None,
        "planner_latency_p50_ms": None,
        "planner_latency_p95_ms": None,
        "vision_calls": 0,
        "vision_prompt_tokens": None,
        "vision_completion_tokens": None,
        "vision_total_cost_usd": None,
        "vision_latency_p50_ms": None,
        "vision_latency_p95_ms": None,
        "total_plans": 0,
        "total_actions": 0,
        "action_families": "",
        "final_quality_score": args.quality_score,
        "final_stringing_score": args.stringing_score,
        "notes": args.notes,
        "run_dir": str(args.run_dir),
    }
    normalized = {key: _csv_value(value) for key, value in row.items()}
    if existing_index is not None:
        rows[existing_index] = normalized
    else:
        rows.append(normalized)

    output_dir = args.experiment_root / "stock" / parsed["run_id"]
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics_json = output_dir / "run_metrics.json"
    metrics_json.write_text(
        json.dumps(
            {
                "run_id": parsed["run_id"],
                "condition": "stock",
                "planner": {
                    "model": args.planner_model,
                    "reasoning_effort": args.planner_reasoning_effort,
                    "calls": 0,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "total_cost_usd": None,
                    "latency_p50_ms": None,
                    "latency_p95_ms": None,
                },
                "vision": {
                    "model": args.vision_model,
                    "calls": 0,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "total_cost_usd": None,
                    "latency_p50_ms": None,
                    "latency_p95_ms": None,
                },
                "print": {
                    "file_name": parsed["file_name"],
                    "filament": args.filament,
                    "printer": args.printer,
                    "started_at": parsed["started_at"],
                    "finished_at": parsed["finished_at"],
                    "duration_min": parsed["duration_min"],
                    "success": parsed["success"],
                },
                "runtime": {
                    "total_plans": 0,
                    "total_actions": 0,
                    "action_families": [],
                    "notes": [args.notes] if args.notes else [],
                },
                "outcome": {
                    "final_quality_score": args.quality_score,
                    "final_stringing_score": args.stringing_score,
                    "summary": args.notes or "",
                },
                "artifacts": {"run_dir": str(args.run_dir)},
            },
            indent=2,
        )
        + "\n"
    )

    if not args.dry_run:
        _write_ledger_rows(args.ledger, fieldnames, rows)

    print(json.dumps({"run_id": parsed["run_id"], "ledger": str(args.ledger), "metrics_json": str(metrics_json), "dry_run": args.dry_run}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
