from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from wallee.packs.prusa_core_one_plus.driver import PrusaDriver, status_to_snapshot
from wallee.packs.prusa_core_one_plus.types import PrintableFile, PrusaLifecycle


@dataclass
class FakeSerialWriter:
    commands: list[str]
    session_keys: list[str | None] | None = None
    pressure_advance: float = 0.04
    print_accel_mm_s2: float = 2000.0
    closed: int = 0

    def bind_session(self, session_key: str | None) -> None:
        if self.session_keys is not None:
            self.session_keys.append(session_key)

    def send_command(self, command: str) -> None:
        self.commands.append(command)
        if command.startswith("M572 S"):
            self.pressure_advance = float(command.split("S", 1)[1])
        if command.startswith("M204 P"):
            self.print_accel_mm_s2 = float(command.split("P", 1)[1])
        if command.startswith("M204 S"):
            self.print_accel_mm_s2 = float(command.split("S", 1)[1])

    def query_command(self, command: str) -> list[str]:
        self.commands.append(f"QUERY:{command}")
        if command == "M572":
            return [f"M572 S{self.pressure_advance:.4f}"]
        if command == "M204":
            return [f"M204 P{int(round(self.print_accel_mm_s2))}"]
        return []

    def close(self) -> None:
        self.closed += 1


class FakeHttp:
    def __init__(
        self,
        *,
        status: dict[str, Any],
        job: dict[str, Any] | None = None,
        info: dict[str, Any] | None = None,
        files: list[PrintableFile] | None = None,
        start_exc: Exception | None = None,
        cancel_exc: Exception | None = None,
        status_errors: list[Exception] | None = None,
        cancel_updates_state: bool = True,
    ):
        self.status = status
        self.job = job if job is not None else status.get("job", {})
        self.info = info or {"model": "CORE One", "serial": "TEST-123", "min_extrusion_temp": 170}
        self.calls: list[tuple[str, Any]] = []
        self.start_exc = start_exc
        self.cancel_exc = cancel_exc
        self.status_errors = list(status_errors or [])
        self.cancel_updates_state = cancel_updates_state
        self.gcodes: list[str] = []
        self.files = list(files or [])

    def get_status(self) -> dict[str, Any]:
        if self.status_errors:
            raise self.status_errors.pop(0)
        return self.status

    def get_job(self) -> dict[str, Any]:
        return self.job

    def get_info(self) -> dict[str, Any]:
        return self.info

    def list_usb_files(self):
        return list(self.files)

    def get_file_info(self, file_path: str):
        return {}

    def download_file_bytes(self, file_path: str, download_path: str | None = None):
        return None

    def download_file_text(self, file_path: str, download_path: str | None = None):
        return None

    def pause_job(self, job_id: int | None = None) -> None:
        self.calls.append(("pause", job_id))
        self.status["printer"]["state"] = "PAUSED"
        self.status.setdefault("job", {})["state"] = "PAUSED"
        if isinstance(self.job, dict):
            self.job["state"] = "PAUSED"

    def resume_job(self, job_id: int | None = None) -> None:
        self.calls.append(("resume", job_id))
        self.status["printer"]["state"] = "PRINTING"
        self.status.setdefault("job", {})["state"] = "PRINTING"
        if isinstance(self.job, dict):
            self.job["state"] = "PRINTING"

    def cancel_job(self, job_id: int | None = None) -> None:
        self.calls.append(("cancel", job_id))
        if self.cancel_updates_state:
            self.status["printer"]["state"] = "IDLE"
            self.status.pop("job", None)
            self.job = {}
        if self.cancel_exc is not None:
            raise self.cancel_exc

    def start_print(self, file_path: str) -> None:
        self.calls.append(("start", file_path))
        if self.start_exc is not None:
            raise self.start_exc
        display_name = file_path
        for item in self.files:
            if file_path == item.path:
                display_name = item.display_name
                break
        self.status["printer"]["state"] = "PRINTING"
        self.status["job"] = {
            "id": 42,
            "state": "PRINTING",
            "progress": 0,
        }
        self.job = {
            "id": 42,
            "state": "PRINTING",
            "progress": 0,
            "file": {"display_name": display_name},
        }

    def send_gcode(self, command: str) -> None:
        self.gcodes.append(command)


