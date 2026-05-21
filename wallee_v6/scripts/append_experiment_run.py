#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_ledger_path() -> Path:
    return _repo_root() / "docs" / "evidence" / "prusa_core_one_plus" / "experimentation_data" / "run_ledger.csv"


def _default_experiment_root() -> Path:
    return _repo_root() / "docs" / "evidence" / "prusa_core_one_plus" / "experimentation_data"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 1)


def _sum_optional(values: list[float | int | None]) -> float | None:
    concrete = [float(v) for v in values if isinstance(v, (int, float))]
    if not concrete:
        return None
    return round(sum(concrete), 6)


def _most_common_nonempty(values: list[str | None]) -> str | None:
    concrete = [value for value in values if value]
    if not concrete:
        return None
    return Counter(concrete).most_common(1)[0][0]


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.6f}".rstrip("0").rstrip(".")
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _action_family(action_id: str) -> str:
    if "_TRIM_SPEED_" in action_id or "_RESTORE_SPEED_" in action_id:
        return "speed"
    if "_TRIM_FLOW_" in action_id or "_RESTORE_FLOW_" in action_id:
        return "flow"
    if "_TRIM_NOZZLE_" in action_id:
        return "nozzle"
    if "_TRIM_BED_" in action_id:
        return "bed"
    if action_id.startswith("A_PRUSA_PAUSE") or action_id.startswith("A_PRUSA_RESUME") or action_id.startswith("A_PRUSA_CANCEL") or action_id.startswith("A_PRUSA_START_"):
        return "lifecycle"
    if "WAIT_COOL" in action_id:
        return "cooldown"
    if "CALL_HUMAN" in action_id or "MANUAL_" in action_id or "_OPERATOR_" in action_id:
        return "operator"
    return "other"


def _normalize_observation_name(ref: str | None) -> str | None:
    if not ref:
        return None
    return Path(ref).name or None


def _load_run_meta(run_dir: Path) -> dict[str, Any]:
    run_meta = run_dir / "run_meta.json"
    if run_meta.exists():
        payload = _read_json(run_meta)
        if isinstance(payload, dict):
            return payload
    return {}


def _join_remote_ref(ref: str, *, run_meta: dict[str, Any]) -> str:
    if ref.startswith("/"):
        return ref
    remote_code = run_meta.get("remote_code")
    if isinstance(remote_code, str) and remote_code:
        return str(Path(remote_code) / ref)
    return ref


def _replace_remote_segment(remote_path: str, old: str, new: str) -> str | None:
    marker = f"/{old}/"
    if marker not in remote_path:
        return None
    return remote_path.replace(marker, f"/{new}/", 1)


def _collect_remote_vision_refs(cycle_paths: list[Path], *, run_meta: dict[str, Any]) -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {"observations": [], "replays": [], "frames": []}
    seen = {key: set() for key in refs}

    def add(kind: str, value: str | None) -> None:
        if not value:
            return
        normalized = _join_remote_ref(value, run_meta=run_meta)
        if normalized in seen[kind]:
            return
        seen[kind].add(normalized)
        refs[kind].append(normalized)

    for path in cycle_paths:
        payload = _read_json(path)
        facts = ((payload.get("decision_input") or {}).get("facts") or {})
        observation_ref = facts.get("printer_1.vision_observation_ref")
        frame_ref = facts.get("printer_1.vision_frame_ref")
        add("observations", observation_ref)
        add("frames", frame_ref)
        if isinstance(observation_ref, str) and observation_ref:
            replay_ref = _replace_remote_segment(_join_remote_ref(observation_ref, run_meta=run_meta), "observations", "replays")
            add("replays", replay_ref)
    return refs


