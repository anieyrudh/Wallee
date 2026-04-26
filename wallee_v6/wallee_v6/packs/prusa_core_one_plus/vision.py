"""Observe-only nozzle vision helpers for the Prusa CORE One/+ pack."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any, Literal
from urllib import error, request

from pydantic import BaseModel, ConfigDict, Field
from PIL import Image, UnidentifiedImageError

from ...openrouter_observability import normalize_openrouter_metadata, utc_now_iso


FindingType = Literal[
    "residue",
    "stringing",
    "spaghetti",
    "blob",
    "unknown",
]
EvidenceStrength = Literal["weak", "moderate", "strong"]
HealthState = Literal["nominal", "degraded", "unusable"]
ComparisonDelta = Literal["better", "same", "worse", "unknown"]

_DEFAULT_DISCOVERY_PORTS = ("8080", "8083", "8084", "8082", "8085", "8090")
_DISCOVERY_TTL_S = 60.0
_DISCOVERY_CACHE: dict[tuple[str, tuple[str, ...]], tuple[str | None, float]] = {}
_SNAPSHOT_STALE_PROBE_DELAY_S = 1.0
_ACTIVE_PRINT_BURST_FRAME_COUNT = 3
_ACTIVE_PRINT_SETTLE_DELAY_S = 0.25
_ACTIVE_PRINT_BURST_FRAME_GAP_S = 0.12
# Active-print nozzle frames are naturally darker and softer than idle probes.
# These gates should reject obvious travel-blur and blown-out frames without
# suppressing most usable in-print captures.
_ACTIVE_PRINT_BLUR_MIN_SCORE = 2.0
_ACTIVE_PRINT_MEAN_LUMA_MIN = 20.0
_ACTIVE_PRINT_MEAN_LUMA_MAX = 245.0
_ACTIVE_PRINT_BRIGHT_RATIO_MAX = 0.25
_ACTIVE_PRINT_DARK_RATIO_MAX = 0.85
_NO_SIGNAL_AHASH = int("0000003c3c000000", 16)
_NO_SIGNAL_DHASH = int("0000000606000000", 16)
_NO_SIGNAL_AHASH_MAX_DISTANCE = 6
_NO_SIGNAL_DHASH_MAX_DISTANCE = 6
_NO_SIGNAL_DARK_RATIO_MIN = 0.90

NOZZLE_CAM_BLOCKER_DEVICE_MISSING = "nozzle_cam_device_missing"
NOZZLE_CAM_BLOCKER_SERVICE_DOWN = "nozzle_cam_service_down"
NOZZLE_CAM_BLOCKER_CAPTURE_FAILED = "nozzle_cam_capture_failed"
NOZZLE_CAM_BLOCKER_FRAME_STALE = "nozzle_cam_frame_stale"
NOZZLE_CAM_BLOCKER_FRAME_INVALID = "nozzle_cam_frame_invalid"
NOZZLE_CAM_BLOCKER_NO_SIGNAL = "nozzle_cam_no_signal"
NOZZLE_CAM_BLOCKER_FRAME_FROZEN = "nozzle_cam_frame_frozen"
NOZZLE_CAM_BLOCKER_UNUSABLE = "nozzle_cam_unusable"


class VisionError(RuntimeError):
    """Raised when observe-only vision capture or analysis fails."""


class VisionFinding(BaseModel):
    """Persisted v1 finding."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str
    finding_type: FindingType
    descriptors: list[str] = Field(default_factory=list)
    evidence_strength: EvidenceStrength
    note: str


