"""Tests for PrusaLink HTTP sensor tools."""

import httpx
import pytest

from wallee.bus.network import HTTPClient
import wallee.device_packs.prusa_link.sensors as sensors_mod
from wallee.device_packs.prusa_link.sensors import (
    read_printer_state,
    read_printer_info,
    read_file_list,
    read_job_phase,
)

BASE_URL = "http://testprinter"


def _mock_client(handler):
    client = HTTPClient(BASE_URL)
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=BASE_URL,
        headers=client._headers,
    )
    return client


def _json_response(data, status_code=200):
    return httpx.Response(status_code, json=data, headers={"content-type": "application/json"})


@pytest.fixture(autouse=True)
def reset_http():
    old = sensors_mod._http
    yield
    sensors_mod._http = old


def _inject(handler):
    sensors_mod._http = _mock_client(handler)


PRUSA_STATUS_PRINTING = {
    "printer": {"state": "PRINTING", "temp_nozzle": 215.0, "target_nozzle": 215.0,
                "temp_bed": 60.0, "target_bed": 60.0, "speed": 100, "flow": 100},
    "job": {"state": "PRINTING", "progress": 42, "time_remaining": 3600, "time_printing": 2400},
}

PRUSA_STATUS_IDLE = {
    "printer": {"state": "IDLE", "temp_nozzle": 23.0, "target_nozzle": 0,
                "temp_bed": 22.0, "target_bed": 0},
}

PRUSA_INFO = {"serial": "CZPX1234", "hostname": "prusa-core-one", "nozzle_diameter": 0.4}
PRUSA_VERSION = {"api": "2.0.0", "server": "2.1.2", "hostname": "prusa-core-one"}

PRUSA_FILES = {
    "children": [
        {"name": "BENCHY~2.BGC", "display_name": "Benchy Rules.bgcode", "type": "PRINT_FILE", "size": 12345},
        {"name": "MMU3", "type": "FOLDER"},
    ]
}


class TestReadPrinterState:
    def test_printing(self):
        _inject(lambda r: _json_response(PRUSA_STATUS_PRINTING))
        result = read_printer_state()
        assert result["printer.state"] == "PRINTING"
        assert result["printer.job_state"] == "PRINTING"
        assert result["printer.job_progress"] == 42
        assert result["printer.job_time_remaining_s"] == 3600

    def test_idle(self):
        _inject(lambda r: _json_response(PRUSA_STATUS_IDLE))
        result = read_printer_state()
        assert result["printer.state"] == "IDLE"
        assert result["printer.job_state"] == "IDLE"

    def test_api_error(self):
        _inject(lambda r: httpx.Response(500))
        result = read_printer_state()
        assert "printer.api_error" in result


class TestReadPrinterInfo:
    def test_full_info(self):
        def handler(r):
            if "/api/v1/info" in str(r.url):
                return _json_response(PRUSA_INFO)
            if "/api/version" in str(r.url):
                return _json_response(PRUSA_VERSION)
            return httpx.Response(404)
        _inject(handler)
        result = read_printer_info()
        assert result["printer.firmware"] == "2.1.2"
        assert result["printer.model"] == "prusa-core-one"
        assert result["printer.serial"] == "CZPX1234"
        assert result["printer.nozzle_diameter"] == 0.4


class TestReadFileList:
    def test_file_listing(self):
        _inject(lambda r: _json_response(PRUSA_FILES))
        result = read_file_list()
        files = result["printer.files"]
        assert len(files) == 2
        assert files[0]["name"] == "Benchy Rules.bgcode"
        assert files[0]["type"] == "PRINT_FILE"
        assert files[1]["type"] == "FOLDER"

    def test_api_error(self):
        _inject(lambda r: httpx.Response(403))
        result = read_file_list()
        assert "printer.api_error" in result