def _scp_copy(*, ssh_target: str, ssh_key: Path | None, remote_path: str, local_path: Path, dry_run: bool) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    if dry_run:
        return
    cmd = ["scp", "-o", "BatchMode=yes"]
    if ssh_key is not None:
        cmd.extend(["-i", str(ssh_key)])
    cmd.extend([f"{ssh_target}:{remote_path}", str(local_path)])
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def sync_remote_vision_artifacts(
    *,
    run_dir: Path,
    ssh_target: str,
    ssh_key: Path | None,
    dry_run: bool,
) -> Path:
    cycle_paths = sorted((run_dir / "runtime_cycles").glob("*.json"))
    run_meta = _load_run_meta(run_dir)
    refs = _collect_remote_vision_refs(cycle_paths, run_meta=run_meta)
    sync_root = run_dir / "vision_sync"
    for kind, remote_paths in refs.items():
        for remote_path in remote_paths:
            if not remote_path.startswith("/"):
                continue
            destination = sync_root / kind / Path(remote_path).name
            if destination.exists():
                continue
            _scp_copy(
                ssh_target=ssh_target,
                ssh_key=ssh_key,
                remote_path=remote_path,
                local_path=destination,
                dry_run=dry_run,
            )
    return sync_root


@dataclass
class RunMetrics:
    run_id: str
    condition: str
    planner_model: str | None
    planner_reasoning_effort: str | None
    vision_model: str | None
    file_name: str | None
    filament: str | None
    printer: str
    started_at: str | None
    finished_at: str | None
    print_duration_min: float | None
    success: bool | None
    planner_calls: int
    planner_prompt_tokens: int | None
    planner_completion_tokens: int | None
    planner_total_cost_usd: float | None
    planner_latency_p50_ms: float | None
    planner_latency_p95_ms: float | None
    vision_calls: int
    vision_prompt_tokens: int | None
    vision_completion_tokens: int | None
    vision_total_cost_usd: float | None
    vision_latency_p50_ms: float | None
    vision_latency_p95_ms: float | None
    total_plans: int
    total_actions: int
    action_families: str
    final_quality_score: int | None
    final_stringing_score: int | None
    notes: str | None
    run_dir: str

    def to_csv_row(self) -> dict[str, str]:
        payload = {
            "run_id": self.run_id,
            "condition": self.condition,
            "planner_model": self.planner_model,
            "planner_reasoning_effort": self.planner_reasoning_effort,
            "vision_model": self.vision_model,
            "file_name": self.file_name,
            "filament": self.filament,
            "printer": self.printer,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "print_duration_min": self.print_duration_min,
            "success": self.success,
            "planner_calls": self.planner_calls,
            "planner_prompt_tokens": self.planner_prompt_tokens,
            "planner_completion_tokens": self.planner_completion_tokens,
            "planner_total_cost_usd": self.planner_total_cost_usd,
            "planner_latency_p50_ms": self.planner_latency_p50_ms,
            "planner_latency_p95_ms": self.planner_latency_p95_ms,
            "vision_calls": self.vision_calls,
            "vision_prompt_tokens": self.vision_prompt_tokens,
            "vision_completion_tokens": self.vision_completion_tokens,
            "vision_total_cost_usd": self.vision_total_cost_usd,
            "vision_latency_p50_ms": self.vision_latency_p50_ms,
            "vision_latency_p95_ms": self.vision_latency_p95_ms,
            "total_plans": self.total_plans,
            "total_actions": self.total_actions,
            "action_families": self.action_families,
            "final_quality_score": self.final_quality_score,
            "final_stringing_score": self.final_stringing_score,
            "notes": self.notes,
            "run_dir": self.run_dir,
        }
        return {key: _csv_value(value) for key, value in payload.items()}

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "condition": self.condition,
            "planner": {
                "model": self.planner_model,
                "reasoning_effort": self.planner_reasoning_effort,
                "calls": self.planner_calls,
                "prompt_tokens": self.planner_prompt_tokens,
                "completion_tokens": self.planner_completion_tokens,
                "total_cost_usd": self.planner_total_cost_usd,
                "latency_p50_ms": self.planner_latency_p50_ms,
                "latency_p95_ms": self.planner_latency_p95_ms,
            },
            "vision": {
                "model": self.vision_model,
                "calls": self.vision_calls,
                "prompt_tokens": self.vision_prompt_tokens,
                "completion_tokens": self.vision_completion_tokens,
                "total_cost_usd": self.vision_total_cost_usd,
                "latency_p50_ms": self.vision_latency_p50_ms,
                "latency_p95_ms": self.vision_latency_p95_ms,
            },
            "print": {
                "file_name": self.file_name,
                "filament": self.filament,
                "printer": self.printer,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "duration_min": self.print_duration_min,
                "success": self.success,
            },
            "runtime": {
                "total_plans": self.total_plans,
                "total_actions": self.total_actions,
                "action_families": self.action_families.split("|") if self.action_families else [],
                "notes": [self.notes] if self.notes else [],
            },
            "outcome": {
                "final_quality_score": self.final_quality_score,
                "final_stringing_score": self.final_stringing_score,
                "summary": self.notes or "",
            },
            "artifacts": {
                "run_dir": self.run_dir,
            },
        }