_STATUS = {
    "printer": {
        "state": "PRINTING",
        "speed": 100,
        "flow": 100,
        "temp_nozzle": 215.0,
        "target_nozzle": 215.0,
        "temp_bed": 60.0,
        "target_bed": 60.0,
    },
    "job": {
        "id": 42,
        "state": "PRINTING",
        "progress": 50.0,
        "file": {"display_name": "Benchy Rules.bgcode"},
    },
}


def test_status_to_snapshot_normalizes_http_status():
    snapshot = status_to_snapshot(_STATUS, info={"model": "CORE One", "serial": "TEST", "min_extrusion_temp": 170})

    assert snapshot.lifecycle == PrusaLifecycle.PRINTING
    assert snapshot.job_active is True
    assert snapshot.speed_pct == 100.0
    assert snapshot.flow_pct == 100.0
    assert snapshot.current_file == "Benchy Rules.bgcode"
    assert snapshot.min_extrusion_temp_c == 170.0


def test_status_to_snapshot_uses_job_surface_when_status_omits_file():
    status = {
        "printer": dict(_STATUS["printer"]),
        "job": {
            "id": 42,
            "state": "PRINTING",
            "progress": 0.0,
        },
    }
    job = {
        "id": 42,
        "state": "PRINTING",
        "progress": 0.0,
        "file": {"display_name": "Stringing Test.bgcode"},
    }

    snapshot = status_to_snapshot(status, job=job, info={"model": "CORE One", "serial": "TEST", "min_extrusion_temp": 170})

    assert snapshot.current_file == "Stringing Test.bgcode"
    assert snapshot.job_progress_pct == 0.0


def test_driver_read_status_uses_job_surface_for_current_file():
    status = {
        "printer": dict(_STATUS["printer"]),
        "job": {
            "id": 42,
            "state": "PRINTING",
            "progress": 0.0,
        },
    }
    job = {
        "id": 42,
        "state": "PRINTING",
        "progress": 0.0,
        "file": {"display_name": "Stringing Test.bgcode"},
    }
    http = FakeHttp(status=status, job=job)
    driver = PrusaDriver(http=http, serial_writer=FakeSerialWriter([]), timeout_s=0.01, poll_s=0.0)

    snapshot = driver.read_status()

    assert snapshot.current_file == "Stringing Test.bgcode"


def test_driver_accel_relative_targets_remain_compatible_with_high_active_baseline():
    driver = PrusaDriver(http=FakeHttp(status={**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}), serial_writer=FakeSerialWriter([]), timeout_s=0.01, poll_s=0.0)

    assert driver.print_accel_target_from_baseline(7000.0, direction="down", magnitude="small") == pytest.approx(6300.0)
    assert driver.print_accel_target_from_baseline(7000.0, direction="down", magnitude="big") == pytest.approx(5600.0)
    assert driver.print_accel_target_from_baseline(7000.0, direction="up", magnitude="small") == pytest.approx(7700.0)
    assert driver.print_accel_target_from_baseline(7000.0, direction="up", magnitude="big") == pytest.approx(8400.0)


def test_driver_pressure_advance_relative_targets_use_fixed_percentage_envelope():
    driver = PrusaDriver(http=FakeHttp(status={**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}), serial_writer=FakeSerialWriter([]), timeout_s=0.01, poll_s=0.0)

    assert driver.pressure_advance_bounds_for_baseline(0.04) == pytest.approx((0.03, 0.05))
    assert driver.pressure_advance_target_from_baseline(0.04, direction="down", magnitude="small") == pytest.approx(0.036)
    assert driver.pressure_advance_target_from_baseline(0.04, direction="down", magnitude="big") == pytest.approx(0.032)
    assert driver.pressure_advance_target_from_baseline(0.04, direction="up", magnitude="small") == pytest.approx(0.044)
    assert driver.pressure_advance_target_from_baseline(0.04, direction="up", magnitude="big") == pytest.approx(0.048)


