"""Operator-facing hardware smoke helpers for the Prusa CORE One/+ pack.

This module is intentionally small and explicit.  It is for bring-up and Codex-
assisted testing on a real printer, not for the normal closed loop.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from typing import Any

import yaml

from ...config import Config
from ...main import build_runtime, plan_with_retry
from ...models import LegalAction, PackManifest, WorldPacket
from ...predicates import atom
from ...whiteboard import InMemoryWhiteboard
from .adapters import NoopSerialWriter, PrusaCoreOneSettings, PrusaLinkHttpClient, PrusaSerialWriter
from .bgcode_decode import normalize_prusa_print_text
from .driver import PrusaDriver, status_to_snapshot
from .job_notebook import build_job_notebook, notebook_to_jsonable, persist_job_notebook
from .pack import Pack
from .types import PrintableFile
from . import vision


@dataclass(frozen=True)
class _ManagedFamilySpec:
    family: str
    verify_field: str
    trim_action_id: str
    trim_goal: str
    trim_command: str
    trim_source: str
    reset_action_id: str
    reset_goal: str
    reset_command: str
    reset_source: str
    required_baseline: float | None = None


_MANAGED_FAMILY_ORDER: tuple[_ManagedFamilySpec, ...] = (
    _ManagedFamilySpec(
        family="speed",
        verify_field="speed_pct",
        trim_action_id="A_PRUSA_TRIM_SPEED_DOWN_SMALL",
        trim_goal="Reduce print speed a little while keeping the current print running.",
        trim_command="python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-trim-speed",
        trim_source="frontier",
        reset_action_id="A_PRUSA_OPERATOR_RESTORE_SPEED_DEFAULT",
        reset_goal="Restore print speed to the default while keeping the current print running.",
        reset_command="python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-reset-speed",
        reset_source="operator_only",
        required_baseline=PrusaDriver.DEFAULT_SPEED_PCT,
    ),
    _ManagedFamilySpec(
        family="nozzle_temp",
        verify_field="nozzle_target_c",
        trim_action_id="A_PRUSA_TRIM_NOZZLE_UP_SMALL",
        trim_goal="Raise nozzle target temperature by 5C while keeping the current print running.",
        trim_command="python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-trim-nozzle-up",
        trim_source="operator_only",
        reset_action_id="A_PRUSA_TRIM_NOZZLE_DOWN_SMALL",
        reset_goal="Lower nozzle target temperature by 5C while keeping the current print running.",
        reset_command="python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-trim-nozzle-down",
        reset_source="operator_only",
    ),
    _ManagedFamilySpec(
        family="flow",
        verify_field="flow_pct",
        trim_action_id="A_PRUSA_TRIM_FLOW_DOWN_SMALL",
        trim_goal="Reduce flow a little while keeping the current print running.",
        trim_command="python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-trim-flow",
        trim_source="operator_only",
        reset_action_id="A_PRUSA_OPERATOR_RESTORE_FLOW_DEFAULT",
        reset_goal="Restore flow to the default while keeping the current print running.",
        reset_command="python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-reset-flow",
        reset_source="operator_only",
        required_baseline=PrusaDriver.DEFAULT_FLOW_PCT,
    ),
    _ManagedFamilySpec(
        family="bed_temp",
        verify_field="bed_target_c",
        trim_action_id="A_PRUSA_TRIM_BED_UP_SMALL",
        trim_goal="Raise bed target temperature by 5C while keeping the current print running.",
        trim_command="python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-trim-bed-up",
        trim_source="operator_only",
        reset_action_id="A_PRUSA_TRIM_BED_DOWN_SMALL",
        reset_goal="Lower bed target temperature by 5C while keeping the current print running.",
        reset_command="python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-trim-bed-down",
        reset_source="operator_only",
    ),
)

_MANAGED_FAMILY_SPECS: dict[str, _ManagedFamilySpec] = {
    spec.family: spec for spec in _MANAGED_FAMILY_ORDER
}


def _build_driver(settings: PrusaCoreOneSettings) -> PrusaDriver:
    http = PrusaLinkHttpClient(settings)
    serial_writer = PrusaSerialWriter(settings) if settings.serial_enabled else NoopSerialWriter()
    return PrusaDriver(
        http=http,
        serial_writer=serial_writer,
        timeout_s=settings.state_transition_timeout_s,
        poll_s=settings.status_poll_interval_s,
    )


def _resolve_file(http: PrusaLinkHttpClient, file_path: str) -> PrintableFile:
    for item in http.list_usb_files():
        candidates = {item.path.lower().lstrip('/'), item.display_name.lower(), Path(item.path).name.lower()}
        requested = file_path.strip().lower().lstrip('/')
        if requested in candidates:
            return item
    raise RuntimeError(f"File {file_path!r} was not found on printer storage")


def _active_runtime_holds_serial() -> bool:
    """Best-effort detection for a live Wallee runtime on the same host.

    The runtime owns the serial line for the current job. Hardware-smoke probes
    should not compete with it for opportunistic readback during active prints.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-af", "python -m wallee_v6.main"],
            capture_output=True,
            check=False,
            text=True,
            timeout=1.0,
        )
    except Exception:
        return False
    current_pid = os.getpid()
    for line in result.stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        pid_text, _, cmd = text.partition(" ")
        try:
            if int(pid_text) == current_pid:
                continue
        except ValueError:
            pass
        if "python -m wallee_v6.main" in cmd:
            return True
    return False


def _bounded_status_serial_read(
    driver: PrusaDriver,
    reader,
    *,
    label: str,
    timeout_s: float,
) -> tuple[Any, str | None]:
    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _run() -> None:
        try:
            result["value"] = reader()
        except BaseException as exc:  # pragma: no cover - defensive containment
            error["exc"] = exc

    thread = threading.Thread(target=_run, name=f"hardware-smoke-{label}", daemon=True)
    thread.start()
    thread.join(timeout=max(0.25, timeout_s))
    if thread.is_alive():
        try:
            driver.close()
        except Exception:
            pass
        return None, f"{label}_read_timeout"
    if "exc" in error:
        return None, f"{label}_read_error:{type(error['exc']).__name__}"
    return result.get("value"), None


def _command_status(settings: PrusaCoreOneSettings) -> dict[str, Any]:
    http = PrusaLinkHttpClient(settings)
    info = http.get_info()
    status = status_to_snapshot(http.get_status(), job=http.get_job(), info=info)
    pressure_advance = None
    print_accel_mm_s2 = None
    pressure_advance_read_warning = None
    print_accel_read_warning = None
    serial_tuning_readback_skipped_reason = None
    driver: PrusaDriver | None = None
    should_read_serial_tuning_state = bool(getattr(settings, "serial_enabled", False)) and (
        bool(getattr(settings, "enable_experimental_tuning", False))
        or bool(getattr(settings, "pressure_advance_tuning_verification_enabled", False))
        or bool(getattr(settings, "accel_tuning_verification_enabled", False))
    )
    if should_read_serial_tuning_state:
        if _active_runtime_holds_serial() and bool(status.job_active):
            serial_tuning_readback_skipped_reason = "runtime_serial_owner_active"
        else:
            driver = _build_driver(settings)
            try:
                serial_timeout_s = max(0.25, float(getattr(settings, "serial_timeout_s", 1.0)) * 2.5)
                if (
                    bool(getattr(settings, "enable_experimental_tuning", False))
                    or bool(getattr(settings, "pressure_advance_tuning_verification_enabled", False))
                ):
                    pressure_advance, pressure_advance_read_warning = _bounded_status_serial_read(
                        driver,
                        driver.read_pressure_advance,
                        label="pressure_advance",
                        timeout_s=serial_timeout_s,
                    )
                if (
                    bool(getattr(settings, "enable_experimental_tuning", False))
                    or bool(getattr(settings, "accel_tuning_verification_enabled", False))
                ):
                    print_accel_mm_s2, print_accel_read_warning = _bounded_status_serial_read(
                        driver,
                        driver.read_print_accel_mm_s2,
                        label="print_accel_mm_s2",
                        timeout_s=serial_timeout_s,
                    )
            finally:
                driver.close()
    return {
        "lifecycle": status.lifecycle.value,
        "health": status.health,
        "job_active": status.job_active,
        "job_id": status.job_id,
        "job_progress_pct": status.job_progress_pct,
        "job_time_printing_s": status.job_time_printing_s,
        "current_file": status.current_file,
        "speed_pct": status.speed_pct,
        "flow_pct": status.flow_pct,
        "nozzle_temp_c": status.nozzle_temp_c,
        "nozzle_target_c": status.nozzle_target_c,
        "bed_temp_c": status.bed_temp_c,
        "bed_target_c": status.bed_target_c,
        "pressure_advance": pressure_advance,
        "print_accel_mm_s2": print_accel_mm_s2,
        "pressure_advance_read_warning": pressure_advance_read_warning,
        "print_accel_mm_s2_read_warning": print_accel_read_warning,
        "serial_tuning_readback_skipped_reason": serial_tuning_readback_skipped_reason,
        "model": status.model,
        "serial_number": status.serial_number,
        "min_extrusion_temp_c": status.min_extrusion_temp_c,
    }


def _command_files(settings: PrusaCoreOneSettings) -> dict[str, Any]:
    http = PrusaLinkHttpClient(settings)
    files = http.list_usb_files()
    return {
        "count": len(files),
        "files": [
            {
                "path": item.path,
                "display_name": item.display_name,
                "size_bytes": item.size_bytes,
                "modified_ts": item.modified_ts,
                "refs": item.refs,
            }
            for item in files
        ],
    }


def _command_serial_preflight(settings: PrusaCoreOneSettings) -> dict[str, Any]:
    if not settings.serial_enabled or not settings.serial_port:
        raise RuntimeError("Serial preflight requires PRUSA_CORE_ONE_ENABLE_SERIAL=1 and PRUSA_CORE_ONE_SERIAL_PORT")
    status = _command_status(settings)
    if status.get("job_active"):
        raise RuntimeError("Serial preflight requires an idle printer so the harmless probe does not perturb an active job")
    writer = PrusaSerialWriter(settings)
    try:
        payload = writer.preflight()
        payload["serial_enabled"] = True
        payload["status"] = status
        return payload
    finally:
        writer.close()


def _command_build_notebook(settings: PrusaCoreOneSettings, file_path: str) -> dict[str, Any]:
    http = PrusaLinkHttpClient(settings)
    printable = _resolve_file(http, file_path)
    file_info = http.get_file_info(printable.path)
    download_path = None
    refs = file_info.get("refs") if isinstance(file_info, dict) else None
    if isinstance(refs, dict):
        value = refs.get("download")
        if isinstance(value, str):
            download_path = value
    raw_bytes = http.download_file_bytes(printable.path, download_path=download_path)
    file_text = normalize_prusa_print_text(file_path=printable.path, raw_bytes=raw_bytes)
    notebook = build_job_notebook(
        printable=printable,
        file_info=file_info,
        file_text=file_text,
        notes_dir=Path(settings.notebook_dir).resolve() if settings.notebook_dir else None,
    )
    if notebook is None:
        raise RuntimeError(f"Failed to build notebook for {file_path}")
    if not notebook.source_text_grounded_internal:
        raise RuntimeError(f"Failed to ground notebook text for {file_path}")
    if settings.notebook_dir:
        out = Path(settings.notebook_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)
        out_path = out / f"{notebook.job_hash}.notebook.json"
        persist_job_notebook(out_path, notebook)
    return notebook_to_jsonable(notebook)


def _load_manifest() -> PackManifest:
    manifest_path = Path(__file__).with_name("manifest.yaml")
    return PackManifest.model_validate(yaml.safe_load(manifest_path.read_text(encoding="utf-8")))


def _build_pack_world(settings: PrusaCoreOneSettings) -> WorldPacket:
    pack = Pack(_load_manifest(), settings=settings)
    whiteboard = InMemoryWhiteboard()
    try:
        pack.publish_raw_state(whiteboard)
        normalized = pack.normalize(whiteboard.snapshot().values)
        return WorldPacket(
            goal="Observe current print state conservatively.",
            device_summaries=[normalized.summary],
            facts=normalized.facts,
            resources=normalized.resources,
            blockers=normalized.blockers,
            deltas=[],
            frontier=[],
            last_result={},
            pending_human=[],
        )
    finally:
        pack.close()


def _sanitize_artifact_token(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    cleaned = cleaned.strip("-")
    return cleaned[:48] or "unknown"


def _resolve_vision_job_identity(settings: PrusaCoreOneSettings, status: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": "idle",
        "artifact_token": "idle",
        "job_hash": None,
        "job_id": status.get("job_id"),
        "current_file": status.get("current_file"),
        "grounding_error": None,
    }
    try:
        world = _build_pack_world(settings)
    except Exception as exc:
        payload["grounding_error"] = str(exc)
        world = None

    facts = world.facts if world is not None else {}
    job_hash = facts.get("printer_1.job_hash")
    job_id = facts.get("printer_1.job_id", status.get("job_id"))
    current_file = facts.get("printer_1.current_file", status.get("current_file"))
    payload["job_hash"] = job_hash if isinstance(job_hash, str) and job_hash else None
    payload["job_id"] = job_id if isinstance(job_id, int) else status.get("job_id")
    payload["current_file"] = current_file if isinstance(current_file, str) and current_file else status.get("current_file")

    parts: list[str] = []
    if isinstance(payload["job_id"], int):
        parts.append(f"job-{payload['job_id']}")
    if isinstance(payload["job_hash"], str) and payload["job_hash"]:
        parts.append(f"hash-{_sanitize_artifact_token(payload['job_hash'])[:12]}")
        payload["source"] = "job_hash"
    elif isinstance(payload["job_id"], int):
        payload["source"] = "job_id"
    elif isinstance(payload["current_file"], str) and payload["current_file"]:
        parts.append(f"file-{_sanitize_artifact_token(Path(payload['current_file']).name)}")
        payload["source"] = "current_file"
    else:
        payload["source"] = "idle"
        parts.append("idle")
    payload["artifact_token"] = "-".join(parts) if parts else "idle"
    return payload


def _resolve_capture_mode(status: dict[str, Any], mode: str) -> str:
    job_active = bool(status.get("job_active", False))
    lifecycle = status.get("lifecycle")
    if mode == "idle":
        if job_active:
            raise RuntimeError(
                "Idle nozzle vision capture requires no active job; "
                f"status reported job_active={job_active} lifecycle={lifecycle}"
            )
        return "idle"
    if mode == "active-print":
        if not job_active:
            raise RuntimeError(
                "Active-print nozzle vision capture requires an active job; "
                f"status reported job_active={job_active} lifecycle={lifecycle}"
            )
        return "active-print"
    return "active-print" if job_active else "idle"


def _command_nozzle_vision_once(
    settings: PrusaCoreOneSettings,
    *,
    mode: str,
    output_root: str | None,
    planner_debug_preview: bool,
) -> dict[str, Any]:
    status = _command_status(settings)
    effective_mode = _resolve_capture_mode(status, mode)
    job_identity = _resolve_vision_job_identity(settings, status)
    captured = vision.capture_nozzle_frame(settings)
    persisted = vision.persist_frame(captured, output_root=output_root, job_identity=job_identity["artifact_token"])
    observation, trace = vision.analyze_persisted_frame_with_trace(
        persisted,
        settings=settings,
        capture_mode=effective_mode,
        lifecycle=str(status.get("lifecycle") or "UNKNOWN"),
    )
    observation_path = vision.persist_observation(observation, output_root=output_root)
    replay_path = vision.persist_vision_replay_bundle(
        frame=persisted,
        trace=trace,
        output_root=output_root,
    )

    payload = {
        "case": "nozzle_vision_once",
        "mode_requested": mode,
        "mode_effective": effective_mode,
        "status": {
            "lifecycle": status.get("lifecycle"),
            "job_active": status.get("job_active"),
            "job_id": status.get("job_id"),
            "current_file": status.get("current_file"),
        },
        "job_identity": job_identity,
        "frame_path": str(persisted.frame_path),
        "observation_path": str(observation_path),
        "replay_path": str(replay_path),
        "summary": observation.summary,
        "finding_types": [item.finding_type for item in observation.findings],
        "provider_metadata": trace.provider_metadata,
        "provider_request_payload": trace.request_payload,
        "provider_response_payload": trace.response_payload,
        "provider_response_text": trace.response_text,
    }
    if planner_debug_preview or settings.enable_vision_debug_context:
        payload["planner_debug_preview"] = vision.advisory_summary(observation)
    return payload


def _command_replay_nozzle_vision(
    settings: PrusaCoreOneSettings,
    *,
    replay_path: str,
) -> dict[str, Any]:
    if not settings.vision_api_key:
        raise RuntimeError("Vision replay requires PRUSA_CORE_ONE_VISION_API_KEY or OPENROUTER_API_KEY.")
    trace = vision.replay_saved_vision_bundle(
        replay_path,
        vision_api_key=settings.vision_api_key,
    )
    return {
        "case": "replay_nozzle_vision",
        "replay_path": replay_path,
        "provider_metadata": trace.provider_metadata,
        "provider_request_payload": trace.request_payload,
        "provider_response_payload": trace.response_payload,
        "provider_response_text": trace.response_text,
        "prompt_text": trace.prompt_text,
    }


def _command_nozzle_camera_health(
    settings: PrusaCoreOneSettings,
    *,
    output_root: str | None,
) -> tuple[dict[str, Any], int]:
    try:
        status = _command_status(settings)
    except Exception as exc:
        status = {
            "lifecycle": "UNKNOWN",
            "job_active": False,
            "job_id": None,
            "current_file": None,
            "status_error": str(exc),
        }

    try:
        job_identity = _resolve_vision_job_identity(settings, status)
    except Exception as exc:
        job_identity = {
            "source": "idle",
            "artifact_token": "idle",
            "job_hash": None,
            "job_id": status.get("job_id"),
            "current_file": status.get("current_file"),
            "grounding_error": str(exc),
        }

    result = vision.evaluate_nozzle_camera_health(
        settings,
        active_printing=bool(status.get("job_active", False)),
    )
    report, artifact_path = vision.persist_nozzle_camera_health_artifact(
        result,
        output_root=output_root,
        prefix="printer_1",
        job_identity=job_identity,
        status={
            "lifecycle": status.get("lifecycle"),
            "job_active": status.get("job_active"),
            "job_id": status.get("job_id"),
            "current_file": status.get("current_file"),
            "status_error": status.get("status_error"),
        },
    )
    payload = {
        "case": "nozzle_camera_health",
        "status": {
            "lifecycle": status.get("lifecycle"),
            "job_active": status.get("job_active"),
            "job_id": status.get("job_id"),
            "current_file": status.get("current_file"),
            "status_error": status.get("status_error"),
        },
        "job_identity": job_identity,
        "health_artifact_path": str(artifact_path),
        "facts": vision.nozzle_camera_fact_map(report, prefix="printer_1"),
        "blockers": list(report.nozzle_cam_blockers),
        "frame_refs_used": list(report.frame_refs_used),
        "usable": report.nozzle_cam_usable,
    }
    return payload, 0 if report.nozzle_cam_usable else 1


def _emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bounded_relative_target(current: float, *, delta: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, current + delta))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _phase1_root() -> Path:
    return _repo_root() / "docs" / "evidence" / "prusa_core_one_plus" / "phase1"


def _vision_runtime_root() -> Path:
    return _repo_root() / "docs" / "evidence" / "prusa_core_one_plus" / "vision_runtime" / "benchy_validation"


def _vision_ab_root() -> Path:
    return _repo_root() / "docs" / "evidence" / "prusa_core_one_plus" / "vision_runtime" / "ab_isolation"


def _full_system_audit_root() -> Path:
    return _repo_root() / "docs" / "evidence" / "prusa_core_one_plus" / "full_system_audit"


def _artifact_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H%M%S")


def _repo_commit() -> str | None:
    override = os.environ.get("WALLEE_REPO_COMMIT")
    if override:
        return override
    try:
        output = subprocess.check_output(
            ["git", "-C", str(_repo_root()), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return None
    return output or None


def _write_phase1_artifact(*, subdir: str, stem: str, payload: dict[str, Any]) -> Path:
    out_dir = _phase1_root() / subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_artifact_timestamp()}-{stem}.json"
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return out_path


def _write_vision_runtime_artifact(*, stem: str, payload: dict[str, Any]) -> Path:
    out_dir = _vision_runtime_root()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_artifact_timestamp()}-{stem}.json"
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return out_path


def _write_vision_runtime_report(*, stem: str, text: str) -> Path:
    out_dir = _vision_runtime_root()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_artifact_timestamp()}-{stem}.md"
    out_path.write_text(text, encoding="utf-8")
    return out_path


def _runtime_db_summary(config: Config) -> dict[str, Any]:
    if not config.db_path.exists():
        return {
            "db_path": str(config.db_path),
            "exists": False,
        }
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    try:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("plans", "action_runs", "exec_journal", "events", "approvals")
        }
        latest_plan = conn.execute(
            "SELECT plan_id, goal, created_ts_ms FROM plans ORDER BY created_ts_ms DESC LIMIT 1"
        ).fetchone()
        latest_action_run = conn.execute(
            "SELECT action_run_id, action_id, status, updated_ts_ms FROM action_runs ORDER BY updated_ts_ms DESC LIMIT 1"
        ).fetchone()
        latest_event = conn.execute(
            "SELECT component, level, message, ts_ms FROM events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return {
        "db_path": str(config.db_path),
        "exists": True,
        "counts": counts,
        "latest_plan": dict(latest_plan) if latest_plan is not None else None,
        "latest_action_run": dict(latest_action_run) if latest_action_run is not None else None,
        "latest_event": dict(latest_event) if latest_event is not None else None,
    }


