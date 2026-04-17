"""Prusa CORE One/+ pack for Wallee v6.

The redesign keeps the core trust boundary intact and drastically simplifies the
Prusa-specific side:

- whiteboard facts come from supported Prusa surfaces
- the pack builds a grounded job notebook automatically when a job file is known
- the planner sees only bounded action IDs
- the driver owns the only real write path for lifecycle and live tuning

FFF is deliberately absent from this pack.  Any future observer should plug in
through whiteboard facts, not by becoming a second controller.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any

from ...models import DeviceSummary, HazardClass, LegalAction, NormalizedPackState, ResourceState, WorldPacket
from ...predicates import all_of, any_of, atom
from ...whiteboard import BaseWhiteboard
from ..base import BasePack
from .adapters import (
    NoopSerialWriter,
    PrusaCoreOneSettings,
    PrusaLinkHttpClient,
    PrusaSerialWriter,
    SupportsPrusaHttp,
    SupportsPrusaSerialWriter,
)
from .bgcode_decode import BgcodeDecodeError, normalize_prusa_print_text
from .driver import PrusaDriver, status_to_snapshot
from .job_notebook import (
    CURRENT_NOTEBOOK_PARSER_VERSION,
    active_notes_to_jsonable,
    build_job_notebook,
    load_persisted_notebook,
    persist_job_notebook,
)
from .types import JobNotebook, PrintableFile, PrusaLifecycle, PrusaStatusSnapshot
from . import vision


@dataclass(slots=True)
class _CachedValue:
    value: Any
    fetched_monotonic: float


@dataclass(slots=True)
class _VisionSnapshotCache:
    payload: dict[str, Any]
    fetched_monotonic: float
    job_identity: str
    capture_mode: str
    lifecycle: str
    job_progress_pct: float | None


@dataclass(slots=True)
class _NotebookBinding:
    scope_token: str
    printable: PrintableFile | None
    notebook: JobNotebook | None
    status: str


_NOTEBOOK_RETRY_STATUSES = {"printable_unknown", "identity_unavailable"}
_VISION_PROGRESS_DELTA_REFRESH_PCT = 2.0
_VISION_INLINE_DRAIN_TIMEOUT_S = 0.01
_TERMINAL_FINISH_PROGRESS_PCT = 99.0
_TERMINAL_TARGET_ZERO_EPSILON_C = 0.5


class Pack(BasePack):
    """Prusa CORE One/+ reference pack."""

    DEVICE_ID = "printer_1"

    def __init__(
        self,
        manifest,
        http_client: SupportsPrusaHttp | None = None,
        serial_writer: SupportsPrusaSerialWriter | None = None,
        settings: PrusaCoreOneSettings | None = None,
    ) -> None:
        super().__init__(manifest)
        self.settings = settings or PrusaCoreOneSettings.from_env()
        self.http = http_client or PrusaLinkHttpClient(self.settings)
        self.serial_writer = serial_writer or (
            PrusaSerialWriter(self.settings) if self.settings.serial_enabled else NoopSerialWriter()
        )
        self.driver = PrusaDriver(
            http=self.http,
            serial_writer=self.serial_writer,
            timeout_s=self.settings.state_transition_timeout_s,
            poll_s=self.settings.status_poll_interval_s,
        )
        self._info_cache: _CachedValue | None = None
        self._files_cache: _CachedValue | None = None
        self._file_info_cache: dict[str, _CachedValue] = {}
        self._notebook_cache: dict[str, JobNotebook] = {}
        self._part_present_inference = False
        self._last_job_token: str | None = None
        self._last_printing_job_identity: str | None = None
        self._last_job_time_printing_s: float | None = None
        self._active_printing_job_identity: str | None = None
        self._notebook_dir = Path(self.settings.notebook_dir).resolve() if self.settings.notebook_dir else None
        self._last_notebook_status: str | None = None
        self._last_active_section_reason: str | None = None
        self._bound_notebook: _NotebookBinding | None = None
        self._vision_snapshot_cache: _VisionSnapshotCache | None = None
        self._vision_refresh_future: Future[_VisionSnapshotCache] | None = None
        self._vision_refresh_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wallee-prusa-vision")

    def close(self) -> None:
        future = self._vision_refresh_future
        if future is not None and not future.done():
            future.cancel()
        self._vision_refresh_pool.shutdown(wait=False, cancel_futures=True)
        self.driver.close()

    def _nozzle_camera_health_result(self, *, active_printing: bool) -> vision.NozzleCameraHealthResult:
        return vision.evaluate_nozzle_camera_health(
            self.settings,
            active_printing=active_printing,
        )

    def _vision_job_identity(self, *, snapshot: PrusaStatusSnapshot, notebook: JobNotebook | None) -> str:
        parts: list[str] = []
        if snapshot.job_id is not None:
            parts.append(f"job-{snapshot.job_id}")
        if notebook is not None and notebook.job_hash:
            parts.append(f"hash-{_artifact_slug(notebook.job_hash)[:12]}")
        elif snapshot.current_file:
            parts.append(f"file-{_artifact_slug(Path(snapshot.current_file).name)}")
        if not parts:
            parts.append("idle")
        return "-".join(parts)

    def _observe_vision_advisory(
        self,
        *,
        snapshot: PrusaStatusSnapshot,
        notebook: JobNotebook | None,
    ) -> dict[str, Any]:
        capture_mode = "active-print" if self._vision_active_printing(snapshot) else "idle"
        result = vision.observe_nozzle_advisory(
            self.settings,
            output_root=None,
            job_identity=self._vision_job_identity(snapshot=snapshot, notebook=notebook),
            capture_mode=capture_mode,
            lifecycle=snapshot.lifecycle.value,
        )
        finding_types = "|".join(result.finding_types) if result.finding_types else None
        strength = _max_evidence_strength(result.observation.findings)
        issue_level = _vision_issue_level(result.finding_types, strength)
        return {
            f"{self.DEVICE_ID}.vision_advisory_summary": result.summary,
            f"{self.DEVICE_ID}.vision_advisory_finding_types": finding_types,
            f"{self.DEVICE_ID}.vision_advisory_strength": strength,
            f"{self.DEVICE_ID}.vision_advisory_issue_level": issue_level,
            f"{self.DEVICE_ID}.vision_observation_ref": str(result.observation_path),
            f"{self.DEVICE_ID}.vision_observed_at": result.observation.captured_at,
            f"{self.DEVICE_ID}.vision_frame_ref": result.frame.image_ref,
            f"{self.DEVICE_ID}.vision_debug_summary": result.summary,
        }

    def _vision_active_printing(self, snapshot: PrusaStatusSnapshot) -> bool:
        if not snapshot.job_active:
            return False
        if snapshot.lifecycle != PrusaLifecycle.PRINTING:
            return False
        if snapshot.job_progress_pct is not None and snapshot.job_progress_pct > 0.0:
            return True
        if snapshot.job_time_printing_s is not None and snapshot.job_time_printing_s > 0.0:
            return True
        return False

    def publish_raw_state(self, whiteboard: BaseWhiteboard, *, mode: str = "full") -> None:
        raw: dict[str, Any] = {}
        raw_observed_at = vision._utc_now_iso()
        requested_file = _string_or_none(
            whiteboard.get(f"{self.DEVICE_ID}.requested_file") or whiteboard.get("factory.requested_file")
        )
        self._drain_vision_refresh_if_ready(whiteboard)

        try:
            info = self._get_info_cached()
            status_payload = self.http.get_status()
            job_payload = self.http.get_job()
            snapshot = status_to_snapshot(status_payload, job=job_payload, info=info)
            self._overlay_verified_rate_factors(snapshot, mode=mode)
            self._apply_terminal_finish_override(snapshot)
        except Exception as exc:
            raw.update(
                {
                    f"{self.DEVICE_ID}.raw.connected": False,
                    f"{self.DEVICE_ID}.raw.http_error": str(exc),
                    f"{self.DEVICE_ID}.raw.lifecycle": PrusaLifecycle.OFFLINE.value,
                    f"{self.DEVICE_ID}.raw.health": "OFFLINE",
                    f"{self.DEVICE_ID}.raw.live_tuning_available": False,
                    f"{self.DEVICE_ID}.raw.observed_at": raw_observed_at,
                }
            )
            raw.update(self._empty_vision_payload())
            whiteboard.batch_publish(raw)
            return

        raw.update(self._snapshot_to_raw(snapshot))
        raw[f"{self.DEVICE_ID}.raw.observed_at"] = raw_observed_at
        raw[f"{self.DEVICE_ID}.raw.connected"] = True
        raw[f"{self.DEVICE_ID}.raw.http_error"] = None
        raw[f"{self.DEVICE_ID}.raw.live_tuning_available"] = self.settings.serial_enabled

        files = self._files_for_publish(snapshot=snapshot, requested_file=requested_file, mode=mode)
        raw[f"{self.DEVICE_ID}.raw.file_count"] = len(files)
        raw[f"{self.DEVICE_ID}.raw.files"] = [
            {
                "path": item.path,
                "display_name": item.display_name,
                "size_bytes": item.size_bytes,
                "modified_ts": item.modified_ts,
            }
            for item in files
        ]

        if requested_file:
            raw[f"{self.DEVICE_ID}.raw.requested_file"] = requested_file

        notebook, notebook_status = self._resolve_job_notebook(
            snapshot=snapshot,
            requested_file=requested_file,
            files=files,
            mode=mode,
        )
        self._last_notebook_status = notebook_status
        if notebook is not None:
            active_section = notebook.active_section(snapshot.job_progress_pct)
            if snapshot.job_progress_pct is None:
                active_section_reason = "job_progress_pct_unavailable"
            elif not notebook.sections:
                active_section_reason = "notebook_sections_unavailable"
            elif active_section is None:
                active_section_reason = "section_window_unresolved"
            else:
                active_section_reason = None
            self._last_active_section_reason = active_section_reason
            active_notes = notebook.select_active_notes(
                snapshot.job_progress_pct,
                lookahead_pct=self.settings.notebook_lookahead_pct,
                lifecycle=snapshot.lifecycle.value,
                job_active=snapshot.job_active,
            )
            raw[f"{self.DEVICE_ID}.raw.job_hash"] = notebook.job_hash
            raw[f"{self.DEVICE_ID}.raw.job_material"] = notebook.material
            raw[f"{self.DEVICE_ID}.raw.job_layer_height_mm"] = notebook.layer_height_mm
            raw[f"{self.DEVICE_ID}.raw.job_notebook_available"] = True
            raw[f"{self.DEVICE_ID}.raw.job_notebook_status"] = notebook_status
            raw[f"{self.DEVICE_ID}.raw.job_active_section"] = active_section.section_id if active_section else None
            raw[f"{self.DEVICE_ID}.raw.job_active_section_reason"] = active_section_reason
            raw[f"{self.DEVICE_ID}.raw.job_active_notes"] = notebook.active_notes_summary(snapshot.job_progress_pct, self.settings.notebook_lookahead_pct)
            raw[f"{self.DEVICE_ID}.raw.active_notes_json"] = json.dumps(
                active_notes_to_jsonable(active_notes),
                sort_keys=True,
            )
            raw[f"{self.DEVICE_ID}.raw.job_section_count"] = len(notebook.sections)
        else:
            self._last_active_section_reason = "notebook_unavailable"
            raw[f"{self.DEVICE_ID}.raw.job_notebook_available"] = False
            raw[f"{self.DEVICE_ID}.raw.job_notebook_status"] = notebook_status
            raw[f"{self.DEVICE_ID}.raw.job_hash"] = None
            raw[f"{self.DEVICE_ID}.raw.job_material"] = None
            raw[f"{self.DEVICE_ID}.raw.job_layer_height_mm"] = None
            raw[f"{self.DEVICE_ID}.raw.job_active_section"] = None
            raw[f"{self.DEVICE_ID}.raw.job_active_section_reason"] = self._last_active_section_reason
            raw[f"{self.DEVICE_ID}.raw.job_active_notes"] = None
            raw[f"{self.DEVICE_ID}.raw.active_notes_json"] = None
            raw[f"{self.DEVICE_ID}.raw.job_section_count"] = 0

        raw[f"{self.DEVICE_ID}.part_present"] = self._infer_part_present(snapshot, whiteboard)
        raw.update(self._vision_payload_for_context(snapshot=snapshot, notebook=notebook))
        self._start_vision_refresh_if_needed(whiteboard, snapshot=snapshot, notebook=notebook)
        self._drain_vision_refresh_if_ready(whiteboard, raw=raw, timeout_s=_VISION_INLINE_DRAIN_TIMEOUT_S)
        whiteboard.batch_publish(raw)

    def _apply_terminal_finish_override(self, snapshot: PrusaStatusSnapshot) -> None:
        if snapshot.lifecycle != PrusaLifecycle.PRINTING:
            return
        progress = snapshot.job_progress_pct
        if progress is None or progress < _TERMINAL_FINISH_PROGRESS_PCT:
            return
        nozzle_target = snapshot.nozzle_target_c
        bed_target = snapshot.bed_target_c
        if nozzle_target is None or bed_target is None:
            return
        if nozzle_target > _TERMINAL_TARGET_ZERO_EPSILON_C or bed_target > _TERMINAL_TARGET_ZERO_EPSILON_C:
            return
        snapshot.lifecycle = PrusaLifecycle.FINISHED
        snapshot.job_active = False
        snapshot.job_state = PrusaLifecycle.FINISHED.value

    def _empty_vision_payload(self) -> dict[str, Any]:
        return {
            f"{self.DEVICE_ID}.nozzle_cam_device_present": False,
            f"{self.DEVICE_ID}.nozzle_cam_service_ok": False,
            f"{self.DEVICE_ID}.nozzle_cam_capture_ok": False,
            f"{self.DEVICE_ID}.nozzle_cam_frame_fresh": False,
            f"{self.DEVICE_ID}.nozzle_cam_frame_valid": False,
            f"{self.DEVICE_ID}.nozzle_cam_frame_not_signal_slate": False,
            f"{self.DEVICE_ID}.nozzle_cam_frame_live": False,
            f"{self.DEVICE_ID}.nozzle_cam_usable": False,
            f"{self.DEVICE_ID}.nozzle_cam_health_state": "unusable",
            f"{self.DEVICE_ID}.nozzle_cam_blockers": [vision.NOZZLE_CAM_BLOCKER_UNUSABLE],
            f"{self.DEVICE_ID}.nozzle_cam_last_capture_at": None,
            f"{self.DEVICE_ID}.nozzle_cam_frame_age_s": None,
            f"{self.DEVICE_ID}.nozzle_cam_last_frame_ref": None,
            f"{self.DEVICE_ID}.nozzle_cam_repeated_identical_count": 0,
            f"{self.DEVICE_ID}.vision_advisory_summary": None,
            f"{self.DEVICE_ID}.vision_advisory_finding_types": None,
            f"{self.DEVICE_ID}.vision_advisory_strength": None,
            f"{self.DEVICE_ID}.vision_advisory_issue_level": None,
            f"{self.DEVICE_ID}.vision_observation_ref": None,
            f"{self.DEVICE_ID}.vision_observed_at": None,
            f"{self.DEVICE_ID}.vision_frame_ref": None,
            f"{self.DEVICE_ID}.vision_debug_summary": None,
        }

    def _notebook_scope_token(self, *, snapshot: PrusaStatusSnapshot, requested_file: str | None) -> str | None:
        text = snapshot.current_file or requested_file
        if not text:
            return None
        if snapshot.job_id is not None:
            return f"job:{snapshot.job_id}:{text}"
        return f"file:{text}"

    def _files_for_publish(
        self,
        *,
        snapshot: PrusaStatusSnapshot,
        requested_file: str | None,
        mode: str,
    ) -> list[PrintableFile]:
        if mode == "verify":
            return list(self._files_cache.value) if self._files_cache else []
        scope_token = self._notebook_scope_token(snapshot=snapshot, requested_file=requested_file)
        if snapshot.job_active and scope_token and self._bound_notebook and self._bound_notebook.scope_token == scope_token:
            return list(self._files_cache.value) if self._files_cache else []
        return self._get_files_cached()

    def _resolve_job_notebook(
        self,
        *,
        snapshot: PrusaStatusSnapshot,
        requested_file: str | None,
        files: list[PrintableFile],
        mode: str,
    ) -> tuple[JobNotebook | None, str]:
        scope_token = self._notebook_scope_token(snapshot=snapshot, requested_file=requested_file)
        if scope_token is None:
            self._bound_notebook = None
            return None, "printable_unknown"

        cached = self._bound_notebook
        if (
            cached is not None
            and cached.scope_token == scope_token
            and cached.status not in _NOTEBOOK_RETRY_STATUSES
        ):
            return cached.notebook, cached.status

        notebook_file = self._select_notebook_file(snapshot=snapshot, requested_file=requested_file, files=files)
        if notebook_file is None and mode == "verify" and cached is not None and cached.scope_token == scope_token:
            return cached.notebook, cached.status

        notebook, notebook_status = self._get_job_notebook(notebook_file)
        self._bound_notebook = _NotebookBinding(
            scope_token=scope_token,
            printable=notebook_file,
            notebook=notebook,
            status=notebook_status,
        )
        return notebook, notebook_status

    def _vision_refresh_due(
        self,
        *,
        snapshot: PrusaStatusSnapshot,
        notebook: JobNotebook | None,
    ) -> bool:
        cached = self._vision_snapshot_cache
        capture_mode = "active-print" if self._vision_active_printing(snapshot) else "idle"
        lifecycle = snapshot.lifecycle.value
        job_identity = self._vision_job_identity(snapshot=snapshot, notebook=notebook)
        if cached is None:
            return True
        if cached.job_identity != job_identity or cached.capture_mode != capture_mode or cached.lifecycle != lifecycle:
            return True
        if (
            cached.job_progress_pct is not None
            and snapshot.job_progress_pct is not None
            and abs(snapshot.job_progress_pct - cached.job_progress_pct) >= _VISION_PROGRESS_DELTA_REFRESH_PCT
        ):
            return True
        return (time.monotonic() - cached.fetched_monotonic) >= self.settings.vision_advisory_interval_s

    def _vision_payload_for_context(
        self,
        *,
        snapshot: PrusaStatusSnapshot,
        notebook: JobNotebook | None,
    ) -> dict[str, Any]:
        cached = self._vision_snapshot_cache
        if cached is None:
            return self._empty_vision_payload()
        capture_mode = "active-print" if self._vision_active_printing(snapshot) else "idle"
        lifecycle = snapshot.lifecycle.value
        job_identity = self._vision_job_identity(snapshot=snapshot, notebook=notebook)
        if (
            cached.job_identity != job_identity
            or cached.capture_mode != capture_mode
            or cached.lifecycle != lifecycle
        ):
            return self._empty_vision_payload()
        return dict(cached.payload)

    def _refresh_vision_snapshot(
        self,
        *,
        snapshot: PrusaStatusSnapshot,
        notebook: JobNotebook | None,
    ) -> _VisionSnapshotCache:
        payload = self._empty_vision_payload()
        active_printing = self._vision_active_printing(snapshot)
        health_result = self._nozzle_camera_health_result(active_printing=active_printing)
        payload.update(vision.nozzle_camera_fact_map(health_result.report, prefix=self.DEVICE_ID))
        if bool(payload.get(f"{self.DEVICE_ID}.nozzle_cam_usable", False)):
            try:
                payload.update(self._observe_vision_advisory(snapshot=snapshot, notebook=notebook))
            except Exception:
                payload.update(
                    {
                        f"{self.DEVICE_ID}.vision_advisory_summary": None,
                        f"{self.DEVICE_ID}.vision_advisory_finding_types": None,
                        f"{self.DEVICE_ID}.vision_advisory_strength": None,
                        f"{self.DEVICE_ID}.vision_advisory_issue_level": None,
                        f"{self.DEVICE_ID}.vision_observation_ref": None,
                        f"{self.DEVICE_ID}.vision_observed_at": None,
                        f"{self.DEVICE_ID}.vision_frame_ref": None,
                        f"{self.DEVICE_ID}.vision_debug_summary": None,
                    }
                )
        return _VisionSnapshotCache(
            payload=payload,
            fetched_monotonic=time.monotonic(),
            job_identity=self._vision_job_identity(snapshot=snapshot, notebook=notebook),
            capture_mode="active-print" if active_printing else "idle",
            lifecycle=snapshot.lifecycle.value,
            job_progress_pct=snapshot.job_progress_pct,
        )

    def _start_vision_refresh_if_needed(
        self,
        whiteboard: BaseWhiteboard,
        *,
        snapshot: PrusaStatusSnapshot,
        notebook: JobNotebook | None,
    ) -> None:
        self._drain_vision_refresh_if_ready(whiteboard)
        if self._vision_refresh_future is not None:
            return
        if not self._vision_refresh_due(snapshot=snapshot, notebook=notebook):
            return
        self._vision_refresh_future = self._vision_refresh_pool.submit(
            self._refresh_vision_snapshot,
            snapshot=snapshot,
            notebook=notebook,
        )

    def _drain_vision_refresh_if_ready(
        self,
        whiteboard: BaseWhiteboard,
        *,
        raw: dict[str, Any] | None = None,
        timeout_s: float = 0.0,
    ) -> bool:
        future = self._vision_refresh_future
        if future is None:
            return False
        try:
            if timeout_s > 0.0:
                snapshot = future.result(timeout=timeout_s)
            elif future.done():
                snapshot = future.result()
            else:
                return False
        except FutureTimeoutError:
            return False
        except Exception:
            self._vision_refresh_future = None
            return False
        self._vision_refresh_future = None
        self._vision_snapshot_cache = snapshot
        whiteboard.batch_publish(dict(snapshot.payload))
        if raw is not None:
            raw.update(snapshot.payload)
        return True

    def _overlay_verified_rate_factors(self, snapshot: PrusaStatusSnapshot, *, mode: str) -> None:
        if not self.settings.serial_enabled:
            return
        if mode != "verify":
            return
        if self.settings.flow_tuning_verification_enabled:
            try:
                snapshot.flow_pct = self.driver.read_flow_factor_pct()
            except Exception:
                snapshot.flow_pct = None

    def normalize(self, snapshot: dict[str, Any]) -> NormalizedPackState:
        lifecycle = str(snapshot.get(f"{self.DEVICE_ID}.raw.lifecycle", PrusaLifecycle.UNKNOWN.value))
        health = str(snapshot.get(f"{self.DEVICE_ID}.raw.health", "UNKNOWN"))
        job_active = bool(snapshot.get(f"{self.DEVICE_ID}.raw.job_active", False))
        job_progress = _float_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.job_progress_pct"))
        job_time_printing = _float_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.job_time_printing_s"))
        current_file = _string_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.current_file"))
        job_id = _int_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.job_id"))
        requested_file = _string_or_none(
            snapshot.get(f"{self.DEVICE_ID}.requested_file")
            or snapshot.get(f"{self.DEVICE_ID}.raw.requested_file")
            or snapshot.get("factory.requested_file")
        )
        speed_pct = _float_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.speed_pct"))
        flow_pct = _float_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.flow_pct"))
        nozzle_temp = _float_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.nozzle_temp_c"))
        nozzle_target = _float_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.nozzle_target_c"))
        bed_temp = _float_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.bed_temp_c"))
        bed_target = _float_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.bed_target_c"))
        min_extrusion_temp = _float_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.min_extrusion_temp_c"))
        part_present = bool(snapshot.get(f"{self.DEVICE_ID}.part_present", False))
        live_tuning_available = bool(snapshot.get(f"{self.DEVICE_ID}.raw.live_tuning_available", False))
        job_hash = _string_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.job_hash"))
        job_material = _string_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.job_material"))
        job_layer_height = _float_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.job_layer_height_mm"))
        job_bed_target_default = _float_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.job_bed_target_c_default"))
        job_active_section = _string_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.job_active_section"))
        job_active_section_reason = _string_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.job_active_section_reason"))
        job_active_notes = _string_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.job_active_notes"))
        active_notes_json = _string_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.active_notes_json"))
        job_notebook_available = bool(snapshot.get(f"{self.DEVICE_ID}.raw.job_notebook_available", False))
        job_notebook_status = _string_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.job_notebook_status"))
        nozzle_cam_device_present = bool(snapshot.get(f"{self.DEVICE_ID}.nozzle_cam_device_present", False))
        nozzle_cam_service_ok = bool(snapshot.get(f"{self.DEVICE_ID}.nozzle_cam_service_ok", False))
        nozzle_cam_capture_ok = bool(snapshot.get(f"{self.DEVICE_ID}.nozzle_cam_capture_ok", False))
        nozzle_cam_frame_fresh = bool(snapshot.get(f"{self.DEVICE_ID}.nozzle_cam_frame_fresh", False))
        nozzle_cam_frame_valid = bool(snapshot.get(f"{self.DEVICE_ID}.nozzle_cam_frame_valid", False))
        nozzle_cam_frame_not_signal_slate = bool(snapshot.get(f"{self.DEVICE_ID}.nozzle_cam_frame_not_signal_slate", False))
        nozzle_cam_frame_live = bool(snapshot.get(f"{self.DEVICE_ID}.nozzle_cam_frame_live", False))
        nozzle_cam_usable = bool(snapshot.get(f"{self.DEVICE_ID}.nozzle_cam_usable", False))
        nozzle_cam_health_state = _string_or_none(snapshot.get(f"{self.DEVICE_ID}.nozzle_cam_health_state")) or "unusable"
        nozzle_cam_blockers = _compact_blocker_list(snapshot.get(f"{self.DEVICE_ID}.nozzle_cam_blockers"))
        nozzle_cam_last_capture_at = _string_or_none(snapshot.get(f"{self.DEVICE_ID}.nozzle_cam_last_capture_at"))
        nozzle_cam_frame_age_s = _float_or_none(snapshot.get(f"{self.DEVICE_ID}.nozzle_cam_frame_age_s"))
        nozzle_cam_last_frame_ref = _string_or_none(snapshot.get(f"{self.DEVICE_ID}.nozzle_cam_last_frame_ref"))
        nozzle_cam_repeated_identical_count = _int_or_none(snapshot.get(f"{self.DEVICE_ID}.nozzle_cam_repeated_identical_count")) or 0
        raw_observed_at = _string_or_none(snapshot.get(f"{self.DEVICE_ID}.raw.observed_at"))
        vision_advisory_summary = _string_or_none(snapshot.get(f"{self.DEVICE_ID}.vision_advisory_summary"))
        vision_advisory_finding_types = _string_or_none(snapshot.get(f"{self.DEVICE_ID}.vision_advisory_finding_types"))
        vision_advisory_strength = _string_or_none(snapshot.get(f"{self.DEVICE_ID}.vision_advisory_strength"))
        vision_advisory_issue_level = _string_or_none(snapshot.get(f"{self.DEVICE_ID}.vision_advisory_issue_level"))
        vision_observation_ref = _string_or_none(snapshot.get(f"{self.DEVICE_ID}.vision_observation_ref"))
        vision_frame_ref = _string_or_none(snapshot.get(f"{self.DEVICE_ID}.vision_frame_ref"))
        vision_debug_summary = _string_or_none(snapshot.get(f"{self.DEVICE_ID}.vision_debug_summary"))
        if not nozzle_cam_usable:
            vision_advisory_summary = None
            vision_advisory_finding_types = None
            vision_advisory_strength = None
            vision_advisory_issue_level = None
            vision_observation_ref = None
            vision_frame_ref = None
            vision_debug_summary = None
        printing_phase = self._derive_printing_phase(
            lifecycle=lifecycle,
            job_active=job_active,
            job_id=job_id,
            job_progress_pct=job_progress,
            current_file=current_file,
            job_time_printing_s=job_time_printing,
        )
        late_tuning_symptom_active = self._late_tuning_symptom_active(
            finding_types_text=vision_advisory_finding_types,
            issue_level=vision_advisory_issue_level,
            printing_phase=printing_phase,
        )
        nozzle_shadow = self._derive_nozzle_shadow_state(
            lifecycle=lifecycle,
            job_active=job_active,
            printing_phase=printing_phase,
            live_tuning_available=live_tuning_available,
            job_progress_pct=job_progress,
            nozzle_target_c=nozzle_target,
            min_extrusion_temp_c=min_extrusion_temp,
            late_tuning_symptom_active=late_tuning_symptom_active,
        )
        flow_shadow = self._derive_flow_shadow_state(
            lifecycle=lifecycle,
            job_active=job_active,
            printing_phase=printing_phase,
            live_tuning_available=live_tuning_available,
            flow_tuning_verification_enabled=self.settings.flow_tuning_verification_enabled,
            job_progress_pct=job_progress,
            flow_pct=flow_pct,
            late_tuning_symptom_active=late_tuning_symptom_active,
        )
        bed_shadow = self._derive_bed_shadow_state(
            lifecycle=lifecycle,
            job_active=job_active,
            printing_phase=printing_phase,
            live_tuning_available=live_tuning_available,
            job_progress_pct=job_progress,
            bed_target_c=bed_target,
            ideal_bed_target_c=job_bed_target_default if job_bed_target_default is not None else bed_target,
        )
        speed_autonomy = self._derive_speed_autonomy_state(
            lifecycle=lifecycle,
            job_active=job_active,
            printing_phase=printing_phase,
            live_tuning_available=live_tuning_available,
            speed_tuning_verification_enabled=self.settings.speed_tuning_verification_enabled,
            speed_pct=speed_pct,
            job_progress_pct=job_progress,
            nozzle_temp_c=nozzle_temp,
            nozzle_target_c=nozzle_target,
            bed_temp_c=bed_temp,
            bed_target_c=bed_target,
            late_tuning_symptom_active=late_tuning_symptom_active,
        )

        known_files = self._files_from_snapshot(snapshot)
        requested_file_present = bool(requested_file and self._match_requested_file(requested_file, known_files))
        top_files = known_files[: self.settings.file_action_limit]

        safe_to_unload = self._compute_safe_to_unload(
            part_present=part_present,
            lifecycle=lifecycle,
            bed_temp_c=bed_temp,
            nozzle_temp_c=nozzle_temp,
            health=health,
        )

        blockers: list[str] = []
        if health == "OFFLINE":
            blockers.append("PrusaLink status is unreachable")
        if lifecycle == PrusaLifecycle.ATTENTION.value:
            blockers.append("Printer is in ATTENTION and needs operator help")
        if lifecycle == PrusaLifecycle.ERROR.value:
            blockers.append("Printer is in ERROR")
        if requested_file and not requested_file_present:
            blockers.append(f"Requested file {requested_file!r} is not on printer storage")

        summary_bits = [f"lifecycle={lifecycle}", f"health={health}"]
        if current_file:
            summary_bits.append(f"file={current_file}")
        if job_progress is not None:
            summary_bits.append(f"progress={job_progress:.1f}%")
        if speed_pct is not None:
            summary_bits.append(f"speed={speed_pct:.0f}%")
        if flow_pct is not None:
            summary_bits.append(f"flow={flow_pct:.0f}%")
        if job_active_notes:
            summary_bits.append(f"notes={job_active_notes}")

        summary = DeviceSummary(
            device_id=self.DEVICE_ID,
            display_name=self.manifest.display_name,
            category=self.manifest.category,
            mode=lifecycle,
            health=health,
            summary=" | ".join(summary_bits),
        )

        resources = [
            ResourceState(
                id=f"{self.DEVICE_ID}.bed",
                kind="bed",
                owner=self.DEVICE_ID,
                state="OCCUPIED" if part_present else "EMPTY",
                attributes={
                    "bed_temp_c": round(bed_temp, 1) if bed_temp is not None else None,
                    "safe_to_unload": safe_to_unload,
                },
            ),
            ResourceState(
                id=f"{self.DEVICE_ID}.usb_storage",
                kind="storage",
                owner=self.DEVICE_ID,
                state="READY" if known_files else "EMPTY",
                attributes={
                    "file_count": len(known_files),
                    "requested_file": requested_file,
                    "requested_file_present": requested_file_present,
                    "top_file_1": top_files[0].display_name if len(top_files) >= 1 else None,
                    "top_file_2": top_files[1].display_name if len(top_files) >= 2 else None,
                },
            ),
        ]

        job_scope_token = current_file or self._last_job_token or requested_file

        facts = {
            f"{self.DEVICE_ID}.lifecycle": lifecycle,
            f"{self.DEVICE_ID}.mode": lifecycle,  # compatibility alias for generic dashboards
            f"{self.DEVICE_ID}.health": health,
            f"{self.DEVICE_ID}.job_active": job_active,
            f"{self.DEVICE_ID}.job_progress_pct": round(job_progress, 1) if job_progress is not None else None,
            f"{self.DEVICE_ID}.job_time_printing_s": round(job_time_printing, 1) if job_time_printing is not None else None,
            f"{self.DEVICE_ID}.current_file": current_file,
            f"{self.DEVICE_ID}.job_scope_token": job_scope_token,
            f"{self.DEVICE_ID}.requested_file": requested_file,
            f"{self.DEVICE_ID}.requested_file_present": requested_file_present,
            f"{self.DEVICE_ID}.part_present": part_present,
            f"{self.DEVICE_ID}.safe_to_unload": safe_to_unload,
            f"{self.DEVICE_ID}.speed_pct": round(speed_pct, 1) if speed_pct is not None else None,
            f"{self.DEVICE_ID}.flow_pct": round(flow_pct, 1) if flow_pct is not None else None,
            f"{self.DEVICE_ID}.nozzle_temp_c": round(nozzle_temp, 1) if nozzle_temp is not None else None,
            f"{self.DEVICE_ID}.nozzle_target_c": round(nozzle_target, 1) if nozzle_target is not None else None,
            f"{self.DEVICE_ID}.bed_temp_c": round(bed_temp, 1) if bed_temp is not None else None,
            f"{self.DEVICE_ID}.bed_target_c": round(bed_target, 1) if bed_target is not None else None,
            f"{self.DEVICE_ID}.min_extrusion_temp_c": round(min_extrusion_temp, 1) if min_extrusion_temp is not None else None,
            f"{self.DEVICE_ID}.live_tuning_available": live_tuning_available,
            f"{self.DEVICE_ID}.job_hash": job_hash,
            f"{self.DEVICE_ID}.job_material": job_material,
            f"{self.DEVICE_ID}.job_layer_height_mm": round(job_layer_height, 3) if job_layer_height is not None else None,
            f"{self.DEVICE_ID}.job_bed_target_c_default": round(job_bed_target_default, 1) if job_bed_target_default is not None else None,
            f"{self.DEVICE_ID}.job_notebook_available": job_notebook_available,
            f"{self.DEVICE_ID}.job_notebook_status": job_notebook_status,
            f"{self.DEVICE_ID}.job_active_section": job_active_section,
            f"{self.DEVICE_ID}.job_active_section_reason": job_active_section_reason,
            f"{self.DEVICE_ID}.job_active_notes": job_active_notes,
            f"{self.DEVICE_ID}.active_notes_json": active_notes_json,
            f"{self.DEVICE_ID}.printing_phase": printing_phase,
            f"{self.DEVICE_ID}.active_printing": printing_phase == "active_printing",
            f"{self.DEVICE_ID}.speed_autonomy_boundary": speed_autonomy["boundary_text"],
            f"{self.DEVICE_ID}.speed_autonomy_eligible": speed_autonomy["eligible"],
            f"{self.DEVICE_ID}.speed_autonomy_blockers": speed_autonomy["blockers_text"],
            f"{self.DEVICE_ID}.nozzle_shadow_eligible": nozzle_shadow["eligible"],
            f"{self.DEVICE_ID}.nozzle_shadow_blockers": nozzle_shadow["blockers_text"],
            f"{self.DEVICE_ID}.nozzle_shadow_actions": nozzle_shadow["actions_text"],
            f"{self.DEVICE_ID}.flow_shadow_eligible": flow_shadow["eligible"],
            f"{self.DEVICE_ID}.flow_shadow_blockers": flow_shadow["blockers_text"],
            f"{self.DEVICE_ID}.flow_shadow_actions": flow_shadow["actions_text"],
            f"{self.DEVICE_ID}.bed_shadow_eligible": bed_shadow["eligible"],
            f"{self.DEVICE_ID}.bed_shadow_blockers": bed_shadow["blockers_text"],
            f"{self.DEVICE_ID}.bed_shadow_actions": bed_shadow["actions_text"],
            f"{self.DEVICE_ID}.nozzle_cam_device_present": nozzle_cam_device_present,
            f"{self.DEVICE_ID}.nozzle_cam_service_ok": nozzle_cam_service_ok,
            f"{self.DEVICE_ID}.nozzle_cam_capture_ok": nozzle_cam_capture_ok,
            f"{self.DEVICE_ID}.nozzle_cam_frame_fresh": nozzle_cam_frame_fresh,
            f"{self.DEVICE_ID}.nozzle_cam_frame_valid": nozzle_cam_frame_valid,
            f"{self.DEVICE_ID}.nozzle_cam_frame_not_signal_slate": nozzle_cam_frame_not_signal_slate,
            f"{self.DEVICE_ID}.nozzle_cam_frame_live": nozzle_cam_frame_live,
            f"{self.DEVICE_ID}.nozzle_cam_usable": nozzle_cam_usable,
            f"{self.DEVICE_ID}.nozzle_cam_health_state": nozzle_cam_health_state,
            f"{self.DEVICE_ID}.nozzle_cam_blockers": "|".join(nozzle_cam_blockers) if nozzle_cam_blockers else None,
            f"{self.DEVICE_ID}.nozzle_cam_last_capture_at": nozzle_cam_last_capture_at,
            f"{self.DEVICE_ID}.nozzle_cam_frame_age_s": round(nozzle_cam_frame_age_s, 3) if nozzle_cam_frame_age_s is not None else None,
            f"{self.DEVICE_ID}.nozzle_cam_last_frame_ref": nozzle_cam_last_frame_ref,
            f"{self.DEVICE_ID}.nozzle_cam_repeated_identical_count": nozzle_cam_repeated_identical_count,
            f"{self.DEVICE_ID}.raw_observed_at": raw_observed_at,
            f"{self.DEVICE_ID}.vision_advisory_summary": vision_advisory_summary,
            f"{self.DEVICE_ID}.vision_advisory_finding_types": vision_advisory_finding_types,
            f"{self.DEVICE_ID}.vision_advisory_strength": vision_advisory_strength,
            f"{self.DEVICE_ID}.vision_advisory_issue_level": vision_advisory_issue_level,
            f"{self.DEVICE_ID}.vision_observation_ref": vision_observation_ref,
            f"{self.DEVICE_ID}.vision_frame_ref": vision_frame_ref,
            f"{self.DEVICE_ID}.vision_debug_summary": vision_debug_summary,
        }
        return NormalizedPackState(summary=summary, facts=facts, resources=resources, blockers=blockers)

    def candidate_actions(self, world: WorldPacket) -> list[LegalAction]:
        lifecycle = str(world.facts.get(f"{self.DEVICE_ID}.lifecycle", PrusaLifecycle.UNKNOWN.value))
        health = str(world.facts.get(f"{self.DEVICE_ID}.health", "UNKNOWN"))
        part_present = bool(world.facts.get(f"{self.DEVICE_ID}.part_present", False))
        safe_to_unload = bool(world.facts.get(f"{self.DEVICE_ID}.safe_to_unload", False))
        requested_file = _string_or_none(world.facts.get(f"{self.DEVICE_ID}.requested_file"))
        requested_file_present = bool(world.facts.get(f"{self.DEVICE_ID}.requested_file_present", False))
        has_handler = any(resource.kind == "gripper" for resource in world.resources)

        actions: list[LegalAction] = []

        printing_phase = str(world.facts.get(f"{self.DEVICE_ID}.printing_phase", "not_printing"))

        if lifecycle == PrusaLifecycle.PRINTING.value:
            actions.extend(
                [
                    LegalAction(
                        action_id="A_PRUSA_CANCEL",
                        verb="STOP_PROCESS",
                        description="Cancel the active Prusa print",
                        owner_pack=self.pack_id,
                        execute_ref="stop_process",
                        args={},
                        required_locks=[f"{self.DEVICE_ID}.motion"],
                        hazard_class=HazardClass.MEDIUM,
                        approval_required=True,
                        target_device=self.DEVICE_ID,
                        verify=all_of(
                            atom(f"{self.DEVICE_ID}.job_active", "==", False),
                            any_of(
                            atom(f"{self.DEVICE_ID}.lifecycle", "==", PrusaLifecycle.IDLE.value),
                            atom(f"{self.DEVICE_ID}.lifecycle", "==", PrusaLifecycle.FINISHED.value),
                            atom(f"{self.DEVICE_ID}.lifecycle", "==", PrusaLifecycle.STOPPED.value),
                        ),
                        ),
                        rank_hint=90,
                    ),
                ]
            )
            if printing_phase == "active_printing":
                actions.append(
                    LegalAction(
                        action_id="A_PRUSA_PAUSE",
                        verb="PAUSE_PROCESS",
                        description="Pause the active Prusa print",
                        owner_pack=self.pack_id,
                        execute_ref="pause_process",
                        args={},
                        required_locks=[f"{self.DEVICE_ID}.motion"],
                        target_device=self.DEVICE_ID,
                        verify=atom(f"{self.DEVICE_ID}.lifecycle", "==", PrusaLifecycle.PAUSED.value),
                        rank_hint=15,
                    )
                )
            actions.extend(self._live_tuning_actions(world, include_experimental=self.settings.enable_experimental_tuning))

        if lifecycle == PrusaLifecycle.PAUSED.value:
            actions.extend(
                [
                    LegalAction(
                        action_id="A_PRUSA_RESUME",
                        verb="RESUME_PROCESS",
                        description="Resume the paused Prusa print",
                        owner_pack=self.pack_id,
                        execute_ref="resume_process",
                        args={},
                        required_locks=[f"{self.DEVICE_ID}.motion"],
                        target_device=self.DEVICE_ID,
                        verify=atom(f"{self.DEVICE_ID}.lifecycle", "==", PrusaLifecycle.PRINTING.value),
                        rank_hint=15,
                    ),
                    LegalAction(
                        action_id="A_PRUSA_CANCEL",
                        verb="STOP_PROCESS",
                        description="Cancel the paused Prusa print",
                        owner_pack=self.pack_id,
                        execute_ref="stop_process",
                        args={},
                        required_locks=[f"{self.DEVICE_ID}.motion"],
                        hazard_class=HazardClass.MEDIUM,
                        approval_required=True,
                        target_device=self.DEVICE_ID,
                        verify=all_of(
                            atom(f"{self.DEVICE_ID}.job_active", "==", False),
                            any_of(
                            atom(f"{self.DEVICE_ID}.lifecycle", "==", PrusaLifecycle.IDLE.value),
                            atom(f"{self.DEVICE_ID}.lifecycle", "==", PrusaLifecycle.FINISHED.value),
                            atom(f"{self.DEVICE_ID}.lifecycle", "==", PrusaLifecycle.STOPPED.value),
                        ),
                        ),
                        rank_hint=90,
                    ),
                ]
            )

        if lifecycle != PrusaLifecycle.PRINTING.value and part_present and not safe_to_unload:
            actions.append(
                LegalAction(
                    action_id="A_PRUSA_WAIT_COOL",
                    verb="WAIT_UNTIL",
                    description="Wait until the print bed and nozzle are cool enough for unloading",
                    owner_pack="builtin",
                    execute_ref="builtin.wait_until",
                    args={
                        "timeout_s": max(1, int(round(self.settings.wait_cool_step_timeout_s))),
                        "predicate": f"{self.DEVICE_ID}.safe_to_unload == true",
                        "nonfatal_timeout": True,
                    },
                    required_locks=[],
                    preconditions=atom(f"{self.DEVICE_ID}.part_present", "==", True),
                    verify=atom(f"{self.DEVICE_ID}.safe_to_unload", "==", True),
                    target_device=self.DEVICE_ID,
                    rank_hint=12,
                )
            )

        if lifecycle in {PrusaLifecycle.IDLE.value, PrusaLifecycle.FINISHED.value, PrusaLifecycle.STOPPED.value} and not part_present and requested_file and requested_file_present:
            actions.append(
                LegalAction(
                    action_id=_file_action_id(requested_file),
                    verb="START_PROCESS",
                    description=f"Start the requested Prusa print file {requested_file}",
                    owner_pack=self.pack_id,
                    execute_ref="start_process",
                    args={"file_path": requested_file},
                    required_locks=[f"{self.DEVICE_ID}.motion", f"{self.DEVICE_ID}.bed"],
                    hazard_class=HazardClass.MEDIUM,
                    approval_required=True,
                    target_device=self.DEVICE_ID,
                    verify=all_of(
                        atom(f"{self.DEVICE_ID}.job_active", "==", True),
                        atom(f"{self.DEVICE_ID}.lifecycle", "==", PrusaLifecycle.PRINTING.value),
                    ),
                    rank_hint=18,
                )
            )

        if health in {"ATTENTION", "ERROR", "OFFLINE"}:
            actions.append(
                LegalAction(
                    action_id="A_PRUSA_CALL_HUMAN",
                    verb="CALL_HUMAN",
                    description="Ask a human to inspect the Prusa printer",
                    owner_pack="builtin",
                    execute_ref="builtin.call_human",
                    args={"message": f"Inspect {self.manifest.display_name}: lifecycle={lifecycle}, health={health}"},
                    required_locks=[],
                    rank_hint=180,
                )
            )
        elif part_present and safe_to_unload and not has_handler:
            actions.append(
                LegalAction(
                    action_id="A_PRUSA_MANUAL_UNLOAD",
                    verb="CALL_HUMAN",
                    description="Ask a human to unload the cooled print because no handling device is present",
                    owner_pack="builtin",
                    execute_ref="builtin.call_human",
                    args={"message": "Unload the cooled print and then mark printer_1.part_present=false"},
                    required_locks=[],
                    rank_hint=140,
                )
            )

        return actions

    def operator_actions(self, world: WorldPacket) -> list[LegalAction]:
        lifecycle = str(world.facts.get(f"{self.DEVICE_ID}.lifecycle", PrusaLifecycle.UNKNOWN.value))
        if lifecycle != PrusaLifecycle.PRINTING.value:
            return []
        return self._live_tuning_actions(
            world,
            include_experimental=True,
            include_operator_resets=True,
        )

    def _realize(self, action: LegalAction, whiteboard: BaseWhiteboard) -> dict[str, Any]:
        if action.verb == "PAUSE_PROCESS":
            return self.driver.pause()
        if action.verb == "RESUME_PROCESS":
            return self.driver.resume()
        if action.verb == "STOP_PROCESS":
            result = self.driver.cancel()
            self._part_present_inference = True
            whiteboard.publish(f"{self.DEVICE_ID}.part_present", True)
            return result
        if action.verb == "START_PROCESS":
            file_path = str(action.args.get("file_path", "")).strip()
            if not file_path:
                raise ValueError("START_PROCESS requires file_path")
            result = self.driver.start_print(file_path)
            self._part_present_inference = True
            whiteboard.publish(f"{self.DEVICE_ID}.part_present", True)
            return result
        if action.execute_ref == "trim_speed_down_small":
            target_speed_pct = _float_or_none(action.args.get("target_speed_pct"))
            if target_speed_pct is None:
                raise ValueError("trim_speed_down_small requires target_speed_pct")
            return self.driver.trim_speed_down_small(target_speed_pct=target_speed_pct)
        if action.execute_ref == "trim_speed_up_small":
            target_speed_pct = _float_or_none(action.args.get("target_speed_pct"))
            if target_speed_pct is None:
                raise ValueError("trim_speed_up_small requires target_speed_pct")
            return self.driver.trim_speed_up_small(target_speed_pct=target_speed_pct)
        if action.execute_ref == "restore_speed_default":
            return self.driver.restore_speed_default()
        if action.execute_ref == "trim_flow_down_small":
            target_flow_pct = _float_or_none(action.args.get("target_flow_pct"))
            if target_flow_pct is None:
                raise ValueError("trim_flow_down_small requires target_flow_pct")
            return self.driver.trim_flow_down_small(target_flow_pct=target_flow_pct)
        if action.execute_ref == "trim_flow_up_small":
            target_flow_pct = _float_or_none(action.args.get("target_flow_pct"))
            if target_flow_pct is None:
                raise ValueError("trim_flow_up_small requires target_flow_pct")
            return self.driver.trim_flow_up_small(target_flow_pct=target_flow_pct)
        if action.execute_ref == "restore_flow_default":
            return self.driver.restore_flow_default()
        if action.execute_ref == "trim_nozzle_down_small":
            info = self._get_info_cached() or {}
            min_nozzle_target_c = _float_or_none(info.get("min_extrusion_temp")) or 170.0
            target_nozzle_c = _float_or_none(action.args.get("target_nozzle_c"))
            if target_nozzle_c is None:
                raise ValueError("trim_nozzle_down_small requires target_nozzle_c")
            return self.driver.trim_nozzle_down_small(
                target_nozzle_c=target_nozzle_c,
                min_nozzle_target_c=min_nozzle_target_c,
            )
        if action.execute_ref == "trim_nozzle_up_small":
            target_nozzle_c = _float_or_none(action.args.get("target_nozzle_c"))
            if target_nozzle_c is None:
                raise ValueError("trim_nozzle_up_small requires target_nozzle_c")
            return self.driver.trim_nozzle_up_small(
                target_nozzle_c=target_nozzle_c,
                max_nozzle_target_c=self.settings.max_nozzle_target_c,
            )
        if action.execute_ref == "trim_bed_down_small":
            target_bed_c = _float_or_none(action.args.get("target_bed_c"))
            if target_bed_c is None:
                raise ValueError("trim_bed_down_small requires target_bed_c")
            return self.driver.trim_bed_down_small(target_bed_c=target_bed_c)
        if action.execute_ref == "trim_bed_up_small":
            target_bed_c = _float_or_none(action.args.get("target_bed_c"))
            if target_bed_c is None:
                raise ValueError("trim_bed_up_small requires target_bed_c")
            return self.driver.trim_bed_up_small(
                target_bed_c=target_bed_c,
                max_bed_target_c=self.settings.max_bed_target_c,
            )
        raise ValueError(f"unsupported Prusa execute_ref {action.execute_ref}")

    def _live_tuning_actions(
        self,
        world: WorldPacket,
        *,
        include_experimental: bool = False,
        include_operator_resets: bool = False,
    ) -> list[LegalAction]:
        actions: list[LegalAction] = []
        if not bool(world.facts.get(f"{self.DEVICE_ID}.live_tuning_available", False)):
            return actions

        speed_autonomy_eligible = bool(world.facts.get(f"{self.DEVICE_ID}.speed_autonomy_eligible", False))
        speed_pct = _float_or_none(world.facts.get(f"{self.DEVICE_ID}.speed_pct"))
        if speed_autonomy_eligible and speed_pct is not None and speed_pct > PrusaDriver.SPEED_MIN_PCT:
            down_target = max(PrusaDriver.SPEED_MIN_PCT, speed_pct - PrusaDriver.RATE_STEP_PCT)
            actions.append(
                LegalAction(
                    action_id="A_PRUSA_TRIM_SPEED_DOWN_SMALL",
                    verb="TUNE_SPEED",
                    description="Reduce print speed a little",
                    owner_pack=self.pack_id,
                    execute_ref="trim_speed_down_small",
                    args={"target_speed_pct": round(down_target, 1)},
                    required_locks=[f"{self.DEVICE_ID}.motion"],
                    target_device=self.DEVICE_ID,
                    verify=atom(f"{self.DEVICE_ID}.speed_pct", "==", round(down_target, 1)),
                    rank_hint=20,
                )
            )
        if speed_autonomy_eligible and speed_pct is not None and speed_pct < PrusaDriver.SPEED_MAX_PCT:
            up_target = min(PrusaDriver.SPEED_MAX_PCT, speed_pct + PrusaDriver.RATE_STEP_PCT)
            actions.append(
                LegalAction(
                    action_id="A_PRUSA_TRIM_SPEED_UP_SMALL",
                    verb="TUNE_SPEED",
                    description="Increase print speed a little",
                    owner_pack=self.pack_id,
                    execute_ref="trim_speed_up_small",
                    args={"target_speed_pct": round(up_target, 1)},
                    required_locks=[f"{self.DEVICE_ID}.motion"],
                    target_device=self.DEVICE_ID,
                    verify=atom(f"{self.DEVICE_ID}.speed_pct", "==", round(up_target, 1)),
                    rank_hint=21,
                )
            )
        if (
            include_operator_resets
            and speed_autonomy_eligible
            and speed_pct is not None
            and abs(speed_pct - PrusaDriver.DEFAULT_SPEED_PCT) >= 0.5
        ):
            actions.append(
                LegalAction(
                    action_id="A_PRUSA_OPERATOR_RESTORE_SPEED_DEFAULT",
                    verb="TUNE_SPEED",
                    description="Operator-only: restore print speed to 100 percent",
                    owner_pack=self.pack_id,
                    execute_ref="restore_speed_default",
                    args={},
                    required_locks=[f"{self.DEVICE_ID}.motion"],
                    target_device=self.DEVICE_ID,
                    verify=atom(f"{self.DEVICE_ID}.speed_pct", "==", PrusaDriver.DEFAULT_SPEED_PCT),
                    rank_hint=22,
                )
            )

        flow_shadow_eligible = bool(world.facts.get(f"{self.DEVICE_ID}.flow_shadow_eligible", False))
        nozzle_shadow_eligible = bool(world.facts.get(f"{self.DEVICE_ID}.nozzle_shadow_eligible", False))
        bed_shadow_eligible = bool(world.facts.get(f"{self.DEVICE_ID}.bed_shadow_eligible", False))
        if not include_experimental:
            flow_pct = _float_or_none(world.facts.get(f"{self.DEVICE_ID}.flow_pct"))
            if flow_shadow_eligible and flow_pct is not None and flow_pct > PrusaDriver.FLOW_MIN_PCT:
                down_target = max(PrusaDriver.FLOW_MIN_PCT, flow_pct - PrusaDriver.RATE_STEP_PCT)
                actions.append(
                    LegalAction(
                        action_id="A_PRUSA_TRIM_FLOW_DOWN_SMALL",
                        verb="TUNE_FLOW",
                        description="Reduce flow a little",
                        owner_pack=self.pack_id,
                        execute_ref="trim_flow_down_small",
                        args={"target_flow_pct": round(down_target, 1)},
                        required_locks=[f"{self.DEVICE_ID}.motion"],
                        target_device=self.DEVICE_ID,
                        verify=atom(f"{self.DEVICE_ID}.flow_pct", "==", round(down_target, 1)),
                        rank_hint=25,
                    )
                )
            if flow_shadow_eligible and flow_pct is not None and flow_pct < PrusaDriver.FLOW_MAX_PCT:
                up_target = min(PrusaDriver.FLOW_MAX_PCT, flow_pct + PrusaDriver.RATE_STEP_PCT)
                actions.append(
                    LegalAction(
                        action_id="A_PRUSA_TRIM_FLOW_UP_SMALL",
                        verb="TUNE_FLOW",
                        description="Increase flow a little",
                        owner_pack=self.pack_id,
                        execute_ref="trim_flow_up_small",
                        args={"target_flow_pct": round(up_target, 1)},
                        required_locks=[f"{self.DEVICE_ID}.motion"],
                        target_device=self.DEVICE_ID,
                        verify=atom(f"{self.DEVICE_ID}.flow_pct", "==", round(up_target, 1)),
                        rank_hint=26,
                    )
                )
            nozzle_target = _float_or_none(world.facts.get(f"{self.DEVICE_ID}.nozzle_target_c"))
            min_extrusion_temp = _float_or_none(world.facts.get(f"{self.DEVICE_ID}.min_extrusion_temp_c")) or 170.0
            if nozzle_shadow_eligible and nozzle_target is not None:
                down_target = max(min_extrusion_temp, nozzle_target - PrusaDriver.TEMP_STEP_C)
                up_target = min(self.settings.max_nozzle_target_c, nozzle_target + PrusaDriver.TEMP_STEP_C)
                if down_target < nozzle_target:
                    actions.append(
                        LegalAction(
                            action_id="A_PRUSA_TRIM_NOZZLE_DOWN_SMALL",
                            verb="TUNE_NOZZLE_TEMP",
                            description="Lower nozzle target temperature by 5C",
                            owner_pack=self.pack_id,
                            execute_ref="trim_nozzle_down_small",
                            args={"target_nozzle_c": round(down_target, 1)},
                            required_locks=[f"{self.DEVICE_ID}.motion"],
                            target_device=self.DEVICE_ID,
                            verify=atom(f"{self.DEVICE_ID}.nozzle_target_c", "==", round(down_target, 1)),
                            rank_hint=30,
                        )
                    )
                if up_target > nozzle_target:
                    actions.append(
                        LegalAction(
                            action_id="A_PRUSA_TRIM_NOZZLE_UP_SMALL",
                            verb="TUNE_NOZZLE_TEMP",
                            description="Raise nozzle target temperature by 5C",
                            owner_pack=self.pack_id,
                            execute_ref="trim_nozzle_up_small",
                            args={"target_nozzle_c": round(up_target, 1)},
                            required_locks=[f"{self.DEVICE_ID}.motion"],
                            target_device=self.DEVICE_ID,
                            verify=atom(f"{self.DEVICE_ID}.nozzle_target_c", "==", round(up_target, 1)),
                            rank_hint=31,
                        )
                    )
            bed_target = _float_or_none(world.facts.get(f"{self.DEVICE_ID}.bed_target_c"))
            ideal_bed_target = _float_or_none(world.facts.get(f"{self.DEVICE_ID}.job_bed_target_c_default"))
            if bed_shadow_eligible and bed_target is not None:
                bed_floor = max(0.0, (ideal_bed_target if ideal_bed_target is not None else bed_target) - (3 * PrusaDriver.TEMP_STEP_C))
                bed_ceiling = min(
                    self.settings.max_bed_target_c,
                    (ideal_bed_target if ideal_bed_target is not None else bed_target) + (3 * PrusaDriver.TEMP_STEP_C),
                )
                down_target = max(bed_floor, bed_target - PrusaDriver.TEMP_STEP_C)
                up_target = min(bed_ceiling, bed_target + PrusaDriver.TEMP_STEP_C)
                if down_target < bed_target:
                    actions.append(
                        LegalAction(
                            action_id="A_PRUSA_TRIM_BED_DOWN_SMALL",
                            verb="TUNE_BED_TEMP",
                            description="Lower bed target temperature by 5C",
                            owner_pack=self.pack_id,
                            execute_ref="trim_bed_down_small",
                            args={"target_bed_c": round(down_target, 1)},
                            required_locks=[f"{self.DEVICE_ID}.motion"],
                            target_device=self.DEVICE_ID,
                            verify=atom(f"{self.DEVICE_ID}.bed_target_c", "==", round(down_target, 1)),
                            rank_hint=40,
                        )
                    )
                if up_target > bed_target:
                    actions.append(
                        LegalAction(
                            action_id="A_PRUSA_TRIM_BED_UP_SMALL",
                            verb="TUNE_BED_TEMP",
                            description="Raise bed target temperature by 5C",
                            owner_pack=self.pack_id,
                            execute_ref="trim_bed_up_small",
                            args={"target_bed_c": round(up_target, 1)},
                            required_locks=[f"{self.DEVICE_ID}.motion"],
                            target_device=self.DEVICE_ID,
                            verify=atom(f"{self.DEVICE_ID}.bed_target_c", "==", round(up_target, 1)),
                            rank_hint=41,
                        )
                    )
            return actions

        flow_pct = _float_or_none(world.facts.get(f"{self.DEVICE_ID}.flow_pct"))
        if flow_shadow_eligible and flow_pct is not None and flow_pct > PrusaDriver.FLOW_MIN_PCT:
            down_target = max(PrusaDriver.FLOW_MIN_PCT, flow_pct - PrusaDriver.RATE_STEP_PCT)
            actions.append(
                LegalAction(
                    action_id="A_PRUSA_TRIM_FLOW_DOWN_SMALL",
                    verb="TUNE_FLOW",
                    description="Reduce flow a little",
                    owner_pack=self.pack_id,
                    execute_ref="trim_flow_down_small",
                    args={"target_flow_pct": round(down_target, 1)},
                    required_locks=[f"{self.DEVICE_ID}.motion"],
                    target_device=self.DEVICE_ID,
                    verify=atom(f"{self.DEVICE_ID}.flow_pct", "==", round(down_target, 1)),
                    rank_hint=25,
                )
            )
        if flow_shadow_eligible and flow_pct is not None and flow_pct < PrusaDriver.FLOW_MAX_PCT:
            up_target = min(PrusaDriver.FLOW_MAX_PCT, flow_pct + PrusaDriver.RATE_STEP_PCT)
            actions.append(
                LegalAction(
                    action_id="A_PRUSA_TRIM_FLOW_UP_SMALL",
                    verb="TUNE_FLOW",
                    description="Increase flow a little",
                    owner_pack=self.pack_id,
                    execute_ref="trim_flow_up_small",
                    args={"target_flow_pct": round(up_target, 1)},
                    required_locks=[f"{self.DEVICE_ID}.motion"],
                    target_device=self.DEVICE_ID,
                    verify=atom(f"{self.DEVICE_ID}.flow_pct", "==", round(up_target, 1)),
                    rank_hint=26,
                )
            )
        if include_operator_resets and flow_pct is not None and abs(flow_pct - PrusaDriver.DEFAULT_FLOW_PCT) >= 0.5:
            actions.append(
                LegalAction(
                    action_id="A_PRUSA_OPERATOR_RESTORE_FLOW_DEFAULT",
                    verb="TUNE_FLOW",
                    description="Operator-only: restore flow to 100 percent",
                    owner_pack=self.pack_id,
                    execute_ref="restore_flow_default",
                    args={},
                    required_locks=[f"{self.DEVICE_ID}.motion"],
                    target_device=self.DEVICE_ID,
                    verify=atom(f"{self.DEVICE_ID}.flow_pct", "==", PrusaDriver.DEFAULT_FLOW_PCT),
                    rank_hint=27,
                )
            )

        nozzle_target = _float_or_none(world.facts.get(f"{self.DEVICE_ID}.nozzle_target_c"))
        min_extrusion_temp = _float_or_none(world.facts.get(f"{self.DEVICE_ID}.min_extrusion_temp_c")) or 170.0
        if nozzle_shadow_eligible and nozzle_target is not None:
            down_target = max(min_extrusion_temp, nozzle_target - PrusaDriver.TEMP_STEP_C)
            up_target = min(self.settings.max_nozzle_target_c, nozzle_target + PrusaDriver.TEMP_STEP_C)
            if down_target < nozzle_target:
                actions.append(
                    LegalAction(
                        action_id="A_PRUSA_TRIM_NOZZLE_DOWN_SMALL",
                        verb="TUNE_NOZZLE_TEMP",
                        description="Lower nozzle target temperature by 5C",
                        owner_pack=self.pack_id,
                        execute_ref="trim_nozzle_down_small",
                        args={"target_nozzle_c": round(down_target, 1)},
                        required_locks=[f"{self.DEVICE_ID}.motion"],
                        target_device=self.DEVICE_ID,
                        verify=atom(f"{self.DEVICE_ID}.nozzle_target_c", "==", round(down_target, 1)),
                        rank_hint=30,
                    )
                )
            if up_target > nozzle_target:
                actions.append(
                    LegalAction(
                        action_id="A_PRUSA_TRIM_NOZZLE_UP_SMALL",
                        verb="TUNE_NOZZLE_TEMP",
                        description="Raise nozzle target temperature by 5C",
                        owner_pack=self.pack_id,
                        execute_ref="trim_nozzle_up_small",
                        args={"target_nozzle_c": round(up_target, 1)},
                        required_locks=[f"{self.DEVICE_ID}.motion"],
                        target_device=self.DEVICE_ID,
                        verify=atom(f"{self.DEVICE_ID}.nozzle_target_c", "==", round(up_target, 1)),
                        rank_hint=31,
                    )
                )

        bed_target = _float_or_none(world.facts.get(f"{self.DEVICE_ID}.bed_target_c"))
        ideal_bed_target = _float_or_none(world.facts.get(f"{self.DEVICE_ID}.job_bed_target_c_default"))
        if bed_shadow_eligible and bed_target is not None:
            bed_floor = max(0.0, (ideal_bed_target if ideal_bed_target is not None else bed_target) - (3 * PrusaDriver.TEMP_STEP_C))
            bed_ceiling = min(
                self.settings.max_bed_target_c,
                (ideal_bed_target if ideal_bed_target is not None else bed_target) + (3 * PrusaDriver.TEMP_STEP_C),
            )
            down_target = max(bed_floor, bed_target - PrusaDriver.TEMP_STEP_C)
            up_target = min(bed_ceiling, bed_target + PrusaDriver.TEMP_STEP_C)
            if down_target < bed_target:
                actions.append(
                    LegalAction(
                        action_id="A_PRUSA_TRIM_BED_DOWN_SMALL",
                        verb="TUNE_BED_TEMP",
                        description="Lower bed target temperature by 5C",
                        owner_pack=self.pack_id,
                        execute_ref="trim_bed_down_small",
                        args={"target_bed_c": round(down_target, 1)},
                        required_locks=[f"{self.DEVICE_ID}.motion"],
                        target_device=self.DEVICE_ID,
                        verify=atom(f"{self.DEVICE_ID}.bed_target_c", "==", round(down_target, 1)),
                        rank_hint=40,
                    )
                )
            if up_target > bed_target:
                actions.append(
                    LegalAction(
                        action_id="A_PRUSA_TRIM_BED_UP_SMALL",
                        verb="TUNE_BED_TEMP",
                        description="Raise bed target temperature by 5C",
                        owner_pack=self.pack_id,
                        execute_ref="trim_bed_up_small",
                        args={"target_bed_c": round(up_target, 1)},
                        required_locks=[f"{self.DEVICE_ID}.motion"],
                        target_device=self.DEVICE_ID,
                        verify=atom(f"{self.DEVICE_ID}.bed_target_c", "==", round(up_target, 1)),
                        rank_hint=41,
                    )
                )

        return actions

    def _snapshot_to_raw(self, snapshot: PrusaStatusSnapshot) -> dict[str, Any]:
        return {
            f"{self.DEVICE_ID}.raw.lifecycle": snapshot.lifecycle.value,
            f"{self.DEVICE_ID}.raw.health": snapshot.health,
            f"{self.DEVICE_ID}.raw.job_active": snapshot.job_active,
            f"{self.DEVICE_ID}.raw.job_id": snapshot.job_id,
            f"{self.DEVICE_ID}.raw.job_state": snapshot.job_state,
            f"{self.DEVICE_ID}.raw.job_progress_pct": snapshot.job_progress_pct,
            f"{self.DEVICE_ID}.raw.job_time_printing_s": snapshot.job_time_printing_s,
            f"{self.DEVICE_ID}.raw.current_file": snapshot.current_file,
            f"{self.DEVICE_ID}.raw.speed_pct": snapshot.speed_pct,
            f"{self.DEVICE_ID}.raw.flow_pct": snapshot.flow_pct,
            f"{self.DEVICE_ID}.raw.nozzle_temp_c": snapshot.nozzle_temp_c,
            f"{self.DEVICE_ID}.raw.nozzle_target_c": snapshot.nozzle_target_c,
            f"{self.DEVICE_ID}.raw.bed_temp_c": snapshot.bed_temp_c,
            f"{self.DEVICE_ID}.raw.bed_target_c": snapshot.bed_target_c,
            f"{self.DEVICE_ID}.raw.min_extrusion_temp_c": snapshot.min_extrusion_temp_c,
            f"{self.DEVICE_ID}.raw.nozzle_diameter_mm": snapshot.nozzle_diameter_mm,
            f"{self.DEVICE_ID}.raw.model": snapshot.model,
            f"{self.DEVICE_ID}.raw.serial": snapshot.serial_number,
        }

    def _get_info_cached(self) -> dict[str, Any] | None:
        now = time.monotonic()
        if self._info_cache and (now - self._info_cache.fetched_monotonic) < 60.0:
            return self._info_cache.value
        try:
            value = self.http.get_info()
        except Exception:
            return self._info_cache.value if self._info_cache else None
        self._info_cache = _CachedValue(value=value, fetched_monotonic=now)
        return value

    def _get_files_cached(self) -> list[PrintableFile]:
        now = time.monotonic()
        if self._files_cache and (now - self._files_cache.fetched_monotonic) < 20.0:
            return self._files_cache.value
        try:
            value = self.http.list_usb_files()
        except Exception:
            return self._files_cache.value if self._files_cache else []
        self._files_cache = _CachedValue(value=value, fetched_monotonic=now)
        return value

    def _get_file_info_cached(self, file_path: str) -> dict[str, Any] | None:
        now = time.monotonic()
        cached = self._file_info_cache.get(file_path)
        if cached and (now - cached.fetched_monotonic) < 20.0:
            return cached.value
        try:
            value = self.http.get_file_info(file_path)
        except Exception:
            return cached.value if cached else None
        self._file_info_cache[file_path] = _CachedValue(value=value, fetched_monotonic=now)
        return value

    def _select_notebook_file(
        self,
        *,
        snapshot: PrusaStatusSnapshot,
        requested_file: str | None,
        files: list[PrintableFile],
    ) -> PrintableFile | None:
        if snapshot.current_file:
            current = self._match_requested_file(snapshot.current_file, files)
            if current is not None:
                return current
        if requested_file:
            requested = self._match_requested_file(requested_file, files)
            if requested is not None:
                return requested
        return None

    def _get_job_notebook(self, printable: PrintableFile | None) -> tuple[JobNotebook | None, str]:
        if printable is None:
            return None, "printable_unknown"
        file_info = self._get_file_info_cached(printable.path)
        identity_stub = build_job_notebook(printable=printable, file_info=file_info, file_text=None)
        if identity_stub is None:
            return None, "identity_unavailable"
        cache_key = identity_stub.job_hash
        cached = self._notebook_cache.get(cache_key)
        if cached is not None:
            return cached, "memory_cache"

        notebook_path = None
        notes_path = None
        if self._notebook_dir is not None:
            self._notebook_dir.mkdir(parents=True, exist_ok=True)
            notebook_path = self._notebook_dir / f"{identity_stub.job_hash}.notebook.json"
            notes_path = self._notebook_dir / f"{identity_stub.job_hash}.notes.json"

        persisted_invalid_reason: str | None = None
        if notebook_path is not None and notebook_path.exists():
            try:
                if notes_path is not None and notes_path.exists() and notebook_path.stat().st_mtime < notes_path.stat().st_mtime:
                    raise ValueError("persisted_notebook_older_than_notes")
                persisted = load_persisted_notebook(notebook_path)
                self._validate_loaded_notebook(persisted, printable=printable, expected_job_hash=identity_stub.job_hash)
            except Exception as exc:
                persisted_invalid_reason = f"persisted_invalid:{exc}"
            else:
                self._notebook_cache[cache_key] = persisted
                return persisted, "persisted_reused"

        file_text: str | None = None
        download_failed = False
        decode_failed_reason: str | None = None
        if self.settings.notebook_download_enabled:
            download_path = None
            if isinstance(file_info, dict):
                refs = file_info.get("refs") if isinstance(file_info.get("refs"), dict) else {}
                if isinstance(refs, dict):
                    download_path = _string_or_none(refs.get("download"))
            try:
                raw_bytes = self.http.download_file_bytes(printable.path, download_path=download_path)
                file_text = normalize_prusa_print_text(file_path=printable.path, raw_bytes=raw_bytes)
            except BgcodeDecodeError as exc:
                file_text = None
                download_failed = True
                decode_failed_reason = f"decode_failed:{exc}"
            except Exception:
                file_text = None
                download_failed = True

        notebook = build_job_notebook(
            printable=printable,
            file_info=file_info,
            file_text=file_text,
            notes_dir=self._notebook_dir,
        )
        if notebook is None:
            return None, persisted_invalid_reason or "generation_failed"
        if notebook.job_hash != identity_stub.job_hash:
            return None, "identity_mismatch_after_build"
        if not self._notebook_has_grounding(notebook):
            status = "generation_failed:no_grounding"
            if download_failed:
                status += "|download_failed"
            if decode_failed_reason:
                status += f"|{decode_failed_reason}"
            if persisted_invalid_reason:
                status += f"|{persisted_invalid_reason}"
            return None, status
        if notebook_path is not None:
            persist_job_notebook(notebook_path, notebook)
        self._notebook_cache[cache_key] = notebook
        if persisted_invalid_reason:
            return notebook, f"persisted_rebuilt|{persisted_invalid_reason}"
        if download_failed and file_text is None:
            if decode_failed_reason:
                return None, f"generation_failed|{decode_failed_reason}"
            return notebook, "generated_without_download"
        return notebook, "generated"

    def _validate_loaded_notebook(self, notebook: JobNotebook, *, printable: PrintableFile, expected_job_hash: str) -> None:
        if notebook.job_hash != expected_job_hash:
            raise ValueError(f"job_hash_mismatch expected={expected_job_hash} actual={notebook.job_hash}")
        if notebook.parser_version != CURRENT_NOTEBOOK_PARSER_VERSION:
            raise ValueError(
                f"parser_version_mismatch expected={CURRENT_NOTEBOOK_PARSER_VERSION} actual={notebook.parser_version}"
            )
        expected_path = "/" + printable.path.strip().lstrip("/")
        if (notebook.job.file_path or expected_path) != expected_path:
            raise ValueError(f"file_path_mismatch expected={expected_path} actual={notebook.job.file_path}")
        if notebook.job.file_name != printable.display_name:
            raise ValueError(f"file_name_mismatch expected={printable.display_name} actual={notebook.job.file_name}")

    def _notebook_has_grounding(self, notebook: JobNotebook) -> bool:
        if not notebook.source_text_grounded_internal:
            return False
        if notebook.sections:
            return True
        if notebook.global_notes or notebook.local_notes:
            return True
        if notebook.job.material_profile_name or notebook.job.slicer_name or notebook.job.slicer_profile:
            return True
        if notebook.baselines.nozzle_target_c_default is not None or notebook.baselines.bed_target_c_default is not None:
            return True
        return False

    def _files_from_snapshot(self, snapshot: dict[str, Any]) -> list[PrintableFile]:
        entries = snapshot.get(f"{self.DEVICE_ID}.raw.files") or []
        files: list[PrintableFile] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path = _string_or_none(entry.get("path"))
            display_name = _string_or_none(entry.get("display_name"))
            if not path or not display_name:
                continue
            size = entry.get("size_bytes")
            files.append(
                PrintableFile(
                    path=path,
                    display_name=display_name,
                    size_bytes=int(size) if isinstance(size, (int, float)) else None,
                    modified_ts=int(entry["modified_ts"]) if isinstance(entry.get("modified_ts"), (int, float)) else None,
                )
            )
        return files

    def _match_requested_file(self, requested: str, files: list[PrintableFile]) -> PrintableFile | None:
        requested_norm = requested.strip().lower().lstrip("/")
        for item in files:
            candidates = {
                item.path.lower().lstrip("/"),
                item.display_name.lower(),
                Path(item.path).name.lower(),
            }
            if requested_norm in candidates:
                return item
        return None

    def _compute_safe_to_unload(
        self,
        *,
        part_present: bool,
        lifecycle: str,
        bed_temp_c: float | None,
        nozzle_temp_c: float | None,
        health: str,
    ) -> bool:
        if not part_present:
            return False
        if lifecycle in {PrusaLifecycle.PRINTING.value, PrusaLifecycle.PAUSED.value}:
            return False
        if health in {"OFFLINE", "ERROR"}:
            return False
        if bed_temp_c is None or nozzle_temp_c is None:
            return False
        if bed_temp_c > self.settings.safe_to_unload_bed_c:
            return False
        if nozzle_temp_c > self.settings.safe_to_touch_nozzle_c:
            return False
        return True

    def _infer_part_present(self, snapshot: PrusaStatusSnapshot, whiteboard: BaseWhiteboard) -> bool:
        external = whiteboard.get(f"{self.DEVICE_ID}.part_present")
        current_file = snapshot.current_file
        if current_file:
            self._last_job_token = current_file

        if snapshot.lifecycle in {PrusaLifecycle.PRINTING, PrusaLifecycle.PAUSED}:
            self._part_present_inference = True
            return True

        if isinstance(external, bool):
            self._part_present_inference = external
            if not external:
                self._last_job_token = None
            return self._part_present_inference

        if snapshot.lifecycle in {PrusaLifecycle.FINISHED, PrusaLifecycle.ATTENTION, PrusaLifecycle.STOPPED} and self._last_job_token:
            self._part_present_inference = True
        return self._part_present_inference

    def _derive_printing_phase(
        self,
        *,
        lifecycle: str,
        job_active: bool,
        job_id: int | None,
        job_progress_pct: float | None,
        current_file: str | None,
        job_time_printing_s: float | None,
    ) -> str:
        identity = self._job_identity(job_id=job_id, current_file=current_file)
        if lifecycle != PrusaLifecycle.PRINTING.value or not job_active:
            self._last_printing_job_identity = None
            self._last_job_time_printing_s = None
            self._active_printing_job_identity = None
            return "not_printing"
        if self._active_printing_job_identity is not None and self._active_printing_job_identity != identity:
            self._active_printing_job_identity = None
        if job_progress_pct is not None and job_progress_pct > 0.0:
            self._last_printing_job_identity = identity
            self._last_job_time_printing_s = job_time_printing_s
            self._active_printing_job_identity = identity
            return "active_printing"
        if self._job_time_printing_is_advancing(
            job_id=job_id,
            current_file=current_file,
            job_time_printing_s=job_time_printing_s,
        ):
            self._active_printing_job_identity = identity
            return "active_printing"
        if identity is not None and self._active_printing_job_identity == identity:
            return "active_printing"
        return "startup_printing"

    def _job_identity(self, *, job_id: int | None, current_file: str | None) -> str | None:
        if current_file:
            return f"file:{current_file}"
        if job_id is not None:
            return f"job:{job_id}"
        return None

    def _job_time_printing_is_advancing(
        self,
        *,
        job_id: int | None,
        current_file: str | None,
        job_time_printing_s: float | None,
    ) -> bool:
        identity = self._job_identity(job_id=job_id, current_file=current_file)
        previous_identity = self._last_printing_job_identity
        previous_time = self._last_job_time_printing_s

        self._last_printing_job_identity = identity
        self._last_job_time_printing_s = job_time_printing_s

        if identity is None or job_time_printing_s is None:
            return False
        if previous_identity != identity:
            return False
        if previous_time is None:
            return False
        return job_time_printing_s > previous_time

    def _derive_nozzle_shadow_state(
        self,
        *,
        lifecycle: str,
        job_active: bool,
        printing_phase: str,
        live_tuning_available: bool,
        job_progress_pct: float | None,
        nozzle_target_c: float | None,
        min_extrusion_temp_c: float | None,
        late_tuning_symptom_active: bool,
    ) -> dict[str, Any]:
        blockers: list[str] = []
        actions: list[str] = []

        def add_blocker(reason: str) -> None:
            if reason not in blockers:
                blockers.append(reason)

        if lifecycle != PrusaLifecycle.PRINTING.value or not job_active:
            add_blocker("not_printing")
        if printing_phase != "active_printing":
            add_blocker(printing_phase)
        if not live_tuning_available:
            add_blocker("live_tuning_unavailable")
        progress_ready, progress_reason = self._bounded_progress_ready(
            job_progress_pct,
            family="nozzle",
            late_tuning_symptom_active=late_tuning_symptom_active,
        )
        if not progress_ready and progress_reason is not None:
            add_blocker(progress_reason)
        if nozzle_target_c is None:
            add_blocker("nozzle_target_missing")
        if min_extrusion_temp_c is None:
            add_blocker("min_extrusion_temp_missing")

        if not blockers and nozzle_target_c is not None and min_extrusion_temp_c is not None:
            if max(min_extrusion_temp_c, nozzle_target_c - PrusaDriver.TEMP_STEP_C) < nozzle_target_c:
                actions.append("A_PRUSA_TRIM_NOZZLE_DOWN_SMALL")
            if min(self.settings.max_nozzle_target_c, nozzle_target_c + PrusaDriver.TEMP_STEP_C) > nozzle_target_c:
                actions.append("A_PRUSA_TRIM_NOZZLE_UP_SMALL")
            if not actions:
                add_blocker("no_bounded_nozzle_step")

        return {
            "eligible": not blockers,
            "blockers_text": "|".join(blockers) if blockers else None,
            "actions_text": "|".join(actions) if actions else None,
        }

    def _derive_flow_shadow_state(
        self,
        *,
        lifecycle: str,
        job_active: bool,
        printing_phase: str,
        live_tuning_available: bool,
        flow_tuning_verification_enabled: bool,
        job_progress_pct: float | None,
        flow_pct: float | None,
        late_tuning_symptom_active: bool,
    ) -> dict[str, Any]:
        blockers: list[str] = []
        actions: list[str] = []

        def add_blocker(reason: str) -> None:
            if reason not in blockers:
                blockers.append(reason)

        if lifecycle != PrusaLifecycle.PRINTING.value or not job_active:
            add_blocker("not_printing")
        if printing_phase != "active_printing":
            add_blocker(printing_phase)
        if not live_tuning_available:
            add_blocker("live_tuning_unavailable")
        if not flow_tuning_verification_enabled:
            add_blocker("flow_verification_unavailable")
        progress_ready, progress_reason = self._bounded_progress_ready(
            job_progress_pct,
            family="flow",
            late_tuning_symptom_active=late_tuning_symptom_active,
        )
        if not progress_ready and progress_reason is not None:
            add_blocker(progress_reason)
        if flow_pct is None:
            add_blocker("flow_pct_missing")

        if not blockers and flow_pct is not None:
            if flow_pct > PrusaDriver.FLOW_MIN_PCT:
                actions.append("A_PRUSA_TRIM_FLOW_DOWN_SMALL")
            if flow_pct < PrusaDriver.FLOW_MAX_PCT:
                actions.append("A_PRUSA_TRIM_FLOW_UP_SMALL")
            if abs(flow_pct - PrusaDriver.DEFAULT_FLOW_PCT) >= 0.5:
                actions.append("A_PRUSA_OPERATOR_RESTORE_FLOW_DEFAULT")
            if not actions:
                add_blocker("no_bounded_flow_step")

        return {
            "eligible": not blockers,
            "blockers_text": "|".join(blockers) if blockers else None,
            "actions_text": "|".join(actions) if actions else None,
        }

    def _derive_bed_shadow_state(
        self,
        *,
        lifecycle: str,
        job_active: bool,
        printing_phase: str,
        live_tuning_available: bool,
        job_progress_pct: float | None,
        bed_target_c: float | None,
        ideal_bed_target_c: float | None,
    ) -> dict[str, Any]:
        blockers: list[str] = []
        actions: list[str] = []

        def add_blocker(reason: str) -> None:
            if reason not in blockers:
                blockers.append(reason)

        if lifecycle != PrusaLifecycle.PRINTING.value or not job_active:
            add_blocker("not_printing")
        if printing_phase != "active_printing":
            add_blocker(printing_phase)
        if not live_tuning_available:
            add_blocker("live_tuning_unavailable")
        progress_ready, progress_reason = self._bounded_progress_ready(job_progress_pct, family="bed")
        if not progress_ready and progress_reason is not None:
            add_blocker(progress_reason)
        if bed_target_c is None:
            add_blocker("bed_target_missing")

        if not blockers and bed_target_c is not None:
            ideal_target = ideal_bed_target_c if ideal_bed_target_c is not None else bed_target_c
            down_target = max(0.0, ideal_target - (3 * PrusaDriver.TEMP_STEP_C))
            up_target = min(self.settings.max_bed_target_c, ideal_target + (3 * PrusaDriver.TEMP_STEP_C))
            next_down_target = max(down_target, bed_target_c - PrusaDriver.TEMP_STEP_C)
            next_up_target = min(up_target, bed_target_c + PrusaDriver.TEMP_STEP_C)
            if next_down_target < bed_target_c:
                actions.append("A_PRUSA_TRIM_BED_DOWN_SMALL")
            if next_up_target > bed_target_c:
                actions.append("A_PRUSA_TRIM_BED_UP_SMALL")
            if not actions:
                add_blocker("no_bounded_bed_step")

        return {
            "eligible": not blockers,
            "blockers_text": "|".join(blockers) if blockers else None,
            "actions_text": "|".join(actions) if actions else None,
        }

    def _derive_speed_autonomy_state(
        self,
        *,
        lifecycle: str,
        job_active: bool,
        printing_phase: str,
        live_tuning_available: bool,
        speed_tuning_verification_enabled: bool,
        speed_pct: float | None,
        job_progress_pct: float | None,
        nozzle_temp_c: float | None,
        nozzle_target_c: float | None,
        bed_temp_c: float | None,
        bed_target_c: float | None,
        late_tuning_symptom_active: bool,
    ) -> dict[str, Any]:
        blockers: list[str] = []

        def add_blocker(reason: str) -> None:
            if reason not in blockers:
                blockers.append(reason)

        if lifecycle != PrusaLifecycle.PRINTING.value or not job_active:
            add_blocker("not_printing")
        if printing_phase != "active_printing":
            add_blocker(printing_phase)
        if not live_tuning_available:
            add_blocker("live_tuning_unavailable")
        if not speed_tuning_verification_enabled:
            add_blocker("speed_verification_unavailable")
        if speed_pct is None:
            add_blocker("speed_pct_missing")
        progress_ready, progress_reason = self._bounded_progress_ready(
            job_progress_pct,
            family="speed",
            late_tuning_symptom_active=late_tuning_symptom_active,
        )
        if not progress_ready and progress_reason is not None:
            add_blocker(progress_reason)

        return {
            "boundary_text": "active_printing",
            "eligible": not blockers,
            "blockers_text": "|".join(blockers) if blockers else None,
        }

    def _bounded_progress_ready(
        self,
        job_progress_pct: float | None,
        *,
        family: str,
        late_tuning_symptom_active: bool = False,
    ) -> tuple[bool, str | None]:
        if job_progress_pct is None or job_progress_pct < self.settings.live_tuning_min_progress_pct:
            return False, f"{family}_progress_not_ready"
        max_progress_pct = self.settings.live_tuning_max_progress_pct
        if job_progress_pct >= max_progress_pct:
            return False, "print_nearly_finished"
        return True, None

    def _late_tuning_symptom_active(
        self,
        *,
        finding_types_text: str | None,
        issue_level: str | None,
        printing_phase: str,
    ) -> bool:
        if printing_phase != "active_printing":
            return False
        finding_types = set(_compact_blocker_list(finding_types_text))
        if "stringing" in finding_types or "spaghetti" in finding_types:
            return True
        return "residue" in finding_types and issue_level in {"medium", "high"}


def _file_action_id(requested_file: str) -> str:
    slug = Path(requested_file).name.upper()
    slug = "".join(ch if ch.isalnum() else "_" for ch in slug)
    slug = slug[:18] if slug else "FILE"
    return f"A_PRUSA_START_{slug}"


def _artifact_slug(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "unknown"


def _max_evidence_strength(findings: list[Any]) -> str | None:
    best_rank = -1
    best_strength: str | None = None
    for item in findings:
        strength = _string_or_none(getattr(item, "evidence_strength", None))
        rank = _strength_rank(strength)
        if rank > best_rank:
            best_rank = rank
            best_strength = strength
    return best_strength


def _vision_issue_level(finding_types: tuple[str, ...] | list[str], strength: str | None) -> str | None:
    findings = {str(item).strip() for item in finding_types if str(item).strip()}
    if not findings:
        return None
    if "spaghetti" in findings or "stringing" in findings or "blob" in findings:
        return "high"
    if "residue" in findings:
        return "high" if strength == "strong" else "medium"
    return "low"


def _strength_rank(value: str | None) -> int:
    return {
        None: -1,
        "weak": 0,
        "moderate": 1,
        "strong": 2,
    }.get(value, -1)


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _compact_blocker_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [part for part in text.split("|") if part]