def test_driver_pause_resume_cancel_and_start():
    http = FakeHttp(status={**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])})
    driver = PrusaDriver(http=http, serial_writer=FakeSerialWriter([]), timeout_s=0.01, poll_s=0.0)

    paused = driver.pause()
    assert paused["lifecycle"] == "PAUSED"

    resumed = driver.resume()
    assert resumed["lifecycle"] == "PRINTING"

    cancelled = driver.cancel()
    assert cancelled["lifecycle"] == "IDLE"
    assert cancelled["job_active"] is False

    started = driver.start_print("Benchy Rules.bgcode")
    assert started["lifecycle"] == "PRINTING"
    assert started["current_file"] == "Benchy Rules.bgcode"


def test_driver_start_print_recovers_when_timeout_still_started_requested_job():
    status = {
        "printer": dict(_STATUS["printer"]),
        "job": {
            "id": 77,
            "state": "PRINTING",
            "progress": 0.0,
        },
    }
    job = {
        "id": 77,
        "state": "PRINTING",
        "progress": 0.0,
        "file": {"display_name": "Benchy Rules.bgcode"},
    }
    http = FakeHttp(status=status, job=job, start_exc=TimeoutError("timed out"))
    driver = PrusaDriver(http=http, serial_writer=FakeSerialWriter([]), timeout_s=0.01, poll_s=0.0)

    started = driver.start_print("/usb/Benchy Rules.bgcode")

    assert started["lifecycle"] == "PRINTING"
    assert started["job_active"] is True
    assert started["current_file"] == "Benchy Rules.bgcode"
    assert started["recovered_from_start_timeout"] is True


def test_driver_start_print_resolves_display_name_to_printer_path():
    status = {
        "printer": {"state": "IDLE"},
        "job": {},
    }
    http = FakeHttp(
        status=status,
        files=[
            PrintableFile(
                path="/usb/STRIN~27.BGC",
                display_name="Stringing_Test_PLA_COREONE.bgcode",
                size_bytes=1234,
            )
        ],
    )
    driver = PrusaDriver(http=http, serial_writer=FakeSerialWriter([]), timeout_s=0.01, poll_s=0.0)

    started = driver.start_print("Stringing_Test_PLA_COREONE.bgcode")

    assert http.calls[0] == ("start", "/usb/STRIN~27.BGC")
    assert started["current_file"] == "Stringing_Test_PLA_COREONE.bgcode"


def test_driver_start_print_timeout_raises_when_requested_job_is_not_running():
    status = {
        "printer": {"state": "PRINTING"},
        "job": {
            "id": 77,
            "state": "PRINTING",
            "progress": 0.0,
        },
    }
    job = {
        "id": 77,
        "state": "PRINTING",
        "progress": 0.0,
        "file": {"display_name": "Different File.bgcode"},
    }
    http = FakeHttp(status=status, job=job, start_exc=TimeoutError("timed out"))
    driver = PrusaDriver(http=http, serial_writer=FakeSerialWriter([]), timeout_s=0.01, poll_s=0.0)

    try:
        driver.start_print("Benchy Rules.bgcode")
    except TimeoutError as exc:
        assert "timed out" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected TimeoutError when requested job did not recover")


def test_driver_wait_for_retries_transient_read_timeout_until_success():
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    http = FakeHttp(status=status, status_errors=[TimeoutError("timed out")])
    driver = PrusaDriver(http=http, serial_writer=FakeSerialWriter([]), timeout_s=0.02, poll_s=0.0)

    snapshot = driver._wait_for(lambda snapshot: snapshot.lifecycle == PrusaLifecycle.PRINTING)

    assert snapshot.lifecycle == PrusaLifecycle.PRINTING


