"""Tests for generic serial bus and prusa_serial actuators."""

import pytest
from unittest.mock import patch, MagicMock

from wallee.bus.serial import SerialBus, find_serial_port, _is_garbled
from wallee.device_packs.prusa_serial.actuators import (
    PRUSA_BLACKLISTED,
    PRUSA_ALLOWED_DIAGNOSTIC_GCODE,
    PRUSA_M115_SPAM,
)
import wallee.device_packs.prusa_serial.actuators as actuators_mod
from wallee.device_packs.prusa_serial.actuators import (
    read_endstops,
    send_gcode,
)


class TestGarbleDetection:
    def test_clean_line(self):
        assert _is_garbled("ok T:215.00/215.00 B:60.00/60.00") is False

    def test_garbled_line(self):
        assert _is_garbled("\x01\x03\x00\x1b\x04\x02MA\x00\x01") is True

    def test_empty_line(self):
        assert _is_garbled("") is True

    def test_mostly_printable(self):
        assert _is_garbled("X:242.00 Y:-9.00 Z:4.00 E:0.00") is False


class TestSpamFilter:
    def test_firmware_name(self):
        bus = SerialBus(spam_patterns=PRUSA_M115_SPAM)
        assert bus._is_spam("FIRMWARE_NAME:Prusa-Firmware-Buddy 6.4.0") is True

    def test_capability(self):
        bus = SerialBus(spam_patterns=PRUSA_M115_SPAM)
        assert bus._is_spam("Cap:SERIAL_XON_XOFF:0") is True

    def test_normal_line(self):
        bus = SerialBus(spam_patterns=PRUSA_M115_SPAM)
        assert bus._is_spam("ok T:215.00/215.00 B:60.00/60.00") is False


class TestBlacklist:
    def test_blacklisted_commands(self):
        assert "M997" in PRUSA_BLACKLISTED
        assert "M112" in PRUSA_BLACKLISTED

    def test_blacklist_enforced(self):
        serial = SerialBus(port="/dev/null", blacklisted_commands=PRUSA_BLACKLISTED)
        for cmd in PRUSA_BLACKLISTED:
            result = serial.send_command(cmd)
            assert "error" in result
            assert "BLACKLISTED" in result["error"]

    def test_normal_commands_not_blacklisted(self):
        serial = SerialBus(port="/dev/fake_nonexistent", blacklisted_commands=PRUSA_BLACKLISTED)
        result = serial.send_command("M105")
        assert "BLACKLISTED" not in result.get("error", "")


class TestFindSerialPort:
    @patch("os.path.isdir", return_value=False)
    @patch("os.path.exists", return_value=False)
    def test_no_port_found(self, mock_exists, mock_isdir):
        assert find_serial_port(by_id_pattern="Prusa", fallback_path="/dev/ttyACM0") is None

    @patch("os.path.isdir", return_value=False)
    @patch("os.path.exists", return_value=True)
    def test_fallback_ttyACM0(self, mock_exists, mock_isdir):
        assert find_serial_port(by_id_pattern="Prusa", fallback_path="/dev/ttyACM0") == "/dev/ttyACM0"

    @patch("os.path.isdir", return_value=True)
    @patch("os.listdir", return_value=[
        "usb-Prusa_Research__prusa3d.com__Original_Prusa_COREONE_123-if00"
    ])
    @patch("os.path.realpath", return_value="/dev/ttyACM0")
    @patch("os.path.exists", return_value=True)
    def test_find_by_id(self, mock_exists, mock_realpath, mock_listdir, mock_isdir):
        assert find_serial_port(by_id_pattern="Prusa") == "/dev/ttyACM0"


class TestSerialBusSendCommand:
    def test_no_port(self):
        serial = SerialBus(port=None, find_port_fn=lambda: None)
        result = serial.send_command("M105")
        assert "error" in result
        assert "No serial port" in result["error"]

    def test_explicit_port_not_found(self):
        serial = SerialBus(port="/dev/nonexistent_port")
        result = serial.send_command("M105")
        assert "error" in result


class TestReadEndstops:
    @pytest.fixture(autouse=True)
    def mock_serial(self):
        old = actuators_mod._serial
        mock = MagicMock(spec=SerialBus)
        actuators_mod._serial = mock
        yield mock
        actuators_mod._serial = old

    def test_success(self, mock_serial):
        mock_serial.send_command.return_value = {
            "status": "success",
            "lines": [
                "Reporting endstop status",
                "x_min: open",
                "x_max: open",
                "y_min: open",
                "y_max: open",
                "z_min: open",
                "z_max: open",
                "ok",
            ],
        }
        result = read_endstops()
        assert result["printer.endstop_x_min"] == "open"
        assert result["printer.endstop_z_max"] == "open"
        assert len([k for k in result if k.startswith("printer.endstop_")]) == 6

    def test_triggered(self, mock_serial):
        mock_serial.send_command.return_value = {
            "status": "success",
            "lines": ["z_min: TRIGGERED", "ok"],
        }
        result = read_endstops()
        assert result["printer.endstop_z_min"] == "TRIGGERED"

    def test_serial_error(self, mock_serial):
        mock_serial.send_command.return_value = {"error": "Serial port not available"}
        result = read_endstops()
        assert "error" in result

    def test_metadata(self):
        meta = read_endstops._tool_meta
        assert meta["kind"] == "actuator"
        assert meta["requires_approval"] is False


class TestSendGcode:
    @pytest.fixture(autouse=True)
    def mock_serial(self):
        old = actuators_mod._serial
        mock = MagicMock(spec=SerialBus)
        actuators_mod._serial = mock
        yield mock
        actuators_mod._serial = old

    def test_success(self, mock_serial):
        mock_serial.send_command.return_value = {
            "status": "success",
            "lines": ["echo:E0 Flow: 100%", "ok"],
        }
        result = send_gcode(command="M105")
        assert result["status"] == "success"
        assert result["command"] == "M105"

    def test_rejects_non_allowlisted_command(self, mock_serial):
        result = send_gcode(command="M221")
        assert "error" in result
        assert "not allowed" in result["error"]
        mock_serial.send_command.assert_not_called()

    def test_rejects_multiline_or_parameterized_command(self, mock_serial):
        result = send_gcode(command="M105 ; M112")
        assert "error" in result
        mock_serial.send_command.assert_not_called()

        mock_serial.reset_mock()
        result = send_gcode(command="M105 S1")
        assert "error" in result
        mock_serial.send_command.assert_not_called()

    def test_allowlist_contains_only_diagnostic_commands(self):
        assert PRUSA_ALLOWED_DIAGNOSTIC_GCODE == {"M105", "M114", "M115", "M119", "M503"}

    def test_requires_approval(self):
        meta = send_gcode._tool_meta
        assert meta["requires_approval"] is False