class VisionObservation(BaseModel):
    """Persisted v1 observation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    frame_id: str
    camera_id: str
    captured_at: str
    image_ref: str
    reference_image_ref: str | None = None
    findings: list[VisionFinding]
    summary: str
    comparison_delta: ComparisonDelta | None = None
    comparison_confidence: EvidenceStrength | None = None
    comparison_summary: str | None = None


class NozzleCameraHealthReport(BaseModel):
    """Deterministic nozzle-camera health facts for runtime and tooling."""

    model_config = ConfigDict(extra="forbid")

    nozzle_cam_device_present: bool
    nozzle_cam_service_ok: bool
    nozzle_cam_capture_ok: bool
    nozzle_cam_frame_fresh: bool
    nozzle_cam_frame_valid: bool
    nozzle_cam_frame_not_signal_slate: bool
    nozzle_cam_frame_live: bool
    nozzle_cam_usable: bool
    nozzle_cam_health_state: HealthState
    nozzle_cam_blockers: list[str] = Field(default_factory=list)
    nozzle_cam_last_capture_at: str | None = None
    nozzle_cam_frame_age_s: float | None = None
    nozzle_cam_last_frame_ref: str | None = None
    nozzle_cam_repeated_identical_count: int = 0
    frame_refs_used: list[str] = Field(default_factory=list)


class _VisionFindingDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_type: FindingType
    descriptors: list[str] = Field(default_factory=list)
    evidence_strength: EvidenceStrength
    note: str


class _VisionAnalysisDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[_VisionFindingDraft] = Field(default_factory=list)
    summary: str
    comparison_delta: ComparisonDelta | None = None
    comparison_confidence: EvidenceStrength | None = None
    comparison_summary: str | None = None


@dataclass(frozen=True)
class CapturedNozzleFrame:
    """One in-memory nozzle-camera frame."""

    camera_id: str
    captured_at: str
    jpeg_bytes: bytes


@dataclass(frozen=True)
class PersistedFrame:
    """One persisted nozzle-camera frame."""

    frame_id: str
    camera_id: str
    captured_at: str
    frame_path: Path
    image_ref: str
    artifact_stem: str
    jpeg_bytes: bytes


@dataclass(frozen=True)
class VisionAdvisoryResult:
    """Persisted vision artifacts plus the compact advisory fields used at runtime."""

    observation: VisionObservation
    observation_path: Path
    frame: PersistedFrame
    summary: str
    finding_types: tuple[str, ...]
    replay_path: Path | None = None


@dataclass(frozen=True)
class VisionAnalysisTrace:
    """Normalized provider metadata and request/response trace for one vision call."""

    provider_metadata: dict[str, Any]
    request_payload: dict[str, Any]
    response_payload: dict[str, Any]
    response_text: str
    prompt_text: str = ""


@dataclass(frozen=True)
class HealthProbeFrame:
    """One deterministic probe frame used for health checks."""

    captured_at: str
    jpeg_bytes: bytes
    sha256: str
    frame_path: Path | None = None
    frame_ref: str | None = None


@dataclass(frozen=True)
class NozzleCameraHealthResult:
    """Health report plus any probe frames used to derive it."""

    report: NozzleCameraHealthReport
    frames: tuple[HealthProbeFrame, ...] = ()


@dataclass(frozen=True)
class _FrameAnalysis:
    valid: bool
    width: int
    height: int
    dark_ratio: float
    ahash: int
    dhash: int
    bright_ratio: float = 0.0
    mean_luma: float = 0.0
    blur_score: float = 0.0


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _vision_root(output_root: str | Path | None = None) -> Path:
    if output_root is None:
        return _repo_root() / "docs" / "evidence" / "prusa_core_one_plus" / "vision"
    return Path(output_root).resolve()


def _vision_replay_dir(output_root: str | Path | None = None) -> Path:
    path = _vision_root(output_root) / "replays"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _utc_now_iso() -> str:
    return utc_now_iso()


def _timestamp_token(captured_at: str) -> str:
    dt = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    return dt.strftime("%Y-%m-%dT%H%M%SZ")


def _collapse_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _short_sentence(value: Any, *, fallback: str, limit: int = 160) -> str:
    text = _collapse_text(value)
    if not text:
        return fallback
    for marker in (". ", "! ", "? "):
        if marker in text:
            text = text.split(marker, 1)[0].strip()
            break
    text = text.rstrip(".!?")
    if not text:
        return fallback
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def _clean_descriptors(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    cleaned: list[str] = []
    for raw in values:
        text = _collapse_text(raw).lower().replace(" ", "_")
        if not text or text in cleaned:
            continue
        cleaned.append(text[:48])
    return cleaned


def _candidate_nozzle_ports(settings) -> tuple[str, ...]:
    ports: list[str] = []
    if settings.nozzle_camera_port:
        ports.append(str(settings.nozzle_camera_port))
    for port in settings.nozzle_camera_discovery_ports or _DEFAULT_DISCOVERY_PORTS:
        text = str(port).strip()
        if text and text not in ports:
            ports.append(text)
    return tuple(ports)


def _http_read(url: str, *, timeout_s: float, accept: str = "image/jpeg") -> tuple[bytes, str]:
    req = request.Request(url, headers={"Accept": accept}, method="GET")
    with request.urlopen(req, timeout=timeout_s) as response:
        body = response.read()
        return body, str(response.headers.get("Content-Type", ""))


def _looks_like_jpeg(data: bytes) -> bool:
    return len(data) >= 100 and data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")


def discover_nozzle_camera_port(settings, *, force: bool = False) -> str | None:
    """Return the first responsive nozzle-camera port, if any."""

    key = (settings.nozzle_camera_host, _candidate_nozzle_ports(settings))
    now = time.monotonic()
    cached = _DISCOVERY_CACHE.get(key)
    if cached is not None and not force and (now - cached[1]) < _DISCOVERY_TTL_S:
        return cached[0]

    discovered: str | None = None
    for port in key[1]:
        base_url = f"http://{settings.nozzle_camera_host}:{port}"
        try:
            body, content_type = _http_read(f"{base_url}/snapshot", timeout_s=1.5)
        except Exception:
            continue
        if _looks_like_jpeg(body) or "image/jpeg" in content_type.lower():
            discovered = port
            break

    _DISCOVERY_CACHE[key] = (discovered, now)
    return discovered


def capture_nozzle_frame(
    settings,
    *,
    timeout_s: float = 5.0,
    active_printing: bool = False,
) -> CapturedNozzleFrame:
    """Capture one nozzle-camera frame or raise a loud error."""

    port = discover_nozzle_camera_port(settings)
    if not port:
        port = discover_nozzle_camera_port(settings, force=True)
    if not port:
        port = settings.nozzle_camera_port
    if not port:
        raise VisionError(
            "Nozzle camera unavailable: no responsive port found. "
            f"Checked host={settings.nozzle_camera_host} ports={list(_candidate_nozzle_ports(settings))}"
        )

    base_url = f"http://{settings.nozzle_camera_host}:{port}"
    snapshot_error: str | None = None
    if active_printing:
        burst_bytes = _capture_active_print_stream_burst_frame(base_url, timeout_s=timeout_s)
        if burst_bytes is not None:
            return CapturedNozzleFrame(
                camera_id="camera-nozzle",
                captured_at=_utc_now_iso(),
                jpeg_bytes=burst_bytes,
            )
        raise VisionError(
            "Active-print nozzle capture produced no acceptable stable frame. "
            f"Rejected burst frames from {base_url}/stream."
        )

    try:
        snapshot_bytes = _capture_snapshot_frame(base_url, timeout_s=timeout_s)
        if snapshot_bytes is not None:
            snapshot_bytes = _prefer_live_stream_if_snapshot_stale(
                base_url,
                snapshot_bytes,
                timeout_s=timeout_s,
            )
            return CapturedNozzleFrame(
                camera_id="camera-nozzle",
                captured_at=_utc_now_iso(),
                jpeg_bytes=snapshot_bytes,
            )
        snapshot_error = f"/snapshot returned non-JPEG payload on {base_url}"
    except Exception as exc:
        snapshot_error = f"/snapshot failed on {base_url}: {exc}"

    stream_bytes = _capture_stream_frame(base_url)
    if stream_bytes is not None:
        return CapturedNozzleFrame(
            camera_id="camera-nozzle",
            captured_at=_utc_now_iso(),
            jpeg_bytes=stream_bytes,
        )

    raise VisionError(
        "Nozzle camera capture failed after /snapshot and /stream attempts. "
        f"Last snapshot error: {snapshot_error}"
    )


def _capture_snapshot_frame(base_url: str, *, timeout_s: float) -> bytes | None:
    snapshot_bytes, _content_type = _http_read(f"{base_url}/snapshot", timeout_s=timeout_s)
    return snapshot_bytes if _looks_like_jpeg(snapshot_bytes) else None


def _prefer_live_stream_if_snapshot_stale(base_url: str, snapshot_bytes: bytes, *, timeout_s: float) -> bytes:
    """Prefer a live /stream frame when /snapshot appears frozen."""

    try:
        time.sleep(min(_SNAPSHOT_STALE_PROBE_DELAY_S, max(timeout_s / 4.0, 0.0)))
    except Exception:
        return snapshot_bytes

    later_snapshot: bytes | None
    try:
        later_snapshot = _capture_snapshot_frame(base_url, timeout_s=min(timeout_s, 2.0))
    except Exception:
        later_snapshot = None

    if later_snapshot is not None and later_snapshot != snapshot_bytes:
        return snapshot_bytes

    stream_bytes = _capture_stream_frame(base_url, timeout_s=timeout_s)
    if stream_bytes is not None and stream_bytes != snapshot_bytes:
        return stream_bytes
    return snapshot_bytes


def _capture_stream_frame(base_url: str, *, timeout_s: float = 5.0) -> bytes | None:
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
            tmp_path = handle.name
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                f"{base_url}/stream",
                "-vframes",
                "1",
                "-q:v",
                "3",
                "-f",
                "image2",
                tmp_path,
            ],
            capture_output=True,
            timeout=timeout_s,
        )
        if result.returncode != 0:
            return None
        data = Path(tmp_path).read_bytes()
        return data if _looks_like_jpeg(data) else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


def _capture_active_print_stream_burst_frame(base_url: str, *, timeout_s: float) -> bytes | None:
    candidates: list[bytes] = []
    per_frame_timeout = max(1.0, timeout_s / max(_ACTIVE_PRINT_BURST_FRAME_COUNT, 1))
    time.sleep(_ACTIVE_PRINT_SETTLE_DELAY_S)
    for index in range(_ACTIVE_PRINT_BURST_FRAME_COUNT):
        frame = _capture_stream_frame(base_url, timeout_s=per_frame_timeout)
        if frame is not None:
            analysis = _analyze_frame_bytes(frame)
            if _frame_is_acceptable_for_active_print(analysis):
                candidates.append(frame)
        if index + 1 < _ACTIVE_PRINT_BURST_FRAME_COUNT:
            time.sleep(_ACTIVE_PRINT_BURST_FRAME_GAP_S)
    if not candidates:
        return None
    return candidates[-1]


def artifact_stem(*, captured_at: str, job_identity: str, camera_id: str) -> str:
    return f"{_timestamp_token(captured_at)}-nozzle-{job_identity}-{camera_id}"


def persist_frame(
    frame: CapturedNozzleFrame,
    *,
    output_root: str | Path | None = None,
    job_identity: str,
) -> PersistedFrame:
    """Persist one captured frame and return its artifact metadata."""

    root = _vision_root(output_root)
    frames_dir = root / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    stem = artifact_stem(captured_at=frame.captured_at, job_identity=job_identity, camera_id=frame.camera_id)
    frame_path = frames_dir / f"{stem}.jpg"
    frame_path.write_bytes(frame.jpeg_bytes)
    return PersistedFrame(
        frame_id=stem,
        camera_id=frame.camera_id,
        captured_at=frame.captured_at,
        frame_path=frame_path,
        image_ref=_artifact_ref(frame_path),
        artifact_stem=stem,
        jpeg_bytes=frame.jpeg_bytes,
    )


def persist_observation(
    observation: VisionObservation,
    *,
    output_root: str | Path | None = None,
) -> Path:
    """Persist one observation JSON artifact."""

    root = _vision_root(output_root)
    observations_dir = root / "observations"
    observations_dir.mkdir(parents=True, exist_ok=True)
    observation_path = observations_dir / f"{observation.frame_id}.json"
    payload = observation.model_dump(mode="json", exclude_none=True)
    observation_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return observation_path


def observe_nozzle_advisory(
    settings,
    *,
    output_root: str | Path | None,
    job_identity: str,
    capture_mode: str,
    lifecycle: str | None,
) -> VisionAdvisoryResult:
    """Capture, persist, analyze, and summarize one bounded nozzle-camera advisory."""

    captured = capture_nozzle_frame(settings, active_printing=(capture_mode == "active-print"))
    persisted = persist_frame(captured, output_root=output_root, job_identity=job_identity)
    previous_frame = latest_previous_persisted_frame(
        output_root=output_root,
        job_identity=job_identity,
        camera_id=persisted.camera_id,
        before_frame_id=persisted.frame_id,
    )
    observation, trace = analyze_persisted_frame_with_trace(
        persisted,
        settings=settings,
        capture_mode=capture_mode,
        lifecycle=lifecycle,
        previous_frame=previous_frame,
    )
    observation_path = persist_observation(observation, output_root=output_root)
    replay_path = persist_vision_replay_bundle(
        frame=persisted,
        trace=trace,
        output_root=output_root,
        previous_frame=previous_frame,
    )
    return VisionAdvisoryResult(
        observation=observation,
        observation_path=observation_path,
        frame=persisted,
        summary=advisory_summary(observation),
        finding_types=tuple(item.finding_type for item in observation.findings),
        replay_path=replay_path,
    )


def evaluate_nozzle_camera_health(
    settings,
    *,
    active_printing: bool,
) -> NozzleCameraHealthResult:
    """Return deterministic nozzle-camera health plus the probe frames used."""

    blockers: list[str] = []
    device_present = Path(settings.nozzle_camera_device_path).exists()
    if not device_present:
        blockers.append(NOZZLE_CAM_BLOCKER_DEVICE_MISSING)

    port = discover_nozzle_camera_port(settings, force=True) or settings.nozzle_camera_port
    base_url = f"http://{settings.nozzle_camera_host}:{port}" if port else None
    service_ok = _service_ok(base_url) if base_url else False
    capture_ok = False
    frame_fresh = False
    frame_valid = False
    frame_not_signal_slate = False
    frame_live = False
    repeated_identical_count = 0
    frames: list[HealthProbeFrame] = []
    last_capture_at: str | None = None
    frame_age_s: float | None = None
    last_frame_ref: str | None = None

    if not service_ok:
        blockers.append(NOZZLE_CAM_BLOCKER_SERVICE_DOWN)

    probe_count = settings.nozzle_camera_liveness_window_size if active_printing else 1
    if base_url is not None:
        for _index in range(max(1, probe_count)):
            try:
                captured = capture_nozzle_frame(settings, active_printing=active_printing)
            except Exception:
                break
            probe = HealthProbeFrame(
                captured_at=captured.captured_at,
                jpeg_bytes=captured.jpeg_bytes,
                sha256=hashlib.sha256(captured.jpeg_bytes).hexdigest(),
                frame_ref=f"sha256:{hashlib.sha256(captured.jpeg_bytes).hexdigest()[:16]}",
            )
            frames.append(probe)

    if frames:
        capture_ok = True
        service_ok = True
        last_capture_at = frames[-1].captured_at
        frame_age_s = _frame_age_s(last_capture_at)
        frame_fresh = frame_age_s <= float(settings.nozzle_camera_max_frame_age_s)
        analysis = _analyze_frame_bytes(frames[-1].jpeg_bytes)
        frame_valid = analysis.valid
        frame_not_signal_slate = frame_valid and not _looks_like_no_signal_slate(analysis)
        last_frame_ref = frames[-1].frame_ref
        if active_printing and len(frames) >= settings.nozzle_camera_liveness_window_size:
            hashes = [frame.sha256 for frame in frames]
            if len(set(hashes)) == 1:
                repeated_identical_count = len(hashes)
        frame_live = capture_ok and (not active_printing or repeated_identical_count < settings.nozzle_camera_liveness_window_size)
    else:
        blockers.append(NOZZLE_CAM_BLOCKER_CAPTURE_FAILED)

    if capture_ok and not frame_fresh:
        blockers.append(NOZZLE_CAM_BLOCKER_FRAME_STALE)
    if capture_ok and not frame_valid:
        blockers.append(NOZZLE_CAM_BLOCKER_FRAME_INVALID)
    if capture_ok and frame_valid and not frame_not_signal_slate:
        blockers.append(NOZZLE_CAM_BLOCKER_NO_SIGNAL)
    if capture_ok and not frame_live:
        blockers.append(NOZZLE_CAM_BLOCKER_FRAME_FROZEN)

    usable = (
        device_present
        and service_ok
        and capture_ok
        and frame_fresh
        and frame_valid
        and frame_not_signal_slate
        and frame_live
    )
    if not usable:
        blockers.append(NOZZLE_CAM_BLOCKER_UNUSABLE)

    fatal_blockers = {
        NOZZLE_CAM_BLOCKER_DEVICE_MISSING,
        NOZZLE_CAM_BLOCKER_SERVICE_DOWN,
        NOZZLE_CAM_BLOCKER_CAPTURE_FAILED,
        NOZZLE_CAM_BLOCKER_FRAME_INVALID,
        NOZZLE_CAM_BLOCKER_NO_SIGNAL,
    }
    if usable:
        health_state: HealthState = "nominal"
    elif any(blocker in fatal_blockers for blocker in blockers):
        health_state = "unusable"
    else:
        health_state = "degraded"

    report = NozzleCameraHealthReport(
        nozzle_cam_device_present=device_present,
        nozzle_cam_service_ok=service_ok,
        nozzle_cam_capture_ok=capture_ok,
        nozzle_cam_frame_fresh=frame_fresh,
        nozzle_cam_frame_valid=frame_valid,
        nozzle_cam_frame_not_signal_slate=frame_not_signal_slate,
        nozzle_cam_frame_live=frame_live,
        nozzle_cam_usable=usable,
        nozzle_cam_health_state=health_state,
        nozzle_cam_blockers=_unique_blockers(blockers),
        nozzle_cam_last_capture_at=last_capture_at,
        nozzle_cam_frame_age_s=round(frame_age_s, 3) if frame_age_s is not None else None,
        nozzle_cam_last_frame_ref=last_frame_ref,
        nozzle_cam_repeated_identical_count=repeated_identical_count,
        frame_refs_used=[frame.frame_ref for frame in frames if frame.frame_ref],
    )
    return NozzleCameraHealthResult(report=report, frames=tuple(frames))


def nozzle_camera_fact_map(report: NozzleCameraHealthReport, *, prefix: str) -> dict[str, Any]:
    """Return the exact prefixed nozzle-camera facts for the pack/runtime."""

    return {
        f"{prefix}.nozzle_cam_device_present": report.nozzle_cam_device_present,
        f"{prefix}.nozzle_cam_service_ok": report.nozzle_cam_service_ok,
        f"{prefix}.nozzle_cam_capture_ok": report.nozzle_cam_capture_ok,
        f"{prefix}.nozzle_cam_frame_fresh": report.nozzle_cam_frame_fresh,
        f"{prefix}.nozzle_cam_frame_valid": report.nozzle_cam_frame_valid,
        f"{prefix}.nozzle_cam_frame_not_signal_slate": report.nozzle_cam_frame_not_signal_slate,
        f"{prefix}.nozzle_cam_frame_live": report.nozzle_cam_frame_live,
        f"{prefix}.nozzle_cam_usable": report.nozzle_cam_usable,
        f"{prefix}.nozzle_cam_health_state": report.nozzle_cam_health_state,
        f"{prefix}.nozzle_cam_blockers": list(report.nozzle_cam_blockers),
        f"{prefix}.nozzle_cam_last_capture_at": report.nozzle_cam_last_capture_at,
        f"{prefix}.nozzle_cam_frame_age_s": report.nozzle_cam_frame_age_s,
        f"{prefix}.nozzle_cam_last_frame_ref": report.nozzle_cam_last_frame_ref,
        f"{prefix}.nozzle_cam_repeated_identical_count": report.nozzle_cam_repeated_identical_count,
    }


def persist_nozzle_camera_health_artifact(
    result: NozzleCameraHealthResult,
    *,
    output_root: str | Path | None = None,
    prefix: str,
    job_identity: dict[str, Any] | None = None,
    status: dict[str, Any] | None = None,
) -> tuple[NozzleCameraHealthReport, Path]:
    """Persist one compact nozzle-camera health artifact and any probe frames used."""

    root = _vision_root(output_root) / "health"
    root.mkdir(parents=True, exist_ok=True)
    frames_dir = root / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    capture_ts = result.report.nozzle_cam_last_capture_at or _utc_now_iso()
    identity_token = _collapse_text((job_identity or {}).get("artifact_token") or "idle")
    stem = f"{_timestamp_token(capture_ts)}-nozzle-camera-health-{identity_token}"

    frame_refs: list[str] = []
    last_frame_ref = result.report.nozzle_cam_last_frame_ref
    for index, frame in enumerate(result.frames, start=1):
        frame_path = frames_dir / f"{stem}-f{index:02d}.jpg"
        frame_path.write_bytes(frame.jpeg_bytes)
        frame_ref = _artifact_ref(frame_path)
        frame_refs.append(frame_ref)
        last_frame_ref = frame_ref

    report = result.report.model_copy(
        update={
            "nozzle_cam_last_frame_ref": last_frame_ref,
            "frame_refs_used": frame_refs or list(result.report.frame_refs_used),
        }
    )
    artifact_path = root / f"{stem}.json"
    payload = {
        "timestamp": _utc_now_iso(),
        "job_identity": job_identity or {},
        "status": status or {},
        "facts": nozzle_camera_fact_map(report, prefix=prefix),
        "blockers": list(report.nozzle_cam_blockers),
        "frame_refs_used": list(report.frame_refs_used),
        "usable": report.nozzle_cam_usable,
    }
    artifact_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return report, artifact_path


def analyze_persisted_frame(
    frame: PersistedFrame,
    *,
    settings,
    capture_mode: str,
    lifecycle: str | None,
) -> VisionObservation:
    """Run one compact multimodal analysis and return a strict observation."""

    observation, _trace = analyze_persisted_frame_with_trace(
        frame,
        settings=settings,
        capture_mode=capture_mode,
        lifecycle=lifecycle,
    )
    return observation


def analyze_persisted_frame_with_trace(
    frame: PersistedFrame,
    *,
    settings,
    capture_mode: str,
    lifecycle: str | None,
    previous_frame: PersistedFrame | None = None,
) -> tuple[VisionObservation, VisionAnalysisTrace]:
    """Run one compact multimodal analysis and return the observation plus trace metadata."""

    if not settings.vision_api_key:
        raise VisionError("Vision analysis unavailable: PRUSA_CORE_ONE_VISION_API_KEY or OPENROUTER_API_KEY is not set")
    if not settings.vision_model:
        raise VisionError("Vision analysis unavailable: PRUSA_CORE_ONE_VISION_MODEL or OPENROUTER_MODEL is not set")

    draft, trace = _analyze_frame_draft(
        jpeg_bytes=frame.jpeg_bytes,
        previous_jpeg_bytes=previous_frame.jpeg_bytes if previous_frame is not None else None,
        vision_api_key=settings.vision_api_key,
        vision_model=settings.vision_model,
        camera_id=frame.camera_id,
        capture_mode=capture_mode,
        lifecycle=lifecycle,
    )
    findings = [
        VisionFinding(
            finding_id=f"{frame.frame_id}-f{index + 1:02d}",
            finding_type=item.finding_type,
            descriptors=_clean_descriptors(item.descriptors),
            evidence_strength=item.evidence_strength,
            note=_short_sentence(item.note, fallback="Visible feature is present."),
        )
        for index, item in enumerate(draft.findings)
    ]
    observation = VisionObservation(
        schema_version="1.0",
        frame_id=frame.frame_id,
        camera_id=frame.camera_id,
        captured_at=frame.captured_at,
        image_ref=frame.image_ref,
        reference_image_ref=previous_frame.image_ref if previous_frame is not None else None,
        findings=findings,
        summary=_short_sentence(draft.summary, fallback="No clear visible defect."),
        comparison_delta=_normalize_comparison_delta(draft.comparison_delta),
        comparison_confidence=_normalize_comparison_confidence(draft.comparison_confidence),
        comparison_summary=_short_sentence(
            draft.comparison_summary,
            fallback="Comparison unavailable.",
        )
        if previous_frame is not None
        else None,
    )
    return observation, trace


def advisory_summary(observation: VisionObservation, *, limit: int = 160) -> str:
    """Return a compact planner-safe advisory summary."""

    if observation.findings:
        finding_labels = ",".join(item.finding_type for item in observation.findings[:2])
        text = f"vision:{finding_labels}; {observation.summary}"
    else:
        text = f"vision:normal; {observation.summary}"
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def _frame_age_s(captured_at: str) -> float:
    captured_dt = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    return max(0.0, (datetime.now(timezone.utc) - captured_dt).total_seconds())


def _service_ok(base_url: str) -> bool:
    try:
        body, _content_type = _http_read(f"{base_url}/snapshot", timeout_s=2.0)
        return bool(body)
    except Exception:
        return False


def _analyze_frame_bytes(jpeg_bytes: bytes) -> _FrameAnalysis:
    try:
        with Image.open(BytesIO(jpeg_bytes)) as image:
            decoded = image.convert("L")
            width, height = decoded.size
            thumb = decoded.resize((128, 72))
            pixels = list(thumb.getdata())
            dark_ratio = (sum(1 for value in pixels if value < 20) / len(pixels)) if pixels else 1.0
            bright_ratio = (sum(1 for value in pixels if value > 235) / len(pixels)) if pixels else 0.0
            mean_luma = (sum(pixels) / len(pixels)) if pixels else 0.0
            ahash = _average_hash(decoded)
            dhash = _difference_hash(decoded)
            blur_score = _sharpness_score(jpeg_bytes)
    except (UnidentifiedImageError, OSError, ValueError):
        return _FrameAnalysis(
            valid=False,
            width=0,
            height=0,
            dark_ratio=1.0,
            ahash=0,
            dhash=0,
            bright_ratio=0.0,
            mean_luma=0.0,
            blur_score=0.0,
        )
    return _FrameAnalysis(
        valid=bool(width > 0 and height > 0),
        width=width,
        height=height,
        dark_ratio=dark_ratio,
        ahash=ahash,
        dhash=dhash,
        bright_ratio=bright_ratio,
        mean_luma=mean_luma,
        blur_score=blur_score,
    )


def _frame_is_acceptable_for_active_print(analysis: _FrameAnalysis) -> bool:
    if not analysis.valid:
        return False
    if _looks_like_no_signal_slate(analysis):
        return False
    if analysis.blur_score < _ACTIVE_PRINT_BLUR_MIN_SCORE:
        return False
    if analysis.mean_luma < _ACTIVE_PRINT_MEAN_LUMA_MIN:
        return False
    if analysis.mean_luma > _ACTIVE_PRINT_MEAN_LUMA_MAX:
        return False
    if analysis.bright_ratio > _ACTIVE_PRINT_BRIGHT_RATIO_MAX:
        return False
    if analysis.dark_ratio > _ACTIVE_PRINT_DARK_RATIO_MAX:
        return False
    return True


def _sharpness_score(jpeg_bytes: bytes) -> float:
    try:
        with Image.open(BytesIO(jpeg_bytes)) as image:
            decoded = image.convert("L").resize((160, 90))
            width, height = decoded.size
            pixels = list(decoded.getdata())
    except (UnidentifiedImageError, OSError, ValueError):
        return 0.0
    if width < 2 or height < 2 or not pixels:
        return 0.0

    def _px(x: int, y: int) -> int:
        return pixels[(y * width) + x]

    edge_energy = 0
    comparisons = 0
    for y in range(height - 1):
        for x in range(width - 1):
            center = _px(x, y)
            edge_energy += abs(center - _px(x + 1, y))
            edge_energy += abs(center - _px(x, y + 1))
            comparisons += 2
    if comparisons == 0:
        return 0.0
    return edge_energy / comparisons


def _average_hash(image: Image.Image) -> int:
    resized = image.resize((8, 8))
    pixels = list(resized.getdata())
    average = (sum(pixels) / len(pixels)) if pixels else 0
    bits = "".join("1" if value >= average else "0" for value in pixels)
    return int(bits or "0", 2)


def _difference_hash(image: Image.Image) -> int:
    resized = image.resize((9, 8))
    pixels = list(resized.getdata())
    bits: list[str] = []
    for row in range(8):
        row_pixels = pixels[row * 9 : (row + 1) * 9]
        for column in range(8):
            bits.append("1" if row_pixels[column] > row_pixels[column + 1] else "0")
    return int("".join(bits) or "0", 2)


def _looks_like_no_signal_slate(analysis: _FrameAnalysis) -> bool:
    if not analysis.valid:
        return False
    if analysis.dark_ratio < _NO_SIGNAL_DARK_RATIO_MIN:
        return False
    ahash_distance = (analysis.ahash ^ _NO_SIGNAL_AHASH).bit_count()
    dhash_distance = (analysis.dhash ^ _NO_SIGNAL_DHASH).bit_count()
    return ahash_distance <= _NO_SIGNAL_AHASH_MAX_DISTANCE and dhash_distance <= _NO_SIGNAL_DHASH_MAX_DISTANCE


def _unique_blockers(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _artifact_ref(path: Path) -> str:
    repo_root = _repo_root()
    try:
        return path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return str(path.resolve())


def _analyze_frame_draft(
    *,
    jpeg_bytes: bytes,
    previous_jpeg_bytes: bytes | None,
    vision_api_key: str,
    vision_model: str,
    camera_id: str,
    capture_mode: str,
    lifecycle: str | None,
) -> tuple[_VisionAnalysisDraft, VisionAnalysisTrace]:
    prompt = _build_prompt(camera_id=camera_id, capture_mode=capture_mode, lifecycle=lifecycle)
    image_b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if previous_jpeg_bytes is not None:
        previous_b64 = base64.b64encode(previous_jpeg_bytes).decode("ascii")
        content.extend(
            [
                {"type": "text", "text": "PREVIOUS frame (reference):"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{previous_b64}"}},
                {"type": "text", "text": "CURRENT frame (now):"},
            ]
        )
    payload = {
        "model": vision_model,
        "stream": False,
        "temperature": 0.1,
        "messages": [
            {
                "role": "user",
                "content": [*content, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}],
            }
        ],
    }
    req = request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {vision_api_key}",
            "Content-Type": "application/json",
        },
    )
    request_started_at = utc_now_iso()
    request_started_monotonic = time.time()
    try:
        with request.urlopen(req, timeout=45) as response:
            raw_body = response.read()
            response_headers = dict(response.headers.items())
    except error.HTTPError as exc:
        snippet = exc.read().decode("utf-8", errors="replace")[:300]
        raise VisionError(f"Vision analysis request failed with HTTP {exc.code}: {snippet}") from exc
    except error.URLError as exc:
        raise VisionError(f"Vision analysis request failed: {exc.reason}") from exc
    response_received_at = utc_now_iso()
    latency_ms = round((time.time() - request_started_monotonic) * 1000.0, 1)
    body = raw_body.decode("utf-8")

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise VisionError(f"Vision analysis returned invalid JSON envelope: {body[:200]!r}") from exc
    content_text = _response_text(data)
    draft_payload = _extract_json_object(content_text)
    try:
        draft = _VisionAnalysisDraft.model_validate(draft_payload)
    except Exception as exc:
        raise VisionError(f"Vision analysis payload failed schema validation: {exc}") from exc
    trace = VisionAnalysisTrace(
        provider_metadata=normalize_openrouter_metadata(
            payload=data,
            requested_model=vision_model,
            latency_ms=latency_ms,
            response_size_bytes=len(raw_body),
            retry_count=0,
            schema_valid=True,
            request_started_at=request_started_at,
            response_received_at=response_received_at,
            headers=response_headers,
        ),
        request_payload=payload,
        response_payload=data,
        response_text=content_text,
        prompt_text=prompt,
    )
    return draft, trace


def persist_vision_replay_bundle(
    *,
    frame: PersistedFrame,
    trace: VisionAnalysisTrace,
    output_root: str | Path | None = None,
    previous_frame: PersistedFrame | None = None,
) -> Path:
    replay_path = _vision_replay_dir(output_root) / f"{frame.frame_id}.json"
    payload = {
        "schema_version": "1.0",
        "frame_id": frame.frame_id,
        "camera_id": frame.camera_id,
        "captured_at": frame.captured_at,
        "image_ref": frame.image_ref,
        "image_sha256": hashlib.sha256(frame.jpeg_bytes).hexdigest(),
        "image_bytes_base64": base64.b64encode(frame.jpeg_bytes).decode("ascii"),
        "reference_image_ref": previous_frame.image_ref if previous_frame is not None else None,
        "reference_image_sha256": hashlib.sha256(previous_frame.jpeg_bytes).hexdigest() if previous_frame is not None else None,
        "reference_image_bytes_base64": base64.b64encode(previous_frame.jpeg_bytes).decode("ascii")
        if previous_frame is not None
        else None,
        "prompt_text": trace.prompt_text,
        "model": trace.request_payload.get("model"),
        "request_params": {
            "stream": trace.request_payload.get("stream"),
            "temperature": trace.request_payload.get("temperature"),
        },
        "request_payload": trace.request_payload,
        "provider_metadata": trace.provider_metadata,
        "response_payload": trace.response_payload,
        "response_text": trace.response_text,
    }
    replay_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return replay_path


def replay_saved_vision_bundle(
    bundle_path: str | Path,
    *,
    vision_api_key: str,
    timeout_s: float = 45.0,
) -> VisionAnalysisTrace:
    payload = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
    request_payload = payload.get("request_payload")
    if not isinstance(request_payload, dict):
        raise VisionError("Replay bundle is missing request_payload")
    prompt_text = _collapse_text(payload.get("prompt_text"))
    if not prompt_text:
        raise VisionError("Replay bundle is missing prompt_text")
    req = request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(request_payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {vision_api_key}",
            "Content-Type": "application/json",
        },
    )
    request_started_at = utc_now_iso()
    request_started_monotonic = time.time()
    try:
        with request.urlopen(req, timeout=timeout_s) as response:
            raw_body = response.read()
            response_headers = dict(response.headers.items())
    except error.HTTPError as exc:
        snippet = exc.read().decode("utf-8", errors="replace")[:300]
        raise VisionError(f"Vision replay request failed with HTTP {exc.code}: {snippet}") from exc
    except error.URLError as exc:
        raise VisionError(f"Vision replay request failed: {exc.reason}") from exc
    response_received_at = utc_now_iso()
    latency_ms = round((time.time() - request_started_monotonic) * 1000.0, 1)
    body = raw_body.decode("utf-8")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise VisionError(f"Vision replay returned invalid JSON envelope: {body[:200]!r}") from exc
    content_text = _response_text(data)
    draft_payload = _extract_json_object(content_text)
    try:
        _VisionAnalysisDraft.model_validate(draft_payload)
        schema_valid = True
    except Exception:
        schema_valid = False
    return VisionAnalysisTrace(
        provider_metadata=normalize_openrouter_metadata(
            payload=data,
            requested_model=str(request_payload.get("model") or ""),
            latency_ms=latency_ms,
            response_size_bytes=len(raw_body),
            retry_count=0,
            schema_valid=schema_valid,
            request_started_at=request_started_at,
            response_received_at=response_received_at,
            headers=response_headers,
        ),
        request_payload=request_payload,
        response_payload=data,
        response_text=content_text,
        prompt_text=prompt_text,
    )


def _response_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        raise VisionError("Vision analysis returned no choices")
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return str(content)


def _extract_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0 or end < start:
        raise VisionError(f"Vision analysis returned no JSON object: {text[:200]!r}")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise VisionError(f"Vision analysis returned invalid JSON payload: {text[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise VisionError("Vision analysis payload must be a JSON object")
    return payload


def _build_prompt(*, camera_id: str, capture_mode: str, lifecycle: str | None) -> str:
    lifecycle_text = _collapse_text(lifecycle or "unknown")
    return (
        "Analyze this nozzle-camera image from a Prusa CORE One/+ print. "
        "Return JSON only with exactly these top-level keys: summary, findings, comparison_delta, comparison_confidence, comparison_summary. "
        "summary must be one short literal sentence about visible appearance only. "
        "findings must be an array of objects with exactly these keys: finding_type, descriptors, "
        "evidence_strength, note. Allowed finding_type values: residue, stringing, spaghetti, blob, unknown. "
        "Multiple findings may coexist when the image shows more than one visible issue. "
        "Use residue only for ordinary adhered nozzle residue or discoloration that is visible without a distinct thin strand. "
        "If any visible thin strand is trailing from, hanging from, or extending away from the nozzle, include stringing even if residue is also visible. "
        "Use blob only for larger thick or bulky hanging material buildup, not for a thin strand. "
        "Use spaghetti for loose tangled extruded filament or a collapsed nest of unsupported strands near the nozzle. "
        "Use unknown when the image is too unclear to determine whether a visible feature is a strand or when the issue does not fit these labels. "
        "If a previous frame is also shown, compare the current frame against it for the same visible issue. "
        "comparison_delta must be one of: better, same, worse, unknown. "
        "comparison_confidence must be one of: weak, moderate, strong, or null if no reliable comparison is possible. "
        "comparison_summary must be one short literal sentence stating whether the visible issue looks better, same, worse, or comparison is unavailable. "
        "If only one frame is shown, set comparison_delta, comparison_confidence, and comparison_summary to null. "
        "Allowed evidence_strength values: weak, moderate, strong. "
        "Do not output confidence, description, region, location, scope, suggested_families, supports, "
        "actions, recommendations, causes, or long prose. "
        "If nothing clear is visible, return an empty findings array and a short literal summary. "
        f"Context: camera_id={camera_id}; capture_mode={capture_mode}; lifecycle={lifecycle_text}."
    )


def latest_previous_persisted_frame(
    *,
    output_root: str | Path | None,
    job_identity: str,
    camera_id: str,
    before_frame_id: str,
) -> PersistedFrame | None:
    frames_dir = _vision_root(output_root) / "frames"
    if not frames_dir.exists():
        return None
    suffix = f"-nozzle-{job_identity}-{camera_id}"
    candidates = sorted(
        path
        for path in frames_dir.glob("*.jpg")
        if path.stem.endswith(suffix) and path.stem != before_frame_id
    )
    if not candidates:
        return None
    frame_path = candidates[-1]
    return PersistedFrame(
        frame_id=frame_path.stem,
        camera_id=camera_id,
        captured_at=_captured_at_from_frame_stem(frame_path.stem),
        frame_path=frame_path,
        image_ref=_artifact_ref(frame_path),
        artifact_stem=frame_path.stem,
        jpeg_bytes=frame_path.read_bytes(),
    )


def _captured_at_from_frame_stem(stem: str) -> str:
    token = stem.split("-nozzle-", 1)[0]
    try:
        dt = datetime.strptime(token, "%Y-%m-%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    except ValueError:
        return _utc_now_iso()


def _normalize_comparison_delta(value: Any) -> ComparisonDelta | None:
    text = _collapse_text(value).lower()
    if text in {"better", "same", "worse", "unknown"}:
        return text  # type: ignore[return-value]
    return None


def _normalize_comparison_confidence(value: Any) -> EvidenceStrength | None:
    text = _collapse_text(value).lower()
    if text in {"weak", "moderate", "strong"}:
        return text  # type: ignore[return-value]
    return None