def _collect_planner_metrics(cycle_paths: list[Path]) -> dict[str, Any]:
    provider_rows: list[dict[str, Any]] = []
    for path in cycle_paths:
        payload = _read_json(path)
        provider = payload.get("planner_provider_metadata") or {}
        if provider:
            provider_rows.append(provider)
    return {
        "calls": len(provider_rows),
        "model": _most_common_nonempty([row.get("model") for row in provider_rows]),
        "prompt_tokens": _sum_optional([row.get("tokens_prompt") for row in provider_rows]),
        "completion_tokens": _sum_optional([row.get("tokens_completion") for row in provider_rows]),
        "total_cost_usd": _sum_optional(
            [
                (row.get("usage") or {}).get("cost") if isinstance(row.get("usage"), dict) else row.get("cost")
                for row in provider_rows
            ]
        ),
        "latency_p50_ms": _percentile(
            [float(row["latency_ms"]) for row in provider_rows if isinstance(row.get("latency_ms"), (int, float))],
            0.50,
        ),
        "latency_p95_ms": _percentile(
            [float(row["latency_ms"]) for row in provider_rows if isinstance(row.get("latency_ms"), (int, float))],
            0.95,
        ),
    }


def _resolve_vision_metadata_by_name(name: str, vision_root: Path) -> dict[str, Any] | None:
    replay_path = vision_root / "replays" / name
    if replay_path.exists():
        return _read_json(replay_path)
    observation_path = vision_root / "observations" / name
    if observation_path.exists():
        return _read_json(observation_path)
    return None


def _candidate_vision_roots(run_dir: Path, vision_root: Path) -> list[Path]:
    candidates: list[Path] = [vision_root]
    sync_root = run_dir / "vision_sync"
    if sync_root.exists():
        candidates.append(sync_root)
    remote_bundle = run_dir / "remote_run_bundle"
    if remote_bundle.exists():
        candidates.extend(path for path in remote_bundle.glob("**/vision") if path.is_dir())
    deduped: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(candidate)
    return deduped


def _extract_observation_names(cycle_paths: list[Path]) -> list[str]:
    names: list[str] = []
    for path in cycle_paths:
        payload = _read_json(path)
        facts = ((payload.get("decision_input") or {}).get("facts") or {})
        name = _normalize_observation_name(facts.get("printer_1.vision_observation_ref"))
        if name:
            names.append(name)
    deduped = []
    seen = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        deduped.append(name)
    return deduped


def _collect_vision_metrics(cycle_paths: list[Path], vision_root: Path) -> dict[str, Any]:
    observation_names = _extract_observation_names(cycle_paths)
    run_dir = cycle_paths[0].parents[1] if cycle_paths else vision_root
    candidate_roots = _candidate_vision_roots(run_dir, vision_root)
    payloads = []
    for name in observation_names:
        for candidate_root in candidate_roots:
            payload = _resolve_vision_metadata_by_name(name, candidate_root)
            if payload:
                payloads.append(payload)
                break

    provider_rows = []
    for payload in payloads:
        provider = payload.get("provider_metadata") or {}
        if provider:
            provider_rows.append(provider)

    return {
        "calls": len(observation_names),
        "model": _most_common_nonempty(
            [row.get("model") for row in provider_rows]
            + [payload.get("model") for payload in payloads if payload.get("model")]
            + [((payload.get("request_payload") or {}).get("model")) for payload in payloads if isinstance(payload.get("request_payload"), dict)]
        ),
        "prompt_tokens": _sum_optional([row.get("tokens_prompt") for row in provider_rows]),
        "completion_tokens": _sum_optional([row.get("tokens_completion") for row in provider_rows]),
        "total_cost_usd": _sum_optional(
            [
                (row.get("usage") or {}).get("cost") if isinstance(row.get("usage"), dict) else row.get("cost")
                for row in provider_rows
            ]
        ),
        "latency_p50_ms": _percentile(
            [float(row["latency_ms"]) for row in provider_rows if isinstance(row.get("latency_ms"), (int, float))],
            0.50,
        ),
        "latency_p95_ms": _percentile(
            [float(row["latency_ms"]) for row in provider_rows if isinstance(row.get("latency_ms"), (int, float))],
            0.95,
        ),
    }


