"""Tests for PrusaLink HTTP actuator tools."""

import json
import httpx
import fakeredis
import pytest

from wallee.bus.network import HTTPClient
from wallee.whiteboard.client import Whiteboard
import wallee.device_packs.prusa_link.sensors as sensors_mod
from wallee.device_packs.prusa_link.actuators import (
    pause_print,
    resume_print,
    cancel_print,
    start_print,
    set_temperature,
    home_axes,
    disable_motors,
    set_speed_factor,
    set_flow_factor,
    set_position,
    extrude,
    retract,
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


def _ok(request):
    return httpx.Response(204, headers={"content-type": "text/plain"})


@pytest.fixture
def wb():
    return Whiteboard(_redis=fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture(autouse=True)
def reset_http():
    old = sensors_mod._http
    yield
    sensors_mod._http = old


def _inject(handler=None):
    sensors_mod._http = _mock_client(handler or _ok)


class TestPausePrint:
    def test_success(self, wb):
        def handler(r):
            assert r.method == "POST"
            assert "/api/v1/gcode" in str(r.url)
            body = json.loads(r.content)
            assert body["command"] == "M25"
            return httpx.Response(204, headers={"content-type": "text/plain"})
        _inject(handler)
        wb.publish("printer.job_state", "PRINTING")
        result = pause_print(whiteboard=wb)
        assert result["status"] == "success"
        assert result["gcode"] == "M25"

    def test_rejects_not_printing(self, wb):
        _inject()
        wb.publish("printer.job_state", "IDLE")
        result = pause_print(whiteboard=wb)
        assert "error" in result

    def test_no_approval_required(self):
        assert pause_print._tool_meta["requires_approval"] is False


class TestResumePrint:
    def test_success(self, wb):
        def handler(r):
            assert r.method == "POST"
            assert "/api/v1/gcode" in str(r.url)
            body = json.loads(r.content)
            assert body["command"] == "M24"
            return httpx.Response(204, headers={"content-type": "text/plain"})
        _inject(handler)
        wb.publish("printer.job_state", "PAUSED")
        wb.publish("printer.state", "PAUSED")
        result = resume_print(whiteboard=wb)
        assert result["status"] == "success"
        assert result["gcode"] == "M24"

    def test_rejects_not_paused(self, wb):
        _inject()
        wb.publish("printer.job_state", "PRINTING")
        result = resume_print(whiteboard=wb)
        assert "error" in result

    def test_rejects_error_state(self, wb):
        _inject()
        wb.publish("printer.job_state", "PAUSED")
        wb.publish("printer.state", "ERROR")
        result = resume_print(whiteboard=wb)
        assert "error" in result

    def test_requires_approval(self):
        assert resume_print._tool_meta["requires_approval"] is False


class TestCancelPrint:
    def test_success(self, wb):
        def handler(r):
            assert r.method == "DELETE"
            return httpx.Response(204, headers={"content-type": "text/plain"})
        _inject(handler)
        wb.publish("printer.job_state", "PRINTING")
        result = cancel_print(whiteboard=wb)
        assert result["status"] == "success"

    def test_rejects_idle(self, wb):
        _inject()
        wb.publish("printer.job_state", "IDLE")
        result = cancel_print(whiteboard=wb)
        assert "error" in result


class TestStartPrint:
    def test_success(self, wb):
        def handler(r):
            assert r.method == "POST"
            assert "/pprint" in str(r.url)
            return httpx.Response(204, headers={"content-type": "text/plain"})
        _inject(handler)
        wb.publish("printer.job_state", "IDLE")
        result = start_print(whiteboard=wb, file_path="/usb/BENCHY~2.BGC")
        assert result["status"] == "success"

    def test_strips_usb_prefix(self, wb):
        def handler(r):
            assert "usb/BENCHY~2.BGC/pprint" in str(r.url)
            return httpx.Response(204, headers={"content-type": "text/plain"})
        _inject(handler)
        wb.publish("printer.job_state", "IDLE")
        start_print(whiteboard=wb, file_path="/usb/BENCHY~2.BGC")

    def test_rejects_printing(self, wb):
        _inject()
        wb.publish("printer.job_state", "PRINTING")
        result = start_print(whiteboard=wb, file_path="/usb/test.bgc")
        assert "error" in result

    def test_rejects_empty_path(self, wb):
        _inject()
        wb.publish("printer.job_state", "IDLE")
        result = start_print(whiteboard=wb, file_path="")
        assert "error" in result

    def test_longer_deadline(self):
        assert start_print._tool_meta["max_proposal_age_ms"] == 60000


class TestSetTemperature:
    def test_nozzle(self, wb):
        def handler(r):
            body = json.loads(r.content)
            assert body["command"] == "M104 S215.0"
            return httpx.Response(204, headers={"content-type": "text/plain"})
        _inject(handler)
        wb.publish("printer.state", "IDLE")
        result = set_temperature(whiteboard=wb, target=215.0, heater="nozzle")
        assert result["status"] == "success"
        assert result["heater"] == "nozzle"

    def test_bed(self, wb):
        def handler(r):
            body = json.loads(r.content)
            assert body["command"] == "M140 S60.0"
            return httpx.Response(204, headers={"content-type": "text/plain"})
        _inject(handler)
        wb.publish("printer.state", "IDLE")
        result = set_temperature(whiteboard=wb, target=60.0, heater="bed")
        assert result["status"] == "success"

    def test_chamber(self, wb):
        def handler(r):
            body = json.loads(r.content)
            assert body["command"] == "M141 S36.0"
            return httpx.Response(204, headers={"content-type": "text/plain"})
        _inject(handler)
        wb.publish("printer.state", "IDLE")
        result = set_temperature(whiteboard=wb, target=36.0, heater="chamber")
        assert result["status"] == "success"

    def test_rejects_invalid_heater(self, wb):
        _inject()
        result = set_temperature(whiteboard=wb, target=200, heater="enclosure")
        assert "error" in result

    def test_rejects_nozzle_over_300(self, wb):
        _inject()
        result = set_temperature(whiteboard=wb, target=350, heater="nozzle")
        assert "error" in result

    def test_rejects_bed_over_120(self, wb):
        _inject()
        result = set_temperature(whiteboard=wb, target=130, heater="bed")
        assert "error" in result

    def test_rejects_chamber_over_50(self, wb):
        _inject()
        result = set_temperature(whiteboard=wb, target=60, heater="chamber")
        assert "error" in result

    def test_rejects_negative(self, wb):
        _inject()
        result = set_temperature(whiteboard=wb, target=-10, heater="nozzle")
        assert "error" in result

    def test_rejects_error_state(self, wb):
        _inject()
        wb.publish("printer.state", "ERROR")
        result = set_temperature(whiteboard=wb, target=200, heater="nozzle")
        assert "error" in result

    def test_no_approval_required(self):
        assert set_temperature._tool_meta["requires_approval"] is False


class TestHomeAxes:
    def test_success_idle(self, wb):
        def handler(r):
            body = json.loads(r.content)
            assert body["command"] == "G28"
            return httpx.Response(204, headers={"content-type": "text/plain"})
        _inject(handler)
        wb.publish("printer.state", "IDLE")
        result = home_axes(whiteboard=wb)
        assert result["status"] == "success"

    def test_rejects_during_print(self, wb):
        _inject()
        wb.publish("printer.state", "PRINTING")
        result = home_axes(whiteboard=wb)
        assert "error" in result

    def test_requires_approval(self):
        assert home_axes._tool_meta["requires_approval"] is False


class TestDisableMotors:
    def test_success(self, wb):
        def handler(r):
            body = json.loads(r.content)
            assert body["command"] == "M18"
            return httpx.Response(204, headers={"content-type": "text/plain"})
        _inject(handler)
        wb.publish("printer.state", "IDLE")
        result = disable_motors(whiteboard=wb)
        assert result["status"] == "success"

    def test_rejects_during_print(self, wb):
        _inject()
        wb.publish("printer.state", "PRINTING")
        result = disable_motors(whiteboard=wb)
        assert "error" in result

    def test_no_approval(self):
        assert disable_motors._tool_meta["requires_approval"] is False


class TestSetSpeedFactor:
    def test_success(self, wb):
        def handler(r):
            body = json.loads(r.content)
            assert body["command"] == "M220 S150"
            return httpx.Response(204, headers={"content-type": "text/plain"})
        _inject(handler)
        wb.publish("printer.state", "PRINTING")
        result = set_speed_factor(whiteboard=wb, percent=150)
        assert result["status"] == "success"
        assert result["percent"] == 150

    def test_rejects_below_10(self, wb):
        _inject()
        result = set_speed_factor(whiteboard=wb, percent=5)
        assert "error" in result

    def test_rejects_above_200(self, wb):
        _inject()
        result = set_speed_factor(whiteboard=wb, percent=250)
        assert "error" in result

    def test_rejects_when_idle(self, wb):
        _inject()
        wb.publish("printer.state", "IDLE")
        result = set_speed_factor(whiteboard=wb, percent=100)
        assert "error" in result


class TestSetFlowFactor:
    def test_success(self, wb):
        _inject()
        wb.publish("printer.state", "PRINTING")
        result = set_flow_factor(whiteboard=wb, percent=120)
        assert result["status"] == "success"

    def test_rejects_above_150(self, wb):
        _inject()
        result = set_flow_factor(whiteboard=wb, percent=200)
        assert "error" in result

    def test_rejects_when_idle(self, wb):
        _inject()
        wb.publish("printer.state", "IDLE")
        result = set_flow_factor(whiteboard=wb, percent=100)
        assert "error" in result


class TestSetPosition:
    def test_success(self, wb):
        def handler(r):
            body = json.loads(r.content)
            assert "G1 X100" in body["command"]
            return httpx.Response(204, headers={"content-type": "text/plain"})
        _inject(handler)
        wb.publish("printer.state", "IDLE")
        result = set_position(whiteboard=wb, x=100, y=100, z=50)
        assert result["status"] == "success"

    def test_rejects_x_out_of_bounds(self, wb):
        _inject()
        wb.publish("printer.state", "IDLE")
        result = set_position(whiteboard=wb, x=300, y=100, z=50)
        assert "error" in result

    def test_rejects_y_out_of_bounds(self, wb):
        _inject()
        wb.publish("printer.state", "IDLE")
        result = set_position(whiteboard=wb, x=100, y=250, z=50)
        assert "error" in result

    def test_rejects_during_print(self, wb):
        _inject()
        wb.publish("printer.state", "PRINTING")
        result = set_position(whiteboard=wb, x=100, y=100, z=50)
        assert "error" in result
        assert "destroy" in result["error"]

    def test_requires_approval(self):
        assert set_position._tool_meta["requires_approval"] is False


class TestExtrude:
    def test_success(self, wb):
        _inject()
        wb.publish("printer.state", "IDLE")
        wb.publish("printer.temp_nozzle", 215.0)
        result = extrude(whiteboard=wb, length_mm=20, feedrate=300)
        assert result["status"] == "success"
        assert result["length_mm"] == 20

    def test_rejects_cold_nozzle(self, wb):
        _inject()
        wb.publish("printer.state", "IDLE")
        wb.publish("printer.temp_nozzle", 25.0)
        result = extrude(whiteboard=wb, length_mm=10)
        assert "error" in result
        assert "170" in result["error"]

    def test_rejects_too_long(self, wb):
        _inject()
        result = extrude(whiteboard=wb, length_mm=150)
        assert "error" in result

    def test_rejects_during_print(self, wb):
        _inject()
        wb.publish("printer.state", "PRINTING")
        result = extrude(whiteboard=wb, length_mm=10)
        assert "error" in result

    def test_requires_approval(self):
        assert extrude._tool_meta["requires_approval"] is False


class TestRetract:
    def test_success(self, wb):
        _inject()
        wb.publish("printer.state", "IDLE")
        wb.publish("printer.temp_nozzle", 200.0)
        result = retract(whiteboard=wb, length_mm=5, feedrate=600)
        assert result["status"] == "success"

    def test_rejects_cold_nozzle(self, wb):
        _inject()
        wb.publish("printer.state", "IDLE")
        wb.publish("printer.temp_nozzle", 100.0)
        result = retract(whiteboard=wb, length_mm=5)
        assert "error" in result

    def test_rejects_too_long(self, wb):
        _inject()
        result = retract(whiteboard=wb, length_mm=150)
        assert "error" in result