def test_driver_cancel_recovers_when_timeout_still_cancelled():
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    http = FakeHttp(status=status, cancel_exc=TimeoutError("timed out"))
    driver = PrusaDriver(http=http, serial_writer=FakeSerialWriter([]), timeout_s=0.01, poll_s=0.0)

    cancelled = driver.cancel()

    assert cancelled["lifecycle"] == "IDLE"
    assert cancelled["job_active"] is False
    assert cancelled["recovered_from_cancel_timeout"] is True


def test_driver_cancel_timeout_raises_when_job_is_still_running():
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    http = FakeHttp(status=status, cancel_exc=TimeoutError("timed out"), cancel_updates_state=False)
    driver = PrusaDriver(http=http, serial_writer=FakeSerialWriter([]), timeout_s=0.01, poll_s=0.0)

    try:
        driver.cancel()
    except TimeoutError as exc:
        assert "timed out" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected TimeoutError when cancel did not recover")


def test_driver_cancel_recovers_when_status_surface_is_stale_after_delete(monkeypatch):
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    http = FakeHttp(status=status, cancel_exc=TimeoutError("timed out"), cancel_updates_state=False)
    driver = PrusaDriver(http=http, serial_writer=FakeSerialWriter([]), timeout_s=0.01, poll_s=0.0)

    stale = status_to_snapshot(
        {
            "printer": {
                "state": "PRINTING",
                "speed": 100,
                "flow": 100,
                "temp_nozzle": 238.0,
                "target_nozzle": 0.0,
                "temp_bed": 57.0,
                "target_bed": 0.0,
            },
            "job": {
                "id": 42,
                "state": "PRINTING",
                "progress": 2.0,
                "file": {"display_name": "Benchy Rules.bgcode"},
            },
        }
    )
    recovered = status_to_snapshot(
        {
            "printer": {
                "state": "STOPPED",
                "speed": 100,
                "flow": 100,
                "temp_nozzle": 190.0,
                "target_nozzle": 0.0,
                "temp_bed": 55.0,
                "target_bed": 0.0,
            },
            "job": {},
        }
    )
    snapshots = iter([stale, recovered])

    monkeypatch.setattr(driver, "_wait_for", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("timed out")))
    monkeypatch.setattr(driver, "read_status", lambda info=None: next(snapshots))

    cancelled = driver.cancel()

    assert cancelled["lifecycle"] == "STOPPED"
    assert cancelled["job_active"] is False
    assert cancelled["recovered_from_cancel_timeout"] is True


def test_driver_trim_speed_down_small_writes_one_serial_command_and_verifies():
    serial = FakeSerialWriter([])
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    http = FakeHttp(status=status)

    def send_and_apply(command: str) -> None:
        serial.commands.append(command)
        if command == "M220 S95":
            http.status["printer"]["speed"] = 95

    serial.send_command = send_and_apply  # type: ignore[method-assign]
    driver = PrusaDriver(http=http, serial_writer=serial, timeout_s=0.01, poll_s=0.0)

    result = driver.trim_speed_down_small(target_speed_pct=95.0)

    assert serial.commands == ["M220 S95"]
    assert result["speed_pct"] == 95.0


def test_driver_wait_for_http_speed_factor_uses_extended_timeout(monkeypatch):
    driver = PrusaDriver(
        http=FakeHttp(status={**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}),
        serial_writer=FakeSerialWriter([]),
        timeout_s=8.0,
        poll_s=0.0,
    )
    observed: dict[str, float | None] = {"timeout_s": None}

    def _wait_for(predicate, *, timeout_s=None):
        observed["timeout_s"] = timeout_s
        return status_to_snapshot(
            {
                "printer": {
                    **_STATUS["printer"],
                    "speed": 85,
                },
                "job": dict(_STATUS["job"]),
            }
        )

    monkeypatch.setattr(driver, "_wait_for", _wait_for)

    confirmed = driver._wait_for_http_speed_factor(85.0)

    assert confirmed == 85.0
    assert observed["timeout_s"] == PrusaDriver.SPEED_HTTP_VERIFY_TIMEOUT_S


