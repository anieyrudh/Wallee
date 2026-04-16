"""Prusa-specific driver logic.

This module owns all concrete control mechanics for the CORE One/+ pack.
The pack decides which bounded action is legal; the driver turns that action
into one real hardware write path and one real verification path.
"""

from __future__ import annotations

import time
import re
from pathlib import Path
from typing import Any, Callable

from .adapters import SupportsPrusaHttp, SupportsPrusaSerialWriter
from .types import PrusaLifecycle, PrusaStatusSnapshot


class PrusaDriver:
    """Hardware driver for bounded Prusa lifecycle and live-tuning actions."""

    RATE_STEP_PCT = 5.0
    SPEED_MIN_PCT = 65.0
    DEFAULT_SPEED_PCT = 100.0
    SPEED_MAX_PCT = 135.0
    SPEED_HTTP_VERIFY_TIMEOUT_S = 45.0
    CANCEL_VERIFY_TIMEOUT_S = 20.0
    CANCEL_STALE_SETTLE_TIMEOUT_S = 15.0
    FLOW_MIN_PCT = 65.0
    DEFAULT_FLOW_PCT = 100.0
    FLOW_MAX_PCT = 135.0
    FLOW_HTTP_VERIFY_TIMEOUT_S = 45.0
    TEMP_STEP_C = 5.0
    _SPEED_RE = re.compile(r"(?:FR|Speed).*?(\d+(?:\.\d+)?)\s*%")
    _FLOW_RE = re.compile(r"Flow:\s*(\d+(?:\.\d+)?)\s*%")

    def __init__(self, *, http: SupportsPrusaHttp, serial_writer: SupportsPrusaSerialWriter, timeout_s: float = 8.0, poll_s: float = 0.25) -> None:
        self.http = http
        self.serial_writer = serial_writer
        self.timeout_s = timeout_s
        self.poll_s = poll_s

    def read_status(self, info: dict[str, Any] | None = None) -> PrusaStatusSnapshot:
        status = self.http.get_status()
        job = self.http.get_job()
        return status_to_snapshot(status, job=job, info=info)

    def pause(self) -> dict[str, Any]:
        status = self.http.get_status()
        self.http.pause_job(job_id=_job_id(status))
        confirmed = self._wait_for(lambda snapshot: snapshot.lifecycle == PrusaLifecycle.PAUSED)
        return {"lifecycle": confirmed.lifecycle.value, "job_id": confirmed.job_id}

    def resume(self) -> dict[str, Any]:
        status = self.http.get_status()
        self.http.resume_job(job_id=_job_id(status))
        confirmed = self._wait_for(lambda snapshot: snapshot.lifecycle == PrusaLifecycle.PRINTING)
        return {"lifecycle": confirmed.lifecycle.value, "job_id": confirmed.job_id}

    def cancel(self) -> dict[str, Any]:
        status = self.http.get_status()
        try:
            self.http.cancel_job(job_id=_job_id(status))
            confirmed = self._wait_for(
                lambda snapshot: (not snapshot.job_active) and snapshot.lifecycle in {PrusaLifecycle.IDLE, PrusaLifecycle.FINISHED, PrusaLifecycle.STOPPED},
                timeout_s=max(self.timeout_s, self.CANCEL_VERIFY_TIMEOUT_S),
            )
        except Exception as exc:
            if not _is_recoverable_control_timeout(exc):
                raise
            snapshot = self._wait_for_cancel_recovery()
            if not _cancel_timeout_recovered(snapshot):
                raise
            return {
                "lifecycle": snapshot.lifecycle.value,
                "job_active": snapshot.job_active,
                "recovered_from_cancel_timeout": True,
            }
        return {"lifecycle": confirmed.lifecycle.value, "job_active": confirmed.job_active}

    def start_print(self, file_path: str) -> dict[str, Any]:
        printer_path, requested_variants = self._resolve_start_print_path(file_path)
        try:
            self.http.start_print(printer_path)
        except Exception as exc:
            if not _is_recoverable_start_timeout(exc):
                raise
            snapshot = self.read_status()
            if not _start_timeout_recovered(snapshot=snapshot, requested_variants=requested_variants):
                raise
            return {
                "lifecycle": snapshot.lifecycle.value,
                "job_active": snapshot.job_active,
                "current_file": snapshot.current_file,
                "recovered_from_start_timeout": True,
            }
        confirmed = self._wait_for(lambda snapshot: snapshot.job_active and snapshot.lifecycle == PrusaLifecycle.PRINTING)
        return {
            "lifecycle": confirmed.lifecycle.value,
            "job_active": confirmed.job_active,
            "current_file": confirmed.current_file,
        }

    def trim_speed_down_small(self, *, target_speed_pct: float) -> dict[str, Any]:
        status = self.read_status()
        self._bind_serial_session(status)
        current = status.speed_pct
        if current is None:
            raise RuntimeError("HTTP status does not expose current speed_pct")
        target = max(self.SPEED_MIN_PCT, float(target_speed_pct))
        if current <= target:
            return {"speed_pct": current, "effect": "noop_already_trimmed"}
        command = f"M220 S{int(round(target))}"
        try:
            self.serial_writer.send_command(command)
            confirmed = self._wait_for_http_speed_factor(target)
        except Exception as exc:
            raise self._tuning_failure(
                exc,
                command=command,
                verification_surface="http_status.printer.speed",
                target_field="speed_pct",
                target_value=target,
            ) from exc
        return {"speed_pct": confirmed, "lifecycle": self.read_status().lifecycle.value}

    def trim_speed_up_small(self, *, target_speed_pct: float) -> dict[str, Any]:
        status = self.read_status()
        self._bind_serial_session(status)
        current = status.speed_pct
        if current is None:
            raise RuntimeError("HTTP status does not expose current speed_pct")
        target = min(self.SPEED_MAX_PCT, float(target_speed_pct))
        if current >= target:
            return {"speed_pct": current, "effect": "noop_already_raised"}
        command = f"M220 S{int(round(target))}"
        try:
            self.serial_writer.send_command(command)
            confirmed = self._wait_for_http_speed_factor(target)
        except Exception as exc:
            raise self._tuning_failure(
                exc,
                command=command,
                verification_surface="http_status.printer.speed",
                target_field="speed_pct",
                target_value=target,
            ) from exc
        return {"speed_pct": confirmed, "lifecycle": self.read_status().lifecycle.value}

    def restore_speed_default(self) -> dict[str, Any]:
        status = self.read_status()
        self._bind_serial_session(status)
        current = status.speed_pct
        if current is None:
            raise RuntimeError("HTTP status does not expose current speed_pct")
        if abs(current - self.DEFAULT_SPEED_PCT) < 0.5:
            return {"speed_pct": current, "effect": "noop_already_default"}
        command = f"M220 S{int(self.DEFAULT_SPEED_PCT)}"
        try:
            self.serial_writer.send_command(command)
            confirmed = self._wait_for_http_speed_factor(self.DEFAULT_SPEED_PCT)
        except Exception as exc:
            raise self._tuning_failure(
                exc,
                command=command,
                verification_surface="http_status.printer.speed",
                target_field="speed_pct",
                target_value=self.DEFAULT_SPEED_PCT,
            ) from exc
        return {"speed_pct": confirmed, "lifecycle": self.read_status().lifecycle.value}

    def trim_flow_down_small(self, *, target_flow_pct: float) -> dict[str, Any]:
        status = self.read_status()
        self._bind_serial_session(status)
        current = status.flow_pct
        if current is None:
            raise RuntimeError("HTTP status does not expose current flow_pct")
        target = max(self.FLOW_MIN_PCT, float(target_flow_pct))
        if current <= target:
            return {"flow_pct": current, "effect": "noop_already_trimmed"}
        command = f"M221 S{int(round(target))}"
        try:
            self.serial_writer.send_command(command)
            confirmed = self._wait_for_flow_factor(target)
        except Exception as exc:
            raise self._tuning_failure(
                exc,
                command=command,
                verification_surface="serial_query:M221->Flow%",
                target_field="flow_pct",
                target_value=target,
            ) from exc
        return {"flow_pct": confirmed, "lifecycle": self.read_status().lifecycle.value}

    def trim_flow_up_small(self, *, target_flow_pct: float) -> dict[str, Any]:
        status = self.read_status()
        self._bind_serial_session(status)
        current = status.flow_pct
        if current is None:
            raise RuntimeError("HTTP status does not expose current flow_pct")
        target = min(self.FLOW_MAX_PCT, float(target_flow_pct))
        if current >= target:
            return {"flow_pct": current, "effect": "noop_already_raised"}
        command = f"M221 S{int(round(target))}"
        try:
            self.serial_writer.send_command(command)
            confirmed = self._wait_for_flow_factor(target)
        except Exception as exc:
            raise self._tuning_failure(
                exc,
                command=command,
                verification_surface="serial_query:M221->Flow%",
                target_field="flow_pct",
                target_value=target,
            ) from exc
        return {"flow_pct": confirmed, "lifecycle": self.read_status().lifecycle.value}

    def restore_flow_default(self) -> dict[str, Any]:
        status = self.read_status()
        self._bind_serial_session(status)
        current = status.flow_pct
        if current is None:
            raise RuntimeError("HTTP status does not expose current flow_pct")
        if abs(current - self.DEFAULT_FLOW_PCT) < 0.5:
            return {"flow_pct": current, "effect": "noop_already_default"}
        command = f"M221 S{int(self.DEFAULT_FLOW_PCT)}"
        try:
            self.serial_writer.send_command(command)
            confirmed = self._wait_for_flow_factor(self.DEFAULT_FLOW_PCT)
        except Exception as exc:
            raise self._tuning_failure(
                exc,
                command=command,
                verification_surface="serial_query:M221->Flow%",
                target_field="flow_pct",
                target_value=self.DEFAULT_FLOW_PCT,
            ) from exc
        return {"flow_pct": confirmed, "lifecycle": self.read_status().lifecycle.value}

    def trim_nozzle_down_small(self, *, target_nozzle_c: float, min_nozzle_target_c: float) -> dict[str, Any]:
        status = self.read_status()
        self._bind_serial_session(status)
        current = status.nozzle_target_c
        if current is None:
            raise RuntimeError("HTTP status does not expose target nozzle temperature")
        target = max(min_nozzle_target_c, float(target_nozzle_c))
        if abs(target - current) < 0.1:
            return {"target_nozzle_c": current, "effect": "noop_floor_reached"}
        command = f"M104 S{int(round(target))}"
        try:
            self.serial_writer.send_command(command)
            confirmed = self._wait_for(lambda snapshot: snapshot.nozzle_target_c is not None and abs(snapshot.nozzle_target_c - target) < 0.5)
        except Exception as exc:
            raise self._tuning_failure(
                exc,
                command=command,
                verification_surface="http_status.printer.target_nozzle",
                target_field="nozzle_target_c",
                target_value=target,
            ) from exc
        return {"target_nozzle_c": confirmed.nozzle_target_c, "lifecycle": confirmed.lifecycle.value}

    def trim_nozzle_up_small(self, *, target_nozzle_c: float, max_nozzle_target_c: float) -> dict[str, Any]:
        status = self.read_status()
        self._bind_serial_session(status)
        current = status.nozzle_target_c
        if current is None:
            raise RuntimeError("HTTP status does not expose target nozzle temperature")
        target = min(max_nozzle_target_c, float(target_nozzle_c))
        if abs(target - current) < 0.1:
            return {"target_nozzle_c": current, "effect": "noop_ceiling_reached"}
        command = f"M104 S{int(round(target))}"
        try:
            self.serial_writer.send_command(command)
            confirmed = self._wait_for(lambda snapshot: snapshot.nozzle_target_c is not None and abs(snapshot.nozzle_target_c - target) < 0.5)
        except Exception as exc:
            raise self._tuning_failure(
                exc,
                command=command,
                verification_surface="http_status.printer.target_nozzle",
                target_field="nozzle_target_c",
                target_value=target,
            ) from exc
        return {"target_nozzle_c": confirmed.nozzle_target_c, "lifecycle": confirmed.lifecycle.value}

    def trim_bed_down_small(self, *, target_bed_c: float) -> dict[str, Any]:
        status = self.read_status()
        self._bind_serial_session(status)
        current = status.bed_target_c
        if current is None:
            raise RuntimeError("HTTP status does not expose target bed temperature")
        target = max(0.0, float(target_bed_c))
        if abs(target - current) < 0.1:
            return {"target_bed_c": current, "effect": "noop_floor_reached"}
        command = f"M140 S{int(round(target))}"
        try:
            self.serial_writer.send_command(command)
            confirmed = self._wait_for(lambda snapshot: snapshot.bed_target_c is not None and abs(snapshot.bed_target_c - target) < 0.5)
        except Exception as exc:
            raise self._tuning_failure(
                exc,
                command=command,
                verification_surface="http_status.printer.target_bed",
                target_field="bed_target_c",
                target_value=target,
            ) from exc
        return {"target_bed_c": confirmed.bed_target_c, "lifecycle": confirmed.lifecycle.value}

    def trim_bed_up_small(self, *, target_bed_c: float, max_bed_target_c: float) -> dict[str, Any]:
        status = self.read_status()
        self._bind_serial_session(status)
        current = status.bed_target_c
        if current is None:
            raise RuntimeError("HTTP status does not expose target bed temperature")
        target = min(max_bed_target_c, float(target_bed_c))
        if abs(target - current) < 0.1:
            return {"target_bed_c": current, "effect": "noop_ceiling_reached"}
        command = f"M140 S{int(round(target))}"
        try:
            self.serial_writer.send_command(command)
            confirmed = self._wait_for(lambda snapshot: snapshot.bed_target_c is not None and abs(snapshot.bed_target_c - target) < 0.5)
        except Exception as exc:
            raise self._tuning_failure(
                exc,
                command=command,
                verification_surface="http_status.printer.target_bed",
                target_field="bed_target_c",
                target_value=target,
            ) from exc
        return {"target_bed_c": confirmed.bed_target_c, "lifecycle": confirmed.lifecycle.value}

    def _wait_for(self, predicate: Callable[[PrusaStatusSnapshot], bool], *, timeout_s: float | None = None) -> PrusaStatusSnapshot:
        deadline = time.monotonic() + (self.timeout_s if timeout_s is None else timeout_s)
        last_snapshot: PrusaStatusSnapshot | None = None
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                snapshot = self.read_status()
            except Exception as exc:
                if not _is_transient_read_error(exc):
                    raise
                last_error = exc
            else:
                last_snapshot = snapshot
                if predicate(snapshot):
                    return snapshot
            time.sleep(self.poll_s)
        detail = f"last={last_snapshot!r}" if last_snapshot is not None else f"last_error={last_error!r}"
        raise RuntimeError(f"Prusa did not reach expected post-state before timeout; {detail}")

    def read_speed_factor_pct(self) -> float | None:
        self._bind_serial_session(self.read_status())
        return self._read_rate_factor("M220", pattern=self._SPEED_RE)

    def read_flow_factor_pct(self) -> float | None:
        self._bind_serial_session(self.read_status())
        return self._read_rate_factor("M221", pattern=self._FLOW_RE)

    def _wait_for_speed_factor(self, target_pct: float) -> float:
        return self._wait_for_rate_factor(lambda: self.read_speed_factor_pct(), target_pct=target_pct, label="speed_pct")

    def _wait_for_flow_factor(self, target_pct: float) -> float:
        try:
            return self._wait_for_rate_factor(lambda: self.read_flow_factor_pct(), target_pct=target_pct, label="flow_pct")
        except Exception:
            confirmed = self._wait_for(
                lambda snapshot: snapshot.flow_pct is not None and abs(snapshot.flow_pct - target_pct) < 0.5,
                timeout_s=max(self.timeout_s, self.FLOW_HTTP_VERIFY_TIMEOUT_S),
            )
            assert confirmed.flow_pct is not None
            return confirmed.flow_pct

    def _wait_for_http_speed_factor(self, target_pct: float) -> float:
        confirmed = self._wait_for(
            lambda snapshot: snapshot.speed_pct is not None and abs(snapshot.speed_pct - target_pct) < 0.5,
            timeout_s=max(self.timeout_s, self.SPEED_HTTP_VERIFY_TIMEOUT_S),
        )
        assert confirmed.speed_pct is not None
        return confirmed.speed_pct

    def _wait_for_cancel_recovery(self) -> PrusaStatusSnapshot:
        deadline = time.monotonic() + max(self.timeout_s, self.CANCEL_STALE_SETTLE_TIMEOUT_S)
        last_snapshot: PrusaStatusSnapshot | None = None
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                snapshot = self.read_status()
            except Exception as exc:
                if not _is_transient_read_error(exc):
                    raise
                last_error = exc
                time.sleep(self.poll_s)
                continue
            last_snapshot = snapshot
            if _cancel_timeout_recovered(snapshot):
                return snapshot
            if not _is_stale_cancel_surface(snapshot):
                break
            time.sleep(self.poll_s)
        if last_snapshot is not None:
            return last_snapshot
        if last_error is not None:
            raise last_error
        raise RuntimeError("cancel recovery did not produce a status snapshot")

    def close(self) -> None:
        self.serial_writer.close()

    def _bind_serial_session(self, status: PrusaStatusSnapshot) -> None:
        job_key = _session_key_for_status(status)
        self.serial_writer.bind_session(job_key)

    def _resolve_start_print_path(self, file_path: str) -> tuple[str, tuple[str, ...]]:
        requested = str(file_path).strip()
        variants = _requested_file_variants(requested)
        if not requested:
            return requested, variants
        requested_tokens = {_canonical_file_token(requested), _canonical_file_token(Path(requested).name)}
        try:
            known_files = self.http.list_usb_files()
        except Exception:
            return requested, variants
        for item in known_files:
            item_tokens = {
                _canonical_file_token(item.path),
                _canonical_file_token(Path(item.path).name),
                _canonical_file_token(item.display_name),
            }
            if any(token and token in item_tokens for token in requested_tokens):
                return item.path, _requested_file_variants(requested, item.path, item.display_name)
        return requested, variants

    def _wait_for_rate_factor(self, reader: Callable[[], float | None], *, target_pct: float, label: str) -> float:
        deadline = time.monotonic() + self.timeout_s
        last_value: float | None = None
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                last_value = reader()
            except Exception as exc:
                last_error = exc
            else:
                if last_value is not None and abs(last_value - target_pct) < 0.5:
                    return last_value
            time.sleep(self.poll_s)
        detail = f"last_{label}={last_value!r}" if last_error is None else f"last_error={last_error!r}"
        raise RuntimeError(f"Prusa did not reach expected {label} before timeout; {detail}")

    def _read_rate_factor(self, command: str, *, pattern: re.Pattern[str]) -> float | None:
        lines = self.serial_writer.query_command(command)
        for line in lines:
            match = pattern.search(line)
            if match:
                return float(match.group(1))
        return None

    def _tuning_failure(
        self,
        exc: Exception,
        *,
        command: str,
        verification_surface: str,
        target_field: str,
        target_value: float,
    ) -> Exception:
        parts = [
            f"send_command={command}",
            f"acceptance_surface=serial:{self._serial_surface_label()}",
            f"verification_surface={verification_surface}",
            f"target_{target_field}={target_value}",
        ]
        try:
            snapshot = self.read_status()
        except Exception as status_exc:
            parts.append(f"status_read_error={status_exc!r}")
        else:
            parts.extend(
                [
                    f"http_lifecycle={snapshot.lifecycle.value}",
                    f"http_job_progress_pct={snapshot.job_progress_pct!r}",
                    f"http_speed_pct={snapshot.speed_pct!r}",
                    f"http_flow_pct={snapshot.flow_pct!r}",
                    f"http_nozzle_target_c={snapshot.nozzle_target_c!r}",
                    f"http_bed_target_c={snapshot.bed_target_c!r}",
                ]
            )
        parts.append(f"cause={exc}")
        message = "; ".join(parts)
        if isinstance(exc, TimeoutError):
            return TimeoutError(message)
        return RuntimeError(message)

    def _serial_surface_label(self) -> str:
        settings = getattr(self.serial_writer, "settings", None)
        port = getattr(settings, "serial_port", None)
        return str(port or "unknown_port")