def _collect_action_metrics(snapshot_path: Path) -> dict[str, Any]:
    if not snapshot_path.exists():
        return {"total_actions": 0, "families": ""}

    conn = sqlite3.connect(snapshot_path)
    try:
        rows = list(conn.execute("SELECT action_id FROM action_runs ORDER BY created_ts_ms ASC"))
    finally:
        conn.close()

    action_ids = [str(row[0]) for row in rows]
    family_counts = Counter(_action_family(action_id) for action_id in action_ids)
    families = "|".join(f"{family}:{count}" for family, count in sorted(family_counts.items()))
    return {"total_actions": len(action_ids), "families": families}


def _derive_timing(run_dir: Path) -> tuple[str | None, str | None, float | None]:
    status_polls = run_dir / "status_polls.json"
    if status_polls.exists():
        payload = _read_json(status_polls)
        if isinstance(payload, list) and payload:
            started = _parse_ts(payload[0].get("ts"))
            finished = _parse_ts(payload[-1].get("ts"))
            if started and finished:
                duration = round((finished - started).total_seconds() / 60.0, 3)
                return started.isoformat(), finished.isoformat(), duration

    checkpoints = run_dir / "checkpoints.json"
    if checkpoints.exists():
        payload = _read_json(checkpoints)
        if isinstance(payload, list) and payload:
            started = _parse_ts(payload[0].get("ts"))
            finished = _parse_ts(payload[-1].get("ts"))
            if started and finished:
                duration = round((finished - started).total_seconds() / 60.0, 3)
                return started.isoformat(), finished.isoformat(), duration
    return None, None, None


def _derive_file_name(run_dir: Path) -> str | None:
    run_meta = run_dir / "run_meta.json"
    if run_meta.exists():
        payload = _read_json(run_meta)
        file_name = payload.get("file_name")
        if isinstance(file_name, str) and file_name:
            return file_name
    for name in ("initial_status.json", "final_status.json"):
        path = run_dir / name
        if path.exists():
            payload = _read_json(path)
            file_name = payload.get("current_file")
            if isinstance(file_name, str) and file_name:
                return Path(file_name).name
    return None


def _derive_success(run_dir: Path) -> bool | None:
    final_status = run_dir / "final_status.json"
    if not final_status.exists():
        return None
    payload = _read_json(final_status)
    lifecycle = payload.get("lifecycle")
    if lifecycle is None:
        return None
    return lifecycle == "FINISHED"