def test_driver_restore_speed_default_writes_one_serial_command_and_verifies():
    serial = FakeSerialWriter([])
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    status["printer"]["speed"] = 85
    http = FakeHttp(status=status)

    def send_and_apply(command: str) -> None:
        serial.commands.append(command)
        if command == "M220 S100":
            http.status["printer"]["speed"] = 100

    serial.send_command = send_and_apply  # type: ignore[method-assign]
    driver = PrusaDriver(http=http, serial_writer=serial, timeout_s=0.01, poll_s=0.0)

    result = driver.restore_speed_default()

    assert serial.commands == ["M220 S100"]
    assert result["speed_pct"] == 100.0


def test_driver_binds_serial_session_to_current_job_before_trim_and_query():
    serial = FakeSerialWriter([], session_keys=[])
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    status["job"]["id"] = 77
    http = FakeHttp(status=status)

    def send_and_apply(command: str) -> None:
        serial.commands.append(command)
        if command == "M220 S95":
            http.status["printer"]["speed"] = 95

    serial.send_command = send_and_apply  # type: ignore[method-assign]
    driver = PrusaDriver(http=http, serial_writer=serial, timeout_s=0.01, poll_s=0.0)

    driver.trim_speed_down_small(target_speed_pct=95.0)
    driver.read_flow_factor_pct()
    driver.close()

    assert serial.session_keys == ["job:77", "job:77"]
    assert serial.closed == 1


def test_driver_restore_flow_default_writes_one_serial_command_and_verifies():
    serial = FakeSerialWriter([])
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    status["printer"]["flow"] = 95
    http = FakeHttp(status=status)

    def send_and_apply(command: str) -> None:
        serial.commands.append(command)

    def query_and_apply(command: str) -> list[str]:
        if command == "M221":
            return ["echo:E0 Flow: 100%"]
        return []

    serial.send_command = send_and_apply  # type: ignore[method-assign]
    serial.query_command = query_and_apply  # type: ignore[method-assign]
    driver = PrusaDriver(http=http, serial_writer=serial, timeout_s=0.01, poll_s=0.0)

    result = driver.restore_flow_default()

    assert serial.commands == ["M221 S100"]
    assert result["flow_pct"] == 100.0


def test_driver_trim_flow_down_small_writes_one_serial_command_and_verifies():
    serial = FakeSerialWriter([])
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    http = FakeHttp(status=status)

    def send_and_apply(command: str) -> None:
        serial.commands.append(command)

    def query_and_apply(command: str) -> list[str]:
        if command == "M221":
            return ["echo:E0 Flow: 95%"]
        return []

    serial.send_command = send_and_apply  # type: ignore[method-assign]
    serial.query_command = query_and_apply  # type: ignore[method-assign]
    driver = PrusaDriver(http=http, serial_writer=serial, timeout_s=0.01, poll_s=0.0)

    result = driver.trim_flow_down_small(target_flow_pct=95.0)

    assert serial.commands == ["M221 S95"]
    assert result["flow_pct"] == 95.0


def test_driver_trim_flow_down_small_failure_reports_send_and_verify_surfaces():
    serial = FakeSerialWriter([])
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    http = FakeHttp(status=status)

    def send_and_apply(command: str) -> None:
        serial.commands.append(command)

    serial.send_command = send_and_apply  # type: ignore[method-assign]
    driver = PrusaDriver(http=http, serial_writer=serial, timeout_s=0.01, poll_s=0.0)
    driver.FLOW_HTTP_VERIFY_TIMEOUT_S = 0.01

    with pytest.raises(RuntimeError) as exc:
        driver.trim_flow_down_small(target_flow_pct=95.0)

    message = str(exc.value)
    assert "send_command=M221 S95" in message
    assert "acceptance_surface=serial:unknown_port" in message
    assert "verification_surface=serial_query:M221->Flow%" in message
    assert "http_flow_pct=100.0" in message


def test_driver_trim_nozzle_down_small_failure_reports_http_verify_surface():
    serial = FakeSerialWriter([])
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    http = FakeHttp(status=status)
    driver = PrusaDriver(http=http, serial_writer=serial, timeout_s=0.01, poll_s=0.0)

    with pytest.raises(RuntimeError) as exc:
        driver.trim_nozzle_down_small(target_nozzle_c=210.0, min_nozzle_target_c=170.0)

    message = str(exc.value)
    assert "send_command=M104 S210" in message
    assert "verification_surface=http_status.printer.target_nozzle" in message
    assert "http_nozzle_target_c=215.0" in message