def status_to_snapshot(
    status: dict[str, Any],
    *,
    job: dict[str, Any] | None = None,
    info: dict[str, Any] | None = None,
) -> PrusaStatusSnapshot:
    """Normalize the HTTP status payload into a stable snapshot."""
    printer = status.get("printer") or {}
    status_job = status.get("job") or {}
    effective_job = dict(status_job) if isinstance(status_job, dict) else {}
    if isinstance(job, dict):
        for key, value in job.items():
            if value is not None:
                effective_job[key] = value
    info = info or {}
    lifecycle = _map_lifecycle(status)
    health = "OFFLINE" if lifecycle == PrusaLifecycle.OFFLINE else (
        lifecycle.value if lifecycle in {PrusaLifecycle.ERROR, PrusaLifecycle.ATTENTION} else "OK"
    )
    current_file = None
    if isinstance(effective_job.get("file"), dict):
        current_file = _string_or_none(effective_job["file"].get("display_name") or effective_job["file"].get("name"))
    return PrusaStatusSnapshot(
        lifecycle=lifecycle,
        health=health,
        job_active=_job_active(status),
        job_id=_job_id(status),
        job_state=_string_or_none(effective_job.get("state")),
        job_progress_pct=_float_or_none(effective_job.get("progress")),
        job_time_printing_s=_float_or_none(effective_job.get("time_printing")),
        current_file=current_file,
        speed_pct=_float_or_none(printer.get("speed")),
        flow_pct=_float_or_none(printer.get("flow")),
        nozzle_temp_c=_float_or_none(printer.get("temp_nozzle")),
        nozzle_target_c=_float_or_none(printer.get("target_nozzle")),
        bed_temp_c=_float_or_none(printer.get("temp_bed")),
        bed_target_c=_float_or_none(printer.get("target_bed")),
        min_extrusion_temp_c=_float_or_none(info.get("min_extrusion_temp")),
        nozzle_diameter_mm=_float_or_none(info.get("nozzle_diameter")),
        model=_string_or_none(info.get("model") or info.get("hostname")),
        serial_number=_string_or_none(info.get("serial")),
    )