def build_metrics(
    *,
    run_dir: Path,
    condition: str,
    vision_root: Path,
    planner_reasoning_effort: str | None,
    filament: str | None,
    printer: str,
    quality_score: int | None,
    stringing_score: int | None,
    notes: str | None,
    planner_model_override: str | None,
    vision_model_override: str | None,
) -> RunMetrics:
    cycle_paths = sorted((run_dir / "runtime_cycles").glob("*.json"))
    planner = _collect_planner_metrics(cycle_paths)
    vision = _collect_vision_metrics(cycle_paths, vision_root)
    actions = _collect_action_metrics(run_dir / "runtime.db.snapshot")
    started_at, finished_at, duration_min = _derive_timing(run_dir)
    file_name = _derive_file_name(run_dir)
    success = _derive_success(run_dir)

    planner_prompt_tokens = int(planner["prompt_tokens"]) if planner["prompt_tokens"] is not None else None
    planner_completion_tokens = int(planner["completion_tokens"]) if planner["completion_tokens"] is not None else None
    vision_prompt_tokens = int(vision["prompt_tokens"]) if vision["prompt_tokens"] is not None else None
    vision_completion_tokens = int(vision["completion_tokens"]) if vision["completion_tokens"] is not None else None

    return RunMetrics(
        run_id=run_dir.name,
        condition=condition,
        planner_model=planner_model_override or planner["model"],
        planner_reasoning_effort=planner_reasoning_effort,
        vision_model=vision_model_override or vision["model"],
        file_name=file_name,
        filament=filament,
        printer=printer,
        started_at=started_at,
        finished_at=finished_at,
        print_duration_min=duration_min,
        success=success,
        planner_calls=planner["calls"],
        planner_prompt_tokens=planner_prompt_tokens,
        planner_completion_tokens=planner_completion_tokens,
        planner_total_cost_usd=planner["total_cost_usd"],
        planner_latency_p50_ms=planner["latency_p50_ms"],
        planner_latency_p95_ms=planner["latency_p95_ms"],
        vision_calls=vision["calls"],
        vision_prompt_tokens=vision_prompt_tokens,
        vision_completion_tokens=vision_completion_tokens,
        vision_total_cost_usd=vision["total_cost_usd"],
        vision_latency_p50_ms=vision["latency_p50_ms"],
        vision_latency_p95_ms=vision["latency_p95_ms"],
        total_plans=len(cycle_paths),
        total_actions=actions["total_actions"],
        action_families=actions["families"],
        final_quality_score=quality_score,
        final_stringing_score=stringing_score,
        notes=notes,
        run_dir=str(run_dir),
    )


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


def _store_metrics_json(metrics: RunMetrics, experiment_root: Path) -> Path:
    output_dir = experiment_root / metrics.condition / metrics.run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "run_metrics.json"
    output_path.write_text(json.dumps(metrics.to_json(), indent=2) + "\n")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract Wallee run metrics and append them to the experimentation ledger.")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--condition", default="wallee", choices=["stock", "wallee"])
    parser.add_argument("--ledger", type=Path, default=_default_ledger_path())
    parser.add_argument("--experiment-root", type=Path, default=_default_experiment_root())
    parser.add_argument("--vision-root", type=Path, default=_repo_root() / "docs" / "evidence" / "prusa_core_one_plus" / "vision")
    parser.add_argument("--planner-reasoning-effort")
    parser.add_argument("--planner-model")
    parser.add_argument("--vision-model")
    parser.add_argument("--filament")
    parser.add_argument("--printer", default="Prusa CORE One/+")
    parser.add_argument("--quality-score", type=int)
    parser.add_argument("--stringing-score", type=int)
    parser.add_argument("--notes")
    parser.add_argument("--sync-vision", action="store_true")
    parser.add_argument("--ssh-target", default="b0@192.168.0.188")
    parser.add_argument("--ssh-key", type=Path, default=Path("/Users/anieyrudh/.ssh/wallee_pi"))
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.sync_vision:
        sync_remote_vision_artifacts(
            run_dir=args.run_dir,
            ssh_target=args.ssh_target,
            ssh_key=args.ssh_key,
            dry_run=args.dry_run,
        )

    metrics = build_metrics(
        run_dir=args.run_dir,
        condition=args.condition,
        vision_root=args.vision_root,
        planner_reasoning_effort=args.planner_reasoning_effort,
        filament=args.filament,
        printer=args.printer,
        quality_score=args.quality_score,
        stringing_score=args.stringing_score,
        notes=args.notes,
        planner_model_override=args.planner_model,
        vision_model_override=args.vision_model,
    )

    rows, fieldnames = _read_ledger_rows(args.ledger)
    existing_index = next((idx for idx, row in enumerate(rows) if row.get("run_id") == metrics.run_id), None)
    if existing_index is not None and not args.replace:
        raise SystemExit(f"Run id already exists in ledger: {metrics.run_id}. Use --replace to update it.")

    row = metrics.to_csv_row()
    if existing_index is not None:
        rows[existing_index] = row
    else:
        rows.append(row)

    metrics_json_path = _store_metrics_json(metrics, args.experiment_root)

    if not args.dry_run:
        _write_ledger_rows(args.ledger, fieldnames, rows)

    print(json.dumps({"run_id": metrics.run_id, "ledger": str(args.ledger), "metrics_json": str(metrics_json_path), "dry_run": args.dry_run}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