def test_driver_trim_speed_up_small_writes_one_serial_command_and_verifies():
    serial = FakeSerialWriter([])
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    status["printer"]["speed"] = 95
    http = FakeHttp(status=status)

    def send_and_apply(command: str) -> None:
        serial.commands.append(command)
        if command == "M220 S100":
            http.status["printer"]["speed"] = 100

    serial.send_command = send_and_apply  # type: ignore[method-assign]
    driver = PrusaDriver(http=http, serial_writer=serial, timeout_s=0.01, poll_s=0.0)

    result = driver.trim_speed_up_small(target_speed_pct=100.0)

    assert serial.commands == ["M220 S100"]
    assert result["speed_pct"] == 100.0


def test_driver_trim_flow_up_small_writes_one_serial_command_and_verifies():
    serial = FakeSerialWriter([])
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    status["printer"]["flow"] = 95
    http = FakeHttp(status=status)

    def send_and_apply(command: str) -> None:
        serial.commands.append(command)

    def query_and_apply(command: str) -> list[str]:
        if command == "M221":
            return ["echo:E0 Flow: 100%"]
        return []

    serial.send_command = send_and_apply  # type: ignore[method-assign]
    serial.query_command = query_and_apply  # type: ignore[method-assign]
    driver = PrusaDriver(http=http, serial_writer=serial, timeout_s=0.01, poll_s=0.0)

    result = driver.trim_flow_up_small(target_flow_pct=100.0)

    assert serial.commands == ["M221 S100"]
    assert result["flow_pct"] == 100.0


def test_driver_big_trim_variants_use_explicit_targets_and_verify():
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    http = FakeHttp(status=status)
    serial = FakeSerialWriter([])

    def send_and_apply(command: str) -> None:
        serial.commands.append(command)
        if command == "M220 S90":
            http.status["printer"]["speed"] = 90
        if command == "M221 S90":
            http.status["printer"]["flow"] = 90
            serial.flow_pct = 90.0
        if command == "M104 S205":
            http.status["printer"]["target_nozzle"] = 205
        if command == "M140 S50":
            http.status["printer"]["target_bed"] = 50

    serial.send_command = send_and_apply  # type: ignore[method-assign]
    driver = PrusaDriver(http=http, serial_writer=serial, timeout_s=0.01, poll_s=0.0)

    assert driver.trim_speed_down_big(target_speed_pct=90.0)["speed_pct"] == 90.0
    assert driver.trim_flow_down_big(target_flow_pct=90.0)["flow_pct"] == 90.0
    assert driver.trim_nozzle_down_big(target_nozzle_c=205.0, min_nozzle_target_c=170.0)["target_nozzle_c"] == 205.0
    assert driver.trim_bed_down_big(target_bed_c=50.0)["target_bed_c"] == 50.0
    assert "M220 S90" in serial.commands
    assert "M221 S90" in serial.commands
    assert "M104 S205" in serial.commands
    assert "M140 S50" in serial.commands


def test_driver_pressure_advance_write_and_readback():
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    http = FakeHttp(status=status)
    serial = FakeSerialWriter([], pressure_advance=0.04)
    driver = PrusaDriver(http=http, serial_writer=serial, timeout_s=0.01, poll_s=0.0)

    result = driver.trim_pressure_advance_up_big(target_pressure_advance=0.06)

    assert result["pressure_advance"] == 0.06
    assert "M572 S0.0600" in serial.commands
    assert driver.read_pressure_advance() == 0.06


