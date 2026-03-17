"""Tests for prusa_metrics sensor tools using a mock MetricsBuffer."""

import pytest
from wallee.bus.udp_listener import MetricsBuffer, Metric
import wallee.device_packs.prusa_metrics.sensors as sensors_mod
from wallee.device_packs.prusa_metrics.sensors import (
    read_temperatures,
    read_electrical,
    read_fans,
    read_position,
    read_filament,
    read_enclosure,
    read_firmware_health,
    read_print_state,
)


@pytest.fixture(autouse=True)
def mock_buffer():
    """Inject a fresh MetricsBuffer for each test."""
    buf = MetricsBuffer()
    old_buf = sensors_mod._buffer
    old_listener = sensors_mod._listener
    sensors_mod._buffer = buf
    sensors_mod._listener = "fake"  # prevent real listener from starting
    yield buf
    sensors_mod._buffer = old_buf
    sensors_mod._listener = old_listener


def _put(buf, name, tags=None, **fields):
    buf.update(Metric(name=name, tags=tags or {}, fields=fields))


class TestReadTemperatures:
    def test_all_temps(self, mock_buffer):
        _put(mock_buffer, "temp_noz", {"n": "0", "a": "1"}, value=215.2)
        _put(mock_buffer, "ttemp_noz", {"n": "0", "a": "1"}, value=215)
        _put(mock_buffer, "temp_bed", v=60.05)
        _put(mock_buffer, "ttemp_bed", v=60)
        _put(mock_buffer, "chamber_temp", v=36.5)
        _put(mock_buffer, "temp_hbr", {"n": "0", "a": "1"}, value=33.3)
        _put(mock_buffer, "temp_brd", v=38.7)
        _put(mock_buffer, "temp_mcu", v=44)

        result = read_temperatures()
        assert result["printer.temp_nozzle"] == pytest.approx(215.2)
        assert result["printer.target_nozzle"] == 215
        assert result["printer.temp_bed"] == pytest.approx(60.05)
        assert result["printer.target_bed"] == 60
        assert result["printer.temp_chamber"] == pytest.approx(36.5)
        assert result["printer.temp_heatbreak"] == pytest.approx(33.3)
        assert result["printer.temp_board"] == pytest.approx(38.7)
        assert result["printer.temp_mcu"] == 44

    def test_empty_buffer(self, mock_buffer):
        result = read_temperatures()
        assert "error" in result

    def test_partial_data(self, mock_buffer):
        _put(mock_buffer, "temp_bed", v=60.0)
        result = read_temperatures()
        assert result["printer.temp_bed"] == 60.0
        assert "printer.temp_nozzle" not in result


class TestReadElectrical:
    def test_all_electrical(self, mock_buffer):
        _put(mock_buffer, "volt_bed", v=24.06)
        _put(mock_buffer, "volt_nozz", v=0.036)
        _put(mock_buffer, "curr_nozz", v=0.047)
        _put(mock_buffer, "oc_nozz", v=0)
        _put(mock_buffer, "oc_inp", v=0)

        result = read_electrical()
        assert result["printer.volt_bed"] == pytest.approx(24.06, abs=0.01)
        assert result["printer.oc_nozzle"] == 0
        assert result["printer.oc_input"] == 0

    def test_overcurrent_detected(self, mock_buffer):
        _put(mock_buffer, "oc_nozz", v=1)
        result = read_electrical()
        assert result["printer.oc_nozzle"] == 1


class TestReadFans:
    def test_all_fans(self, mock_buffer):
        _put(mock_buffer, "fan", {"fan": "heatbreak"}, state=1, pwm=200, measured=6039)
        _put(mock_buffer, "fan", {"fan": "print"}, state=1, pwm=255, measured=5200)
        _put(mock_buffer, "xbe_fan", {"fan": "1"}, pwm=128, rpm=3000)

        result = read_fans()
        assert result["printer.fan_heatbreak_rpm"] == 6039
        assert result["printer.fan_print_pwm"] == 255
        assert result["printer.xbe_fan_1_rpm"] == 3000


class TestReadPosition:
    def test_positions(self, mock_buffer):
        _put(mock_buffer, "pos_x", v=120.5)
        _put(mock_buffer, "pos_y", v=-9.0)
        _put(mock_buffer, "pos_z", v=4.0)
        _put(mock_buffer, "ipos_x", v=-8)
        _put(mock_buffer, "ipos_y", v=8)
        _put(mock_buffer, "ipos_z", v=1600)

        result = read_position()
        assert result["printer.pos_x"] == pytest.approx(120.5)
        assert result["printer.pos_z"] == pytest.approx(4.0)
        assert result["printer.ipos_z"] == 1600


class TestReadFilament:
    def test_filament_present(self, mock_buffer):
        _put(mock_buffer, "fsensor", {"n": "0"}, st=2, f=1057502, r=27225, ri=1308458)
        result = read_filament()
        assert result["printer.fsensor_state"] == 2
        assert result["printer.fsensor_flow"] == 1057502

    def test_no_data(self, mock_buffer):
        result = read_filament()
        assert "error" in result


class TestReadEnclosure:
    def test_door_and_chamber(self, mock_buffer):
        _put(mock_buffer, "door_sensor", v=11)
        _put(mock_buffer, "chamber_temp", v=36.5)
        result = read_enclosure()
        assert result["printer.door_sensor"] == 11
        assert result["printer.temp_chamber"] == pytest.approx(36.5)


class TestReadFirmwareHealth:
    def test_health_metrics(self, mock_buffer):
        _put(mock_buffer, "heap", free=63548, total=81700)
        _put(mock_buffer, "cpu_usage", v=80)
        _put(mock_buffer, "stp_stall", v=10)
        result = read_firmware_health()
        assert result["printer.heap_free"] == 63548
        assert result["printer.heap_total"] == 81700
        assert result["printer.cpu_usage"] == 80
        assert result["printer.stepper_stall"] == 10


class TestReadPrintState:
    def test_printing(self, mock_buffer):
        _put(mock_buffer, "is_printing", v=1)
        _put(mock_buffer, "print_filename", v="benchy.bgcode")
        _put(mock_buffer, "heater_enabled", v=1)
        _put(mock_buffer, "nozzle_pwm", v=80)
        _put(mock_buffer, "bed_pwm", v=50)
        result = read_print_state()
        assert result["printer.is_printing"] is True
        assert result["printer.print_filename"] == "benchy.bgcode"
        assert result["printer.pwm_nozzle"] == 80

    def test_idle(self, mock_buffer):
        _put(mock_buffer, "is_printing", v=0)
        _put(mock_buffer, "print_filename", v="")
        result = read_print_state()
        assert result["printer.is_printing"] is False


class TestSensorMetadata:
    def test_temperatures_meta(self):
        meta = read_temperatures._tool_meta
        assert meta["kind"] == "sensor"
        assert meta["refresh_hz"] == 0.3
        assert meta["history_depth"] == 30

    def test_position_meta(self):
        meta = read_position._tool_meta
        assert meta["refresh_hz"] == 5.0
        assert meta["history_depth"] == 10

    def test_firmware_health_meta(self):
        meta = read_firmware_health._tool_meta
        assert meta["refresh_hz"] == 0.2
        assert meta["history_depth"] == 5
