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

from ...coerce import float_or_none as _float_or_none, string_or_none as _string_or_none
from .adapters import PrusaCoreOneSettings, SupportsPrusaHttp, SupportsPrusaSerialWriter
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
    TEMP_BIG_STEP_C = 10.0
    PRESSURE_ADVANCE_STEP_PCT = 10.0
    PRESSURE_ADVANCE_BIG_STEP_PCT = 20.0
    PRESSURE_ADVANCE_ENVELOPE_PCT = 25.0
    PRESSURE_ADVANCE_MIN = 0.0
    PRESSURE_ADVANCE_MAX = 0.12
    ACCEL_STEP_PCT = 10.0
    ACCEL_BIG_STEP_PCT = 20.0
    ACCEL_ENVELOPE_PCT = 25.0
    ACCEL_MIN_MM_S2 = 500.0
    ACCEL_MAX_MM_S2 = 12000.0
    _SPEED_RE = re.compile(r"(?:FR|Speed).*?(\d+(?:\.\d+)?)\s*%")
    _FLOW_RE = re.compile(r"Flow:\s*(\d+(?:\.\d+)?)\s*%")
    _PRESSURE_ADVANCE_RE = re.compile(r"(?:\bM572\b[^\r\n;]*\bS|pressure advance[^0-9-]*)(-?\d+(?:\.\d+)?)", re.IGNORECASE)
    _ACCEL_RE = re.compile(r"(?:\bM204\b[^\r\n;]*\b[PS]|acceleration[^0-9-]*)(-?\d+(?:\.\d+)?)", re.IGNORECASE)

    def __init__(
        self,
        *,
        http: SupportsPrusaHttp,
        serial_writer: SupportsPrusaSerialWriter,
        timeout_s: float = 8.0,
        poll_s: float = 0.25,
        settings: PrusaCoreOneSettings | None = None,
    ) -> None:
        self.http = http
        self.serial_writer = serial_writer
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        tuning = settings
        self.speed_small_step_pct = tuning.speed_small_step_pct if tuning is not None else self.RATE_STEP_PCT
        self.speed_big_step_pct = tuning.speed_big_step_pct if tuning is not None else self.RATE_STEP_PCT * 2
        self.speed_min_pct = tuning.speed_min_pct if tuning is not None else self.SPEED_MIN_PCT
        self.speed_default_pct = tuning.speed_default_pct if tuning is not None else self.DEFAULT_SPEED_PCT
        self.speed_max_pct = tuning.speed_max_pct if tuning is not None else self.SPEED_MAX_PCT
        self.flow_small_step_pct = tuning.flow_small_step_pct if tuning is not None else self.RATE_STEP_PCT
        self.flow_big_step_pct = tuning.flow_big_step_pct if tuning is not None else self.RATE_STEP_PCT * 2
        self.flow_min_pct = tuning.flow_min_pct if tuning is not None else self.FLOW_MIN_PCT
        self.flow_default_pct = tuning.flow_default_pct if tuning is not None else self.DEFAULT_FLOW_PCT
        self.flow_max_pct = tuning.flow_max_pct if tuning is not None else self.FLOW_MAX_PCT
        self.temp_small_step_c = tuning.temp_small_step_c if tuning is not None else self.TEMP_STEP_C
        self.temp_big_step_c = tuning.temp_big_step_c if tuning is not None else self.TEMP_BIG_STEP_C
        self.pressure_advance_small_step_pct = (
            tuning.pressure_advance_small_step_pct if tuning is not None else self.PRESSURE_ADVANCE_STEP_PCT
        )
        self.pressure_advance_big_step_pct = (
            tuning.pressure_advance_big_step_pct if tuning is not None else self.PRESSURE_ADVANCE_BIG_STEP_PCT
        )
        self.pressure_advance_envelope_pct = (
            tuning.pressure_advance_envelope_pct if tuning is not None else self.PRESSURE_ADVANCE_ENVELOPE_PCT
        )
        self.pressure_advance_min = tuning.pressure_advance_min if tuning is not None else self.PRESSURE_ADVANCE_MIN
        self.pressure_advance_max = tuning.pressure_advance_max if tuning is not None else self.PRESSURE_ADVANCE_MAX
        self.accel_small_step_pct = tuning.accel_small_step_pct if tuning is not None else self.ACCEL_STEP_PCT
        self.accel_big_step_pct = tuning.accel_big_step_pct if tuning is not None else self.ACCEL_BIG_STEP_PCT
        self.accel_envelope_pct = tuning.accel_envelope_pct if tuning is not None else self.ACCEL_ENVELOPE_PCT
        self.accel_min_mm_s2 = tuning.accel_min_mm_s2 if tuning is not None else self.ACCEL_MIN_MM_S2
        self.accel_max_mm_s2 = tuning.accel_max_mm_s2 if tuning is not None else self.ACCEL_MAX_MM_S2

    def pressure_advance_target_from_baseline(
        self,
        baseline: float,
        *,
        direction: str,
        magnitude: str,
    ) -> float:
        step_pct = self.pressure_advance_big_step_pct if magnitude == "big" else self.pressure_advance_small_step_pct
        factor = 1.0 + (step_pct / 100.0) if direction == "up" else 1.0 - (step_pct / 100.0)
        target = baseline * factor
        floor, ceiling = self.pressure_advance_bounds_for_baseline(baseline)
        return min(ceiling, max(floor, target))

    def print_accel_target_from_baseline(
        self,
        baseline: float,
        *,
        direction: str,
        magnitude: str,
    ) -> float:
        step_pct = self.accel_big_step_pct if magnitude == "big" else self.accel_small_step_pct
        factor = 1.0 + (step_pct / 100.0) if direction == "up" else 1.0 - (step_pct / 100.0)
        target = baseline * factor
        floor, ceiling = self.print_accel_bounds_for_baseline(baseline)
        return min(ceiling, max(floor, target))

    def pressure_advance_bounds_for_baseline(self, baseline: float) -> tuple[float, float]:
        return self._relative_envelope_bounds(
            baseline,
            envelope_pct=self.pressure_advance_envelope_pct,
            floor=self.pressure_advance_min,
            ceiling=self.pressure_advance_max,
        )

    def print_accel_bounds_for_baseline(self, baseline: float) -> tuple[float, float]:
        return self._relative_envelope_bounds(
            baseline,
            envelope_pct=self.accel_envelope_pct,
            floor=self.accel_min_mm_s2,
            ceiling=self.accel_max_mm_s2,
        )

    @staticmethod
    def _relative_envelope_bounds(
        baseline: float,
        *,
        envelope_pct: float,
        floor: float,
        ceiling: float,
    ) -> tuple[float, float]:
        factor = max(0.0, envelope_pct) / 100.0
        return max(floor, baseline * (1.0 - factor)), min(ceiling, baseline * (1.0 + factor))

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
        return self._set_speed_factor(target_speed_pct=target_speed_pct, clamp_min=self.speed_min_pct, effect="noop_already_trimmed")

    def trim_speed_down_big(self, *, target_speed_pct: float) -> dict[str, Any]:
        return self._set_speed_factor(target_speed_pct=target_speed_pct, clamp_min=self.speed_min_pct, effect="noop_already_trimmed")

    def trim_speed_up_small(self, *, target_speed_pct: float) -> dict[str, Any]:
        return self._set_speed_factor(target_speed_pct=target_speed_pct, clamp_max=self.speed_max_pct, effect="noop_already_raised")

    def trim_speed_up_big(self, *, target_speed_pct: float) -> dict[str, Any]:
        return self._set_speed_factor(target_speed_pct=target_speed_pct, clamp_max=self.speed_max_pct, effect="noop_already_raised")

    def restore_speed_default(self) -> dict[str, Any]:
        status = self.read_status()
        self._bind_serial_session(status)
        current = status.speed_pct
        if current is None:
            raise RuntimeError("HTTP status does not expose current speed_pct")
        if abs(current - self.speed_default_pct) < 0.5:
            return {"speed_pct": current, "effect": "noop_already_default"}
        command = f"M220 S{int(self.speed_default_pct)}"
        try:
            self.serial_writer.send_command(command)
            confirmed = self._wait_for_http_speed_factor(self.speed_default_pct)
        except Exception as exc:
            raise self._tuning_failure(
                exc,
                command=command,
                verification_surface="http_status.printer.speed",
                target_field="speed_pct",
                target_value=self.speed_default_pct,
            ) from exc
        return {"speed_pct": confirmed, "lifecycle": self.read_status().lifecycle.value}

    def _set_speed_factor(
        self,
        *,
        target_speed_pct: float,
        clamp_min: float | None = None,
        clamp_max: float | None = None,
        effect: str,
    ) -> dict[str, Any]:
        status = self.read_status()
        self._bind_serial_session(status)
        current = status.speed_pct
        if current is None:
            raise RuntimeError("HTTP status does not expose current speed_pct")
        target = float(target_speed_pct)
        if clamp_min is not None:
            target = max(clamp_min, target)
            if current <= target:
                return {"speed_pct": current, "effect": effect}
        if clamp_max is not None:
            target = min(clamp_max, target)
            if current >= target:
                return {"speed_pct": current, "effect": effect}
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

    def trim_flow_down_small(self, *, target_flow_pct: float) -> dict[str, Any]:
        return self._set_flow_factor(target_flow_pct=target_flow_pct, clamp_min=self.flow_min_pct, effect="noop_already_trimmed")

    def trim_flow_down_big(self, *, target_flow_pct: float) -> dict[str, Any]:
        return self._set_flow_factor(target_flow_pct=target_flow_pct, clamp_min=self.flow_min_pct, effect="noop_already_trimmed")

    def trim_flow_up_small(self, *, target_flow_pct: float) -> dict[str, Any]:
        return self._set_flow_factor(target_flow_pct=target_flow_pct, clamp_max=self.flow_max_pct, effect="noop_already_raised")

    def trim_flow_up_big(self, *, target_flow_pct: float) -> dict[str, Any]:
        return self._set_flow_factor(target_flow_pct=target_flow_pct, clamp_max=self.flow_max_pct, effect="noop_already_raised")

    def restore_flow_default(self) -> dict[str, Any]:
        status = self.read_status()
        self._bind_serial_session(status)
        current = status.flow_pct
        if current is None:
            raise RuntimeError("HTTP status does not expose current flow_pct")
        if abs(current - self.flow_default_pct) < 0.5:
            return {"flow_pct": current, "effect": "noop_already_default"}
        command = f"M221 S{int(self.flow_default_pct)}"
        try:
            self.serial_writer.send_command(command)
            confirmed = self._wait_for_flow_factor(self.flow_default_pct)
        except Exception as exc:
            raise self._tuning_failure(
                exc,
                command=command,
                verification_surface="serial_query:M221->Flow%",
                target_field="flow_pct",
                target_value=self.flow_default_pct,
            ) from exc
        return {"flow_pct": confirmed, "lifecycle": self.read_status().lifecycle.value}

    def _set_flow_factor(
        self,
        *,
        target_flow_pct: float,
        clamp_min: float | None = None,
        clamp_max: float | None = None,
        effect: str,
    ) -> dict[str, Any]:
        status = self.read_status()
        self._bind_serial_session(status)
        current = status.flow_pct
        if current is None:
            raise RuntimeError("HTTP status does not expose current flow_pct")
        target = float(target_flow_pct)
        if clamp_min is not None:
            target = max(clamp_min, target)
            if current <= target:
                return {"flow_pct": current, "effect": effect}
        if clamp_max is not None:
            target = min(clamp_max, target)
            if current >= target:
                return {"flow_pct": current, "effect": effect}
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

    def trim_nozzle_down_small(self, *, target_nozzle_c: float, min_nozzle_target_c: float) -> dict[str, Any]:
        return self._set_nozzle_target(target_nozzle_c=target_nozzle_c, clamp_min=min_nozzle_target_c, effect="noop_floor_reached")

    def trim_nozzle_down_big(self, *, target_nozzle_c: float, min_nozzle_target_c: float) -> dict[str, Any]:
        return self._set_nozzle_target(target_nozzle_c=target_nozzle_c, clamp_min=min_nozzle_target_c, effect="noop_floor_reached")

    def trim_nozzle_up_small(self, *, target_nozzle_c: float, max_nozzle_target_c: float) -> dict[str, Any]:
        return self._set_nozzle_target(target_nozzle_c=target_nozzle_c, clamp_max=max_nozzle_target_c, effect="noop_ceiling_reached")

    def trim_nozzle_up_big(self, *, target_nozzle_c: float, max_nozzle_target_c: float) -> dict[str, Any]:
        return self._set_nozzle_target(target_nozzle_c=target_nozzle_c, clamp_max=max_nozzle_target_c, effect="noop_ceiling_reached")

    def _set_nozzle_target(
        self,
        *,
        target_nozzle_c: float,
        clamp_min: float | None = None,
        clamp_max: float | None = None,
        effect: str,
    ) -> dict[str, Any]:
        status = self.read_status()
        self._bind_serial_session(status)
        current = status.nozzle_target_c
        if current is None:
            raise RuntimeError("HTTP status does not expose target nozzle temperature")
        target = float(target_nozzle_c)
        if clamp_min is not None:
            target = max(clamp_min, target)
        if clamp_max is not None:
            target = min(clamp_max, target)
        if abs(target - current) < 0.1:
            return {"target_nozzle_c": current, "effect": effect}
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
        return self._set_bed_target(target_bed_c=target_bed_c, clamp_min=0.0, effect="noop_floor_reached")

    def trim_bed_down_big(self, *, target_bed_c: float) -> dict[str, Any]:
        return self._set_bed_target(target_bed_c=target_bed_c, clamp_min=0.0, effect="noop_floor_reached")

    def trim_bed_up_small(self, *, target_bed_c: float, max_bed_target_c: float) -> dict[str, Any]:
        return self._set_bed_target(target_bed_c=target_bed_c, clamp_max=max_bed_target_c, effect="noop_ceiling_reached")

    def trim_bed_up_big(self, *, target_bed_c: float, max_bed_target_c: float) -> dict[str, Any]:
        return self._set_bed_target(target_bed_c=target_bed_c, clamp_max=max_bed_target_c, effect="noop_ceiling_reached")

    def _set_bed_target(
        self,
        *,
        target_bed_c: float,
        clamp_min: float | None = None,
        clamp_max: float | None = None,
        effect: str,
    ) -> dict[str, Any]:
        status = self.read_status()
        self._bind_serial_session(status)
        current = status.bed_target_c
        if current is None:
            raise RuntimeError("HTTP status does not expose target bed temperature")
        target = float(target_bed_c)
        if clamp_min is not None:
            target = max(clamp_min, target)
        if clamp_max is not None:
            target = min(clamp_max, target)
        if abs(target - current) < 0.1:
            return {"target_bed_c": current, "effect": effect}
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

    def trim_pressure_advance_down_small(self, *, target_pressure_advance: float) -> dict[str, Any]:
        return self._set_pressure_advance(target_pressure_advance=target_pressure_advance, clamp_min=self.pressure_advance_min, effect="noop_floor_reached")

    def trim_pressure_advance_down_big(self, *, target_pressure_advance: float) -> dict[str, Any]:
        return self._set_pressure_advance(target_pressure_advance=target_pressure_advance, clamp_min=self.pressure_advance_min, effect="noop_floor_reached")

    def trim_pressure_advance_up_small(self, *, target_pressure_advance: float) -> dict[str, Any]:
        return self._set_pressure_advance(target_pressure_advance=target_pressure_advance, clamp_max=self.pressure_advance_max, effect="noop_ceiling_reached")

    def trim_pressure_advance_up_big(self, *, target_pressure_advance: float) -> dict[str, Any]:
        return self._set_pressure_advance(target_pressure_advance=target_pressure_advance, clamp_max=self.pressure_advance_max, effect="noop_ceiling_reached")

    def restore_pressure_advance_default(self, *, target_pressure_advance: float) -> dict[str, Any]:
        return self._set_pressure_advance(target_pressure_advance=target_pressure_advance, effect="noop_already_default")

    def _set_pressure_advance(
        self,
        *,
        target_pressure_advance: float,
        clamp_min: float | None = None,
        clamp_max: float | None = None,
        effect: str,
    ) -> dict[str, Any]:
        status = self.read_status()
        self._bind_serial_session(status)
        current = self.read_pressure_advance()
        if current is None:
            raise RuntimeError("serial query does not expose current pressure advance")
        target = float(target_pressure_advance)
        if clamp_min is not None:
            target = max(clamp_min, target)
        if clamp_max is not None:
            target = min(clamp_max, target)
        if abs(target - current) < 0.0005:
            return {"pressure_advance": round(current, 4), "effect": effect}
        command = f"M572 S{target:.4f}"
        try:
            self.serial_writer.send_command(command)
            confirmed = self._wait_for_pressure_advance(target)
        except Exception as exc:
            raise self._tuning_failure(
                exc,
                command=command,
                verification_surface="serial_query:M572->pressure_advance",
                target_field="pressure_advance",
                target_value=target,
            ) from exc
        return {"pressure_advance": round(confirmed, 4), "lifecycle": status.lifecycle.value}

    def trim_print_accel_down_small(self, *, target_print_accel_mm_s2: float) -> dict[str, Any]:
        return self._set_print_accel(target_print_accel_mm_s2=target_print_accel_mm_s2, clamp_min=self.accel_min_mm_s2, effect="noop_floor_reached")

    def trim_print_accel_down_big(self, *, target_print_accel_mm_s2: float) -> dict[str, Any]:
        return self._set_print_accel(target_print_accel_mm_s2=target_print_accel_mm_s2, clamp_min=self.accel_min_mm_s2, effect="noop_floor_reached")

    def trim_print_accel_up_small(self, *, target_print_accel_mm_s2: float) -> dict[str, Any]:
        return self._set_print_accel(target_print_accel_mm_s2=target_print_accel_mm_s2, clamp_max=self.accel_max_mm_s2, effect="noop_ceiling_reached")

    def trim_print_accel_up_big(self, *, target_print_accel_mm_s2: float) -> dict[str, Any]:
        return self._set_print_accel(target_print_accel_mm_s2=target_print_accel_mm_s2, clamp_max=self.accel_max_mm_s2, effect="noop_ceiling_reached")

    def restore_print_accel_default(self, *, target_print_accel_mm_s2: float) -> dict[str, Any]:
        return self._set_print_accel(target_print_accel_mm_s2=target_print_accel_mm_s2, effect="noop_already_default")

    def _set_print_accel(
        self,
        *,
        target_print_accel_mm_s2: float,
        clamp_min: float | None = None,
        clamp_max: float | None = None,
        effect: str,
    ) -> dict[str, Any]:
        status = self.read_status()
        self._bind_serial_session(status)
        current = self.read_print_accel_mm_s2()
        if current is None:
            raise RuntimeError("serial query does not expose current print acceleration")
        target = float(target_print_accel_mm_s2)
        if clamp_min is not None:
            target = max(clamp_min, target)
        if clamp_max is not None:
            target = min(clamp_max, target)
        if abs(target - current) < 0.5:
            return {"print_accel_mm_s2": round(current, 1), "effect": effect}
        command = f"M204 P{int(round(target))}"
        try:
            self.serial_writer.send_command(command)
            confirmed = self._wait_for_print_accel(target)
        except Exception as exc:
            raise self._tuning_failure(
                exc,
                command=command,
                verification_surface="serial_query:M204->print_accel_P",
                target_field="print_accel_mm_s2",
                target_value=target,
            ) from exc
        return {"print_accel_mm_s2": round(confirmed, 1), "lifecycle": status.lifecycle.value}

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

    def read_pressure_advance(self) -> float | None:
        self._bind_serial_session(self.read_status())
        lines = self.serial_writer.query_command("M572")
        for line in lines:
            if "pressure advance" in line.lower() and "disabled" in line.lower():
                return 0.0
            match = self._PRESSURE_ADVANCE_RE.search(line)
            if match:
                return float(match.group(1))
        return None

    def read_print_accel_mm_s2(self) -> float | None:
        self._bind_serial_session(self.read_status())
        return self._read_rate_factor("M204", pattern=self._ACCEL_RE)

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

    def _wait_for_pressure_advance(self, target_value: float) -> float:
        return self._wait_for_rate_factor(
            lambda: self.read_pressure_advance(),
            target_pct=target_value,
            label="pressure_advance",
            tolerance=0.0005,
        )

    def _wait_for_print_accel(self, target_value: float) -> float:
        return self._wait_for_rate_factor(
            lambda: self.read_print_accel_mm_s2(),
            target_pct=target_value,
            label="print_accel_mm_s2",
            tolerance=0.5,
        )

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

    def _wait_for_rate_factor(
        self,
        reader: Callable[[], float | None],
        *,
        target_pct: float,
        label: str,
        tolerance: float = 0.5,
    ) -> float:
        deadline = time.monotonic() + self.timeout_s
        last_value: float | None = None
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                last_value = reader()
            except Exception as exc:
                last_error = exc
            else:
                if last_value is not None and abs(last_value - target_pct) < tolerance:
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
        pressure_advance=None,
        print_accel_mm_s2=None,
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