def test_driver_pressure_advance_disabled_readback_maps_to_zero():
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    http = FakeHttp(status=status)
    serial = FakeSerialWriter([], pressure_advance=0.0)

    def disabled_query(command: str) -> list[str]:
        serial.commands.append(f"QUERY:{command}")
        if command == "M572":
            return ["echo:Pressure advance: disabled", "ok"]
        return []

    serial.query_command = disabled_query  # type: ignore[method-assign]
    driver = PrusaDriver(http=http, serial_writer=serial, timeout_s=0.01, poll_s=0.0)

    assert driver.read_pressure_advance() == 0.0


def test_driver_print_accel_write_and_readback():
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    http = FakeHttp(status=status)
    serial = FakeSerialWriter([], print_accel_mm_s2=2000.0)
    driver = PrusaDriver(http=http, serial_writer=serial, timeout_s=0.01, poll_s=0.0)

    result = driver.trim_print_accel_down_big(target_print_accel_mm_s2=1500.0)

    assert result["print_accel_mm_s2"] == 1500.0
    assert "M204 P1500" in serial.commands
    assert driver.read_print_accel_mm_s2() == 1500.0


def test_driver_trim_nozzle_up_small_uses_explicit_target_and_verifies():
    serial = FakeSerialWriter([])
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    http = FakeHttp(status=status)

    def send_and_apply(command: str) -> None:
        serial.commands.append(command)
        if command == "M104 S220":
            http.status["printer"]["target_nozzle"] = 220

    serial.send_command = send_and_apply  # type: ignore[method-assign]
    driver = PrusaDriver(http=http, serial_writer=serial, timeout_s=0.01, poll_s=0.0)

    http.status["printer"]["target_nozzle"] = 218

    result = driver.trim_nozzle_up_small(target_nozzle_c=220.0, max_nozzle_target_c=300.0)

    assert serial.commands == ["M104 S220"]
    assert result["target_nozzle_c"] == 220.0


def test_driver_trim_nozzle_down_small_uses_explicit_target_and_verifies():
    serial = FakeSerialWriter([])
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    http = FakeHttp(status=status)

    def send_and_apply(command: str) -> None:
        serial.commands.append(command)
        if command == "M104 S210":
            http.status["printer"]["target_nozzle"] = 210

    serial.send_command = send_and_apply  # type: ignore[method-assign]
    driver = PrusaDriver(http=http, serial_writer=serial, timeout_s=0.01, poll_s=0.0)

    http.status["printer"]["target_nozzle"] = 213

    result = driver.trim_nozzle_down_small(target_nozzle_c=210.0, min_nozzle_target_c=170.0)

    assert serial.commands == ["M104 S210"]
    assert result["target_nozzle_c"] == 210.0


def test_driver_trim_bed_up_small_uses_explicit_target_and_verifies():
    serial = FakeSerialWriter([])
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    http = FakeHttp(status=status)

    def send_and_apply(command: str) -> None:
        serial.commands.append(command)
        if command == "M140 S65":
            http.status["printer"]["target_bed"] = 65

    serial.send_command = send_and_apply  # type: ignore[method-assign]
    driver = PrusaDriver(http=http, serial_writer=serial, timeout_s=0.01, poll_s=0.0)

    http.status["printer"]["target_bed"] = 62

    result = driver.trim_bed_up_small(target_bed_c=65.0, max_bed_target_c=120.0)

    assert serial.commands == ["M140 S65"]
    assert result["target_bed_c"] == 65.0


def test_driver_trim_bed_down_small_uses_explicit_target_and_verifies():
    serial = FakeSerialWriter([])
    status = {**_STATUS, "printer": dict(_STATUS["printer"]), "job": dict(_STATUS["job"])}
    http = FakeHttp(status=status)

    def send_and_apply(command: str) -> None:
        serial.commands.append(command)
        if command == "M140 S55":
            http.status["printer"]["target_bed"] = 55

    serial.send_command = send_and_apply  # type: ignore[method-assign]
    driver = PrusaDriver(http=http, serial_writer=serial, timeout_s=0.01, poll_s=0.0)

    http.status["printer"]["target_bed"] = 57

    result = driver.trim_bed_down_small(target_bed_c=55.0)

    assert serial.commands == ["M140 S55"]
    assert result["target_bed_c"] == 55.0