class TestSensorMetadata:
    def test_printer_state_meta(self):
        meta = read_printer_state._tool_meta
        assert meta["kind"] == "sensor"
        assert meta["refresh_hz"] == 0.5

    def test_printer_info_meta(self):
        meta = read_printer_info._tool_meta
        assert meta["refresh_hz"] == 0.1

    def test_file_list_meta(self):
        meta = read_file_list._tool_meta
        assert meta["refresh_hz"] == 0.02

    def test_job_phase_meta(self):
        meta = read_job_phase._tool_meta
        assert meta["kind"] == "sensor"
        assert meta["refresh_hz"] == 1.0


def _phase_status(state="IDLE", temp_nozzle=23.0, target_nozzle=0, temp_bed=22.0,
                   target_bed=0, progress=0, job_state="IDLE"):
    """Build a PrusaLink /api/v1/status response for phase testing."""
    return {
        "printer": {"state": state, "temp_nozzle": temp_nozzle, "target_nozzle": target_nozzle,
                     "temp_bed": temp_bed, "target_bed": target_bed},
        "job": {"state": job_state, "progress": progress},
    }


class TestReadJobPhase:
    @pytest.fixture(autouse=True)
    def reset_phase_state(self):
        """Reset module-level phase tracking between tests."""
        sensors_mod._phase_state = {"phase": "IDLE", "entered": 0.0}
        yield
        sensors_mod._phase_state = {"phase": "IDLE", "entered": 0.0}

    def test_idle(self):
        _inject(lambda r: _json_response(_phase_status(state="IDLE")))
        result = read_job_phase()
        assert result["job.phase"] == "IDLE"
        assert "No active job" in result["job.phase_detail"]

    def test_preparing(self):
        """PRINTING state with temps below target = PREPARING."""
        _inject(lambda r: _json_response(_phase_status(
            state="PRINTING", temp_nozzle=100.0, target_nozzle=215.0,
            temp_bed=30.0, target_bed=60.0, progress=0, job_state="PRINTING",
        )))
        result = read_job_phase()
        assert result["job.phase"] == "PREPARING"
        assert "Heating" in result["job.phase_detail"]

    def test_printing(self):
        """PRINTING state with temps at target and progress > 0 = PRINTING."""
        _inject(lambda r: _json_response(_phase_status(
            state="PRINTING", temp_nozzle=215.0, target_nozzle=215.0,
            temp_bed=60.0, target_bed=60.0, progress=50, job_state="PRINTING",
        )))
        result = read_job_phase()
        assert result["job.phase"] == "PRINTING"
        assert "50%" in result["job.phase_detail"]

    def test_paused(self):
        _inject(lambda r: _json_response(_phase_status(
            state="PAUSED", temp_nozzle=215.0, target_nozzle=215.0,
            temp_bed=60.0, target_bed=60.0, progress=30, job_state="PAUSED",
        )))
        result = read_job_phase()
        assert result["job.phase"] == "PAUSED"

    def test_finished(self):
        _inject(lambda r: _json_response(_phase_status(state="FINISHED")))
        result = read_job_phase()
        assert result["job.phase"] == "FINISHED"
        assert "complete" in result["job.phase_detail"]

    def test_error(self):
        _inject(lambda r: _json_response(_phase_status(state="ERROR")))
        result = read_job_phase()
        assert result["job.phase"] == "ERROR"

    def test_attention_maps_to_error(self):
        _inject(lambda r: _json_response(_phase_status(state="ATTENTION")))
        result = read_job_phase()
        assert result["job.phase"] == "ERROR"

    def test_phase_transition_resets_time(self):
        """When phase changes, time_in_phase_s should reset to ~0."""
        import time
        _inject(lambda r: _json_response(_phase_status(state="IDLE")))
        result1 = read_job_phase()
        assert result1["job.phase"] == "IDLE"

        time.sleep(0.05)

        _inject(lambda r: _json_response(_phase_status(
            state="PRINTING", temp_nozzle=215.0, target_nozzle=215.0,
            temp_bed=60.0, target_bed=60.0, progress=10, job_state="PRINTING",
        )))
        result2 = read_job_phase()
        assert result2["job.phase"] == "PRINTING"
        assert result2["job.time_in_phase_s"] == 0  # just transitioned