def _map_lifecycle(status: dict[str, Any]) -> PrusaLifecycle:
    state = str((status.get("printer") or {}).get("state") or "UNKNOWN").upper()
    mapping = {
        "IDLE": PrusaLifecycle.IDLE,
        "PRINTING": PrusaLifecycle.PRINTING,
        "PAUSED": PrusaLifecycle.PAUSED,
        "FINISHED": PrusaLifecycle.FINISHED,
        "STOPPED": PrusaLifecycle.STOPPED,
        "ATTENTION": PrusaLifecycle.ATTENTION,
        "ERROR": PrusaLifecycle.ERROR,
    }
    return mapping.get(state, PrusaLifecycle.UNKNOWN)


def _job_active(status: dict[str, Any]) -> bool:
    job = status.get("job")
    if not isinstance(job, dict):
        return False
    state = str(job.get("state") or "").upper()
    return state in {"PRINTING", "PAUSED", "PREPARING", "BUSY"} or bool(job.get("id"))


def _job_id(status: dict[str, Any]) -> int | None:
    job = status.get("job")
    if isinstance(job, dict) and isinstance(job.get("id"), int):
        return int(job["id"])
    return None


def _session_key_for_status(status: PrusaStatusSnapshot) -> str:
    if status.job_id is not None:
        return f"job:{status.job_id}"
    if status.current_file:
        return f"file:{Path(status.current_file).name}"
    return f"lifecycle:{status.lifecycle.value}"


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_recoverable_start_timeout(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    text = str(exc).lower()
    return "timed out" in text and "start" not in text


def _start_timeout_recovered(*, snapshot: PrusaStatusSnapshot, requested_variants: tuple[str, ...]) -> bool:
    if snapshot.lifecycle != PrusaLifecycle.PRINTING or not snapshot.job_active:
        return False
    if not snapshot.current_file:
        return False
    current_tokens = {
        _canonical_file_token(snapshot.current_file),
        _canonical_file_token(Path(snapshot.current_file).name),
    }
    for requested in requested_variants:
        token = _canonical_file_token(requested)
        if token and token in current_tokens:
            return True
    return False


def _cancel_timeout_recovered(snapshot: PrusaStatusSnapshot) -> bool:
    return (not snapshot.job_active) and snapshot.lifecycle in {
        PrusaLifecycle.IDLE,
        PrusaLifecycle.FINISHED,
        PrusaLifecycle.STOPPED,
    }


def _is_stale_cancel_surface(snapshot: PrusaStatusSnapshot) -> bool:
    return (
        snapshot.job_active
        and snapshot.lifecycle == PrusaLifecycle.PRINTING
        and (snapshot.nozzle_target_c or 0.0) <= 0.0
        and (snapshot.bed_target_c or 0.0) <= 0.0
    )


def _is_recoverable_control_timeout(exc: Exception) -> bool:
    return _is_transient_read_error(exc)


def _is_transient_read_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    text = str(exc).lower()
    transient_markers = ("timed out", "temporary failure", "connection reset", "connection refused")
    return any(marker in text for marker in transient_markers)


def _requested_file_variants(*values: str | None) -> tuple[str, ...]:
    variants: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        for candidate in (text, Path(text).name):
            normalized = candidate.strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                variants.append(normalized)
    return tuple(variants)


def _canonical_file_token(value: str | None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return Path(text).name.lower()