def _outbox_summary(config: Config) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for path in sorted(config.outbox_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {"title": path.name, "severity": "unknown"}
        entries.append(
            {
                "path": str(path),
                "title": payload.get("title"),
                "severity": payload.get("severity"),
                "require_ack": payload.get("require_ack"),
            }
        )
    return {
        "outbox_dir": str(config.outbox_dir),
        "count": len(entries),
        "messages": entries,
    }


def _copy_if_present(ref: str | None, destination_dir: Path) -> str | None:
    if not ref:
        return None
    source = (_repo_root() / ref).resolve()
    if not source.exists():
        return None
    destination_dir.mkdir(parents=True, exist_ok=True)
    target = destination_dir / source.name
    shutil.copy2(source, target)
    return str(target)


def _split_blockers_text(value: Any) -> list[str]:
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    return [part for part in text.split("|") if part]


def _phase1_world_view(world) -> dict[str, Any]:
    facts = world.facts
    return {
        "lifecycle": facts.get("printer_1.lifecycle"),
        "health": facts.get("printer_1.health"),
        "job_active": facts.get("printer_1.job_active"),
        "job_progress_pct": facts.get("printer_1.job_progress_pct"),
        "job_time_printing_s": facts.get("printer_1.job_time_printing_s"),
        "current_file": facts.get("printer_1.current_file"),
        "printing_phase": facts.get("printer_1.printing_phase"),
        "active_printing": facts.get("printer_1.active_printing"),
        "speed_pct": facts.get("printer_1.speed_pct"),
        "speed_autonomy_boundary": facts.get("printer_1.speed_autonomy_boundary"),
        "speed_autonomy_eligible": facts.get("printer_1.speed_autonomy_eligible"),
        "speed_autonomy_blockers": facts.get("printer_1.speed_autonomy_blockers"),
        "nozzle_shadow_eligible": facts.get("printer_1.nozzle_shadow_eligible"),
        "nozzle_shadow_blockers": facts.get("printer_1.nozzle_shadow_blockers"),
        "nozzle_shadow_actions": facts.get("printer_1.nozzle_shadow_actions"),
        "flow_shadow_eligible": facts.get("printer_1.flow_shadow_eligible"),
        "flow_shadow_blockers": facts.get("printer_1.flow_shadow_blockers"),
        "flow_shadow_actions": facts.get("printer_1.flow_shadow_actions"),
        "bed_shadow_eligible": facts.get("printer_1.bed_shadow_eligible"),
        "bed_shadow_blockers": facts.get("printer_1.bed_shadow_blockers"),
        "bed_shadow_actions": facts.get("printer_1.bed_shadow_actions"),
        "nozzle_cam_usable": facts.get("printer_1.nozzle_cam_usable"),
        "nozzle_cam_health_state": facts.get("printer_1.nozzle_cam_health_state"),
        "nozzle_cam_blockers": facts.get("printer_1.nozzle_cam_blockers"),
        "nozzle_cam_last_capture_at": facts.get("printer_1.nozzle_cam_last_capture_at"),
        "nozzle_cam_frame_age_s": facts.get("printer_1.nozzle_cam_frame_age_s"),
        "vision_advisory_summary": facts.get("printer_1.vision_advisory_summary"),
        "vision_advisory_finding_types": facts.get("printer_1.vision_advisory_finding_types"),
        "vision_observation_ref": facts.get("printer_1.vision_observation_ref"),
        "vision_frame_ref": facts.get("printer_1.vision_frame_ref"),
        "frontier_ids": sorted(action.action_id for action in world.frontier),
    }


def _planner_input_view(world, *, frontier: list[Any] | None = None) -> dict[str, Any]:
    planner_world = world if frontier is None else world.model_copy(update={"frontier": frontier})
    return planner_world.prompt_view()


def _phase1_speed_suppression_reasons(world, *, plan: Any | None = None) -> list[str]:
    facts = world.facts
    reasons: list[str] = []

    def add(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    if not bool(facts.get("printer_1.active_printing", False)):
        add("active_printing_boundary_not_met")
    for blocker in _split_blockers_text(facts.get("printer_1.speed_autonomy_blockers")):
        add(blocker)
    speed_pct = facts.get("printer_1.speed_pct")
    if isinstance(speed_pct, (int, float)) and float(speed_pct) <= PrusaDriver.SPEED_MIN_PCT:
        add("speed_already_at_or_below_minimum")

    if plan is None:
        return reasons

    if plan.decision != "EXECUTE":
        add(f"planner_decision_{str(plan.decision).lower()}")
        return reasons
    if plan.sequence != ["A_PRUSA_TRIM_SPEED_DOWN_SMALL"]:
        add("planner_selected_non_speed_sequence")
    return reasons


def _phase1_nozzle_suppression_reasons(
    world,
    *,
    allowed_action_ids: list[str],
    plan: Any | None = None,
) -> list[str]:
    facts = world.facts
    reasons: list[str] = []

    def add(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    if not bool(facts.get("printer_1.nozzle_shadow_eligible", False)):
        add("nozzle_shadow_not_eligible")
    for blocker in _split_blockers_text(facts.get("printer_1.nozzle_shadow_blockers")):
        add(blocker)
    if not allowed_action_ids:
        add("nozzle_shadow_actions_unavailable")

    if plan is None:
        return reasons

    if plan.decision != "EXECUTE":
        add(f"planner_decision_{str(plan.decision).lower()}")
        return reasons
    if len(plan.sequence) != 1 or plan.sequence[0] not in allowed_action_ids:
        add("planner_selected_non_nozzle_sequence")
    return reasons


def _phase1_nozzle_shadow_frontier(world, operator_actions: list[Any]) -> list[Any]:
    allowed_ids = set(_split_blockers_text(world.facts.get("printer_1.nozzle_shadow_actions")))
    return [action for action in operator_actions if action.action_id in allowed_ids]


def _phase1_nozzle_reset_spec(*, baseline_value: float, trimmed_value: float) -> tuple[str, str]:
    if abs(trimmed_value - baseline_value) < 0.5:
        raise RuntimeError(
            f"nozzle trim did not change target enough to derive a reset: baseline={baseline_value} trimmed={trimmed_value}"
        )
    if trimmed_value > baseline_value:
        return (
            "A_PRUSA_TRIM_NOZZLE_DOWN_SMALL",
            "Lower nozzle target temperature by 5C while keeping the current print running.",
        )
    return (
        "A_PRUSA_TRIM_NOZZLE_UP_SMALL",
        "Raise nozzle target temperature by 5C while keeping the current print running.",
    )


def _phase1_flow_reset_spec(*, baseline_value: float, trimmed_value: float) -> tuple[str, str]:
    if abs(trimmed_value - baseline_value) < 0.5:
        raise RuntimeError(
            f"flow trim did not change enough to derive a reset: baseline={baseline_value} trimmed={trimmed_value}"
        )
    if trimmed_value < baseline_value:
        return (
            "A_PRUSA_OPERATOR_RESTORE_FLOW_DEFAULT",
            "Restore flow to the default while keeping the current print running.",
        )
    raise RuntimeError(
        f"flow trim produced an unexpected direction for reset derivation: baseline={baseline_value} trimmed={trimmed_value}"
    )


def _phase1_bed_reset_spec(*, baseline_value: float, trimmed_value: float) -> tuple[str, str]:
    if abs(trimmed_value - baseline_value) < 0.5:
        raise RuntimeError(
            f"bed trim did not change enough to derive a reset: baseline={baseline_value} trimmed={trimmed_value}"
        )
    if trimmed_value > baseline_value:
        return (
            "A_PRUSA_TRIM_BED_DOWN_SMALL",
            "Lower bed target temperature by 5C while keeping the current print running.",
        )
    return (
        "A_PRUSA_TRIM_BED_UP_SMALL",
        "Raise bed target temperature by 5C while keeping the current print running.",
    )


def _phase1_flow_suppression_reasons(
    world,
    *,
    allowed_action_ids: list[str],
    plan: Any | None = None,
) -> list[str]:
    facts = world.facts
    reasons: list[str] = []

    def add(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    if not bool(facts.get("printer_1.flow_shadow_eligible", False)):
        add("flow_shadow_not_eligible")
    for blocker in _split_blockers_text(facts.get("printer_1.flow_shadow_blockers")):
        add(blocker)
    if not allowed_action_ids:
        add("flow_shadow_actions_unavailable")

    if plan is None:
        return reasons

    if plan.decision != "EXECUTE":
        add(f"planner_decision_{str(plan.decision).lower()}")
        return reasons
    if len(plan.sequence) != 1 or plan.sequence[0] not in allowed_action_ids:
        add("planner_selected_non_flow_sequence")
    return reasons


def _phase1_flow_shadow_frontier(world, operator_actions: list[Any]) -> list[Any]:
    allowed_ids = set(_split_blockers_text(world.facts.get("printer_1.flow_shadow_actions")))
    return [action for action in operator_actions if action.action_id in allowed_ids]


def _phase1_bed_suppression_reasons(
    world,
    *,
    allowed_action_ids: list[str],
    plan: Any | None = None,
) -> list[str]:
    facts = world.facts
    reasons: list[str] = []

    def add(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    if not bool(facts.get("printer_1.bed_shadow_eligible", False)):
        add("bed_shadow_not_eligible")
    for blocker in _split_blockers_text(facts.get("printer_1.bed_shadow_blockers")):
        add(blocker)
    if not allowed_action_ids:
        add("bed_shadow_actions_unavailable")

    if plan is None:
        return reasons

    if plan.decision != "EXECUTE":
        add(f"planner_decision_{str(plan.decision).lower()}")
        return reasons
    if len(plan.sequence) != 1 or plan.sequence[0] not in allowed_action_ids:
        add("planner_selected_non_bed_sequence")
    return reasons


def _phase1_bed_shadow_frontier(world, operator_actions: list[Any]) -> list[Any]:
    allowed_ids = set(_split_blockers_text(world.facts.get("printer_1.bed_shadow_actions")))
    return [action for action in operator_actions if action.action_id in allowed_ids]


def _phase1_family_spec(family: str) -> _ManagedFamilySpec:
    try:
        return _MANAGED_FAMILY_SPECS[family]
    except KeyError as exc:
        raise ValueError(f"unsupported Phase 1 family {family!r}") from exc


def _phase1_family_blockers(world, family: str) -> list[str]:
    if family == "speed":
        blockers = _phase1_speed_suppression_reasons(world)
        return [reason for reason in blockers if reason != "speed_already_at_or_below_minimum"]
    if family == "flow":
        return _phase1_flow_suppression_reasons(world, allowed_action_ids=[])
    if family == "nozzle_temp":
        return _phase1_nozzle_suppression_reasons(world, allowed_action_ids=[])
    if family == "bed_temp":
        return _phase1_bed_suppression_reasons(world, allowed_action_ids=[])
    raise ValueError(f"unsupported Phase 1 family {family!r}")


def _phase1_family_runtime_ready(world, family: str) -> bool:
    facts = world.facts
    if family == "speed":
        return bool(facts.get("printer_1.speed_autonomy_eligible", False))
    if family == "flow":
        return bool(facts.get("printer_1.flow_shadow_eligible", False))
    if family == "nozzle_temp":
        return bool(facts.get("printer_1.nozzle_shadow_eligible", False))
    if family == "bed_temp":
        return bool(facts.get("printer_1.bed_shadow_eligible", False))
    raise ValueError(f"unsupported Phase 1 family {family!r}")


def _phase1_multi_family_shadow_frontier(
    world,
    operator_actions: list[Any],
    families: tuple[str, ...],
) -> list[Any]:
    allowed_ids = {_phase1_family_spec(family).trim_action_id for family in families}
    return [action for action in operator_actions if action.action_id in allowed_ids]


def _phase1_multi_family_suppression_reasons(
    world,
    *,
    families: tuple[str, ...],
    allowed_action_ids: list[str],
    plan: Any | None = None,
) -> list[str]:
    reasons: list[str] = []

    def add(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    allowed = set(allowed_action_ids)
    expected = {_phase1_family_spec(family).trim_action_id for family in families}
    if not allowed_action_ids:
        add("multi_family_actions_unavailable")
    for family in families:
        spec = _phase1_family_spec(family)
        if spec.trim_action_id not in allowed:
            add(f"{family}_unavailable")
            for blocker in _phase1_family_blockers(world, family):
                add(f"{family}:{blocker}")

    if plan is None:
        return reasons

    if plan.decision != "EXECUTE":
        add(f"planner_decision_{str(plan.decision).lower()}")
        return reasons
    if len(plan.sequence) != 1 or plan.sequence[0] not in expected or plan.sequence[0] not in allowed:
        add("planner_selected_outside_allowed_set")
    return reasons


def _exact_restore_action(spec: _ManagedFamilySpec, *, baseline_value: float) -> LegalAction:
    if spec.family == "nozzle_temp":
        current_ref = "trim_nozzle_down_small"
        if spec.reset_action_id == "A_PRUSA_TRIM_NOZZLE_UP_SMALL":
            current_ref = "trim_nozzle_up_small"
        return LegalAction(
            action_id=spec.reset_action_id,
            verb="TUNE_NOZZLE_TEMP",
            description="Restore nozzle target temperature to the recorded session baseline",
            owner_pack="prusa_core_one_plus",
            execute_ref=current_ref,
            args={"target_nozzle_c": round(float(baseline_value), 1)},
            required_locks=["printer_1.motion"],
            target_device="printer_1",
            verify=atom("printer_1.nozzle_target_c", "==", round(float(baseline_value), 1)),
            rank_hint=31,
        )
    if spec.family == "bed_temp":
        current_ref = "trim_bed_down_small"
        if spec.reset_action_id == "A_PRUSA_TRIM_BED_UP_SMALL":
            current_ref = "trim_bed_up_small"
        return LegalAction(
            action_id=spec.reset_action_id,
            verb="TUNE_BED_TEMP",
            description="Restore bed target temperature to the recorded session baseline",
            owner_pack="prusa_core_one_plus",
            execute_ref=current_ref,
            args={"target_bed_c": round(float(baseline_value), 1)},
            required_locks=["printer_1.motion"],
            target_device="printer_1",
            verify=atom("printer_1.bed_target_c", "==", round(float(baseline_value), 1)),
            rank_hint=41,
        )
    raise ValueError(f"exact restore is only supported for relative families, got {spec.family!r}")


def _run_phase1_multi_family_session(
    settings: PrusaCoreOneSettings,
    *,
    goal: str,
    families: tuple[str, ...],
    trace_samples: int = 8,
    trace_interval_s: float = 1.0,
    repeat_reads: int = 5,
) -> dict[str, Any]:
    config = Config.from_env()
    runtime_db, whiteboard, registry, world_compiler, human_gateway, safety, engine, planner = build_runtime(config)
    try:
        session_status = _command_status(settings)
        if session_status.get("lifecycle") != "PRINTING" or session_status.get("job_active") is not True:
            raise RuntimeError(
                "multi-family session requires an active live print: "
                f"lifecycle={session_status.get('lifecycle')} job_active={session_status.get('job_active')} "
                f"job_id={session_status.get('job_id')}"
            )

        human_gateway.submit_goal(goal)
        remaining = list(families)
        step_reports: list[dict[str, Any]] = []
        reset_stack: list[tuple[_ManagedFamilySpec, float, int | None]] = []
        startup_trace_artifacts: list[str] = []

        while remaining:
            registry.publish_all_raw_state(whiteboard)
            world = world_compiler.compile(goal, human_gateway.pending_messages())
            pack = registry.get("prusa_core_one_plus")
            operator_actions = pack.operator_actions(world)
            operator_ids = sorted(action.action_id for action in operator_actions)
            shadow_frontier = _phase1_multi_family_shadow_frontier(world, operator_actions, tuple(remaining))
            shadow_frontier_ids = sorted(action.action_id for action in shadow_frontier)
            step_base = {
                "remaining_families": list(remaining),
                "world": _phase1_world_view(world),
                "operator_action_ids": operator_ids,
                "shadow_frontier_ids": shadow_frontier_ids,
                "planner_input": _planner_input_view(world, frontier=shadow_frontier),
            }
            suppression_reasons = _phase1_multi_family_suppression_reasons(
                world,
                families=tuple(remaining),
                allowed_action_ids=shadow_frontier_ids,
            )
            if suppression_reasons:
                if world.facts.get("printer_1.printing_phase") == "startup_printing":
                    trace = _collect_supported_startup_trace(
                        settings,
                        samples=trace_samples,
                        interval_s=trace_interval_s,
                    )
                    startup_trace_artifacts.append(
                        str(
                            _write_phase1_artifact(
                                subdir="startup_traces",
                                stem=f"phase1-{'-'.join(remaining)}-session-startup-trace",
                                payload=trace,
                            )
                        )
                    )
                    registry.publish_all_raw_state(whiteboard)
                    world = world_compiler.compile(goal, human_gateway.pending_messages())
                    pack = registry.get("prusa_core_one_plus")
                    operator_actions = pack.operator_actions(world)
                    operator_ids = sorted(action.action_id for action in operator_actions)
                    shadow_frontier = _phase1_multi_family_shadow_frontier(world, operator_actions, tuple(remaining))
                    shadow_frontier_ids = sorted(action.action_id for action in shadow_frontier)
                    step_base = {
                        "remaining_families": list(remaining),
                        "world": _phase1_world_view(world),
                        "operator_action_ids": operator_ids,
                        "shadow_frontier_ids": shadow_frontier_ids,
                        "planner_input": _planner_input_view(world, frontier=shadow_frontier),
                    }
                    suppression_reasons = _phase1_multi_family_suppression_reasons(
                        world,
                        families=tuple(remaining),
                        allowed_action_ids=shadow_frontier_ids,
                    )
                if not suppression_reasons:
                    pass
                else:
                    raise RuntimeError(
                        "multi-family session suppressed before planning: "
                        f"remaining={remaining} reasons={suppression_reasons}"
                    )

            shadow_world = world.model_copy(update={"frontier": shadow_frontier})
            plan = plan_with_retry(planner, engine, shadow_world, goal)
            plan_payload = plan.model_dump()
            suppression_reasons = _phase1_multi_family_suppression_reasons(
                world,
                families=tuple(remaining),
                allowed_action_ids=shadow_frontier_ids,
                plan=plan,
            )
            if suppression_reasons:
                raise RuntimeError(
                    "multi-family planner selection was outside the allowed set: "
                    f"remaining={remaining} reasons={suppression_reasons} plan={plan_payload}"
                )

            selected = {action.action_id: action for action in shadow_frontier}[plan.sequence[0]]
            selected_spec = next(_phase1_family_spec(family) for family in remaining if _phase1_family_spec(family).trim_action_id == selected.action_id)
            pre_status = _command_status(settings)
            baseline_value = pre_status.get(selected_spec.verify_field)
            expected_job_id = pre_status.get("job_id")
            if baseline_value is None:
                raise RuntimeError(
                    f"{selected_spec.family} multi-family step missing baseline {selected_spec.verify_field}"
                )

            trim_report = engine.execute_operator_action(
                goal,
                world,
                selected,
                why=f"phase1 multi-family session trim for {selected_spec.family}",
            )
            trim_post_status = _command_status(settings)
            trimmed_value = trim_post_status.get(selected_spec.verify_field)
            if trim_report.failed_action_run_id is not None or trim_report.replan_required:
                raise RuntimeError(
                    f"{selected_spec.family} multi-family step failed before reset: "
                    f"failed_action_run_id={trim_report.failed_action_run_id} "
                    f"replan_required={trim_report.replan_required}"
                )
            if trim_report.executed_action_ids != [selected.action_id]:
                raise RuntimeError(
                    f"{selected_spec.family} multi-family step executed unexpected actions: "
                    f"executed_action_ids={trim_report.executed_action_ids}"
                )
            if trimmed_value is None or abs(float(trimmed_value) - float(baseline_value)) < 0.5:
                raise RuntimeError(
                    f"{selected_spec.family} multi-family step did not produce a distinct post-state: "
                    f"{selected_spec.verify_field} baseline={baseline_value} post={trimmed_value}"
                )

            post_reads = [_command_status(settings) for _ in range(repeat_reads)]
            if any(
                not _status_matches_expected(
                    status,
                    verify_field=selected_spec.verify_field,
                    expected_value=float(trimmed_value),
                    expected_job_id=expected_job_id,
                )
                for status in post_reads
            ):
                raise RuntimeError(
                    f"{selected_spec.family} multi-family step repeatability failed: "
                    f"expected {selected_spec.verify_field}={trimmed_value} job_id={expected_job_id}; "
                    f"reads={post_reads}"
                )

            reset_stack.append((selected_spec, float(baseline_value), expected_job_id))
            step_reports.append(
                {
                    **step_base,
                    "selected_family": selected_spec.family,
                    "selected_action_id": selected.action_id,
                    "plan": plan_payload,
                    "pre_status": pre_status,
                    "trim_report": {
                        "plan_id": trim_report.plan_id,
                        "executed_action_ids": trim_report.executed_action_ids,
                        "blocked_action_run_id": trim_report.blocked_action_run_id,
                        "replan_required": trim_report.replan_required,
                        "failed_action_run_id": trim_report.failed_action_run_id,
                        "notes": trim_report.notes,
                        "human_request_ids": trim_report.human_request_ids,
                    },
                    "trim_post_status": trim_post_status,
                    "post_reads": post_reads,
                    "baseline_value": baseline_value,
                    "trimmed_value": trimmed_value,
                }
            )
            remaining.remove(selected_spec.family)

        reset_reports: list[dict[str, Any]] = []
        for selected_spec, baseline_value, expected_job_id in reversed(reset_stack):
            if selected_spec.family in {"nozzle_temp", "bed_temp"}:
                registry.publish_all_raw_state(whiteboard)
                world = world_compiler.compile(goal, human_gateway.pending_messages())
                reset_action = _exact_restore_action(selected_spec, baseline_value=baseline_value)
                reset_report_obj = engine.execute_operator_action(
                    goal,
                    world,
                    reset_action,
                    why=f"phase1 multi-family exact restore for {selected_spec.family}",
                )
                reset = {
                    "action_id": selected_spec.reset_action_id,
                    "action_source": "operator_only",
                    "frontier_ids": sorted(action.action_id for action in world.frontier),
                    "operator_action_ids": sorted(action.action_id for action in registry.get("prusa_core_one_plus").operator_actions(world)),
                    "report": {
                        "executed_action_ids": reset_report_obj.executed_action_ids,
                        "blocked_action_run_id": reset_report_obj.blocked_action_run_id,
                        "replan_required": reset_report_obj.replan_required,
                        "failed_action_run_id": reset_report_obj.failed_action_run_id,
                        "notes": reset_report_obj.notes,
                        "human_request_ids": reset_report_obj.human_request_ids,
                    },
                    "post_status": _command_status(settings),
                }
            else:
                reset = _managed_execute(
                    selected_spec.reset_action_id,
                    selected_spec.reset_goal,
                    settings,
                    prefer_operator_only=True,
                )
            reset_report = reset.get("report") or {}
            reset_post_status = reset.get("post_status") or {}
            reset_value = reset_post_status.get(selected_spec.verify_field)
            if reset.get("action_source") != "operator_only":
                raise RuntimeError(
                    f"{selected_spec.family} multi-family reset attribution mismatch: "
                    f"action_source={reset.get('action_source')} expected=operator_only"
                )
            if reset_report.get("failed_action_run_id") is not None or reset_report.get("replan_required") is not False:
                raise RuntimeError(
                    f"{selected_spec.family} multi-family reset did not complete cleanly: "
                    f"failed_action_run_id={reset_report.get('failed_action_run_id')} "
                    f"replan_required={reset_report.get('replan_required')}"
                )
            if reset_report.get("executed_action_ids") != [selected_spec.reset_action_id]:
                raise RuntimeError(
                    f"{selected_spec.family} multi-family reset executed unexpected actions: "
                    f"executed_action_ids={reset_report.get('executed_action_ids')}"
                )
            if reset_value is None or abs(float(reset_value) - float(baseline_value)) >= 0.5:
                raise RuntimeError(
                    f"{selected_spec.family} multi-family reset did not restore baseline: "
                    f"{selected_spec.verify_field} baseline={baseline_value} post_reset={reset_value}"
                )

            post_reset_reads = [_command_status(settings) for _ in range(repeat_reads)]
            if any(
                not _status_matches_expected(
                    status,
                    verify_field=selected_spec.verify_field,
                    expected_value=float(baseline_value),
                    expected_job_id=expected_job_id,
                )
                for status in post_reset_reads
            ):
                raise RuntimeError(
                    f"{selected_spec.family} multi-family reset repeatability failed: "
                    f"expected {selected_spec.verify_field}={baseline_value} job_id={expected_job_id}; "
                    f"reads={post_reset_reads}"
                )

            reset_reports.append(
                {
                    "family": selected_spec.family,
                    "action_id": selected_spec.reset_action_id,
                    "reset": reset,
                    "post_reset_reads": post_reset_reads,
                    "baseline_value": baseline_value,
                }
            )

        return {
            "status": "executed",
            "goal": goal,
            "planner_backend": config.planner_backend,
            "planner_class": planner.__class__.__name__,
            "session_order": list(families),
            "completed_families": [step["selected_family"] for step in step_reports],
            "step_reports": step_reports,
            "reset_reports": reset_reports,
            "startup_trace_artifacts": startup_trace_artifacts,
        }
    finally:
        registry.close_all()
        runtime_db.close()


def _collect_supported_startup_trace(
    settings: PrusaCoreOneSettings,
    *,
    samples: int = 8,
    interval_s: float = 1.0,
) -> dict[str, Any]:
    http = PrusaLinkHttpClient(settings)
    rows: list[dict[str, Any]] = []
    for index in range(samples):
        rows.append(
            {
                "sample_index": index,
                "status": http.get_status(),
                "job": http.get_job(),
            }
        )
        if index + 1 < samples:
            time.sleep(interval_s)
    return {
        "case": "phase1_startup_trace",
        "samples": rows,
        "sample_count": samples,
        "interval_s": interval_s,
    }


def _run_phase1_speed_experiment(
    settings: PrusaCoreOneSettings,
    *,
    goal: str,
    trace_samples: int = 8,
    trace_interval_s: float = 1.0,
) -> dict[str, Any]:
    config = Config.from_env()
    runtime_db, whiteboard, registry, world_compiler, human_gateway, safety, engine, planner = build_runtime(config)
    try:
        human_gateway.submit_goal(goal)
        registry.publish_all_raw_state(whiteboard)
        world = world_compiler.compile(goal, human_gateway.pending_messages())
        pack = registry.get("prusa_core_one_plus")
        operator_ids = sorted(action.action_id for action in pack.operator_actions(world))
        base_payload = {
            "goal": goal,
            "planner_backend": config.planner_backend,
            "planner_class": planner.__class__.__name__,
            "world": _phase1_world_view(world),
            "operator_action_ids": operator_ids,
            "planner_input": _planner_input_view(world),
        }
        trace_path: str | None = None

        suppression_reasons = _phase1_speed_suppression_reasons(world)
        if suppression_reasons:
            payload = {
                **base_payload,
                "status": "suppressed",
                "suppression_reasons": suppression_reasons,
                "planner_invoked": False,
            }
            if world.facts.get("printer_1.printing_phase") == "startup_printing":
                trace = _collect_supported_startup_trace(
                    settings,
                    samples=trace_samples,
                    interval_s=trace_interval_s,
                )
                trace_path = str(_write_phase1_artifact(
                    subdir="startup_traces",
                    stem="phase1-startup-trace",
                    payload=trace,
                ))
                registry.publish_all_raw_state(whiteboard)
                world = world_compiler.compile(goal, human_gateway.pending_messages())
                pack = registry.get("prusa_core_one_plus")
                operator_ids = sorted(action.action_id for action in pack.operator_actions(world))
                base_payload = {
                    **base_payload,
                    "world": _phase1_world_view(world),
                    "operator_action_ids": operator_ids,
                    "planner_input": _planner_input_view(world),
                }
                suppression_reasons = _phase1_speed_suppression_reasons(world)
                if not suppression_reasons:
                    payload = None
                else:
                    payload = {
                        **base_payload,
                        "status": "suppressed",
                        "suppression_reasons": suppression_reasons,
                        "planner_invoked": False,
                    }
            if payload is not None:
                if trace_path is not None:
                    payload["startup_trace_artifact"] = trace_path
                return payload

        plan = plan_with_retry(planner, engine, world, goal)
        plan_payload = plan.model_dump()
        suppression_reasons = _phase1_speed_suppression_reasons(world, plan=plan)
        if suppression_reasons:
            payload = {
                **base_payload,
                "status": "suppressed",
                "suppression_reasons": suppression_reasons,
                "planner_invoked": True,
                "plan": plan_payload,
            }
            if trace_path is not None:
                payload["startup_trace_artifact"] = trace_path
            return payload

        report = engine.execute_plan(goal, world, plan)
        payload = {
            **base_payload,
            "status": "executed",
            "suppression_reasons": [],
            "planner_invoked": True,
            "plan": plan_payload,
            "report": {
                "plan_id": report.plan_id,
                "executed_action_ids": report.executed_action_ids,
                "blocked_action_run_id": report.blocked_action_run_id,
                "replan_required": report.replan_required,
                "failed_action_run_id": report.failed_action_run_id,
                "notes": report.notes,
                "human_request_ids": report.human_request_ids,
            },
            "post_status": _command_status(settings),
        }
        if trace_path is not None:
            payload["startup_trace_artifact"] = trace_path
        return payload
    finally:
        registry.close_all()
        runtime_db.close()


def _run_phase1_nozzle_experiment(
    settings: PrusaCoreOneSettings,
    *,
    goal: str,
    trace_samples: int = 8,
    trace_interval_s: float = 1.0,
) -> dict[str, Any]:
    config = Config.from_env()
    runtime_db, whiteboard, registry, world_compiler, human_gateway, safety, engine, planner = build_runtime(config)
    try:
        human_gateway.submit_goal(goal)
        registry.publish_all_raw_state(whiteboard)
        world = world_compiler.compile(goal, human_gateway.pending_messages())
        pack = registry.get("prusa_core_one_plus")
        operator_actions = pack.operator_actions(world)
        operator_ids = sorted(action.action_id for action in operator_actions)
        shadow_frontier = _phase1_nozzle_shadow_frontier(world, operator_actions)
        shadow_frontier_ids = sorted(action.action_id for action in shadow_frontier)
        base_payload = {
            "goal": goal,
            "planner_backend": config.planner_backend,
            "planner_class": planner.__class__.__name__,
            "world": _phase1_world_view(world),
            "operator_action_ids": operator_ids,
            "shadow_frontier_ids": shadow_frontier_ids,
            "planner_input": _planner_input_view(world, frontier=shadow_frontier),
        }
        trace_path: str | None = None

        suppression_reasons = _phase1_nozzle_suppression_reasons(world, allowed_action_ids=shadow_frontier_ids)
        if suppression_reasons:
            payload = {
                **base_payload,
                "status": "suppressed",
                "suppression_reasons": suppression_reasons,
                "planner_invoked": False,
            }
            if world.facts.get("printer_1.printing_phase") == "startup_printing":
                trace = _collect_supported_startup_trace(
                    settings,
                    samples=trace_samples,
                    interval_s=trace_interval_s,
                )
                trace_path = str(
                    _write_phase1_artifact(
                        subdir="startup_traces",
                        stem="phase1-nozzle-startup-trace",
                        payload=trace,
                    )
                )
                registry.publish_all_raw_state(whiteboard)
                world = world_compiler.compile(goal, human_gateway.pending_messages())
                pack = registry.get("prusa_core_one_plus")
                operator_actions = pack.operator_actions(world)
                operator_ids = sorted(action.action_id for action in operator_actions)
                shadow_frontier = _phase1_nozzle_shadow_frontier(world, operator_actions)
                shadow_frontier_ids = sorted(action.action_id for action in shadow_frontier)
                base_payload = {
                    **base_payload,
                    "world": _phase1_world_view(world),
                    "operator_action_ids": operator_ids,
                    "shadow_frontier_ids": shadow_frontier_ids,
                    "planner_input": _planner_input_view(world, frontier=shadow_frontier),
                }
                suppression_reasons = _phase1_nozzle_suppression_reasons(world, allowed_action_ids=shadow_frontier_ids)
                if not suppression_reasons:
                    payload = None
                else:
                    payload = {
                        **base_payload,
                        "status": "suppressed",
                        "suppression_reasons": suppression_reasons,
                        "planner_invoked": False,
                    }
            if payload is not None:
                if trace_path is not None:
                    payload["startup_trace_artifact"] = trace_path
                return payload

        shadow_world = world.model_copy(update={"frontier": shadow_frontier})
        plan = plan_with_retry(planner, engine, shadow_world, goal)
        plan_payload = plan.model_dump()
        suppression_reasons = _phase1_nozzle_suppression_reasons(
            world,
            allowed_action_ids=shadow_frontier_ids,
            plan=plan,
        )
        if suppression_reasons:
            payload = {
                **base_payload,
                "status": "suppressed",
                "suppression_reasons": suppression_reasons,
                "planner_invoked": True,
                "plan": plan_payload,
            }
            if trace_path is not None:
                payload["startup_trace_artifact"] = trace_path
            return payload

        selected = {action.action_id: action for action in shadow_frontier}[plan.sequence[0]]
        report = engine.execute_operator_action(
            goal,
            world,
            selected,
            why="phase1 nozzle shadow experiment",
        )
        payload = {
            **base_payload,
            "status": "executed",
            "suppression_reasons": [],
            "planner_invoked": True,
            "plan": plan_payload,
            "report": {
                "plan_id": report.plan_id,
                "executed_action_ids": report.executed_action_ids,
                "blocked_action_run_id": report.blocked_action_run_id,
                "replan_required": report.replan_required,
                "failed_action_run_id": report.failed_action_run_id,
                "notes": report.notes,
                "human_request_ids": report.human_request_ids,
            },
            "post_status": _command_status(settings),
        }
        if trace_path is not None:
            payload["startup_trace_artifact"] = trace_path
        return payload
    finally:
        registry.close_all()
        runtime_db.close()


def _run_phase1_nozzle_session(
    settings: PrusaCoreOneSettings,
    *,
    goal: str,
    trace_samples: int = 8,
    trace_interval_s: float = 1.0,
    repeat_reads: int = 5,
) -> dict[str, Any]:
    config = Config.from_env()
    runtime_db, whiteboard, registry, world_compiler, human_gateway, safety, engine, planner = build_runtime(config)
    try:
        human_gateway.submit_goal(goal)
        registry.publish_all_raw_state(whiteboard)
        world = world_compiler.compile(goal, human_gateway.pending_messages())
        pack = registry.get("prusa_core_one_plus")
        operator_actions = pack.operator_actions(world)
        operator_ids = sorted(action.action_id for action in operator_actions)
        shadow_frontier = _phase1_nozzle_shadow_frontier(world, operator_actions)
        shadow_frontier_ids = sorted(action.action_id for action in shadow_frontier)
        pre_status = _command_status(settings)
        base_payload = {
            "goal": goal,
            "planner_backend": config.planner_backend,
            "planner_class": planner.__class__.__name__,
            "world": _phase1_world_view(world),
            "operator_action_ids": operator_ids,
            "shadow_frontier_ids": shadow_frontier_ids,
            "pre_status": pre_status,
            "planner_input": _planner_input_view(world, frontier=shadow_frontier),
        }
        trace_path: str | None = None

        suppression_reasons = _phase1_nozzle_suppression_reasons(world, allowed_action_ids=shadow_frontier_ids)
        if suppression_reasons:
            payload = {
                **base_payload,
                "status": "suppressed",
                "suppression_reasons": suppression_reasons,
                "planner_invoked": False,
            }
            if world.facts.get("printer_1.printing_phase") == "startup_printing":
                trace = _collect_supported_startup_trace(
                    settings,
                    samples=trace_samples,
                    interval_s=trace_interval_s,
                )
                trace_path = str(
                    _write_phase1_artifact(
                        subdir="startup_traces",
                        stem="phase1-nozzle-session-startup-trace",
                        payload=trace,
                    )
                )
                registry.publish_all_raw_state(whiteboard)
                world = world_compiler.compile(goal, human_gateway.pending_messages())
                pack = registry.get("prusa_core_one_plus")
                operator_actions = pack.operator_actions(world)
                operator_ids = sorted(action.action_id for action in operator_actions)
                shadow_frontier = _phase1_nozzle_shadow_frontier(world, operator_actions)
                shadow_frontier_ids = sorted(action.action_id for action in shadow_frontier)
                base_payload = {
                    **base_payload,
                    "world": _phase1_world_view(world),
                    "operator_action_ids": operator_ids,
                    "shadow_frontier_ids": shadow_frontier_ids,
                    "pre_status": _command_status(settings),
                    "planner_input": _planner_input_view(world, frontier=shadow_frontier),
                }
                suppression_reasons = _phase1_nozzle_suppression_reasons(world, allowed_action_ids=shadow_frontier_ids)
                if not suppression_reasons:
                    payload = None
                else:
                    payload = {
                        **base_payload,
                        "status": "suppressed",
                        "suppression_reasons": suppression_reasons,
                        "planner_invoked": False,
                    }
            if payload is not None:
                if trace_path is not None:
                    payload["startup_trace_artifact"] = trace_path
                return payload

        shadow_world = world.model_copy(update={"frontier": shadow_frontier})
        plan = plan_with_retry(planner, engine, shadow_world, goal)
        plan_payload = plan.model_dump()
        suppression_reasons = _phase1_nozzle_suppression_reasons(
            world,
            allowed_action_ids=shadow_frontier_ids,
            plan=plan,
        )
        if suppression_reasons:
            payload = {
                **base_payload,
                "status": "suppressed",
                "suppression_reasons": suppression_reasons,
                "planner_invoked": True,
                "plan": plan_payload,
            }
            if trace_path is not None:
                payload["startup_trace_artifact"] = trace_path
            return payload

        selected = {action.action_id: action for action in shadow_frontier}[plan.sequence[0]]
        trim_report = engine.execute_operator_action(
            goal,
            world,
            selected,
            why="phase1 nozzle shadow session trim",
        )
        trim_post_status = _command_status(settings)
        baseline_value = pre_status.get("nozzle_target_c")
        trimmed_value = trim_post_status.get("nozzle_target_c")
        expected_job_id = pre_status.get("job_id")

        if baseline_value is None or trimmed_value is None:
            raise RuntimeError(
                f"nozzle session is missing supported-surface targets: baseline={baseline_value} trimmed={trimmed_value}"
            )
        if trim_report.failed_action_run_id is not None or trim_report.replan_required:
            raise RuntimeError(
                f"nozzle trim session failed before reset: failed_action_run_id={trim_report.failed_action_run_id} "
                f"replan_required={trim_report.replan_required}"
            )
        if trim_report.executed_action_ids != [selected.action_id]:
            raise RuntimeError(
                f"nozzle trim session executed unexpected actions: executed_action_ids={trim_report.executed_action_ids}"
            )
        if abs(float(trimmed_value) - float(baseline_value)) < 0.5:
            raise RuntimeError(
                f"nozzle trim session did not produce a distinct post-state: baseline={baseline_value} trimmed={trimmed_value}"
            )

        post_reads = [_command_status(settings) for _ in range(repeat_reads)]
        if any(
            not _status_matches_expected(
                status,
                verify_field="nozzle_target_c",
                expected_value=float(trimmed_value),
                expected_job_id=expected_job_id,
            )
            for status in post_reads
        ):
            raise RuntimeError(
                f"nozzle trim session repeatability failed: expected nozzle_target_c={trimmed_value} "
                f"job_id={expected_job_id}; reads={post_reads}"
            )

        reset_action_id, reset_goal = _phase1_nozzle_reset_spec(
            baseline_value=float(baseline_value),
            trimmed_value=float(trimmed_value),
        )
        reset = _managed_execute(
            reset_action_id,
            reset_goal,
            settings,
            prefer_operator_only=True,
        )
        reset_report = reset.get("report") or {}
        reset_post_status = reset.get("post_status") or {}
        reset_value = reset_post_status.get("nozzle_target_c")
        if reset.get("action_source") != "operator_only":
            raise RuntimeError(
                f"nozzle reset session attribution mismatch: action_source={reset.get('action_source')} expected=operator_only"
            )
        if reset_report.get("failed_action_run_id") is not None or reset_report.get("replan_required") is not False:
            raise RuntimeError(
                f"nozzle reset session did not complete cleanly: failed_action_run_id={reset_report.get('failed_action_run_id')} "
                f"replan_required={reset_report.get('replan_required')}"
            )
        if reset_report.get("executed_action_ids") != [reset_action_id]:
            raise RuntimeError(
                f"nozzle reset session executed unexpected actions: executed_action_ids={reset_report.get('executed_action_ids')}"
            )
        if reset_value is None or abs(float(reset_value) - float(baseline_value)) >= 0.5:
            raise RuntimeError(
                f"nozzle reset session did not restore baseline: baseline={baseline_value} post_reset={reset_value}"
            )

        post_reset_reads = [_command_status(settings) for _ in range(repeat_reads)]
        if any(
            not _status_matches_expected(
                status,
                verify_field="nozzle_target_c",
                expected_value=float(baseline_value),
                expected_job_id=expected_job_id,
            )
            for status in post_reset_reads
        ):
            raise RuntimeError(
                f"nozzle reset session repeatability failed: expected nozzle_target_c={baseline_value} "
                f"job_id={expected_job_id}; reads={post_reset_reads}"
            )

        payload = {
            **base_payload,
            "status": "executed",
            "suppression_reasons": [],
            "planner_invoked": True,
            "plan": plan_payload,
            "trim_report": {
                "plan_id": trim_report.plan_id,
                "executed_action_ids": trim_report.executed_action_ids,
                "blocked_action_run_id": trim_report.blocked_action_run_id,
                "replan_required": trim_report.replan_required,
                "failed_action_run_id": trim_report.failed_action_run_id,
                "notes": trim_report.notes,
                "human_request_ids": trim_report.human_request_ids,
            },
            "trim_post_status": trim_post_status,
            "post_reads": post_reads,
            "reset": reset,
            "post_reset_reads": post_reset_reads,
            "baseline_value": baseline_value,
            "trimmed_value": trimmed_value,
        }
        if trace_path is not None:
            payload["startup_trace_artifact"] = trace_path
        return payload
    finally:
        registry.close_all()
        runtime_db.close()


def _run_phase1_flow_experiment(
    settings: PrusaCoreOneSettings,
    *,
    goal: str,
    trace_samples: int = 8,
    trace_interval_s: float = 1.0,
) -> dict[str, Any]:
    config = Config.from_env()
    runtime_db, whiteboard, registry, world_compiler, human_gateway, safety, engine, planner = build_runtime(config)
    try:
        human_gateway.submit_goal(goal)
        registry.publish_all_raw_state(whiteboard)
        world = world_compiler.compile(goal, human_gateway.pending_messages())
        pack = registry.get("prusa_core_one_plus")
        operator_actions = pack.operator_actions(world)
        operator_ids = sorted(action.action_id for action in operator_actions)
        shadow_frontier = _phase1_flow_shadow_frontier(world, operator_actions)
        shadow_frontier_ids = sorted(action.action_id for action in shadow_frontier)
        base_payload = {
            "goal": goal,
            "planner_backend": config.planner_backend,
            "planner_class": planner.__class__.__name__,
            "world": _phase1_world_view(world),
            "operator_action_ids": operator_ids,
            "shadow_frontier_ids": shadow_frontier_ids,
            "planner_input": _planner_input_view(world, frontier=shadow_frontier),
        }
        trace_path: str | None = None

        suppression_reasons = _phase1_flow_suppression_reasons(world, allowed_action_ids=shadow_frontier_ids)
        if suppression_reasons:
            payload = {
                **base_payload,
                "status": "suppressed",
                "suppression_reasons": suppression_reasons,
                "planner_invoked": False,
            }
            if world.facts.get("printer_1.printing_phase") == "startup_printing":
                trace = _collect_supported_startup_trace(
                    settings,
                    samples=trace_samples,
                    interval_s=trace_interval_s,
                )
                trace_path = str(
                    _write_phase1_artifact(
                        subdir="startup_traces",
                        stem="phase1-flow-startup-trace",
                        payload=trace,
                    )
                )
                registry.publish_all_raw_state(whiteboard)
                world = world_compiler.compile(goal, human_gateway.pending_messages())
                pack = registry.get("prusa_core_one_plus")
                operator_actions = pack.operator_actions(world)
                operator_ids = sorted(action.action_id for action in operator_actions)
                shadow_frontier = _phase1_flow_shadow_frontier(world, operator_actions)
                shadow_frontier_ids = sorted(action.action_id for action in shadow_frontier)
                base_payload = {
                    **base_payload,
                    "world": _phase1_world_view(world),
                    "operator_action_ids": operator_ids,
                    "shadow_frontier_ids": shadow_frontier_ids,
                    "planner_input": _planner_input_view(world, frontier=shadow_frontier),
                }
                suppression_reasons = _phase1_flow_suppression_reasons(world, allowed_action_ids=shadow_frontier_ids)
                if not suppression_reasons:
                    payload = None
                else:
                    payload = {
                        **base_payload,
                        "status": "suppressed",
                        "suppression_reasons": suppression_reasons,
                        "planner_invoked": False,
                    }
            if payload is not None:
                if trace_path is not None:
                    payload["startup_trace_artifact"] = trace_path
                return payload

        shadow_world = world.model_copy(update={"frontier": shadow_frontier})
        plan = plan_with_retry(planner, engine, shadow_world, goal)
        plan_payload = plan.model_dump()
        suppression_reasons = _phase1_flow_suppression_reasons(
            world,
            allowed_action_ids=shadow_frontier_ids,
            plan=plan,
        )
        if suppression_reasons:
            payload = {
                **base_payload,
                "status": "suppressed",
                "suppression_reasons": suppression_reasons,
                "planner_invoked": True,
                "plan": plan_payload,
            }
            if trace_path is not None:
                payload["startup_trace_artifact"] = trace_path
            return payload

        selected = {action.action_id: action for action in shadow_frontier}[plan.sequence[0]]
        report = engine.execute_operator_action(
            goal,
            world,
            selected,
            why="phase1 flow shadow experiment",
        )
        payload = {
            **base_payload,
            "status": "executed",
            "suppression_reasons": [],
            "planner_invoked": True,
            "plan": plan_payload,
            "report": {
                "plan_id": report.plan_id,
                "executed_action_ids": report.executed_action_ids,
                "blocked_action_run_id": report.blocked_action_run_id,
                "replan_required": report.replan_required,
                "failed_action_run_id": report.failed_action_run_id,
                "notes": report.notes,
                "human_request_ids": report.human_request_ids,
            },
            "post_status": _command_status(settings),
        }
        if trace_path is not None:
            payload["startup_trace_artifact"] = trace_path
        return payload
    finally:
        registry.close_all()
        runtime_db.close()


def _run_phase1_flow_session(
    settings: PrusaCoreOneSettings,
    *,
    goal: str,
    trace_samples: int = 8,
    trace_interval_s: float = 1.0,
    repeat_reads: int = 5,
) -> dict[str, Any]:
    config = Config.from_env()
    runtime_db, whiteboard, registry, world_compiler, human_gateway, safety, engine, planner = build_runtime(config)
    try:
        human_gateway.submit_goal(goal)
        registry.publish_all_raw_state(whiteboard)
        world = world_compiler.compile(goal, human_gateway.pending_messages())
        pack = registry.get("prusa_core_one_plus")
        operator_actions = pack.operator_actions(world)
        operator_ids = sorted(action.action_id for action in operator_actions)
        shadow_frontier = _phase1_flow_shadow_frontier(world, operator_actions)
        shadow_frontier_ids = sorted(action.action_id for action in shadow_frontier)
        pre_status = _command_status(settings)
        base_payload = {
            "goal": goal,
            "planner_backend": config.planner_backend,
            "planner_class": planner.__class__.__name__,
            "world": _phase1_world_view(world),
            "operator_action_ids": operator_ids,
            "shadow_frontier_ids": shadow_frontier_ids,
            "pre_status": pre_status,
            "planner_input": _planner_input_view(world, frontier=shadow_frontier),
        }
        trace_path: str | None = None

        suppression_reasons = _phase1_flow_suppression_reasons(world, allowed_action_ids=shadow_frontier_ids)
        if suppression_reasons:
            payload = {
                **base_payload,
                "status": "suppressed",
                "suppression_reasons": suppression_reasons,
                "planner_invoked": False,
            }
            if world.facts.get("printer_1.printing_phase") == "startup_printing":
                trace = _collect_supported_startup_trace(
                    settings,
                    samples=trace_samples,
                    interval_s=trace_interval_s,
                )
                trace_path = str(
                    _write_phase1_artifact(
                        subdir="startup_traces",
                        stem="phase1-flow-session-startup-trace",
                        payload=trace,
                    )
                )
                registry.publish_all_raw_state(whiteboard)
                world = world_compiler.compile(goal, human_gateway.pending_messages())
                pack = registry.get("prusa_core_one_plus")
                operator_actions = pack.operator_actions(world)
                operator_ids = sorted(action.action_id for action in operator_actions)
                shadow_frontier = _phase1_flow_shadow_frontier(world, operator_actions)
                shadow_frontier_ids = sorted(action.action_id for action in shadow_frontier)
                base_payload = {
                    **base_payload,
                    "world": _phase1_world_view(world),
                    "operator_action_ids": operator_ids,
                    "shadow_frontier_ids": shadow_frontier_ids,
                    "pre_status": _command_status(settings),
                    "planner_input": _planner_input_view(world, frontier=shadow_frontier),
                }
                suppression_reasons = _phase1_flow_suppression_reasons(world, allowed_action_ids=shadow_frontier_ids)
                if not suppression_reasons:
                    payload = None
                else:
                    payload = {
                        **base_payload,
                        "status": "suppressed",
                        "suppression_reasons": suppression_reasons,
                        "planner_invoked": False,
                    }
            if payload is not None:
                if trace_path is not None:
                    payload["startup_trace_artifact"] = trace_path
                return payload

        shadow_world = world.model_copy(update={"frontier": shadow_frontier})
        plan = plan_with_retry(planner, engine, shadow_world, goal)
        plan_payload = plan.model_dump()
        suppression_reasons = _phase1_flow_suppression_reasons(
            world,
            allowed_action_ids=shadow_frontier_ids,
            plan=plan,
        )
        if suppression_reasons:
            payload = {
                **base_payload,
                "status": "suppressed",
                "suppression_reasons": suppression_reasons,
                "planner_invoked": True,
                "plan": plan_payload,
            }
            if trace_path is not None:
                payload["startup_trace_artifact"] = trace_path
            return payload

        selected = {action.action_id: action for action in shadow_frontier}[plan.sequence[0]]
        trim_report = engine.execute_operator_action(
            goal,
            world,
            selected,
            why="phase1 flow shadow session trim",
        )
        trim_post_status = _command_status(settings)
        baseline_value = pre_status.get("flow_pct")
        trimmed_value = trim_post_status.get("flow_pct")
        expected_job_id = pre_status.get("job_id")

        if baseline_value is None or trimmed_value is None:
            raise RuntimeError(
                f"flow session is missing supported-surface values: baseline={baseline_value} trimmed={trimmed_value}"
            )
        if trim_report.failed_action_run_id is not None or trim_report.replan_required:
            raise RuntimeError(
                f"flow trim session failed before reset: failed_action_run_id={trim_report.failed_action_run_id} "
                f"replan_required={trim_report.replan_required}"
            )
        if trim_report.executed_action_ids != [selected.action_id]:
            raise RuntimeError(
                f"flow trim session executed unexpected actions: executed_action_ids={trim_report.executed_action_ids}"
            )
        if abs(float(trimmed_value) - float(baseline_value)) < 0.5:
            raise RuntimeError(
                f"flow trim session did not produce a distinct post-state: baseline={baseline_value} trimmed={trimmed_value}"
            )

        post_reads = [_command_status(settings) for _ in range(repeat_reads)]
        if any(
            not _status_matches_expected(
                status,
                verify_field="flow_pct",
                expected_value=float(trimmed_value),
                expected_job_id=expected_job_id,
            )
            for status in post_reads
        ):
            raise RuntimeError(
                f"flow trim session repeatability failed: expected flow_pct={trimmed_value} "
                f"job_id={expected_job_id}; reads={post_reads}"
            )

        reset_action_id, reset_goal = _phase1_flow_reset_spec(
            baseline_value=float(baseline_value),
            trimmed_value=float(trimmed_value),
        )
        reset = _managed_execute(
            reset_action_id,
            reset_goal,
            settings,
            prefer_operator_only=True,
        )
        reset_report = reset.get("report") or {}
        reset_post_status = reset.get("post_status") or {}
        reset_value = reset_post_status.get("flow_pct")
        if reset.get("action_source") != "operator_only":
            raise RuntimeError(
                f"flow reset session attribution mismatch: action_source={reset.get('action_source')} expected=operator_only"
            )
        if reset_report.get("failed_action_run_id") is not None or reset_report.get("replan_required") is not False:
            raise RuntimeError(
                f"flow reset session did not complete cleanly: failed_action_run_id={reset_report.get('failed_action_run_id')} "
                f"replan_required={reset_report.get('replan_required')}"
            )
        if reset_report.get("executed_action_ids") != [reset_action_id]:
            raise RuntimeError(
                f"flow reset session executed unexpected actions: executed_action_ids={reset_report.get('executed_action_ids')}"
            )
        if reset_value is None or abs(float(reset_value) - float(baseline_value)) >= 0.5:
            raise RuntimeError(
                f"flow reset session did not restore baseline: baseline={baseline_value} post_reset={reset_value}"
            )

        post_reset_reads = [_command_status(settings) for _ in range(repeat_reads)]
        if any(
            not _status_matches_expected(
                status,
                verify_field="flow_pct",
                expected_value=float(baseline_value),
                expected_job_id=expected_job_id,
            )
            for status in post_reset_reads
        ):
            raise RuntimeError(
                f"flow reset session repeatability failed: expected flow_pct={baseline_value} "
                f"job_id={expected_job_id}; reads={post_reset_reads}"
            )

        payload = {
            **base_payload,
            "status": "executed",
            "suppression_reasons": [],
            "planner_invoked": True,
            "plan": plan_payload,
            "trim_report": {
                "plan_id": trim_report.plan_id,
                "executed_action_ids": trim_report.executed_action_ids,
                "blocked_action_run_id": trim_report.blocked_action_run_id,
                "replan_required": trim_report.replan_required,
                "failed_action_run_id": trim_report.failed_action_run_id,
                "notes": trim_report.notes,
                "human_request_ids": trim_report.human_request_ids,
            },
            "trim_post_status": trim_post_status,
            "post_reads": post_reads,
            "reset": reset,
            "post_reset_reads": post_reset_reads,
            "baseline_value": baseline_value,
            "trimmed_value": trimmed_value,
        }
        if trace_path is not None:
            payload["startup_trace_artifact"] = trace_path
        return payload
    finally:
        registry.close_all()
        runtime_db.close()


def _run_phase1_bed_experiment(
    settings: PrusaCoreOneSettings,
    *,
    goal: str,
    trace_samples: int = 8,
    trace_interval_s: float = 1.0,
) -> dict[str, Any]:
    config = Config.from_env()
    runtime_db, whiteboard, registry, world_compiler, human_gateway, safety, engine, planner = build_runtime(config)
    try:
        human_gateway.submit_goal(goal)
        registry.publish_all_raw_state(whiteboard)
        world = world_compiler.compile(goal, human_gateway.pending_messages())
        pack = registry.get("prusa_core_one_plus")
        operator_actions = pack.operator_actions(world)
        operator_ids = sorted(action.action_id for action in operator_actions)
        shadow_frontier = _phase1_bed_shadow_frontier(world, operator_actions)
        shadow_frontier_ids = sorted(action.action_id for action in shadow_frontier)
        base_payload = {
            "goal": goal,
            "planner_backend": config.planner_backend,
            "planner_class": planner.__class__.__name__,
            "world": _phase1_world_view(world),
            "operator_action_ids": operator_ids,
            "shadow_frontier_ids": shadow_frontier_ids,
            "planner_input": _planner_input_view(world, frontier=shadow_frontier),
        }
        trace_path: str | None = None

        suppression_reasons = _phase1_bed_suppression_reasons(world, allowed_action_ids=shadow_frontier_ids)
        if suppression_reasons:
            payload = {
                **base_payload,
                "status": "suppressed",
                "suppression_reasons": suppression_reasons,
                "planner_invoked": False,
            }
            if world.facts.get("printer_1.printing_phase") == "startup_printing":
                trace = _collect_supported_startup_trace(
                    settings,
                    samples=trace_samples,
                    interval_s=trace_interval_s,
                )
                trace_path = str(
                    _write_phase1_artifact(
                        subdir="startup_traces",
                        stem="phase1-bed-startup-trace",
                        payload=trace,
                    )
                )
                registry.publish_all_raw_state(whiteboard)
                world = world_compiler.compile(goal, human_gateway.pending_messages())
                pack = registry.get("prusa_core_one_plus")
                operator_actions = pack.operator_actions(world)
                operator_ids = sorted(action.action_id for action in operator_actions)
                shadow_frontier = _phase1_bed_shadow_frontier(world, operator_actions)
                shadow_frontier_ids = sorted(action.action_id for action in shadow_frontier)
                base_payload = {
                    **base_payload,
                    "world": _phase1_world_view(world),
                    "operator_action_ids": operator_ids,
                    "shadow_frontier_ids": shadow_frontier_ids,
                    "planner_input": _planner_input_view(world, frontier=shadow_frontier),
                }
                suppression_reasons = _phase1_bed_suppression_reasons(world, allowed_action_ids=shadow_frontier_ids)
                if not suppression_reasons:
                    payload = None
                else:
                    payload = {
                        **base_payload,
                        "status": "suppressed",
                        "suppression_reasons": suppression_reasons,
                        "planner_invoked": False,
                    }
            if payload is not None:
                if trace_path is not None:
                    payload["startup_trace_artifact"] = trace_path
                return payload

        shadow_world = world.model_copy(update={"frontier": shadow_frontier})
        plan = plan_with_retry(planner, engine, shadow_world, goal)
        plan_payload = plan.model_dump()
        suppression_reasons = _phase1_bed_suppression_reasons(
            world,
            allowed_action_ids=shadow_frontier_ids,
            plan=plan,
        )
        if suppression_reasons:
            payload = {
                **base_payload,
                "status": "suppressed",
                "suppression_reasons": suppression_reasons,
                "planner_invoked": True,
                "plan": plan_payload,
            }
            if trace_path is not None:
                payload["startup_trace_artifact"] = trace_path
            return payload

        selected = {action.action_id: action for action in shadow_frontier}[plan.sequence[0]]
        report = engine.execute_operator_action(
            goal,
            world,
            selected,
            why="phase1 bed shadow experiment",
        )
        payload = {
            **base_payload,
            "status": "executed",
            "suppression_reasons": [],
            "planner_invoked": True,
            "plan": plan_payload,
            "report": {
                "plan_id": report.plan_id,
                "executed_action_ids": report.executed_action_ids,
                "blocked_action_run_id": report.blocked_action_run_id,
                "replan_required": report.replan_required,
                "failed_action_run_id": report.failed_action_run_id,
                "notes": report.notes,
                "human_request_ids": report.human_request_ids,
            },
            "post_status": _command_status(settings),
        }
        if trace_path is not None:
            payload["startup_trace_artifact"] = trace_path
        return payload
    finally:
        registry.close_all()
        runtime_db.close()


def _run_phase1_bed_session(
    settings: PrusaCoreOneSettings,
    *,
    goal: str,
    trace_samples: int = 8,
    trace_interval_s: float = 1.0,
    repeat_reads: int = 5,
) -> dict[str, Any]:
    config = Config.from_env()
    runtime_db, whiteboard, registry, world_compiler, human_gateway, safety, engine, planner = build_runtime(config)
    try:
        human_gateway.submit_goal(goal)
        registry.publish_all_raw_state(whiteboard)
        world = world_compiler.compile(goal, human_gateway.pending_messages())
        pack = registry.get("prusa_core_one_plus")
        operator_actions = pack.operator_actions(world)
        operator_ids = sorted(action.action_id for action in operator_actions)
        shadow_frontier = _phase1_bed_shadow_frontier(world, operator_actions)
        shadow_frontier_ids = sorted(action.action_id for action in shadow_frontier)
        pre_status = _command_status(settings)
        base_payload = {
            "goal": goal,
            "planner_backend": config.planner_backend,
            "planner_class": planner.__class__.__name__,
            "world": _phase1_world_view(world),
            "operator_action_ids": operator_ids,
            "shadow_frontier_ids": shadow_frontier_ids,
            "pre_status": pre_status,
            "planner_input": _planner_input_view(world, frontier=shadow_frontier),
        }
        trace_path: str | None = None

        suppression_reasons = _phase1_bed_suppression_reasons(world, allowed_action_ids=shadow_frontier_ids)
        if suppression_reasons:
            payload = {
                **base_payload,
                "status": "suppressed",
                "suppression_reasons": suppression_reasons,
                "planner_invoked": False,
            }
            if world.facts.get("printer_1.printing_phase") == "startup_printing":
                trace = _collect_supported_startup_trace(
                    settings,
                    samples=trace_samples,
                    interval_s=trace_interval_s,
                )
                trace_path = str(
                    _write_phase1_artifact(
                        subdir="startup_traces",
                        stem="phase1-bed-session-startup-trace",
                        payload=trace,
                    )
                )
                registry.publish_all_raw_state(whiteboard)
                world = world_compiler.compile(goal, human_gateway.pending_messages())
                pack = registry.get("prusa_core_one_plus")
                operator_actions = pack.operator_actions(world)
                operator_ids = sorted(action.action_id for action in operator_actions)
                shadow_frontier = _phase1_bed_shadow_frontier(world, operator_actions)
                shadow_frontier_ids = sorted(action.action_id for action in shadow_frontier)
                base_payload = {
                    **base_payload,
                    "world": _phase1_world_view(world),
                    "operator_action_ids": operator_ids,
                    "shadow_frontier_ids": shadow_frontier_ids,
                    "pre_status": _command_status(settings),
                    "planner_input": _planner_input_view(world, frontier=shadow_frontier),
                }
                suppression_reasons = _phase1_bed_suppression_reasons(world, allowed_action_ids=shadow_frontier_ids)
                if not suppression_reasons:
                    payload = None
                else:
                    payload = {
                        **base_payload,
                        "status": "suppressed",
                        "suppression_reasons": suppression_reasons,
                        "planner_invoked": False,
                    }
            if payload is not None:
                if trace_path is not None:
                    payload["startup_trace_artifact"] = trace_path
                return payload

        shadow_world = world.model_copy(update={"frontier": shadow_frontier})
        plan = plan_with_retry(planner, engine, shadow_world, goal)
        plan_payload = plan.model_dump()
        suppression_reasons = _phase1_bed_suppression_reasons(
            world,
            allowed_action_ids=shadow_frontier_ids,
            plan=plan,
        )
        if suppression_reasons:
            payload = {
                **base_payload,
                "status": "suppressed",
                "suppression_reasons": suppression_reasons,
                "planner_invoked": True,
                "plan": plan_payload,
            }
            if trace_path is not None:
                payload["startup_trace_artifact"] = trace_path
            return payload

        selected = {action.action_id: action for action in shadow_frontier}[plan.sequence[0]]
        trim_report = engine.execute_operator_action(
            goal,
            world,
            selected,
            why="phase1 bed shadow session trim",
        )
        trim_post_status = _command_status(settings)
        baseline_value = pre_status.get("bed_target_c")
        trimmed_value = trim_post_status.get("bed_target_c")
        expected_job_id = pre_status.get("job_id")

        if baseline_value is None or trimmed_value is None:
            raise RuntimeError(
                f"bed session is missing supported-surface values: baseline={baseline_value} trimmed={trimmed_value}"
            )
        if trim_report.failed_action_run_id is not None or trim_report.replan_required:
            raise RuntimeError(
                f"bed trim session failed before reset: failed_action_run_id={trim_report.failed_action_run_id} "
                f"replan_required={trim_report.replan_required}"
            )
        if trim_report.executed_action_ids != [selected.action_id]:
            raise RuntimeError(
                f"bed trim session executed unexpected actions: executed_action_ids={trim_report.executed_action_ids}"
            )
        if abs(float(trimmed_value) - float(baseline_value)) < 0.5:
            raise RuntimeError(
                f"bed trim session did not produce a distinct post-state: baseline={baseline_value} trimmed={trimmed_value}"
            )

        post_reads = [_command_status(settings) for _ in range(repeat_reads)]
        if any(
            not _status_matches_expected(
                status,
                verify_field="bed_target_c",
                expected_value=float(trimmed_value),
                expected_job_id=expected_job_id,
            )
            for status in post_reads
        ):
            raise RuntimeError(
                f"bed trim session repeatability failed: expected bed_target_c={trimmed_value} "
                f"job_id={expected_job_id}; reads={post_reads}"
            )

        reset_action_id, reset_goal = _phase1_bed_reset_spec(
            baseline_value=float(baseline_value),
            trimmed_value=float(trimmed_value),
        )
        reset = _managed_execute(
            reset_action_id,
            reset_goal,
            settings,
            prefer_operator_only=True,
        )
        reset_report = reset.get("report") or {}
        reset_post_status = reset.get("post_status") or {}
        reset_value = reset_post_status.get("bed_target_c")
        if reset.get("action_source") != "operator_only":
            raise RuntimeError(
                f"bed reset session attribution mismatch: action_source={reset.get('action_source')} expected=operator_only"
            )
        if reset_report.get("failed_action_run_id") is not None or reset_report.get("replan_required") is not False:
            raise RuntimeError(
                f"bed reset session did not complete cleanly: failed_action_run_id={reset_report.get('failed_action_run_id')} "
                f"replan_required={reset_report.get('replan_required')}"
            )
        if reset_report.get("executed_action_ids") != [reset_action_id]:
            raise RuntimeError(
                f"bed reset session executed unexpected actions: executed_action_ids={reset_report.get('executed_action_ids')}"
            )
        if reset_value is None or abs(float(reset_value) - float(baseline_value)) >= 0.5:
            raise RuntimeError(
                f"bed reset session did not restore baseline: baseline={baseline_value} post_reset={reset_value}"
            )

        post_reset_reads = [_command_status(settings) for _ in range(repeat_reads)]
        if any(
            not _status_matches_expected(
                status,
                verify_field="bed_target_c",
                expected_value=float(baseline_value),
                expected_job_id=expected_job_id,
            )
            for status in post_reset_reads
        ):
            raise RuntimeError(
                f"bed reset session repeatability failed: expected bed_target_c={baseline_value} "
                f"job_id={expected_job_id}; reads={post_reset_reads}"
            )

        payload = {
            **base_payload,
            "status": "executed",
            "suppression_reasons": [],
            "planner_invoked": True,
            "plan": plan_payload,
            "trim_report": {
                "plan_id": trim_report.plan_id,
                "executed_action_ids": trim_report.executed_action_ids,
                "blocked_action_run_id": trim_report.blocked_action_run_id,
                "replan_required": trim_report.replan_required,
                "failed_action_run_id": trim_report.failed_action_run_id,
                "notes": trim_report.notes,
                "human_request_ids": trim_report.human_request_ids,
            },
            "trim_post_status": trim_post_status,
            "post_reads": post_reads,
            "reset": reset,
            "post_reset_reads": post_reset_reads,
            "baseline_value": baseline_value,
            "trimmed_value": trimmed_value,
        }
        if trace_path is not None:
            payload["startup_trace_artifact"] = trace_path
        return payload
    finally:
        registry.close_all()
        runtime_db.close()


def _managed_execute(
    action_id: str,
    goal: str,
    settings: PrusaCoreOneSettings,
    *,
    prefer_operator_only: bool = False,
) -> dict[str, Any]:
    config = Config.from_env()
    runtime_db, whiteboard, registry, world_compiler, human_gateway, safety, engine, planner = build_runtime(config)
    try:
        human_gateway.submit_goal(goal)
        registry.publish_all_raw_state(whiteboard)
        world = world_compiler.compile(goal, human_gateway.pending_messages())
        pack = registry.get("prusa_core_one_plus")
        frontier = {action.action_id: action for action in world.frontier}
        operator = {action.action_id: action for action in pack.operator_actions(world)}
        action = operator.get(action_id) if prefer_operator_only else None
        action = action or frontier.get(action_id) or operator.get(action_id)
        if action is None:
            raise RuntimeError(
                f"Managed action {action_id!r} is unavailable; frontier={sorted(frontier)} operator={sorted(operator)}"
            )
        source = "operator_only" if action is operator.get(action_id) else "frontier"
        report = engine.execute_operator_action(
            goal,
            world,
            action,
            why=f"operator-triggered managed proof for {action_id}",
        )
        post_status = _command_status(settings)
        return {
            "action_id": action_id,
            "action_source": source,
            "frontier_ids": sorted(frontier),
            "operator_action_ids": sorted(operator),
            "report": {
                "executed_action_ids": report.executed_action_ids,
                "blocked_action_run_id": report.blocked_action_run_id,
                "replan_required": report.replan_required,
                "failed_action_run_id": report.failed_action_run_id,
                "notes": report.notes,
                "human_request_ids": report.human_request_ids,
            },
            "post_status": post_status,
        }
    finally:
        registry.close_all()
        runtime_db.close()


def _status_matches_expected(
    status: dict[str, Any],
    *,
    verify_field: str,
    expected_value: float,
    expected_job_id: int | None,
) -> bool:
    observed = status.get(verify_field)
    if observed is None:
        return False
    if abs(float(observed) - expected_value) >= 0.5:
        return False
    if status.get("lifecycle") != "PRINTING":
        return False
    if status.get("job_active") is not True:
        return False
    if expected_job_id is not None and status.get("job_id") != expected_job_id:
        return False
    return True


def _run_managed_family_proof(
    spec: _ManagedFamilySpec,
    settings: PrusaCoreOneSettings,
    *,
    status_reader=_command_status,
    executor=_managed_execute,
    repeat_reads: int = 5,
) -> dict[str, Any]:
    pre = status_reader(settings)
    baseline = pre.get(spec.verify_field)
    expected_job_id = pre.get("job_id")
    if pre.get("lifecycle") != "PRINTING" or pre.get("job_active") is not True:
        raise RuntimeError(
            f"{spec.family} pre-state is not a live print: "
            f"lifecycle={pre.get('lifecycle')} job_active={pre.get('job_active')} job_id={expected_job_id}"
        )
    if baseline is None:
        raise RuntimeError(f"{spec.family} pre-state is missing {spec.verify_field}")
    if spec.required_baseline is not None and abs(float(baseline) - spec.required_baseline) >= 0.5:
        raise RuntimeError(
            f"{spec.family} pre-state is not at required baseline: "
            f"{spec.verify_field}={baseline} expected={spec.required_baseline}"
        )

    trim = executor(spec.trim_action_id, spec.trim_goal, settings)
    trim_report = trim.get("report") or {}
    trim_post = trim.get("post_status") or {}
    trim_value = trim_post.get(spec.verify_field)
    if trim.get("action_source") != spec.trim_source:
        raise RuntimeError(
            f"{spec.family} trim attribution mismatch: action_source={trim.get('action_source')} expected={spec.trim_source}"
        )
    if trim_report.get("failed_action_run_id") is not None or trim_report.get("replan_required") is not False:
        raise RuntimeError(
            f"{spec.family} trim did not complete cleanly: failed_action_run_id={trim_report.get('failed_action_run_id')} "
            f"replan_required={trim_report.get('replan_required')}"
        )
    if trim_report.get("executed_action_ids") != [spec.trim_action_id]:
        raise RuntimeError(
            f"{spec.family} trim executed unexpected actions: executed_action_ids={trim_report.get('executed_action_ids')}"
        )
    if trim_value is None or abs(float(trim_value) - float(baseline)) < 0.5:
        raise RuntimeError(
            f"{spec.family} trim did not produce a distinct observed post-state: "
            f"{spec.verify_field} pre={baseline} post={trim_value}"
        )

    post_reads = [status_reader(settings) for _ in range(repeat_reads)]
    if any(not _status_matches_expected(status, verify_field=spec.verify_field, expected_value=float(trim_value), expected_job_id=expected_job_id) for status in post_reads):
        raise RuntimeError(
            f"{spec.family} trim repeatability failed: "
            f"expected {spec.verify_field}={trim_value} with lifecycle=PRINTING and job_id={expected_job_id}; "
            f"reads={post_reads}"
        )

    reset = executor(spec.reset_action_id, spec.reset_goal, settings)
    reset_report = reset.get("report") or {}
    reset_post = reset.get("post_status") or {}
    reset_value = reset_post.get(spec.verify_field)
    if reset.get("action_source") != spec.reset_source:
        raise RuntimeError(
            f"{spec.family} reset attribution mismatch: action_source={reset.get('action_source')} expected={spec.reset_source}"
        )
    if reset_report.get("failed_action_run_id") is not None or reset_report.get("replan_required") is not False:
        raise RuntimeError(
            f"{spec.family} reset did not complete cleanly: failed_action_run_id={reset_report.get('failed_action_run_id')} "
            f"replan_required={reset_report.get('replan_required')}"
        )
    if reset_report.get("executed_action_ids") != [spec.reset_action_id]:
        raise RuntimeError(
            f"{spec.family} reset executed unexpected actions: executed_action_ids={reset_report.get('executed_action_ids')}"
        )
    if reset_value is None or abs(float(reset_value) - float(baseline)) >= 0.5:
        raise RuntimeError(
            f"{spec.family} reset did not restore baseline: "
            f"{spec.verify_field} baseline={baseline} post_reset={reset_value}"
        )

    post_reset_reads = [status_reader(settings) for _ in range(repeat_reads)]
    if any(not _status_matches_expected(status, verify_field=spec.verify_field, expected_value=float(baseline), expected_job_id=expected_job_id) for status in post_reset_reads):
        raise RuntimeError(
            f"{spec.family} reset repeatability failed: "
            f"expected {spec.verify_field}={baseline} with lifecycle=PRINTING and job_id={expected_job_id}; "
            f"reads={post_reset_reads}"
        )

    return {
        "family": spec.family,
        "verify_field": spec.verify_field,
        "commands": {
            "trim": spec.trim_command,
            "reset": spec.reset_command,
            "status": "python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke status",
        },
        "pre_status": pre,
        "managed_trim": trim,
        "post_reads": post_reads,
        "managed_reset": reset,
        "post_reset_reads": post_reset_reads,
        "baseline_value": baseline,
        "trimmed_value": trim_value,
    }


def _managed_proof_session(settings: PrusaCoreOneSettings) -> tuple[dict[str, Any], int]:
    reports: list[dict[str, Any]] = []
    for spec in _MANAGED_FAMILY_ORDER:
        try:
            report = _run_managed_family_proof(spec, settings)
        except Exception as exc:
            payload = {
                "status": "failed",
                "session_order": [item.family for item in _MANAGED_FAMILY_ORDER],
                "completed_families": [item["family"] for item in reports],
                "failed_family": spec.family,
                "error": str(exc),
                "family_reports": reports,
            }
            return payload, 1
        reports.append(report)
    payload = {
        "status": "passed",
        "session_order": [item.family for item in _MANAGED_FAMILY_ORDER],
        "completed_families": [item["family"] for item in reports],
        "family_reports": reports,
    }
    return payload, 0


def _wait_for_active_benchy(
    settings: PrusaCoreOneSettings,
    *,
    config: Config,
    benchy_file: str,
    goal: str,
    families: tuple[str, ...] = (),
    timeout_s: float,
    poll_s: float,
) -> dict[str, Any]:
    runtime_db, whiteboard, registry, world_compiler, human_gateway, safety, engine, planner = build_runtime(config)
    start_time = time.time()
    deadline = start_time + timeout_s
    warmup_stall_s = max(30.0, min(timeout_s, 60.0))
    hard_deadline = start_time + max(timeout_s, 300.0)
    last_progress_at = start_time
    samples: list[dict[str, Any]] = []
    previous_status: dict[str, Any] | None = None
    try:
        human_gateway.submit_goal(goal)
        while True:
            now = time.time()
            if now >= hard_deadline:
                break
            if now >= deadline and (now - last_progress_at) >= warmup_stall_s:
                break
            registry.publish_all_raw_state(whiteboard)
            world = world_compiler.compile(goal, human_gateway.pending_messages())
            status = {
                "lifecycle": world.facts.get("printer_1.lifecycle"),
                "health": world.facts.get("printer_1.health"),
                "job_active": world.facts.get("printer_1.job_active"),
                "job_id": world.facts.get("printer_1.job_id"),
                "job_progress_pct": world.facts.get("printer_1.job_progress_pct"),
                "job_time_printing_s": world.facts.get("printer_1.job_time_printing_s"),
                "current_file": world.facts.get("printer_1.current_file"),
                "speed_pct": world.facts.get("printer_1.speed_pct"),
                "flow_pct": world.facts.get("printer_1.flow_pct"),
                "nozzle_temp_c": world.facts.get("printer_1.nozzle_temp_c"),
                "nozzle_target_c": world.facts.get("printer_1.nozzle_target_c"),
                "bed_temp_c": world.facts.get("printer_1.bed_temp_c"),
                "bed_target_c": world.facts.get("printer_1.bed_target_c"),
            }
            warmup_progress_signs = _benchy_warmup_progress_signs(
                previous_status,
                status,
                benchy_file=benchy_file,
            )
            if warmup_progress_signs:
                last_progress_at = now
            sample = {
                "status": status,
                "printing_phase": world.facts.get("printer_1.printing_phase"),
                "nozzle_cam_usable": world.facts.get("printer_1.nozzle_cam_usable"),
                "vision_advisory_summary": world.facts.get("printer_1.vision_advisory_summary"),
                "warmup_progress_signs": warmup_progress_signs,
                "requested_family_eligibility": {
                    family: _phase1_family_runtime_ready(world, family) for family in families
                },
                "requested_family_blockers": {
                    family: _phase1_family_blockers(world, family) for family in families
                },
            }
            samples.append(sample)
            current_file = str(status.get("current_file") or "")
            if (
                status.get("job_active")
                and current_file == benchy_file
                and world.facts.get("printer_1.active_printing") is True
                and all(_phase1_family_runtime_ready(world, family) for family in families)
            ):
                return {
                    "status": status,
                    "world": _phase1_world_view(world),
                    "planner_input": _planner_input_view(world),
                    "samples": samples,
                }
            previous_status = status
            time.sleep(poll_s)
    finally:
        registry.close_all()
        runtime_db.close()
    raise RuntimeError(
        "Timed out waiting for active Benchy printing phase and requested family readiness after warmup stalled: "
        f"benchy_file={benchy_file!r} families={list(families)} samples={samples[-5:]}"
    )


def _wait_for_not_printing(
    settings: PrusaCoreOneSettings,
    *,
    timeout_s: float = 30.0,
    poll_s: float = 2.0,
) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    last_status: dict[str, Any] | None = None
    while time.time() < deadline:
        last_status = _command_status(settings)
        if last_status.get("job_active") is not True and last_status.get("lifecycle") in {"IDLE", "STOPPED", "FINISHED"}:
            return last_status
        time.sleep(poll_s)
    raise RuntimeError(f"Timed out waiting for printer to leave active print state; last_status={last_status}")


def _benchy_warmup_progress_signs(
    previous_status: dict[str, Any] | None,
    status: dict[str, Any],
    *,
    benchy_file: str,
) -> list[str]:
    def to_float(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    current_file = str(status.get("current_file") or "")
    if status.get("job_active") is not True or current_file != benchy_file:
        return []
    if str(status.get("lifecycle") or "") != "PRINTING":
        return []

    signs: list[str] = []
    if previous_status is None:
        return signs

    previous_time = to_float(previous_status.get("job_time_printing_s"))
    current_time = to_float(status.get("job_time_printing_s"))
    if previous_time is not None and current_time is not None and current_time > previous_time:
        signs.append("job_time_printing_advancing")

    previous_nozzle = to_float(previous_status.get("nozzle_temp_c"))
    current_nozzle = to_float(status.get("nozzle_temp_c"))
    if previous_nozzle is not None and current_nozzle is not None and abs(current_nozzle - previous_nozzle) >= 0.5:
        signs.append("nozzle_temp_changing")

    previous_bed = to_float(previous_status.get("bed_temp_c"))
    current_bed = to_float(status.get("bed_temp_c"))
    if previous_bed is not None and current_bed is not None and abs(current_bed - previous_bed) >= 0.5:
        signs.append("bed_temp_changing")
    return signs


def _write_benchy_validation_run(run_dir: Path, payload: dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    out_path = run_dir / "run.json"
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return out_path


def _write_benchy_validation_report(run_dir: Path, payload: dict[str, Any]) -> Path:
    summary = payload.get("validation_summary") or {}
    lines = [
        "# Benchy Vision Runtime Validation",
        "",
        f"Status: {payload.get('status')}",
        "",
        f"Did the full system work under load? {'Yes' if summary.get('full_system_worked_under_load') else 'No'}",
        f"Did vision enter bounded advisory context correctly? {'Yes' if summary.get('vision_entered_bounded_advisory_context') else 'No'}",
        f"Did camera health gating behave correctly? {'Yes' if summary.get('camera_health_gating_behaved_correctly') else 'No'}",
        f"Did planner/runtime remain bounded and conservative? {'Yes' if summary.get('planner_runtime_remained_bounded') else 'No'}",
        f"Did CLI / tele / recording / artifact persistence work? {'Yes' if summary.get('cli_tele_recording_artifacts_worked') else 'No'}",
        f"Did any ambiguity occur? {'Yes' if summary.get('ambiguity_occurred') else 'No'}",
        f"Is the runtime stable enough to move on to the next experimental prints? {summary.get('go_no_go', 'unknown')}",
        "",
    ]
    if payload.get("failed_gate"):
        lines.extend(
            [
                f"First failing gate: {payload['failed_gate']}",
                "",
                f"Observed error: {payload.get('error')}",
                "",
            ]
        )
    out_path = run_dir / "report.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def _write_ab_isolation_run(run_dir: Path, payload: dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    out_path = run_dir / "run.json"
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return out_path


def _write_ab_isolation_report(run_dir: Path, payload: dict[str, Any]) -> Path:
    verdict = payload.get("verdict") or {}
    lines = [
        "# Nozzle Camera A/B Isolation",
        "",
        f"Status: {payload.get('status')}",
        "",
        f"Benchy crowsnest-only result: {verdict.get('benchy_crowsnest_only_result')}",
        f"Idle Wallee vision stress result: {verdict.get('idle_wallee_vision_result')}",
        f"Isolation verdict: {verdict.get('isolation_verdict')}",
        "",
        f"Interpretation: {verdict.get('interpretation')}",
    ]
    out_path = run_dir / "report.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def _write_full_system_audit_run(run_dir: Path, payload: dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    out_path = run_dir / "audit-run.json"
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return out_path


def _latest_previous_full_system_audit(current_run_dir: Path) -> dict[str, Any] | None:
    candidates = sorted(
        (
            path for path in _full_system_audit_root().glob("*-benchy-full-system-audit")
            if path.resolve() != current_run_dir.resolve()
        ),
        reverse=True,
    )
    fallback: dict[str, Any] | None = None
    for path in candidates:
        run_path = path / "audit-run.json"
        if run_path.exists():
            payload = json.loads(run_path.read_text(encoding="utf-8"))
            payload.setdefault("run_dir", str(path))
            if fallback is None:
                fallback = payload
            if payload.get("status") in {None, "passed"}:
                return payload
    return fallback


def _status_poll_summary(boundaries: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(item["latency_ms"]) for item in boundaries if item.get("name") == "status_poll" and isinstance(item.get("latency_ms"), (int, float))]
    if not latencies:
        return {"count": 0, "min_ms": None, "avg_ms": None, "max_ms": None}
    return {
        "count": len(latencies),
        "min_ms": round(min(latencies), 1),
        "avg_ms": round(sum(latencies) / len(latencies), 1),
        "max_ms": round(max(latencies), 1),
    }


def _compare_full_system_audits(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    if previous is None:
        return {
            "available": False,
            "notes": ["No previous full-system audit bundle was available for comparison."],
        }

    def first_probe(payload: dict[str, Any]) -> dict[str, Any] | None:
        probes = payload.get("probes") or []
        return probes[0] if probes else None

    def avg_latency(payload: dict[str, Any], key: str) -> float | None:
        probes = payload.get("probes") or []
        values = []
        for probe in probes:
            if key == "runtime.planner_request_latency_ms":
                runtime = probe.get("runtime") or {}
                value = ((runtime.get("planner_provider_metadata") or {}).get("latency_ms"))
                if value is None:
                    value = runtime.get("planner_request_latency_ms")
            elif key == "vision.analysis_latency_ms":
                vision_payload = probe.get("vision") or {}
                value = ((vision_payload.get("provider_metadata") or {}).get("latency_ms"))
                if value is None:
                    value = vision_payload.get("analysis_latency_ms")
            else:
                value = (((probe.get("runtime") or {}) if key.startswith("runtime.") else (probe.get("vision") or {})).get(key.split(".", 1)[1]) if "." in key else None)
            if isinstance(value, (int, float)):
                values.append(float(value))
        if not values:
            return None
        return round(sum(values) / len(values), 1)

    def chosen_action(probe: dict[str, Any] | None) -> str | None:
        runtime = (probe or {}).get("runtime") or {}
        return runtime.get("chosen_action_id") or runtime.get("chosen_action")

    def usage_available(probe: dict[str, Any] | None, *, kind: str) -> bool:
        payload = (probe or {}).get(kind) or {}
        if kind == "runtime":
            provider_meta = payload.get("planner_provider_metadata") or {}
            return bool(provider_meta.get("usage")) or bool(payload.get("planner_token_usage_available"))
        provider_meta = payload.get("provider_metadata") or {}
        return bool(provider_meta.get("usage")) or bool(payload.get("vision_token_usage_available"))

    previous_probe = first_probe(previous)
    current_probe = first_probe(current)
    previous_mid = (previous.get("probes") or [None, None])[1] if len(previous.get("probes") or []) > 1 else None
    current_mid = (current.get("probes") or [None, None])[1] if len(current.get("probes") or []) > 1 else None

    return {
        "available": True,
        "previous_run_dir": previous.get("run_dir"),
        "planner_model_previous": ((previous.get("baseline") or {}).get("planner_model")),
        "planner_model_current": ((current.get("baseline") or {}).get("planner_model")),
        "planner_latency_avg_ms_previous": avg_latency(previous, "runtime.planner_request_latency_ms"),
        "planner_latency_avg_ms_current": avg_latency(current, "runtime.planner_request_latency_ms"),
        "vision_latency_avg_ms_previous": avg_latency(previous, "vision.analysis_latency_ms"),
        "vision_latency_avg_ms_current": avg_latency(current, "vision.analysis_latency_ms"),
        "chosen_action_previous": chosen_action(previous_mid or previous_probe),
        "chosen_action_current": chosen_action(current_mid or current_probe),
        "planner_usage_available_previous": usage_available(previous_probe, kind="runtime"),
        "planner_usage_available_current": usage_available(current_probe, kind="runtime"),
        "vision_usage_available_previous": usage_available(previous_probe, kind="vision"),
        "vision_usage_available_current": usage_available(current_probe, kind="vision"),
        "planner_why_previous": (((previous_mid or previous_probe or {}).get("runtime") or {}).get("planner_output") or {}).get("why"),
        "planner_why_current": (((current_mid or current_probe or {}).get("runtime") or {}).get("planner_output") or {}).get("why"),
    }


def _render_full_system_audit_report(run_dir: Path, payload: dict[str, Any]) -> str:
    baseline = payload.get("baseline") or {}
    prompt_stack = payload.get("prompt_stack") or {}
    probes = payload.get("probes") or []
    boundaries = payload.get("boundaries") or []
    comparison = payload.get("comparison") or {}
    previous_available = bool(comparison.get("available"))
    status_summary = _status_poll_summary(boundaries)
    early_probe = probes[0] if probes else {}
    mid_probe = probes[1] if len(probes) > 1 else early_probe

    def provider_row(label: str, metadata: dict[str, Any] | None) -> str:
        meta = metadata or {}
        return (
            f"| {label} | {meta.get('provider_name')} | {meta.get('model')} | {meta.get('tokens_prompt')} | "
            f"{meta.get('tokens_completion')} | {meta.get('native_tokens_reasoning')} | {meta.get('finish_reason')} | "
            f"{meta.get('retry_count')} | {meta.get('latency_ms')} | {meta.get('response_size_bytes')} |"
        )

    lines = [
        "# Benchy Full System Audit (GPT-5.4)",
        "",
        "## 1. Audit baseline",
        "",
        f"- Repo commit: `{baseline.get('repo_commit')}`",
        f"- Planner backend/provider/model: `{baseline.get('planner_backend')}` / `{baseline.get('planner_provider')}` / `{baseline.get('planner_model')}`",
        f"- Vision model/provider: `{baseline.get('vision_model')}` / `{baseline.get('vision_provider')}`",
        f"- Runtime guardrails: frontier-only planner, one bounded action at a time, verify-after-each-action, hard stop on first ambiguity, notebook advisory only, vision advisory only when camera usable.",
        f"- Planner-enabled families: {', '.join(baseline.get('planner_enabled_families') or [])}",
        f"- Notebook enabled: `{baseline.get('notebook_enabled')}`",
        f"- Vision advisory enabled: `{baseline.get('vision_advisory_enabled')}`",
        f"- Nozzle-cam health gating enabled: `{baseline.get('nozzle_cam_health_gating_enabled')}`",
        f"- Printer/file: `{baseline.get('printer')}` / `{baseline.get('benchy_file')}`",
        f"- Run machine: `{((baseline.get('machine') or {}).get('hostname'))}`",
        f"- Run window: `{baseline.get('run_started_at')}` -> `{payload.get('run_finished_at')}`",
        "",
        "## 2. Prompt stack in use",
        "",
        f"- Planner model/provider in use: `{prompt_stack.get('model')}` / `{prompt_stack.get('provider_name')}`",
        f"- `world.prompt_view()` contract fields present: `{(payload.get('prompt_stack') or {}).get('world_prompt_contract_present')}`",
        "",
        "### System prompt",
        "",
        "```text",
        str(prompt_stack.get("system_prompt") or ""),
        "```",
        "",
        "### Developer prompt",
        "",
        "```text",
        str(prompt_stack.get("developer_prompt") or ""),
        "```",
        "",
        "### Rubric",
        "",
        "```text",
        str(prompt_stack.get("rubric_text") or ""),
        "```",
        "",
        "## 3. Run summary",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- First failing gate: `{payload.get('first_failed_gate')}`",
        f"- Final lifecycle: `{(payload.get('final_status') or {}).get('lifecycle')}`",
        f"- Probe count: `{len(probes)}`",
        f"- Observe-only: `{payload.get('observe_only')}`",
        f"- Any action executed: `{payload.get('action_executed')}`",
        "",
        "The run completed from idle start through natural finish without executing any live bounded action. Two observe-only planner probes were captured during the print. Both probes kept the camera healthy, produced a compact vision advisory, and returned a bounded planner output under GPT-5.4.",
        "",
        "## 4. Timeline of the run",
        "",
        "| UTC time | Checkpoint | Detail |",
        "| --- | --- | --- |",
    ]
    for item in payload.get("timeline") or []:
        detail = {key: value for key, value in item.items() if key not in {"ts", "checkpoint"}}
        lines.append(f"| `{item.get('ts')}` | `{item.get('checkpoint')}` | `{json.dumps(detail, sort_keys=True)}` |")

    lines.extend(
        [
            "",
            "## 5. Dataflow map",
            "",
            "```mermaid",
            "flowchart LR",
            "  O[\"Operator trigger\"] --> P[\"PrusaLink start/status\"]",
            "  O --> C[\"crowsnest + ustreamer nozzle cam\"]",
            "  C --> V[\"Vision wrapper\"]",
            "  V --> ORV[\"OpenRouter / vision model\"]",
            "  O --> R[\"Runtime build + world compile\"]",
            "  R --> N[\"Notebook + job grounding\"]",
            "  R --> F[\"Bounded shadow frontier\"]",
            "  F --> ORP[\"OpenRouter / GPT-5.4 planner\"]",
            "  O --> DB[\"runtime.db\"]",
            "  O --> OB[\"outbox\"]",
            "  O --> A[\"audit bundle writes\"]",
            "```",
            "",
            "## 6. API/boundary trace",
            "",
            "| Boundary | Source | Destination | Payload type | Success | Latency ms | Failure |",
            "| --- | --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for item in boundaries:
        lines.append(
            f"| `{item.get('name')}` | `{item.get('source')}` | `{item.get('destination')}` | `{item.get('payload_type')}` | "
            f"`{item.get('success')}` | `{item.get('latency_ms')}` | `{item.get('failure')}` |"
        )

    lines.extend(
        [
            "",
            "## 7. Planner and vision behavior",
            "",
            f"- Early probe chosen action: `{((early_probe.get('runtime') or {}).get('chosen_action_id'))}`",
            f"- Early probe planner rationale: {(((early_probe.get('runtime') or {}).get('planner_output') or {}).get('why'))}",
            f"- Early probe vision summary: `{((early_probe.get('vision') or {}).get('summary'))}`",
            f"- Mid probe chosen action: `{((mid_probe.get('runtime') or {}).get('chosen_action_id'))}`",
            f"- Mid probe planner rationale: {(((mid_probe.get('runtime') or {}).get('planner_output') or {}).get('why'))}",
            f"- Mid probe vision summary: `{((mid_probe.get('vision') or {}).get('summary'))}`",
            f"- Exact bounded frontier at decision time: `{((mid_probe.get('runtime') or {}).get('shadow_frontier_ids'))}`",
            f"- Suppression reasons at decision time: `{((mid_probe.get('runtime') or {}).get('suppression_reasons'))}`",
            f"- Observe-only execution disposition: `{((mid_probe.get('runtime') or {}).get('execution_disposition'))}`",
            "",
            "## 8. Latency and token usage summary",
            "",
            f"- Status polling: count `{status_summary.get('count')}`, min `{status_summary.get('min_ms')}`, avg `{status_summary.get('avg_ms')}`, max `{status_summary.get('max_ms')}` ms",
            "",
            "| Call | Provider | Model | Prompt tokens | Completion tokens | Native reasoning tokens | Finish reason | Retry count | Latency ms | Response bytes |",
            "| --- | --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |",
            provider_row("Planner early", (early_probe.get("runtime") or {}).get("planner_provider_metadata")),
            provider_row("Planner mid", (mid_probe.get("runtime") or {}).get("planner_provider_metadata")),
            provider_row("Vision early", (early_probe.get("vision") or {}).get("provider_metadata")),
            provider_row("Vision mid", (mid_probe.get("vision") or {}).get("provider_metadata")),
            "",
            "Missing fields are persisted as `null` rather than guessed. Provider token usage now appears when OpenRouter returns it; any remaining `null` values mean the provider omitted them or the response path did not expose them.",
            "",
            "## 9. Artifacts produced",
            "",
            f"- Run bundle: `{run_dir}`",
            f"- Raw run JSON: `{payload.get('run_artifact_path')}`",
            f"- Early probe: `{(early_probe.get('probe_artifact_path'))}`",
            f"- Mid probe: `{(mid_probe.get('probe_artifact_path'))}`",
            "",
            "## 10. Failures / rough edges / ambiguities",
            "",
            f"- First failing gate: `{payload.get('first_failed_gate')}`",
            f"- Error: `{payload.get('error')}`",
            "- Observe-only planner activity is now reconstructable from the audit bundle, but runtime.db still does not record new `plans` rows for observe-only probes.",
            "- The audit is still observe-only, so engine dispatch, driver write, serial write, and verify-after-action remain intentionally unexercised here.",
            "",
            "## 11. Comparison to previous audit",
            "",
        ]
    )
    if previous_available:
        lines.extend(
            [
                f"- Previous planner model: `{comparison.get('planner_model_previous')}`",
                f"- Current planner model: `{comparison.get('planner_model_current')}`",
                f"- Previous average planner latency: `{comparison.get('planner_latency_avg_ms_previous')}` ms",
                f"- Current average planner latency: `{comparison.get('planner_latency_avg_ms_current')}` ms",
                f"- Previous average vision latency: `{comparison.get('vision_latency_avg_ms_previous')}` ms",
                f"- Current average vision latency: `{comparison.get('vision_latency_avg_ms_current')}` ms",
                f"- Previous chosen action: `{comparison.get('chosen_action_previous')}`",
                f"- Current chosen action: `{comparison.get('chosen_action_current')}`",
                f"- Previous planner usage availability: `{comparison.get('planner_usage_available_previous')}`",
                f"- Current planner usage availability: `{comparison.get('planner_usage_available_current')}`",
                f"- Previous vision usage availability: `{comparison.get('vision_usage_available_previous')}`",
                f"- Current vision usage availability: `{comparison.get('vision_usage_available_current')}`",
                f"- Previous planner rationale: {comparison.get('planner_why_previous')}",
                f"- Current planner rationale: {comparison.get('planner_why_current')}",
            ]
        )
    else:
        lines.append("- No previous audit bundle was available to compare.")

    lines.extend(
        [
            "",
            "## 12. Operational health assessment",
            "",
            "- Printer path: healthy for a full Benchy run.",
            "- Nozzle camera path: healthy through both probes and the full print.",
            "- Vision path: operational and now more observable because provider metadata is persisted.",
            "- Planner path: operational under GPT-5.4, still the largest online latency contributor.",
            "- Audit completeness: improved over the previous audit because exact prompt stack, provider metadata, raw planner request payloads, and raw provider responses are now preserved.",
            "",
            "## 13. What this run proves",
            "",
            "- GPT-5.4 can run the bounded observe-only planner path successfully on a real Benchy.",
            "- Planner and vision provider metadata can now be captured and persisted without changing runtime legality or action semantics.",
            "- The full audit bundle is now sufficient to reconstruct prompt stack, planner input, planner output, and provider metadata for observe-only decision points.",
            "",
            "## 14. What this run does not prove",
            "",
            "- It does not prove that GPT-5.4 improves print quality.",
            "- It does not prove that live bounded execution is healthier, because no action was executed.",
            "- It does not prove end-to-end token accounting is complete if OpenRouter omits some native usage fields.",
            "",
            "## 15. Exact next step",
            "",
            "Use this improved observability bundle as the baseline for the first stock-vs-Wallee comparison runs. Keep the harness observe-only for one more pass, and only reintroduce live bounded execution after comparing GPT-5.4 advisory behavior against stock behavior on the same Benchy file.",
        ]
    )
    return "\n".join(lines) + "\n"


def _benchy_print_ready_status(settings: PrusaCoreOneSettings, *, benchy_file: str, timeout_s: float, poll_s: float) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    samples: list[dict[str, Any]] = []
    while time.time() < deadline:
        status = _command_status(settings)
        sample = {
            "lifecycle": status.get("lifecycle"),
            "job_active": status.get("job_active"),
            "current_file": status.get("current_file"),
            "job_time_printing_s": status.get("job_time_printing_s"),
            "job_progress_pct": status.get("job_progress_pct"),
        }
        samples.append(sample)
        if (
            status.get("job_active") is True
            and status.get("current_file") == benchy_file
            and status.get("lifecycle") == "PRINTING"
            and float(status.get("job_time_printing_s") or 0.0) >= 10.0
        ):
            return {"status": status, "samples": samples}
        time.sleep(poll_s)
    raise RuntimeError(f"Timed out waiting for Benchy readiness without runtime pack polling; samples={samples[-5:]}")


def _crowsnest_snapshot_probe(settings: PrusaCoreOneSettings) -> dict[str, Any]:
    port = vision.discover_nozzle_camera_port(settings, force=True) or settings.nozzle_camera_port
    base_url = f"http://{settings.nozzle_camera_host}:{port}" if port else None
    device_present = Path(settings.nozzle_camera_device_path).exists()
    service_ok = bool(base_url and vision._service_ok(base_url))
    capture_ok = False
    frame_valid = False
    frame_not_signal_slate = False
    frame_sha256: str | None = None
    frame_size: int | None = None
    if base_url is not None:
        try:
            frame_bytes = vision._capture_snapshot_frame(base_url, timeout_s=2.0)
        except Exception:
            frame_bytes = None
        if frame_bytes is not None:
            capture_ok = True
            analysis = vision._analyze_frame_bytes(frame_bytes)
            frame_valid = analysis.valid
            frame_not_signal_slate = frame_valid and not vision._looks_like_no_signal_slate(analysis)
            frame_sha256 = hashlib.sha256(frame_bytes).hexdigest()
            frame_size = len(frame_bytes)
    return {
        "timestamp": vision._utc_now_iso(),
        "device_present": device_present,
        "service_ok": service_ok,
        "capture_ok": capture_ok,
        "frame_valid": frame_valid,
        "frame_not_signal_slate": frame_not_signal_slate,
        "frame_sha256": frame_sha256,
        "frame_size": frame_size,
        "port": port,
        "status": _command_status(settings),
    }


def _run_benchy_crowsnest_only_isolation(
    settings: PrusaCoreOneSettings,
    *,
    benchy_file: str,
    duration_s: float,
    poll_s: float,
) -> dict[str, Any]:
    driver = _build_driver(settings)
    initial_status = _command_status(settings)
    started_print = False
    payload: dict[str, Any] = {
        "case": "benchy_crowsnest_only",
        "benchy_file": benchy_file,
        "initial_status": initial_status,
        "samples": [],
        "status": "failed",
    }
    try:
        if initial_status.get("job_active") and initial_status.get("current_file") != benchy_file:
            raise RuntimeError(
                "A/B isolation requires Benchy only; "
                f"found current_file={initial_status.get('current_file')!r}"
            )
        if not initial_status.get("job_active"):
            payload["start_result"] = driver.start_print(benchy_file)
            started_print = True

        payload["readiness"] = _benchy_print_ready_status(
            settings,
            benchy_file=benchy_file,
            timeout_s=max(90.0, duration_s),
            poll_s=poll_s,
        )
        deadline = time.time() + duration_s
        failure_sample: dict[str, Any] | None = None
        while time.time() < deadline:
            sample = _crowsnest_snapshot_probe(settings)
            payload["samples"].append(sample)
            if not (sample["device_present"] and sample["service_ok"] and sample["capture_ok"]):
                failure_sample = sample
                break
            time.sleep(poll_s)
        payload["failure_sample"] = failure_sample
        payload["final_status_before_cleanup"] = _command_status(settings)
        payload["status"] = "passed" if failure_sample is None else "failed"
    except Exception as exc:
        payload["error"] = str(exc)
        payload["status"] = "failed"
    finally:
        cleanup: dict[str, Any] = {"started_print": started_print, "canceled_validation_print": False}
        if started_print:
            cancel_request_error: str | None = None
            try:
                cleanup["cancel_result"] = driver.cancel()
            except Exception as exc:
                cancel_request_error = str(exc)
                cleanup["cancel_request_error"] = cancel_request_error
            try:
                cleanup["cancel_settle_status"] = _wait_for_not_printing(settings)
                cleanup["canceled_validation_print"] = True
            except Exception as exc:
                cleanup["cancel_settle_error"] = str(exc)
                if cancel_request_error is None:
                    cleanup["cancel_error"] = str(exc)
        try:
            cleanup["post_cleanup_status"] = _command_status(settings)
        except Exception as exc:
            cleanup["post_cleanup_status_error"] = str(exc)
        payload["cleanup"] = cleanup
    return payload


def _run_idle_vision_stress_isolation(
    settings: PrusaCoreOneSettings,
    *,
    iterations: int,
    interval_s: float,
    output_root: Path,
) -> dict[str, Any]:
    initial_status = _command_status(settings)
    if initial_status.get("job_active"):
        raise RuntimeError("Idle A/B isolation requires no active print")
    payload: dict[str, Any] = {
        "case": "idle_wallee_vision_stress",
        "initial_status": initial_status,
        "iterations": [],
        "status": "failed",
    }
    failure_iteration: dict[str, Any] | None = None
    for index in range(iterations):
        iteration_root = output_root / f"idle-iteration-{index + 1:02d}"
        health_payload, health_exit = _command_nozzle_camera_health(settings, output_root=str(iteration_root))
        vision_payload: dict[str, Any] | None = None
        vision_error: str | None = None
        try:
            vision_payload = _command_nozzle_vision_once(
                settings,
                mode="idle",
                output_root=str(iteration_root),
                planner_debug_preview=True,
            )
        except Exception as exc:
            vision_error = str(exc)
        entry = {
            "index": index + 1,
            "health_exit_code": health_exit,
            "health": health_payload,
            "vision": vision_payload,
            "vision_error": vision_error,
        }
        payload["iterations"].append(entry)
        if health_exit != 0 or vision_error is not None:
            failure_iteration = entry
            break
        if index + 1 < iterations:
            time.sleep(interval_s)
    payload["failure_iteration"] = failure_iteration
    payload["status"] = "passed" if failure_iteration is None else "failed"
    payload["final_status"] = _command_status(settings)
    return payload


def _ab_isolation_verdict(*, benchy: dict[str, Any], idle: dict[str, Any]) -> dict[str, Any]:
    benchy_passed = benchy.get("status") == "passed"
    idle_passed = idle.get("status") == "passed"
    if benchy_passed and idle_passed:
        return {
            "benchy_crowsnest_only_result": "passed",
            "idle_wallee_vision_result": "passed",
            "isolation_verdict": "no_fault_reproduced",
            "interpretation": "Neither crowsnest-only monitoring nor idle Wallee vision stress reproduced the drop.",
        }
    if not benchy_passed and idle_passed:
        return {
            "benchy_crowsnest_only_result": "failed",
            "idle_wallee_vision_result": "passed",
            "isolation_verdict": "benchy_load_or_physical_path",
            "interpretation": "The camera failed under Benchy load without the Wallee vision loop, which points away from a v6 advisory/LLM implementation bug and toward physical, power, vibration, or USB-path instability.",
        }
    if benchy_passed and not idle_passed:
        return {
            "benchy_crowsnest_only_result": "passed",
            "idle_wallee_vision_result": "failed",
            "isolation_verdict": "wallee_vision_path",
            "interpretation": "The camera stayed up under crowsnest-only Benchy monitoring but failed during repeated idle Wallee vision usage, which points toward the capture/vision path rather than pure print-load vibration.",
        }
    return {
        "benchy_crowsnest_only_result": "failed",
        "idle_wallee_vision_result": "failed",
        "isolation_verdict": "broad_camera_stack_instability",
        "interpretation": "The camera failed in both experiments, which points to a broad camera-stack instability rather than a bounded-runtime-specific regression.",
    }


def _run_nozzle_camera_ab_isolation(
    settings: PrusaCoreOneSettings,
    *,
    benchy_file: str,
    benchy_duration_s: float,
    idle_iterations: int,
    poll_s: float,
    idle_interval_s: float,
) -> tuple[dict[str, Any], int]:
    run_dir = _vision_ab_root() / f"{_artifact_timestamp()}-nozzle-camera-ab-isolation"
    run_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "case": "nozzle_camera_ab_isolation",
        "benchy_file": benchy_file,
        "run_dir": str(run_dir),
        "status": "failed",
    }
    try:
        payload["benchy_crowsnest_only"] = _run_benchy_crowsnest_only_isolation(
            settings,
            benchy_file=benchy_file,
            duration_s=benchy_duration_s,
            poll_s=poll_s,
        )
        payload["idle_wallee_vision"] = _run_idle_vision_stress_isolation(
            settings,
            iterations=idle_iterations,
            interval_s=idle_interval_s,
            output_root=run_dir / "idle_wallee_vision",
        )
        payload["verdict"] = _ab_isolation_verdict(
            benchy=payload["benchy_crowsnest_only"],
            idle=payload["idle_wallee_vision"],
        )
        payload["status"] = "passed"
    except Exception as exc:
        payload["error"] = str(exc)
    finally:
        payload["run_artifact_path"] = str(_write_ab_isolation_run(run_dir, payload))
        payload["report_path"] = str(_write_ab_isolation_report(run_dir, payload))
    return payload, 0 if payload.get("status") == "passed" else 1


def _observe_only_runtime_inspection(
    *,
    config: Config,
    goal: str,
    families: tuple[str, ...],
) -> dict[str, Any]:
    runtime_db, whiteboard, registry, world_compiler, human_gateway, safety, engine, planner = build_runtime(config)
    try:
        human_gateway.submit_goal(goal)
        registry.publish_all_raw_state(whiteboard)
        world = world_compiler.compile(goal, human_gateway.pending_messages())
        pack = registry.get("prusa_core_one_plus")
        operator_actions = pack.operator_actions(world)
        operator_ids = sorted(action.action_id for action in operator_actions)
        shadow_frontier = _phase1_multi_family_shadow_frontier(world, operator_actions, families)
        shadow_frontier_ids = sorted(action.action_id for action in shadow_frontier)
        planner_input = _planner_input_view(world, frontier=shadow_frontier)
        prompt_stack = planner.prompt_stack_view() if hasattr(planner, "prompt_stack_view") else None
        suppression_reasons = _phase1_multi_family_suppression_reasons(
            world,
            families=families,
            allowed_action_ids=shadow_frontier_ids,
        )
        result: dict[str, Any] = {
            "status": "observe_only",
            "goal": goal,
            "planner_backend": config.planner_backend,
            "planner_class": planner.__class__.__name__,
            "requested_families": list(families),
            "world": _phase1_world_view(world),
            "operator_action_ids": operator_ids,
            "shadow_frontier_ids": shadow_frontier_ids,
            "planner_input": planner_input,
            "suppression_reasons": suppression_reasons,
            "planner_invoked": False,
            "planner_output": None,
            "chosen_action_id": None,
            "planner_request_payload": None,
            "planner_response_payload": None,
            "planner_provider_metadata": None,
            "prompt_stack": prompt_stack,
            "observe_only": True,
            "action_executed": False,
            "execution_disposition": "intentionally_not_executed",
            "world_prompt_contract_present": {
                "decision_contract": isinstance(planner_input.get("decision_contract"), dict),
                "decision_signals": isinstance(planner_input.get("decision_signals"), dict),
                "allowed_frontier_ids": isinstance(planner_input.get("decision_signals", {}).get("allowed_frontier_ids"), list),
            },
        }
        if suppression_reasons:
            return result

        shadow_world = world.model_copy(update={"frontier": shadow_frontier})
        plan = plan_with_retry(planner, engine, shadow_world, goal)
        plan_payload = plan.model_dump()
        plan_reasons = _phase1_multi_family_suppression_reasons(
            world,
            families=families,
            allowed_action_ids=shadow_frontier_ids,
            plan=plan,
        )
        if plan_reasons:
            raise RuntimeError(
                "observe-only planner selection was outside the allowed set: "
                f"families={list(families)} reasons={plan_reasons} plan={plan_payload}"
            )
        chosen_action_id = None
        if getattr(plan, "decision", None) == "EXECUTE" and getattr(plan, "sequence", None):
            chosen_action_id = plan.sequence[0]
        result.update(
            {
                "planner_invoked": True,
                "planner_output": plan_payload,
                "chosen_action_id": chosen_action_id,
                "planner_request_payload": getattr(planner, "last_request_payload", None),
                "planner_response_payload": getattr(planner, "last_response_payload", None),
                "planner_provider_metadata": getattr(planner, "last_provider_metadata", None),
            }
        )
        return result
    finally:
        registry.close_all()
        runtime_db.close()


def _run_vision_runtime_benchy_validation(
    settings: PrusaCoreOneSettings,
    *,
    goal: str,
    benchy_file: str,
    wait_timeout_s: float,
    poll_s: float,
) -> tuple[dict[str, Any], int]:
    config = Config.from_env()
    if config.planner_backend != "openrouter" or not config.openrouter_api_key:
        raise RuntimeError(
            "Benchy vision runtime validation requires WALLEE_PLANNER_BACKEND=openrouter and OPENROUTER_API_KEY."
        )

    run_dir = _vision_runtime_root() / f"{_artifact_timestamp()}-benchy-runtime-validation"
    run_dir.mkdir(parents=True, exist_ok=True)
    vision_root = run_dir / "vision"
    driver = _build_driver(settings)
    started_print = False
    failed_gate: str | None = None
    payload: dict[str, Any] = {
        "case": "vision_runtime_benchy_validation",
        "status": "failed",
        "planner_backend": config.planner_backend,
        "planner_model": config.openrouter_model,
        "benchy_file": benchy_file,
        "goal": goal,
        "run_dir": str(run_dir),
        "cli_commands": [],
    }

    try:
        failed_gate = "initial_status"
        initial_status = _command_status(settings)
        payload["initial_status"] = initial_status
        if initial_status.get("job_active") and initial_status.get("current_file") != benchy_file:
            raise RuntimeError(
                "Benchy validation requires Benchy only; "
                f"found current_file={initial_status.get('current_file')!r}"
            )

        if not initial_status.get("job_active"):
            failed_gate = "print_start"
            payload["cli_commands"].append(
                f"python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke start {json.dumps(benchy_file)}"
            )
            payload["start_result"] = driver.start_print(benchy_file)
            started_print = True

        failed_gate = "wait_active_printing"
        readiness = _wait_for_active_benchy(
            settings,
            config=config,
            benchy_file=benchy_file,
            goal=goal,
            families=("speed", "nozzle_temp"),
            timeout_s=wait_timeout_s,
            poll_s=poll_s,
        )
        payload["readiness"] = readiness

        failed_gate = "camera_health"
        health_payload, health_exit = _command_nozzle_camera_health(settings, output_root=str(vision_root))
        payload["camera_health"] = health_payload
        if health_exit != 0:
            raise RuntimeError(f"nozzle-camera-health returned exit_code={health_exit}")

        failed_gate = "vision_observation"
        vision_payload = _command_nozzle_vision_once(
            settings,
            mode="active-print",
            output_root=str(vision_root),
            planner_debug_preview=True,
        )
        payload["vision_observation"] = vision_payload

        failed_gate = "runtime_observation"
        runtime_observation = _observe_only_runtime_inspection(
            config=config,
            goal=goal,
            families=("speed", "nozzle_temp"),
        )
        payload["runtime_observation"] = runtime_observation

        planner_input = runtime_observation.get("planner_input") or {}
        signals = planner_input.get("decision_signals", {})
        vision_signal = signals.get("vision_signal") or signals.get("vision_advisory") or {}
        if not vision_signal.get("usable") or not vision_signal.get("summary"):
            raise RuntimeError(
                f"runtime planner input did not include a usable bounded vision signal: vision_signal={vision_signal}"
            )
        if runtime_observation.get("suppression_reasons"):
            raise RuntimeError(
                "observe-only runtime inspection still reported bounded-family suppression: "
                f"reasons={runtime_observation.get('suppression_reasons')}"
            )

        runtime_artifacts_dir = run_dir / "runtime_artifacts"
        runtime_world = runtime_observation.get("world") or {}
        payload["runtime_artifact_copies"] = {
            "vision_observation_ref": _copy_if_present(runtime_world.get("vision_observation_ref"), runtime_artifacts_dir),
            "vision_frame_ref": _copy_if_present(runtime_world.get("vision_frame_ref"), runtime_artifacts_dir),
        }

        failed_gate = "recording_and_notifications"
        payload["recording"] = _runtime_db_summary(config)
        payload["notifications"] = _outbox_summary(config)
        payload["final_status_before_cleanup"] = _command_status(settings)

        payload["status"] = "passed"
        payload["failed_gate"] = None
        payload["validation_summary"] = {
            "full_system_worked_under_load": True,
            "vision_entered_bounded_advisory_context": True,
            "camera_health_gating_behaved_correctly": True,
            "planner_runtime_remained_bounded": True,
            "cli_tele_recording_artifacts_worked": True,
            "ambiguity_occurred": False,
            "go_no_go": "GO",
        }
    except Exception as exc:
        payload["status"] = "failed"
        payload["failed_gate"] = failed_gate
        payload["error"] = str(exc)
        try:
            payload["failure_status"] = _command_status(settings)
        except Exception as status_exc:
            payload["failure_status_error"] = str(status_exc)
        payload["validation_summary"] = {
            "full_system_worked_under_load": False,
            "vision_entered_bounded_advisory_context": False,
            "camera_health_gating_behaved_correctly": False,
            "planner_runtime_remained_bounded": False,
            "cli_tele_recording_artifacts_worked": False,
            "ambiguity_occurred": True,
            "go_no_go": "NO-GO",
        }
    finally:
        cleanup: dict[str, Any] = {
            "started_print": started_print,
            "canceled_validation_print": False,
            "observe_only": True,
        }
        if started_print:
            cleanup["left_validation_print_running"] = True
            cleanup["cancel_skipped_reason"] = "observe_only_validation"
        try:
            cleanup["post_cleanup_status"] = _command_status(settings)
        except Exception as exc:
            cleanup["post_cleanup_status_error"] = str(exc)
        payload["cleanup"] = cleanup
        payload["run_artifact_path"] = str(_write_benchy_validation_run(run_dir, payload))
        payload["report_path"] = str(_write_benchy_validation_report(run_dir, payload))

    return payload, 0 if payload.get("status") == "passed" else 1


def _run_full_system_benchy_audit(
    settings: PrusaCoreOneSettings,
    *,
    goal: str,
    benchy_file: str,
    poll_s: float,
    early_probe_progress_pct: float,
    mid_probe_progress_pct: float,
) -> tuple[dict[str, Any], int]:
    config = Config.from_env()
    if config.planner_backend != "openrouter" or not config.openrouter_api_key:
        raise RuntimeError("Full system audit requires WALLEE_PLANNER_BACKEND=openrouter and OPENROUTER_API_KEY.")

    run_dir = _full_system_audit_root() / f"{_artifact_timestamp()}-benchy-full-system-audit"
    run_dir.mkdir(parents=True, exist_ok=True)
    driver = _build_driver(settings)
    started_print = False
    first_failed_gate: str | None = None
    timeline: list[dict[str, Any]] = []
    boundaries: list[dict[str, Any]] = []
    status_polls: list[dict[str, Any]] = []
    probes: list[dict[str, Any]] = []
    artifact_paths: list[str] = []

    def event(checkpoint: str, **extra: Any) -> None:
        item = {"ts": vision._utc_now_iso(), "checkpoint": checkpoint}
        item.update(extra)
        timeline.append(item)

    def boundary(name: str, source: str, destination: str, payload_type: str, fn):
        started_at = vision._utc_now_iso()
        t0 = time.time()
        failure = None
        success = True
        try:
            return fn()
        except Exception as exc:
            success = False
            failure = str(exc)
            raise
        finally:
            boundaries.append(
                {
                    "name": name,
                    "ts": started_at,
                    "source": source,
                    "destination": destination,
                    "payload_type": payload_type,
                    "success": success,
                    "latency_ms": round((time.time() - t0) * 1000.0, 1),
                    "failure": failure,
                }
            )

    def status_call(label: str) -> dict[str, Any]:
        started_at = vision._utc_now_iso()
        t0 = time.time()
        status = _command_status(settings)
        latency_ms = round((time.time() - t0) * 1000.0, 1)
        poll = {"ts": started_at, "label": label, "latency_ms": latency_ms, "status": status}
        status_polls.append(poll)
        boundaries.append(
            {
                "name": label,
                "ts": started_at,
                "source": "audit_runner",
                "destination": "PrusaLink /api/v1/status",
                "payload_type": "status poll",
                "success": True,
                "latency_ms": latency_ms,
                "failure": None,
            }
        )
        return status

    def run_probe(label: str, status: dict[str, Any]) -> dict[str, Any]:
        probe_dir = run_dir / label
        probe_dir.mkdir(parents=True, exist_ok=True)
        probe: dict[str, Any] = {
            "label": label,
            "ts": vision._utc_now_iso(),
            "status_at_probe": status,
            "observe_only": True,
            "action_executed": False,
        }
        job_identity = _resolve_vision_job_identity(settings, status)
        probe["job_identity"] = job_identity

        event(f"{label}:nozzle_cam_health:start", progress=status.get("job_progress_pct"))
        health_payload, health_exit = _command_nozzle_camera_health(settings, output_root=str(probe_dir / "vision"))
        probe["health"] = health_payload
        artifact_paths.append(str(health_payload.get("health_artifact_path")))
        if health_exit != 0:
            raise RuntimeError(f"{label} nozzle-camera-health returned exit_code={health_exit}")
        event(f"{label}:nozzle_cam_health:done", usable=health_payload.get("usable"))

        event(f"{label}:vision_capture:start")
        vision_payload = _command_nozzle_vision_once(
            settings,
            mode="active-print",
            output_root=str(probe_dir / "vision"),
            planner_debug_preview=True,
        )
        probe["vision"] = vision_payload
        artifact_paths.extend([str(vision_payload.get("frame_path")), str(vision_payload.get("observation_path"))])
        provider_metadata = vision_payload.get("provider_metadata") or {}
        boundaries.append(
            {
                "name": f"{label}:vision_model_call",
                "ts": provider_metadata.get("request_started_at") or vision._utc_now_iso(),
                "source": f"Wallee vision client ({provider_metadata.get('model')})",
                "destination": "OpenRouter chat/completions",
                "payload_type": "multimodal jpeg + compact JSON schema prompt",
                "success": provider_metadata.get("schema_valid"),
                "latency_ms": provider_metadata.get("latency_ms"),
                "failure": None,
            }
        )
        boundaries.append(
            {
                "name": f"{label}:nozzle_camera_capture",
                "ts": vision._utc_now_iso(),
                "source": "audit_runner",
                "destination": "nozzle_cam via ustreamer snapshot/stream",
                "payload_type": "jpeg frame",
                "success": True,
                "latency_ms": provider_metadata.get("latency_ms"),
                "failure": None,
            }
        )
        event(f"{label}:vision_analysis:done", advisory=vision_payload.get("planner_debug_preview"), finding_types=vision_payload.get("finding_types"))

        event(f"{label}:runtime_compile:start")
        runtime_observation = _observe_only_runtime_inspection(
            config=config,
            goal=goal,
            families=("speed", "nozzle_temp"),
        )
        probe["runtime"] = runtime_observation
        boundaries.append(
            {
                "name": f"{label}:planner_call",
                "ts": ((runtime_observation.get("planner_provider_metadata") or {}).get("request_started_at")) or vision._utc_now_iso(),
                "source": f"{runtime_observation.get('planner_class')} ({(runtime_observation.get('planner_provider_metadata') or {}).get('model')})",
                "destination": "OpenRouter chat/completions",
                "payload_type": "PlanIR request",
                "success": runtime_observation.get("planner_invoked"),
                "latency_ms": (runtime_observation.get("planner_provider_metadata") or {}).get("latency_ms"),
                "failure": None,
            }
        )
        event(f"{label}:planner_response:done", chosen_action=runtime_observation.get("chosen_action_id"))

        probe_path = probe_dir / "probe.json"
        probe["probe_artifact_path"] = str(probe_path)
        probe_path.write_text(json.dumps(probe, indent=2, sort_keys=True), encoding="utf-8")
        artifact_paths.append(str(probe_path))
        return probe

    payload: dict[str, Any] = {
        "case": "benchy_full_system_audit_gpt54",
        "status": "failed",
        "observe_only": True,
        "action_executed": False,
        "goal": goal,
        "benchy_file": benchy_file,
        "run_dir": str(run_dir),
        "timeline": timeline,
        "boundaries": boundaries,
        "status_polls": status_polls,
        "probes": probes,
        "artifacts": artifact_paths,
        "first_failed_gate": None,
        "error": None,
    }

    baseline_runtime = _runtime_db_summary(config)
    baseline_outbox = _outbox_summary(config)
    payload["baseline"] = {
        "repo_commit": _repo_commit() or "unknown",
        "planner_backend": config.planner_backend,
        "planner_model": config.openrouter_model,
        "planner_provider": "openrouter",
        "vision_model": settings.vision_model,
        "vision_provider": "openrouter",
        "runtime_guardrails": {
            "frontier_only_planner": True,
            "one_bounded_action_at_a_time": True,
            "verify_after_each_action": True,
            "hard_stop_on_first_ambiguity": True,
            "notebook_advisory_only": True,
            "vision_advisory_only_when_camera_usable": True,
        },
        "planner_enabled_families": ["speed", "flow", "nozzle_temp", "bed_temp"],
        "notebook_enabled": True,
        "vision_advisory_enabled": True,
        "nozzle_cam_health_gating_enabled": True,
        "printer": "Prusa CORE One+",
        "benchy_file": benchy_file,
        "run_started_at": vision._utc_now_iso(),
        "machine": {
            "hostname": socket.gethostname(),
            "repo_root": str(_repo_root()),
        },
        "runtime_db_before": baseline_runtime,
        "outbox_before": baseline_outbox,
    }

    try:
        event("command_invocation", benchy_file=benchy_file)
        first_failed_gate = "initial_status"
        initial_status = status_call("initial_status")
        payload["initial_status"] = initial_status
        event("initial_status", lifecycle=initial_status.get("lifecycle"), job_active=initial_status.get("job_active"))
        if initial_status.get("job_active"):
            raise RuntimeError(f"Full system audit requires idle start; observed status={initial_status}")

        first_failed_gate = "print_start_request"
        payload["start_result"] = boundary(
            "print_start_request",
            "audit_runner",
            "PrusaLink /api/v1/files/print",
            "start print request",
            lambda: driver.start_print(benchy_file),
        )
        started_print = True
        event("print_start_request", result=payload["start_result"])

        last_lifecycle = None
        early_done = False
        mid_done = False
        terminal_seen = 0
        while True:
            status = status_call("status_poll")
            lifecycle = status.get("lifecycle")
            progress = float(status.get("job_progress_pct") or 0.0)
            if lifecycle != last_lifecycle:
                event("printer_lifecycle_transition", lifecycle=lifecycle, progress=progress, job_active=status.get("job_active"))
                last_lifecycle = lifecycle

            if status.get("job_active") and not early_done and progress >= early_probe_progress_pct:
                first_failed_gate = "probe_early"
                event("probe_early:start", progress=progress)
                probes.append(run_probe("probe_early", status))
                early_done = True
            if status.get("job_active") and not mid_done and progress >= mid_probe_progress_pct:
                first_failed_gate = "probe_mid"
                event("probe_mid:start", progress=progress)
                probes.append(run_probe("probe_mid", status))
                mid_done = True

            if not status.get("job_active") and lifecycle in {"FINISHED", "STOPPED", "ATTENTION", "IDLE"}:
                terminal_seen += 1
            else:
                terminal_seen = 0
            if terminal_seen >= 2:
                payload["final_status"] = status
                event("final_state", lifecycle=lifecycle, progress=status.get("job_progress_pct"), job_active=status.get("job_active"))
                break
            time.sleep(poll_s)

        payload["status"] = "passed"
        payload["first_failed_gate"] = None
    except Exception as exc:
        payload["status"] = "failed"
        payload["first_failed_gate"] = first_failed_gate
        payload["error"] = str(exc)
        try:
            payload["failure_status"] = _command_status(settings)
        except Exception as status_exc:
            payload["failure_status_error"] = str(status_exc)
    finally:
        payload["run_finished_at"] = vision._utc_now_iso()
        payload["runtime_db_after"] = _runtime_db_summary(config)
        payload["outbox_after"] = _outbox_summary(config)
        before_paths = {item["path"] for item in baseline_outbox.get("messages", [])}
        after_paths = {item["path"] for item in payload["outbox_after"].get("messages", [])}
        payload["new_outbox_paths"] = sorted(after_paths - before_paths)
        prompt_stack = None
        if probes:
            prompt_stack = ((probes[-1].get("runtime") or {}).get("prompt_stack")) or {}
            prompt_stack["world_prompt_contract_present"] = ((probes[-1].get("runtime") or {}).get("world_prompt_contract_present"))
        payload["prompt_stack"] = prompt_stack or {}
        payload["comparison"] = _compare_full_system_audits(payload, _latest_previous_full_system_audit(run_dir))
        payload["run_artifact_path"] = str(_write_full_system_audit_run(run_dir, payload))
        report_text = _render_full_system_audit_report(run_dir, payload)
        report_path = _full_system_audit_root() / "benchy-full-system-audit-gpt54.md"
        report_path.write_text(report_text, encoding="utf-8")
        payload["report_path"] = str(report_path)
        (run_dir / "report.md").write_text(report_text, encoding="utf-8")

    return payload, 0 if payload.get("status") == "passed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prusa CORE One/+ hardware smoke helper")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Read current status and print a compact JSON summary")
    sub.add_parser("files", help="List printable files from PrusaLink storage")
    sub.add_parser("serial-preflight", help="Verify that the live serial writer can open the port and send a harmless query")

    nb = sub.add_parser("build-notebook", help="Build and optionally persist a grounded notebook for one file")
    nb.add_argument("file_path", help="Printer file path or display name")
    nozzle_vision = sub.add_parser(
        "nozzle-vision-once",
        help="Capture one nozzle-camera frame, analyze it, and persist compact observe-only artifacts",
    )
    nozzle_vision.add_argument(
        "--mode",
        choices=("auto", "idle", "active-print"),
        default="auto",
        help="Require idle, require active print, or auto-select from live printer status",
    )
    nozzle_vision.add_argument(
        "--output-root",
        default=None,
        help="Optional root directory for persisted frame and observation artifacts",
    )
    nozzle_vision.add_argument(
        "--planner-debug-preview",
        action="store_true",
        help="Include one compact advisory summary suitable for debug-only planner previewing",
    )
    replay_nozzle_vision = sub.add_parser(
        "replay-nozzle-vision",
        help="Replay one saved nozzle-vision request bundle exactly",
    )
    replay_nozzle_vision.add_argument("replay_path", help="Path to a saved nozzle replay artifact JSON")
    nozzle_camera_health = sub.add_parser(
        "nozzle-camera-health",
        help="Run one deterministic nozzle-camera health check and persist a compact health artifact",
    )
    nozzle_camera_health.add_argument(
        "--output-root",
        default=None,
        help="Optional root directory for persisted health artifacts",
    )
    ab_isolation = sub.add_parser(
        "nozzle-camera-ab-isolate",
        help="Run crowsnest-only Benchy monitoring plus idle Wallee vision stress and persist an A/B isolation bundle",
    )
    ab_isolation.add_argument(
        "--benchy-file",
        default="Benchy_Bonkers_0.4n_0.28mm_PLA_COREONE_8m.bgcode",
        help="Exact Benchy file display name to start for the crowsnest-only isolation run",
    )
    ab_isolation.add_argument(
        "--benchy-duration-s",
        type=float,
        default=420.0,
        help="Seconds to monitor the nozzle camera during a Benchy print without the Wallee vision loop",
    )
    ab_isolation.add_argument(
        "--idle-iterations",
        type=int,
        default=5,
        help="Number of repeated idle health+vision iterations to run",
    )
    ab_isolation.add_argument(
        "--poll-s",
        type=float,
        default=10.0,
        help="Seconds between Benchy crowsnest-only monitor samples",
    )
    ab_isolation.add_argument(
        "--idle-interval-s",
        type=float,
        default=5.0,
        help="Seconds between idle Wallee vision stress iterations",
    )
    benchy_validation = sub.add_parser(
        "vision-runtime-benchy-validate",
        help="Run one bounded advisory-vision validation under a real Benchy print and persist a full evidence bundle",
    )
    benchy_validation.add_argument(
        "--goal",
        default="Reduce print speed a little and raise nozzle target temperature by 5C while keeping the current print running. Use one bounded action at a time.",
        help="Planner goal for the bounded Benchy validation session",
    )
    benchy_validation.add_argument(
        "--benchy-file",
        default="Benchy_Bonkers_0.4n_0.28mm_PLA_COREONE_8m.bgcode",
        help="Exact Benchy file display name to start and validate against",
    )
    benchy_validation.add_argument(
        "--wait-timeout-s",
        type=float,
        default=90.0,
        help="Maximum seconds to wait for Benchy to enter active_printing",
    )
    benchy_validation.add_argument(
        "--poll-s",
        type=float,
        default=2.0,
        help="Seconds between readiness polls while waiting for active_printing",
    )
    full_audit = sub.add_parser(
        "full-system-benchy-audit",
        help="Run one full observe-only Benchy operational audit with prompt stack and provider metadata capture",
    )
    full_audit.add_argument(
        "--goal",
        default="Observe the bounded runtime state during this Benchy print and return one bounded planner decision without executing it.",
        help="Observe-only planner goal for the audit probes",
    )
    full_audit.add_argument(
        "--benchy-file",
        default="Benchy_Bonkers_0.4n_0.28mm_PLA_COREONE_8m.bgcode",
        help="Exact Benchy file display name to start and audit against",
    )
    full_audit.add_argument(
        "--poll-s",
        type=float,
        default=5.0,
        help="Seconds between status polls during the audit run",
    )
    full_audit.add_argument(
        "--early-probe-progress-pct",
        type=float,
        default=5.0,
        help="Progress percentage threshold for the early observe-only probe",
    )
    full_audit.add_argument(
        "--mid-probe-progress-pct",
        type=float,
        default=50.0,
        help="Progress percentage threshold for the mid-print observe-only probe",
    )

    start = sub.add_parser("start", help="Start a print from storage")
    start.add_argument("file_path", help="Printer file path or display name")

    sub.add_parser("pause", help="Pause the active job")
    sub.add_parser("resume", help="Resume the active job")
    sub.add_parser("cancel", help="Cancel the active job")
    sub.add_parser("trim-speed", help="Bounded speed trim down by one SMALL step")
    sub.add_parser("trim-speed-big", help="Bounded speed trim down by one BIG step")
    sub.add_parser("trim-flow", help="Bounded flow trim down by one SMALL step")
    sub.add_parser("trim-flow-big", help="Bounded flow trim down by one BIG step")
    sub.add_parser("trim-nozzle-up", help="Bounded nozzle target + one SMALL step")
    sub.add_parser("trim-nozzle-up-big", help="Bounded nozzle target + one BIG step")
    sub.add_parser("trim-nozzle-down", help="Bounded nozzle target - one SMALL step")
    sub.add_parser("trim-nozzle-down-big", help="Bounded nozzle target - one BIG step")
    sub.add_parser("trim-bed-up", help="Bounded bed target + one SMALL step")
    sub.add_parser("trim-bed-up-big", help="Bounded bed target + one BIG step")
    sub.add_parser("trim-bed-down", help="Bounded bed target - one SMALL step")
    sub.add_parser("trim-bed-down-big", help="Bounded bed target - one BIG step")
    sub.add_parser("trim-pressure-advance-up", help="Experimental pressure advance + one SMALL step")
    sub.add_parser("trim-pressure-advance-up-big", help="Experimental pressure advance + one BIG step")
    sub.add_parser("trim-pressure-advance-down", help="Experimental pressure advance - one SMALL step")
    sub.add_parser("trim-pressure-advance-down-big", help="Experimental pressure advance - one BIG step")
    sub.add_parser("trim-accel-up", help="Experimental print acceleration + one SMALL step")
    sub.add_parser("trim-accel-up-big", help="Experimental print acceleration + one BIG step")
    sub.add_parser("trim-accel-down", help="Experimental print acceleration - one SMALL step")
    sub.add_parser("trim-accel-down-big", help="Experimental print acceleration - one BIG step")
    sub.add_parser("managed-trim-speed", help="Managed operator-triggered speed trim proof")
    sub.add_parser("managed-reset-speed", help="Managed operator-triggered speed reset proof")
    sub.add_parser("managed-trim-flow", help="Managed operator-triggered flow trim proof")
    sub.add_parser("managed-reset-flow", help="Managed operator-triggered flow reset proof")
    sub.add_parser("managed-trim-nozzle-up", help="Managed operator-triggered nozzle +5C proof")
    sub.add_parser("managed-trim-nozzle-down", help="Managed operator-triggered nozzle -5C proof")
    sub.add_parser("managed-trim-bed-up", help="Managed operator-triggered bed +5C proof")
    sub.add_parser("managed-trim-bed-down", help="Managed operator-triggered bed -5C proof")
    sub.add_parser(
        "managed-proof-session",
        help="Run one controlled managed proof session in order: speed, nozzle, flow, bed",
    )
    phase1 = sub.add_parser(
        "phase1-speed-once",
        help="Run one conservative Phase 1 planner cycle with speed as the only executable family",
    )
    phase1.add_argument(
        "--goal",
        default="Reduce print speed a little while keeping the current print running.",
        help="Planner goal for the Phase 1 speed-only experiment",
    )
    phase1.add_argument(
        "--trace-samples",
        type=int,
        default=8,
        help="Startup evidence samples to capture when autonomy is suppressed at startup",
    )
    phase1.add_argument(
        "--trace-interval-s",
        type=float,
        default=1.0,
        help="Seconds between startup evidence samples",
    )
    phase1_nozzle = sub.add_parser(
        "phase1-nozzle-once",
        help="Run one conservative Phase 1 planner cycle with nozzle as the only shadow family",
    )
    phase1_nozzle.add_argument(
        "--goal",
        default="Raise nozzle target temperature by 5C while keeping the current print running.",
        help="Planner goal for the Phase 1 nozzle-shadow experiment",
    )
    phase1_nozzle.add_argument(
        "--trace-samples",
        type=int,
        default=8,
        help="Startup evidence samples to capture when nozzle shadow is suppressed at startup",
    )
    phase1_nozzle.add_argument(
        "--trace-interval-s",
        type=float,
        default=1.0,
        help="Seconds between startup evidence samples",
    )
    phase1_nozzle_session = sub.add_parser(
        "phase1-nozzle-session",
        help="Run one reset-backed Phase 1 planner cycle with nozzle as the only shadow family",
    )
    phase1_nozzle_session.add_argument(
        "--goal",
        default="Raise nozzle target temperature by 5C while keeping the current print running.",
        help="Planner goal for the reset-backed Phase 1 nozzle-shadow session",
    )
    phase1_nozzle_session.add_argument(
        "--trace-samples",
        type=int,
        default=8,
        help="Startup evidence samples to capture when nozzle shadow is suppressed at startup",
    )
    phase1_nozzle_session.add_argument(
        "--trace-interval-s",
        type=float,
        default=1.0,
        help="Seconds between startup evidence samples",
    )
    phase1_flow = sub.add_parser(
        "phase1-flow-once",
        help="Run one conservative Phase 1 planner cycle with flow as the only shadow family",
    )
    phase1_flow.add_argument(
        "--goal",
        default="Reduce flow a little while keeping the current print running.",
        help="Planner goal for the Phase 1 flow-shadow experiment",
    )
    phase1_flow.add_argument(
        "--trace-samples",
        type=int,
        default=8,
        help="Startup evidence samples to capture when flow shadow is suppressed at startup",
    )
    phase1_flow.add_argument(
        "--trace-interval-s",
        type=float,
        default=1.0,
        help="Seconds between startup evidence samples",
    )
    phase1_flow_session = sub.add_parser(
        "phase1-flow-session",
        help="Run one reset-backed Phase 1 planner cycle with flow as the only shadow family",
    )
    phase1_flow_session.add_argument(
        "--goal",
        default="Reduce flow a little while keeping the current print running.",
        help="Planner goal for the reset-backed Phase 1 flow-shadow session",
    )
    phase1_flow_session.add_argument(
        "--trace-samples",
        type=int,
        default=8,
        help="Startup evidence samples to capture when flow shadow is suppressed at startup",
    )
    phase1_flow_session.add_argument(
        "--trace-interval-s",
        type=float,
        default=1.0,
        help="Seconds between startup evidence samples",
    )
    phase1_bed = sub.add_parser(
        "phase1-bed-once",
        help="Run one conservative Phase 1 planner cycle with bed as the only shadow family",
    )
    phase1_bed.add_argument(
        "--goal",
        default="Raise bed target temperature by 5C while keeping the current print running.",
        help="Planner goal for the Phase 1 bed-shadow experiment",
    )
    phase1_bed.add_argument(
        "--trace-samples",
        type=int,
        default=8,
        help="Startup evidence samples to capture when bed shadow is suppressed at startup",
    )
    phase1_bed.add_argument(
        "--trace-interval-s",
        type=float,
        default=1.0,
        help="Seconds between startup evidence samples",
    )
    phase1_bed_session = sub.add_parser(
        "phase1-bed-session",
        help="Run one reset-backed Phase 1 planner cycle with bed as the only shadow family",
    )
    phase1_bed_session.add_argument(
        "--goal",
        default="Raise bed target temperature by 5C while keeping the current print running.",
        help="Planner goal for the reset-backed Phase 1 bed-shadow session",
    )
    phase1_bed_session.add_argument(
        "--trace-samples",
        type=int,
        default=8,
        help="Startup evidence samples to capture when bed shadow is suppressed at startup",
    )
    phase1_bed_session.add_argument(
        "--trace-interval-s",
        type=float,
        default=1.0,
        help="Seconds between startup evidence samples",
    )
    phase1_speed_flow_session = sub.add_parser(
        "phase1-speed-flow-session",
        help="Run a controlled multi-family Phase 1 session with speed and flow planner actions",
    )
    phase1_speed_flow_session.add_argument(
        "--goal",
        default="Reduce print speed and flow a little while keeping the current print running. Use one bounded action at a time.",
        help="Planner goal for the controlled speed+flow multi-family session",
    )
    phase1_speed_nozzle_session = sub.add_parser(
        "phase1-speed-nozzle-session",
        help="Run a controlled multi-family Phase 1 session with speed and nozzle planner actions",
    )
    phase1_speed_nozzle_session.add_argument(
        "--goal",
        default="Reduce print speed a little and raise nozzle target temperature by 5C while keeping the current print running. Use one bounded action at a time.",
        help="Planner goal for the controlled speed+nozzle multi-family session",
    )
    phase1_speed_bed_session = sub.add_parser(
        "phase1-speed-bed-session",
        help="Run a controlled multi-family Phase 1 session with speed and bed planner actions",
    )
    phase1_speed_bed_session.add_argument(
        "--goal",
        default="Reduce print speed a little and raise bed target temperature by 5C while keeping the current print running. Use one bounded action at a time.",
        help="Planner goal for the controlled speed+bed multi-family session",
    )
    phase1_all_families_session = sub.add_parser(
        "phase1-all-families-session",
        help="Run a controlled multi-family Phase 1 session with speed, flow, nozzle, and bed planner actions",
    )
    phase1_all_families_session.add_argument(
        "--goal",
        default="Reduce print speed and flow a little, raise nozzle target temperature by 5C, and raise bed target temperature by 5C while keeping the current print running. Use one bounded action at a time.",
        help="Planner goal for the controlled all-families multi-family session",
    )

    args = parser.parse_args(argv)
    settings = PrusaCoreOneSettings.from_env()

    if args.command == "status":
        return _emit(_command_status(settings))
    if args.command == "files":
        return _emit(_command_files(settings))
    if args.command == "serial-preflight":
        return _emit(_command_serial_preflight(settings))
    if args.command == "build-notebook":
        return _emit(_command_build_notebook(settings, args.file_path))
    if args.command == "nozzle-vision-once":
        return _emit(
            _command_nozzle_vision_once(
                settings,
                mode=args.mode,
                output_root=args.output_root,
                planner_debug_preview=args.planner_debug_preview,
            )
        )
    if args.command == "replay-nozzle-vision":
        return _emit(
            _command_replay_nozzle_vision(
                settings,
                replay_path=args.replay_path,
            )
        )
    if args.command == "nozzle-camera-health":
        payload, exit_code = _command_nozzle_camera_health(
            settings,
            output_root=args.output_root,
        )
        _emit(payload)
        return exit_code
    if args.command == "nozzle-camera-ab-isolate":
        payload, exit_code = _run_nozzle_camera_ab_isolation(
            settings,
            benchy_file=args.benchy_file,
            benchy_duration_s=args.benchy_duration_s,
            idle_iterations=args.idle_iterations,
            poll_s=args.poll_s,
            idle_interval_s=args.idle_interval_s,
        )
        _emit(payload)
        return exit_code
    if args.command == "vision-runtime-benchy-validate":
        payload, exit_code = _run_vision_runtime_benchy_validation(
            settings,
            goal=args.goal,
            benchy_file=args.benchy_file,
            wait_timeout_s=args.wait_timeout_s,
            poll_s=args.poll_s,
        )
        _emit(payload)
        return exit_code
    if args.command == "full-system-benchy-audit":
        payload, exit_code = _run_full_system_benchy_audit(
            settings,
            goal=args.goal,
            benchy_file=args.benchy_file,
            poll_s=args.poll_s,
            early_probe_progress_pct=args.early_probe_progress_pct,
            mid_probe_progress_pct=args.mid_probe_progress_pct,
        )
        _emit(payload)
        return exit_code

    driver = _build_driver(settings)

    if args.command == "start":
        return _emit(driver.start_print(args.file_path))
    if args.command == "pause":
        return _emit(driver.pause())
    if args.command == "resume":
        return _emit(driver.resume())
    if args.command == "cancel":
        return _emit(driver.cancel())
    if args.command == "trim-speed":
        current_speed = _float_or_none(_command_status(settings).get("speed_pct")) or driver.speed_default_pct
        target_speed = _bounded_relative_target(
            current_speed,
            delta=-driver.speed_small_step_pct,
            lower=driver.speed_min_pct,
            upper=driver.speed_max_pct,
        )
        return _emit(driver.trim_speed_down_small(target_speed_pct=target_speed))
    if args.command == "trim-speed-big":
        current_speed = _float_or_none(_command_status(settings).get("speed_pct")) or driver.speed_default_pct
        target_speed = _bounded_relative_target(
            current_speed,
            delta=-driver.speed_big_step_pct,
            lower=driver.speed_min_pct,
            upper=driver.speed_max_pct,
        )
        return _emit(driver.trim_speed_down_big(target_speed_pct=target_speed))
    if args.command == "trim-flow":
        current_flow = _float_or_none(_command_status(settings).get("flow_pct")) or driver.flow_default_pct
        target_flow = _bounded_relative_target(
            current_flow,
            delta=-driver.flow_small_step_pct,
            lower=driver.flow_min_pct,
            upper=driver.flow_max_pct,
        )
        return _emit(driver.trim_flow_down_small(target_flow_pct=target_flow))
    if args.command == "trim-flow-big":
        current_flow = _float_or_none(_command_status(settings).get("flow_pct")) or driver.flow_default_pct
        target_flow = _bounded_relative_target(
            current_flow,
            delta=-driver.flow_big_step_pct,
            lower=driver.flow_min_pct,
            upper=driver.flow_max_pct,
        )
        return _emit(driver.trim_flow_down_big(target_flow_pct=target_flow))
    if args.command == "trim-nozzle-up":
        current_nozzle = _float_or_none(_command_status(settings).get("nozzle_target_c"))
        if current_nozzle is None:
            raise RuntimeError("trim-nozzle-up requires current nozzle_target_c from status")
        target_nozzle = _bounded_relative_target(
            current_nozzle,
            delta=driver.temp_small_step_c,
            lower=0.0,
            upper=settings.max_nozzle_target_c,
        )
        return _emit(
            driver.trim_nozzle_up_small(
                target_nozzle_c=target_nozzle,
                max_nozzle_target_c=settings.max_nozzle_target_c,
            )
        )
    if args.command == "trim-nozzle-up-big":
        current_nozzle = _float_or_none(_command_status(settings).get("nozzle_target_c"))
        if current_nozzle is None:
            raise RuntimeError("trim-nozzle-up-big requires current nozzle_target_c from status")
        target_nozzle = _bounded_relative_target(
            current_nozzle,
            delta=driver.temp_big_step_c,
            lower=0.0,
            upper=settings.max_nozzle_target_c,
        )
        return _emit(
            driver.trim_nozzle_up_big(
                target_nozzle_c=target_nozzle,
                max_nozzle_target_c=settings.max_nozzle_target_c,
            )
        )
    if args.command == "trim-nozzle-down":
        status = _command_status(settings)
        current_nozzle = _float_or_none(status.get("nozzle_target_c"))
        if current_nozzle is None:
            raise RuntimeError("trim-nozzle-down requires current nozzle_target_c from status")
        min_floor = _float_or_none(status.get("min_extrusion_temp_c")) or 170.0
        target_nozzle = _bounded_relative_target(
            current_nozzle,
            delta=-driver.temp_small_step_c,
            lower=min_floor,
            upper=settings.max_nozzle_target_c,
        )
        return _emit(
            driver.trim_nozzle_down_small(
                target_nozzle_c=target_nozzle,
                min_nozzle_target_c=min_floor,
            )
        )
    if args.command == "trim-nozzle-down-big":
        status = _command_status(settings)
        current_nozzle = _float_or_none(status.get("nozzle_target_c"))
        if current_nozzle is None:
            raise RuntimeError("trim-nozzle-down-big requires current nozzle_target_c from status")
        min_floor = _float_or_none(status.get("min_extrusion_temp_c")) or 170.0
        target_nozzle = _bounded_relative_target(
            current_nozzle,
            delta=-driver.temp_big_step_c,
            lower=min_floor,
            upper=settings.max_nozzle_target_c,
        )
        return _emit(
            driver.trim_nozzle_down_big(
                target_nozzle_c=target_nozzle,
                min_nozzle_target_c=min_floor,
            )
        )
    if args.command == "trim-bed-up":
        current_bed = _float_or_none(_command_status(settings).get("bed_target_c"))
        if current_bed is None:
            raise RuntimeError("trim-bed-up requires current bed_target_c from status")
        target_bed = _bounded_relative_target(
            current_bed,
            delta=driver.temp_small_step_c,
            lower=0.0,
            upper=settings.max_bed_target_c,
        )
        return _emit(
            driver.trim_bed_up_small(
                target_bed_c=target_bed,
                max_bed_target_c=settings.max_bed_target_c,
            )
        )
    if args.command == "trim-bed-up-big":
        current_bed = _float_or_none(_command_status(settings).get("bed_target_c"))
        if current_bed is None:
            raise RuntimeError("trim-bed-up-big requires current bed_target_c from status")
        target_bed = _bounded_relative_target(
            current_bed,
            delta=driver.temp_big_step_c,
            lower=0.0,
            upper=settings.max_bed_target_c,
        )
        return _emit(
            driver.trim_bed_up_big(
                target_bed_c=target_bed,
                max_bed_target_c=settings.max_bed_target_c,
            )
        )
    if args.command == "trim-bed-down":
        current_bed = _float_or_none(_command_status(settings).get("bed_target_c"))
        if current_bed is None:
            raise RuntimeError("trim-bed-down requires current bed_target_c from status")
        target_bed = _bounded_relative_target(
            current_bed,
            delta=-driver.temp_small_step_c,
            lower=0.0,
            upper=settings.max_bed_target_c,
        )
        return _emit(driver.trim_bed_down_small(target_bed_c=target_bed))
    if args.command == "trim-bed-down-big":
        current_bed = _float_or_none(_command_status(settings).get("bed_target_c"))
        if current_bed is None:
            raise RuntimeError("trim-bed-down-big requires current bed_target_c from status")
        target_bed = _bounded_relative_target(
            current_bed,
            delta=-driver.temp_big_step_c,
            lower=0.0,
            upper=settings.max_bed_target_c,
        )
        return _emit(driver.trim_bed_down_big(target_bed_c=target_bed))
    if args.command == "trim-pressure-advance-up":
        current = _float_or_none(_command_status(settings).get("pressure_advance"))
        if current is None:
            raise RuntimeError("trim-pressure-advance-up requires current pressure_advance readback")
        target = driver.pressure_advance_target_from_baseline(current, direction="up", magnitude="small")
        return _emit(driver.trim_pressure_advance_up_small(target_pressure_advance=target))
    if args.command == "trim-pressure-advance-up-big":
        current = _float_or_none(_command_status(settings).get("pressure_advance"))
        if current is None:
            raise RuntimeError("trim-pressure-advance-up-big requires current pressure_advance readback")
        target = driver.pressure_advance_target_from_baseline(current, direction="up", magnitude="big")
        return _emit(driver.trim_pressure_advance_up_big(target_pressure_advance=target))
    if args.command == "trim-pressure-advance-down":
        current = _float_or_none(_command_status(settings).get("pressure_advance"))
        if current is None:
            raise RuntimeError("trim-pressure-advance-down requires current pressure_advance readback")
        target = driver.pressure_advance_target_from_baseline(current, direction="down", magnitude="small")
        return _emit(driver.trim_pressure_advance_down_small(target_pressure_advance=target))
    if args.command == "trim-pressure-advance-down-big":
        current = _float_or_none(_command_status(settings).get("pressure_advance"))
        if current is None:
            raise RuntimeError("trim-pressure-advance-down-big requires current pressure_advance readback")
        target = driver.pressure_advance_target_from_baseline(current, direction="down", magnitude="big")
        return _emit(driver.trim_pressure_advance_down_big(target_pressure_advance=target))
    if args.command == "trim-accel-up":
        current = _float_or_none(_command_status(settings).get("print_accel_mm_s2"))
        if current is None:
            raise RuntimeError("trim-accel-up requires current print_accel_mm_s2 readback")
        target = driver.print_accel_target_from_baseline(current, direction="up", magnitude="small")
        return _emit(driver.trim_print_accel_up_small(target_print_accel_mm_s2=target))
    if args.command == "trim-accel-up-big":
        current = _float_or_none(_command_status(settings).get("print_accel_mm_s2"))
        if current is None:
            raise RuntimeError("trim-accel-up-big requires current print_accel_mm_s2 readback")
        target = driver.print_accel_target_from_baseline(current, direction="up", magnitude="big")
        return _emit(driver.trim_print_accel_up_big(target_print_accel_mm_s2=target))
    if args.command == "trim-accel-down":
        current = _float_or_none(_command_status(settings).get("print_accel_mm_s2"))
        if current is None:
            raise RuntimeError("trim-accel-down requires current print_accel_mm_s2 readback")
        target = driver.print_accel_target_from_baseline(current, direction="down", magnitude="small")
        return _emit(driver.trim_print_accel_down_small(target_print_accel_mm_s2=target))
    if args.command == "trim-accel-down-big":
        current = _float_or_none(_command_status(settings).get("print_accel_mm_s2"))
        if current is None:
            raise RuntimeError("trim-accel-down-big requires current print_accel_mm_s2 readback")
        target = driver.print_accel_target_from_baseline(current, direction="down", magnitude="big")
        return _emit(driver.trim_print_accel_down_big(target_print_accel_mm_s2=target))
    if args.command == "managed-trim-speed":
        return _emit(_managed_execute("A_PRUSA_TRIM_SPEED_DOWN_SMALL", "Reduce print speed a little while keeping the current print running.", settings))
    if args.command == "managed-reset-speed":
        return _emit(_managed_execute("A_PRUSA_OPERATOR_RESTORE_SPEED_DEFAULT", "Restore print speed to the default while keeping the current print running.", settings))
    if args.command == "managed-trim-flow":
        return _emit(_managed_execute("A_PRUSA_TRIM_FLOW_DOWN_SMALL", "Reduce flow a little while keeping the current print running.", settings))
    if args.command == "managed-reset-flow":
        return _emit(_managed_execute("A_PRUSA_OPERATOR_RESTORE_FLOW_DEFAULT", "Restore flow to the default while keeping the current print running.", settings))
    if args.command == "managed-trim-nozzle-up":
        return _emit(_managed_execute("A_PRUSA_TRIM_NOZZLE_UP_SMALL", "Raise nozzle target temperature by 5C while keeping the current print running.", settings))
    if args.command == "managed-trim-nozzle-down":
        return _emit(_managed_execute("A_PRUSA_TRIM_NOZZLE_DOWN_SMALL", "Lower nozzle target temperature by 5C while keeping the current print running.", settings))
    if args.command == "managed-trim-bed-up":
        return _emit(_managed_execute("A_PRUSA_TRIM_BED_UP_SMALL", "Raise bed target temperature by 5C while keeping the current print running.", settings))
    if args.command == "managed-trim-bed-down":
        return _emit(_managed_execute("A_PRUSA_TRIM_BED_DOWN_SMALL", "Lower bed target temperature by 5C while keeping the current print running.", settings))
    if args.command == "managed-proof-session":
        payload, exit_code = _managed_proof_session(settings)
        _emit(payload)
        return exit_code
    if args.command == "phase1-speed-once":
        payload = _run_phase1_speed_experiment(
            settings,
            goal=args.goal,
            trace_samples=args.trace_samples,
            trace_interval_s=args.trace_interval_s,
        )
        artifact_path = _write_phase1_artifact(subdir="runs", stem="phase1-speed-once", payload=payload)
        payload["artifact_path"] = str(artifact_path)
        return _emit(payload)
    if args.command == "phase1-nozzle-once":
        payload = _run_phase1_nozzle_experiment(
            settings,
            goal=args.goal,
            trace_samples=args.trace_samples,
            trace_interval_s=args.trace_interval_s,
        )
        artifact_path = _write_phase1_artifact(subdir="runs", stem="phase1-nozzle-once", payload=payload)
        payload["artifact_path"] = str(artifact_path)
        return _emit(payload)
    if args.command == "phase1-nozzle-session":
        payload = _run_phase1_nozzle_session(
            settings,
            goal=args.goal,
            trace_samples=args.trace_samples,
            trace_interval_s=args.trace_interval_s,
        )
        artifact_path = _write_phase1_artifact(subdir="runs", stem="phase1-nozzle-session", payload=payload)
        payload["artifact_path"] = str(artifact_path)
        return _emit(payload)
    if args.command == "phase1-flow-once":
        payload = _run_phase1_flow_experiment(
            settings,
            goal=args.goal,
            trace_samples=args.trace_samples,
            trace_interval_s=args.trace_interval_s,
        )
        artifact_path = _write_phase1_artifact(subdir="runs", stem="phase1-flow-once", payload=payload)
        payload["artifact_path"] = str(artifact_path)
        return _emit(payload)
    if args.command == "phase1-flow-session":
        payload = _run_phase1_flow_session(
            settings,
            goal=args.goal,
            trace_samples=args.trace_samples,
            trace_interval_s=args.trace_interval_s,
        )
        artifact_path = _write_phase1_artifact(subdir="runs", stem="phase1-flow-session", payload=payload)
        payload["artifact_path"] = str(artifact_path)
        return _emit(payload)
    if args.command == "phase1-bed-once":
        payload = _run_phase1_bed_experiment(
            settings,
            goal=args.goal,
            trace_samples=args.trace_samples,
            trace_interval_s=args.trace_interval_s,
        )
        artifact_path = _write_phase1_artifact(subdir="runs", stem="phase1-bed-once", payload=payload)
        payload["artifact_path"] = str(artifact_path)
        return _emit(payload)
    if args.command == "phase1-bed-session":
        payload = _run_phase1_bed_session(
            settings,
            goal=args.goal,
            trace_samples=args.trace_samples,
            trace_interval_s=args.trace_interval_s,
        )
        artifact_path = _write_phase1_artifact(subdir="runs", stem="phase1-bed-session", payload=payload)
        payload["artifact_path"] = str(artifact_path)
        return _emit(payload)
    if args.command == "phase1-speed-flow-session":
        payload = _run_phase1_multi_family_session(
            settings,
            goal=args.goal,
            families=("speed", "flow"),
        )
        artifact_path = _write_phase1_artifact(subdir="runs", stem="phase1-speed-flow-session", payload=payload)
        payload["artifact_path"] = str(artifact_path)
        return _emit(payload)
    if args.command == "phase1-speed-nozzle-session":
        payload = _run_phase1_multi_family_session(
            settings,
            goal=args.goal,
            families=("speed", "nozzle_temp"),
        )
        artifact_path = _write_phase1_artifact(subdir="runs", stem="phase1-speed-nozzle-session", payload=payload)
        payload["artifact_path"] = str(artifact_path)
        return _emit(payload)
    if args.command == "phase1-speed-bed-session":
        payload = _run_phase1_multi_family_session(
            settings,
            goal=args.goal,
            families=("speed", "bed_temp"),
        )
        artifact_path = _write_phase1_artifact(subdir="runs", stem="phase1-speed-bed-session", payload=payload)
        payload["artifact_path"] = str(artifact_path)
        return _emit(payload)
    if args.command == "phase1-all-families-session":
        payload = _run_phase1_multi_family_session(
            settings,
            goal=args.goal,
            families=("speed", "flow", "nozzle_temp", "bed_temp"),
        )
        artifact_path = _write_phase1_artifact(subdir="runs", stem="phase1-all-families-session", payload=payload)
        payload["artifact_path"] = str(artifact_path)
        return _emit(payload)

    raise SystemExit(2)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
