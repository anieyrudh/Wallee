"""Supported-surface adapters for the Prusa CORE One/+ pack.

Design rules:

- HTTP is the authoritative read surface.
- HTTP is also the lifecycle-control surface.
- Serial is used only for bounded live-tuning commands.
- Verification always comes back through HTTP-observable post-state.

That keeps one writer path per live action family without turning the planner
into a raw command author.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Any, Protocol
from urllib import error, parse, request

from .types import PrintableFile


_PRINTABLE_SUFFIXES = (".gcode", ".bgcode", ".gco", ".bgc")
_DEFAULT_NOZZLE_CAMERA_DEVICE_PATH = "/dev/v4l/by-id/usb-3DO_3DO_NOZZLE_CAMERA_V2_3DO-video-index0"


def _resolve_env(primary: str, *aliases: str) -> str | None:
    """Return the first non-blank environment value, preferring *primary*."""
    for name in (primary, *aliases):
        value = os.environ.get(name)
        if value is None:
            continue
        text = value.strip()
        if text:
            return text
    return None


def _resolve_csv_env(primary: str, *aliases: str) -> tuple[str, ...]:
    """Return a tuple parsed from the first non-blank CSV environment value."""
    value = _resolve_env(primary, *aliases)
    if value is None:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _default_vision_advisory_interval_s() -> float:
    explicit = os.environ.get("PRUSA_CORE_ONE_VISION_ADVISORY_INTERVAL_S")
    if explicit is not None and explicit.strip():
        return float(explicit)
    planner_interval_s = max(0.0, float(os.environ.get("WALLEE_PLANNER_MIN_INTERVAL_S", "0.0")))
    planner_vision_lead_s = max(0.0, float(os.environ.get("WALLEE_PLANNER_VISION_LEAD_S", "15.0")))
    derived = planner_interval_s - planner_vision_lead_s
    if derived <= 0.0:
        return 10.0
    return max(1.0, derived)


@dataclass(slots=True)
class PrusaCoreOneSettings:
    """Runtime settings for the Prusa CORE One/+ pack."""

    host: str
    api_key: str | None
    http_timeout_s: float
    file_action_limit: int
    state_transition_timeout_s: float
    status_poll_interval_s: float
    safe_to_unload_bed_c: float
    safe_to_touch_nozzle_c: float
    max_nozzle_target_c: float
    max_bed_target_c: float
    serial_enabled: bool
    serial_port: str | None
    serial_baud: int
    serial_timeout_s: float
    notebook_dir: str | None
    notebook_download_enabled: bool
    notebook_lookahead_pct: float
    enable_experimental_tuning: bool
    planner_allowed_tuning_families: tuple[str, ...] = ()
    speed_tuning_verification_enabled: bool = False
    flow_tuning_verification_enabled: bool = False
    nozzle_camera_host: str = "127.0.0.1"
    nozzle_camera_port: str | None = None
    nozzle_camera_discovery_ports: tuple[str, ...] = ()
    nozzle_camera_device_path: str = _DEFAULT_NOZZLE_CAMERA_DEVICE_PATH
    nozzle_camera_max_frame_age_s: float = 30.0
    nozzle_camera_liveness_window_size: int = 2
    vision_api_key: str | None = None
    vision_model: str | None = None
    enable_vision_debug_context: bool = False
    vision_advisory_interval_s: float = 10.0
    live_tuning_min_progress_pct: float = 5.0
    live_tuning_max_progress_pct: float = 95.0
    live_tuning_symptom_max_progress_pct: float = 99.0
    wait_cool_step_timeout_s: float = 30.0
    speed_small_step_pct: float = 5.0
    speed_big_step_pct: float = 10.0
    speed_min_pct: float = 65.0
    speed_default_pct: float = 100.0
    speed_max_pct: float = 135.0
    flow_small_step_pct: float = 5.0
    flow_big_step_pct: float = 10.0
    flow_min_pct: float = 65.0
    flow_default_pct: float = 100.0
    flow_max_pct: float = 135.0
    temp_small_step_c: float = 5.0
    temp_big_step_c: float = 10.0
    pressure_advance_small_step_pct: float = 10.0
    pressure_advance_big_step_pct: float = 20.0
    pressure_advance_envelope_pct: float = 25.0
    pressure_advance_min: float = 0.0
    pressure_advance_max: float = 0.12
    pressure_advance_tuning_verification_enabled: bool = False
    accel_small_step_pct: float = 10.0
    accel_big_step_pct: float = 20.0
    accel_envelope_pct: float = 25.0
    accel_min_mm_s2: float = 500.0
    accel_max_mm_s2: float = 12000.0
    accel_tuning_verification_enabled: bool = False
    family_big_cooldown_s: float = 60.0
    tuning_settle_window_s: float = 25.0
    thermal_tuning_settle_window_s: float = 60.0
    pressure_advance_tuning_settle_window_s: float = 45.0
    accel_tuning_settle_window_s: float = 45.0

    @classmethod
    def from_env(cls) -> "PrusaCoreOneSettings":
        host = (_resolve_env("PRUSA_CORE_ONE_HOST", "PRUSALINK_HOST") or "").rstrip("/")
        if host and not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        return cls(
            host=host,
            api_key=_resolve_env("PRUSA_CORE_ONE_API_KEY", "PRUSALINK_API_KEY"),
            http_timeout_s=float(os.environ.get("PRUSA_CORE_ONE_HTTP_TIMEOUT_S", "2.0")),
            file_action_limit=int(os.environ.get("PRUSA_CORE_ONE_FILE_ACTION_LIMIT", "2")),
            state_transition_timeout_s=float(os.environ.get("PRUSA_CORE_ONE_STATE_TRANSITION_TIMEOUT_S", "8.0")),
            status_poll_interval_s=float(os.environ.get("PRUSA_CORE_ONE_STATUS_POLL_INTERVAL_S", "0.25")),
            safe_to_unload_bed_c=float(os.environ.get("WALLEE_SAFE_TO_UNLOAD_TEMP_C", "35.0")),
            safe_to_touch_nozzle_c=float(os.environ.get("PRUSA_CORE_ONE_SAFE_NOZZLE_TOUCH_C", "50.0")),
            max_nozzle_target_c=float(os.environ.get("PRUSA_CORE_ONE_MAX_NOZZLE_TARGET_C", "300.0")),
            max_bed_target_c=float(os.environ.get("PRUSA_CORE_ONE_MAX_BED_TARGET_C", "120.0")),
            serial_enabled=os.environ.get("PRUSA_CORE_ONE_ENABLE_SERIAL", "0").strip() not in {"0", "false", "False"},
            serial_port=os.environ.get("PRUSA_CORE_ONE_SERIAL_PORT") or None,
            serial_baud=int(os.environ.get("PRUSA_CORE_ONE_SERIAL_BAUD", "115200")),
            serial_timeout_s=float(os.environ.get("PRUSA_CORE_ONE_SERIAL_TIMEOUT_S", "1.0")),
            notebook_dir=(os.environ.get("PRUSA_CORE_ONE_NOTEBOOK_DIR") or "").strip() or None,
            notebook_download_enabled=os.environ.get("PRUSA_CORE_ONE_ENABLE_GCODE_DOWNLOAD", "1").strip()
            not in {"0", "false", "False"},
            notebook_lookahead_pct=float(os.environ.get("PRUSA_CORE_ONE_NOTEBOOK_LOOKAHEAD_PCT", "5.0")),
            enable_experimental_tuning=os.environ.get("PRUSA_CORE_ONE_ENABLE_EXPERIMENTAL_TUNING", "0").strip()
            not in {"0", "false", "False"},
            planner_allowed_tuning_families=tuple(
                family
                for family in (
                    value.strip().lower()
                    for value in os.environ.get("PRUSA_CORE_ONE_PLANNER_ALLOWED_TUNING_FAMILIES", "").split(",")
                )
                if family
            ),
            speed_tuning_verification_enabled=os.environ.get("PRUSA_CORE_ONE_ENABLE_SPEED_TUNING_VERIFICATION", "0").strip()
            not in {"0", "false", "False"},
            flow_tuning_verification_enabled=os.environ.get("PRUSA_CORE_ONE_ENABLE_FLOW_TUNING_VERIFICATION", "0").strip()
            not in {"0", "false", "False"},
            nozzle_camera_host=(_resolve_env("PRUSA_CORE_ONE_NOZZLE_CAMERA_HOST", "NOZZLE_CAMERA_HOST") or "127.0.0.1"),
            nozzle_camera_port=_resolve_env("PRUSA_CORE_ONE_NOZZLE_CAMERA_PORT", "NOZZLE_CAMERA_PORT"),
            nozzle_camera_discovery_ports=_resolve_csv_env(
                "PRUSA_CORE_ONE_NOZZLE_CAMERA_DISCOVERY_PORTS",
                "NOZZLE_CAMERA_DISCOVERY_PORTS",
            ),
            nozzle_camera_device_path=(
                _resolve_env("PRUSA_CORE_ONE_NOZZLE_CAMERA_DEVICE_PATH", "NOZZLE_CAMERA_DEVICE_PATH")
                or _DEFAULT_NOZZLE_CAMERA_DEVICE_PATH
            ),
            nozzle_camera_max_frame_age_s=float(os.environ.get("PRUSA_CORE_ONE_NOZZLE_CAMERA_MAX_FRAME_AGE_S", "30.0")),
            nozzle_camera_liveness_window_size=max(
                2,
                int(os.environ.get("PRUSA_CORE_ONE_NOZZLE_CAMERA_LIVENESS_WINDOW_SIZE", "2")),
            ),
            vision_api_key=_resolve_env("PRUSA_CORE_ONE_VISION_API_KEY", "OPENROUTER_API_KEY"),
            vision_model=_resolve_env("PRUSA_CORE_ONE_VISION_MODEL", "VISION_MODEL", "OPENROUTER_MODEL")
            or "google/gemini-3.1-flash-lite-preview",
            enable_vision_debug_context=os.environ.get("PRUSA_CORE_ONE_ENABLE_VISION_DEBUG_CONTEXT", "0").strip()
            not in {"0", "false", "False"},
            vision_advisory_interval_s=_default_vision_advisory_interval_s(),
            live_tuning_min_progress_pct=float(os.environ.get("PRUSA_CORE_ONE_LIVE_TUNING_MIN_PROGRESS_PCT", "5.0")),
            live_tuning_max_progress_pct=float(os.environ.get("PRUSA_CORE_ONE_LIVE_TUNING_MAX_PROGRESS_PCT", "95.0")),
            live_tuning_symptom_max_progress_pct=float(
                os.environ.get("PRUSA_CORE_ONE_LIVE_TUNING_SYMPTOM_MAX_PROGRESS_PCT", "99.0")
            ),
            wait_cool_step_timeout_s=float(os.environ.get("PRUSA_CORE_ONE_WAIT_COOL_STEP_TIMEOUT_S", "30.0")),
            speed_small_step_pct=float(os.environ.get("PRUSA_CORE_ONE_SPEED_SMALL_STEP_PCT", "5.0")),
            speed_big_step_pct=float(os.environ.get("PRUSA_CORE_ONE_SPEED_BIG_STEP_PCT", "10.0")),
            speed_min_pct=float(os.environ.get("PRUSA_CORE_ONE_SPEED_MIN_PCT", "65.0")),
            speed_default_pct=float(os.environ.get("PRUSA_CORE_ONE_SPEED_DEFAULT_PCT", "100.0")),
            speed_max_pct=float(os.environ.get("PRUSA_CORE_ONE_SPEED_MAX_PCT", "135.0")),
            flow_small_step_pct=float(os.environ.get("PRUSA_CORE_ONE_FLOW_SMALL_STEP_PCT", "5.0")),
            flow_big_step_pct=float(os.environ.get("PRUSA_CORE_ONE_FLOW_BIG_STEP_PCT", "10.0")),
            flow_min_pct=float(os.environ.get("PRUSA_CORE_ONE_FLOW_MIN_PCT", "65.0")),
            flow_default_pct=float(os.environ.get("PRUSA_CORE_ONE_FLOW_DEFAULT_PCT", "100.0")),
            flow_max_pct=float(os.environ.get("PRUSA_CORE_ONE_FLOW_MAX_PCT", "135.0")),
            temp_small_step_c=float(os.environ.get("PRUSA_CORE_ONE_TEMP_SMALL_STEP_C", "5.0")),
            temp_big_step_c=float(os.environ.get("PRUSA_CORE_ONE_TEMP_BIG_STEP_C", "10.0")),
            pressure_advance_small_step_pct=float(
                os.environ.get("PRUSA_CORE_ONE_PRESSURE_ADVANCE_SMALL_STEP_PCT", "10.0")
            ),
            pressure_advance_big_step_pct=float(
                os.environ.get("PRUSA_CORE_ONE_PRESSURE_ADVANCE_BIG_STEP_PCT", "20.0")
            ),
            pressure_advance_envelope_pct=float(
                os.environ.get("PRUSA_CORE_ONE_PRESSURE_ADVANCE_ENVELOPE_PCT", "25.0")
            ),
            pressure_advance_min=float(os.environ.get("PRUSA_CORE_ONE_PRESSURE_ADVANCE_MIN", "0.0")),
            pressure_advance_max=float(os.environ.get("PRUSA_CORE_ONE_PRESSURE_ADVANCE_MAX", "0.12")),
            pressure_advance_tuning_verification_enabled=os.environ.get(
                "PRUSA_CORE_ONE_ENABLE_PRESSURE_ADVANCE_TUNING_VERIFICATION",
                "0",
            ).strip()
            not in {"0", "false", "False"},
            accel_small_step_pct=float(os.environ.get("PRUSA_CORE_ONE_ACCEL_SMALL_STEP_PCT", "10.0")),
            accel_big_step_pct=float(os.environ.get("PRUSA_CORE_ONE_ACCEL_BIG_STEP_PCT", "20.0")),
            accel_envelope_pct=float(os.environ.get("PRUSA_CORE_ONE_ACCEL_ENVELOPE_PCT", "25.0")),
            accel_min_mm_s2=float(os.environ.get("PRUSA_CORE_ONE_ACCEL_MIN_MM_S2", "500.0")),
            accel_max_mm_s2=float(os.environ.get("PRUSA_CORE_ONE_ACCEL_MAX_MM_S2", "12000.0")),
            accel_tuning_verification_enabled=os.environ.get(
                "PRUSA_CORE_ONE_ENABLE_ACCEL_TUNING_VERIFICATION",
                "0",
            ).strip()
            not in {"0", "false", "False"},
            family_big_cooldown_s=float(os.environ.get("PRUSA_CORE_ONE_FAMILY_BIG_COOLDOWN_S", "60.0")),
            tuning_settle_window_s=float(os.environ.get("PRUSA_CORE_ONE_TUNING_SETTLE_WINDOW_S", "25.0")),
            thermal_tuning_settle_window_s=float(
                os.environ.get("PRUSA_CORE_ONE_THERMAL_TUNING_SETTLE_WINDOW_S", "60.0")
            ),
            pressure_advance_tuning_settle_window_s=float(
                os.environ.get("PRUSA_CORE_ONE_PRESSURE_ADVANCE_TUNING_SETTLE_WINDOW_S", "45.0")
            ),
            accel_tuning_settle_window_s=float(
                os.environ.get("PRUSA_CORE_ONE_ACCEL_TUNING_SETTLE_WINDOW_S", "45.0")
            ),
        )


class SupportsPrusaHttp(Protocol):
    """Protocol for the PrusaLink adapter."""

    def get_status(self) -> dict[str, Any]: ...

    def get_job(self) -> dict[str, Any]: ...

    def get_info(self) -> dict[str, Any]: ...

    def list_usb_files(self) -> list[PrintableFile]: ...

    def get_file_info(self, file_path: str) -> dict[str, Any]: ...

    def download_file_bytes(self, file_path: str, download_path: str | None = None) -> bytes | None: ...

    def download_file_text(self, file_path: str, download_path: str | None = None) -> str | None: ...

    def pause_job(self, job_id: int | None = None) -> None: ...

    def resume_job(self, job_id: int | None = None) -> None: ...

    def cancel_job(self, job_id: int | None = None) -> None: ...

    def start_print(self, file_path: str) -> None: ...

    def send_gcode(self, command: str) -> None: ...


class SupportsPrusaSerialWriter(Protocol):
    """Protocol for bounded serial command writes."""

    def bind_session(self, session_key: str | None) -> None: ...

    def send_command(self, command: str) -> None: ...

    def query_command(self, command: str) -> list[str]: ...

    def close(self) -> None: ...


class NoopSerialWriter:
    """Null-object serial writer used when live tuning is disabled."""

    def bind_session(self, session_key: str | None) -> None:  # pragma: no cover - defensive path
        return None

    def send_command(self, command: str) -> None:  # pragma: no cover - defensive path
        raise RuntimeError("serial command writer is disabled")

    def query_command(self, command: str) -> list[str]:  # pragma: no cover - defensive path
        raise RuntimeError("serial command writer is disabled")

    def close(self) -> None:  # pragma: no cover - defensive path
        return None


class PrusaSerialWriter:
    """Minimal persistent serial writer for bounded live-tuning commands."""

    _SPAM_PATTERNS = (
        "FIRMWARE_NAME:",
        "SOURCE_CODE_URL:",
        "PROTOCOL_VERSION:",
        "MACHINE_TYPE:",
        "EXTRUDER_COUNT:",
        "UUID:",
        "Cap:",
    )

    def __init__(self, settings: PrusaCoreOneSettings) -> None:
        self.settings = settings
        if not settings.serial_enabled or not settings.serial_port:
            raise ValueError("serial control requires PRUSA_CORE_ONE_ENABLE_SERIAL=1 and PRUSA_CORE_ONE_SERIAL_PORT")
        self._port = None
        self._session_key: str | None = None

    def bind_session(self, session_key: str | None) -> None:
        normalized = (session_key or "").strip() or None
        if self._port is not None and normalized != self._session_key:
            self.close()
        self._session_key = normalized

    def send_command(self, command: str) -> None:
        self._exchange(command, capture_response=False)

    def query_command(self, command: str) -> list[str]:
        return self._exchange(command, capture_response=True)

    def preflight(self, *, command: str = "M400") -> dict[str, Any]:
        started = time.monotonic()
        lines = self._exchange(command, capture_response=True)
        return {
            "ok": True,
            "port": self.settings.serial_port,
            "command": command,
            "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
            "lines": list(lines),
        }

    def _exchange(self, command: str, *, capture_response: bool) -> list[str]:
        try:
            import serial  # type: ignore
        except Exception as exc:  # pragma: no cover - import error path
            raise RuntimeError("pyserial is required for bounded serial control") from exc

        port = self._ensure_port(serial)
        try:
            port.reset_input_buffer()
        except Exception:
            self.close()
            port = self._ensure_port(serial)
            port.reset_input_buffer()
        port.write((command.rstrip() + "\n").encode("utf-8"))
        port.flush()
        if not capture_response:
            return []
        lines: list[str] = []
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            raw = port.readline()
            if not raw:
                continue
            text = raw.decode("utf-8", errors="replace").strip()
            if not text or self._is_spam(text) or self._is_garbled(text):
                continue
            lines.append(text)
            if text.startswith("ok"):
                break
        return lines

    def _ensure_port(self, serial_module):
        port = self._port
        if port is not None:
            return port
        port = serial_module.Serial(
            self.settings.serial_port,
            baudrate=self.settings.serial_baud,
            timeout=self.settings.serial_timeout_s,
            dsrdtr=False,
        )
        time.sleep(0.1)
        try:
            port.rts = False
        except Exception:
            pass
        self._port = port
        return port

    def close(self) -> None:
        port = self._port
        self._port = None
        self._session_key = None
        if port is None:
            return
        try:
            port.close()
        except Exception:
            return

    def _is_spam(self, line: str) -> bool:
        return any(pattern in line for pattern in self._SPAM_PATTERNS)

    @staticmethod
    def _is_garbled(line: str) -> bool:
        printable = sum(1 for c in line if 32 <= ord(c) <= 126)
        return not line or printable / max(len(line), 1) < 0.7


class PrusaLinkHttpClient:
    """Small PrusaLink client for the endpoints the pack actually uses."""

    def __init__(self, settings: PrusaCoreOneSettings) -> None:
        self.settings = settings
        if not settings.host:
            raise ValueError("PRUSA_CORE_ONE_HOST must be set to enable the real Prusa pack")

    def get_status(self) -> dict[str, Any]:
        return self._request_json("GET", "/api/v1/status")

    def get_job(self) -> dict[str, Any]:
        return self._request_json("GET", "/api/v1/job")

    def get_info(self) -> dict[str, Any]:
        return self._request_json("GET", "/api/v1/info")

    def list_usb_files(self) -> list[PrintableFile]:
        payload = self._request_json("GET", "/api/v1/files/usb")
        return flatten_prusalink_file_tree(payload)

    def get_file_info(self, file_path: str) -> dict[str, Any]:
        return self._request_json("GET", f"/api/v1/files/usb/{quote_printer_path(file_path)}")

    def download_file_bytes(self, file_path: str, download_path: str | None = None) -> bytes | None:
        path = download_path or f"/api/files/usb/{quote_printer_path(file_path)}/raw"
        response = self._request("GET", path, accept="application/octet-stream")
        payload = response.read()
        return payload or None

    def download_file_text(self, file_path: str, download_path: str | None = None) -> str | None:
        payload = self.download_file_bytes(file_path, download_path=download_path)
        if not payload:
            return None
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            return payload.decode("latin-1", errors="replace")

    def pause_job(self, job_id: int | None = None) -> None:
        if job_id is not None:
            self._request_no_content("PUT", f"/api/v1/job/{job_id}/pause")
            return
        self._request_no_content("PUT", "/api/v1/job", payload={"command": "PAUSE"})

    def resume_job(self, job_id: int | None = None) -> None:
        if job_id is not None:
            self._request_no_content("PUT", f"/api/v1/job/{job_id}/resume")
            return
        self._request_no_content("PUT", "/api/v1/job", payload={"command": "RESUME"})

    def cancel_job(self, job_id: int | None = None) -> None:
        if job_id is not None:
            self._request_no_content("DELETE", f"/api/v1/job/{job_id}")
            return
        self._request_no_content("DELETE", "/api/v1/job")

    def start_print(self, file_path: str) -> None:
        self._request_no_content("POST", f"/api/v1/files/usb/{quote_printer_path(file_path)}")

    def send_gcode(self, command: str) -> None:
        self._request_no_content("POST", "/api/v1/gcode", payload={"command": command})

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._request(method, path, payload=payload)
        if response.status == 204:
            return {}
        body = response.read().decode("utf-8")
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Expected JSON response from {path}, got {body[:120]!r}") from exc

    def _request_no_content(self, method: str, path: str, payload: dict[str, Any] | None = None) -> None:
        response = self._request(method, path, payload=payload)
        if response.status not in {200, 201, 204}:
            raise RuntimeError(f"Unexpected status {response.status} for {method} {path}")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        accept: str = "application/json",
    ):
        url = path if path.startswith("http://") or path.startswith("https://") else f"{self.settings.host}{path}"
        headers = {"Accept": accept}
        if self.settings.api_key:
            headers["X-Api-Key"] = self.settings.api_key
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(url, data=body, method=method, headers=headers)
        try:
            return request.urlopen(req, timeout=self.settings.http_timeout_s)
        except error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise RuntimeError(f"PrusaLink {method} {path} failed with HTTP {exc.code}: {body_text[:200]!r}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"PrusaLink {method} {path} failed: {exc.reason}") from exc


def quote_printer_path(path: str) -> str:
    """Return a PrusaLink-safe relative file path."""
    cleaned = str(path).strip().lstrip("/")
    if cleaned.lower().startswith("usb/"):
        cleaned = cleaned[4:]
    return parse.quote(cleaned, safe="/")


def flatten_prusalink_file_tree(payload: Any) -> list[PrintableFile]:
    """Flatten PrusaLink's file-tree responses into printable file entries."""
    results: list[PrintableFile] = []

    def walk(node: Any, parent: str = "") -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, parent)
            return
        if not isinstance(node, dict):
            return

        children = node.get("children")
        raw_path = str(node.get("path") or node.get("name") or "")
        if raw_path and not raw_path.startswith("/"):
            joined = f"{parent.rstrip('/')}/{raw_path.lstrip('/')}" if parent else f"/{raw_path.lstrip('/')}"
        else:
            joined = raw_path or parent

        if isinstance(children, list):
            for child in children:
                walk(child, joined)
            return

        candidate_path = joined or parent
        display_name = str(node.get("display_name") or Path(candidate_path).name)
        printable = display_name.lower().endswith(_PRINTABLE_SUFFIXES) or candidate_path.lower().endswith(_PRINTABLE_SUFFIXES)
        if not printable:
            return
        refs = node.get("refs") if isinstance(node.get("refs"), dict) else {}
        normalized_path = candidate_path if candidate_path.startswith("/") else f"/{candidate_path.lstrip('/')}"
        results.append(
            PrintableFile(
                path=normalized_path,
                display_name=display_name,
                size_bytes=int(node["size"]) if isinstance(node.get("size"), (int, float)) else None,
                modified_ts=int(node["m_timestamp"]) if isinstance(node.get("m_timestamp"), (int, float)) else None,
                refs={str(key): str(value) for key, value in refs.items() if isinstance(value, str)},
            )
        )

    walk(payload)
    return results
