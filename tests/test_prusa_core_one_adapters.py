from __future__ import annotations

import sys
import types
from urllib import request

from wallee.packs.prusa_core_one_plus.adapters import (
    PrusaCoreOneSettings,
    PrintableFile,
    PrusaLinkHttpClient,
    PrusaSerialWriter,
    flatten_prusalink_file_tree,
    quote_printer_path,
)


def test_flatten_prusalink_file_tree_handles_nested_directories_and_refs():
    payload = {
        "path": "/usb",
        "children": [
            {
                "name": "PRINTS",
                "children": [
                    {
                        "name": "BENCHY~2.BGC",
                        "display_name": "Benchy Rules.bgcode",
                        "path": "/usb/PRINTS/BENCHY~2.BGC",
                        "size": 1200000,
                        "m_timestamp": 1711800000,
                        "refs": {"download": "/api/files/usb/PRINTS/BENCHY~2.BGC/raw"},
                    }
                ],
            },
            {
                "name": "README.TXT",
                "path": "/usb/README.TXT",
                "size": 128,
            },
        ],
    }

    files = flatten_prusalink_file_tree(payload)
    assert files == [
        PrintableFile(
            path="/usb/PRINTS/BENCHY~2.BGC",
            display_name="Benchy Rules.bgcode",
            size_bytes=1200000,
            printable=True,
            modified_ts=1711800000,
            refs={"download": "/api/files/usb/PRINTS/BENCHY~2.BGC/raw"},
        )
    ]


def test_quote_printer_path_accepts_multiple_input_forms():
    assert quote_printer_path("/usb/PRINTS/BENCHY~2.BGC") == "PRINTS/BENCHY~2.BGC"
    assert quote_printer_path("usb/PRINTS/BENCHY~2.BGC") == "PRINTS/BENCHY~2.BGC"
    assert quote_printer_path("PRINTS/Benchy Rules.bgcode") == "PRINTS/Benchy%20Rules.bgcode"


def test_settings_from_env_support_current_keys(monkeypatch):
    monkeypatch.setenv("PRUSA_CORE_ONE_HOST", "192.0.2.10")
    monkeypatch.setenv("PRUSA_CORE_ONE_API_KEY", "token")
    monkeypatch.setenv("PRUSA_CORE_ONE_ENABLE_SERIAL", "1")
    monkeypatch.setenv("PRUSA_CORE_ONE_SERIAL_PORT", "/dev/ttyACM0")
    monkeypatch.setenv("PRUSA_CORE_ONE_ENABLE_EXPERIMENTAL_TUNING", "1")
    monkeypatch.setenv("PRUSA_CORE_ONE_ENABLE_SPEED_TUNING_VERIFICATION", "1")
    monkeypatch.setenv("PRUSA_CORE_ONE_ENABLE_FLOW_TUNING_VERIFICATION", "1")
    monkeypatch.setenv("PRUSA_CORE_ONE_NOTEBOOK_DIR", "/tmp/notebooks")
    monkeypatch.setenv("PRUSA_CORE_ONE_NOZZLE_CAMERA_HOST", "127.0.0.1")
    monkeypatch.setenv("PRUSA_CORE_ONE_NOZZLE_CAMERA_PORT", "8083")
    monkeypatch.setenv("PRUSA_CORE_ONE_NOZZLE_CAMERA_DISCOVERY_PORTS", "8083,8084")
    monkeypatch.setenv("PRUSA_CORE_ONE_NOZZLE_CAMERA_DEVICE_PATH", "/dev/video9")
    monkeypatch.setenv("PRUSA_CORE_ONE_NOZZLE_CAMERA_MAX_FRAME_AGE_S", "12.5")
    monkeypatch.setenv("PRUSA_CORE_ONE_NOZZLE_CAMERA_LIVENESS_WINDOW_SIZE", "3")
    monkeypatch.setenv("PRUSA_CORE_ONE_VISION_API_KEY", "vision-token")
    monkeypatch.setenv("PRUSA_CORE_ONE_VISION_MODEL", "vision-model")
    monkeypatch.setenv("PRUSA_CORE_ONE_ENABLE_VISION_DEBUG_CONTEXT", "1")
    monkeypatch.setenv("PRUSA_CORE_ONE_VISION_ADVISORY_INTERVAL_S", "12.0")

    settings = PrusaCoreOneSettings.from_env()

    assert settings.host == "http://192.0.2.10"
    assert settings.api_key == "token"
    assert settings.serial_enabled is True
    assert settings.serial_port == "/dev/ttyACM0"
    assert settings.enable_experimental_tuning is True
    assert settings.speed_tuning_verification_enabled is True
    assert settings.flow_tuning_verification_enabled is True
    assert settings.notebook_dir == "/tmp/notebooks"
    assert settings.nozzle_camera_host == "127.0.0.1"
    assert settings.nozzle_camera_port == "8083"
    assert settings.nozzle_camera_discovery_ports == ("8083", "8084")
    assert settings.nozzle_camera_device_path == "/dev/video9"
    assert settings.nozzle_camera_max_frame_age_s == 12.5
    assert settings.nozzle_camera_liveness_window_size == 3
    assert settings.vision_api_key == "vision-token"
    assert settings.vision_model == "vision-model"
    assert settings.enable_vision_debug_context is True
    assert settings.vision_advisory_interval_s == 12.0


def test_settings_from_env_supports_v5_aliases(monkeypatch):
    monkeypatch.delenv("PRUSA_CORE_ONE_HOST", raising=False)
    monkeypatch.delenv("PRUSA_CORE_ONE_API_KEY", raising=False)
    monkeypatch.delenv("PRUSA_CORE_ONE_NOZZLE_CAMERA_PORT", raising=False)
    monkeypatch.delenv("PRUSA_CORE_ONE_VISION_MODEL", raising=False)
    monkeypatch.setenv("PRUSALINK_HOST", "http://printer.local")
    monkeypatch.setenv("PRUSALINK_API_KEY", "legacy-token")
    monkeypatch.setenv("NOZZLE_CAMERA_PORT", "8080")
    monkeypatch.setenv("VISION_MODEL", "google/gemini-3.1-flash-lite-preview")

    settings = PrusaCoreOneSettings.from_env()

    assert settings.host == "http://printer.local"
    assert settings.api_key == "legacy-token"
    assert settings.nozzle_camera_port == "8080"
    assert settings.vision_model == "google/gemini-3.1-flash-lite-preview"
    assert settings.speed_tuning_verification_enabled is False
    assert settings.flow_tuning_verification_enabled is False


def test_http_client_download_file_text_uses_download_path(monkeypatch):
    calls: list[tuple[str, str]] = []

    class _Response:
        def __init__(self, *, status: int, body: bytes):
            self.status = status
            self._body = body

        def read(self) -> bytes:
            return self._body

    def fake_urlopen(req, timeout):
        calls.append((req.method, req.full_url))
        return _Response(status=200, body=b";LAYER_CHANGE\nG1 X10 Y10\n")

    monkeypatch.setattr(request, "urlopen", fake_urlopen)

    settings = PrusaCoreOneSettings(
        host="http://printer.local",
        api_key="token",
        http_timeout_s=1.0,
        file_action_limit=2,
        state_transition_timeout_s=1.0,
        status_poll_interval_s=0.1,
        safe_to_unload_bed_c=35.0,
        safe_to_touch_nozzle_c=50.0,
        max_nozzle_target_c=300.0,
        max_bed_target_c=120.0,
        serial_enabled=False,
        serial_port=None,
        serial_baud=115200,
        serial_timeout_s=1.0,
        notebook_dir=None,
        notebook_download_enabled=True,
        notebook_lookahead_pct=5.0,
        enable_experimental_tuning=False,
    )
    client = PrusaLinkHttpClient(settings)
    text = client.download_file_text("/usb/PRINTS/BENCHY~2.BGC", download_path="/api/files/usb/PRINTS/BENCHY~2.BGC/raw")

    assert text.startswith(";LAYER_CHANGE")
    assert calls == [("GET", "http://printer.local/api/files/usb/PRINTS/BENCHY~2.BGC/raw")]


def test_http_client_download_file_bytes_uses_download_path(monkeypatch):
    calls: list[tuple[str, str]] = []

    class _Response:
        def __init__(self, *, status: int, body: bytes):
            self.status = status
            self._body = body

        def read(self) -> bytes:
            return self._body

    def fake_urlopen(req, timeout):
        calls.append((req.method, req.full_url))
        return _Response(status=200, body=b"GCDE\x01\x00\x00\x00\x00\x00")

    monkeypatch.setattr(request, "urlopen", fake_urlopen)

    settings = PrusaCoreOneSettings(
        host="http://printer.local",
        api_key="token",
        http_timeout_s=1.0,
        file_action_limit=2,
        state_transition_timeout_s=1.0,
        status_poll_interval_s=0.1,
        safe_to_unload_bed_c=35.0,
        safe_to_touch_nozzle_c=50.0,
        max_nozzle_target_c=300.0,
        max_bed_target_c=120.0,
        serial_enabled=False,
        serial_port=None,
        serial_baud=115200,
        serial_timeout_s=1.0,
        notebook_dir=None,
        notebook_download_enabled=True,
        notebook_lookahead_pct=5.0,
        enable_experimental_tuning=False,
    )
    client = PrusaLinkHttpClient(settings)
    payload = client.download_file_bytes("/usb/PRINTS/BENCHY~2.BGC", download_path="/api/files/usb/PRINTS/BENCHY~2.BGC/raw")

    assert payload == b"GCDE\x01\x00\x00\x00\x00\x00"
    assert calls == [("GET", "http://printer.local/api/files/usb/PRINTS/BENCHY~2.BGC/raw")]


def test_prusa_serial_writer_reuses_port_within_session_and_reopens_for_new_session(monkeypatch):
    created_ports: list[object] = []

    class _FakePort:
        def __init__(self):
            self.commands: list[bytes] = []
            self.closed = False
            self.rts = None

        def reset_input_buffer(self):
            return None

        def write(self, payload: bytes):
            self.commands.append(payload)
            return len(payload)

        def flush(self):
            return None

        def readline(self):
            return b"ok\n"

        def close(self):
            self.closed = True

    def fake_serial_ctor(*args, **kwargs):
        port = _FakePort()
        created_ports.append(port)
        return port

    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=fake_serial_ctor))

    settings = PrusaCoreOneSettings(
        host="http://printer.local",
        api_key="token",
        http_timeout_s=1.0,
        file_action_limit=2,
        state_transition_timeout_s=1.0,
        status_poll_interval_s=0.1,
        safe_to_unload_bed_c=35.0,
        safe_to_touch_nozzle_c=50.0,
        max_nozzle_target_c=300.0,
        max_bed_target_c=120.0,
        serial_enabled=True,
        serial_port="/dev/ttyACM0",
        serial_baud=115200,
        serial_timeout_s=1.0,
        notebook_dir=None,
        notebook_download_enabled=True,
        notebook_lookahead_pct=5.0,
        enable_experimental_tuning=False,
    )

    writer = PrusaSerialWriter(settings)
    writer.bind_session("job:1")
    writer.send_command("M220 S85")
    writer.query_command("M220")
    writer.bind_session("job:1")
    writer.send_command("M221 S95")
    writer.bind_session("job:2")
    writer.send_command("M104 S215")
    writer.close()

    assert len(created_ports) == 2
    first_port = created_ports[0]
    second_port = created_ports[1]
    assert first_port.commands == [b"M220 S85\n", b"M220\n", b"M221 S95\n"]
    assert first_port.closed is True
    assert second_port.commands == [b"M104 S215\n"]
    assert second_port.closed is True


def test_prusa_serial_writer_send_command_uses_persistent_port(monkeypatch):
    observed: dict[str, object] = {}

    class _FakePort:
        def __init__(self):
            self.commands: list[bytes] = []
            self.closed = False
            self.rts = None

        def reset_input_buffer(self):
            observed["reset"] = True

        def write(self, payload: bytes):
            self.commands.append(payload)
            observed["last_payload"] = payload
            return len(payload)

        def flush(self):
            observed["flushed"] = True

        def readline(self):
            return b""

        def close(self):
            self.closed = True

    fake_port = _FakePort()

    def fake_serial_ctor(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return fake_port

    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=fake_serial_ctor))

    settings = PrusaCoreOneSettings(
        host="http://printer.local",
        api_key="token",
        http_timeout_s=1.0,
        file_action_limit=2,
        state_transition_timeout_s=1.0,
        status_poll_interval_s=0.1,
        safe_to_unload_bed_c=35.0,
        safe_to_touch_nozzle_c=50.0,
        max_nozzle_target_c=300.0,
        max_bed_target_c=120.0,
        serial_enabled=True,
        serial_port="/dev/ttyACM0",
        serial_baud=115200,
        serial_timeout_s=1.0,
        notebook_dir=None,
        notebook_download_enabled=True,
        notebook_lookahead_pct=5.0,
        enable_experimental_tuning=False,
    )

    writer = PrusaSerialWriter(settings)
    writer.send_command("M220 S95")

    assert observed["kwargs"] == {
        "baudrate": 115200,
        "timeout": 1.0,
        "dsrdtr": False,
    }
    assert observed["last_payload"] == b"M220 S95\n"
    assert observed["flushed"] is True


def test_prusa_serial_writer_preflight_returns_command_payload(monkeypatch):
    class _FakePort:
        def __init__(self):
            self.rts = None

        def reset_input_buffer(self):
            return None

        def write(self, payload: bytes):
            return len(payload)

        def flush(self):
            return None

        def readline(self):
            return b"ok\n"

        def close(self):
            return None

    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=lambda *args, **kwargs: _FakePort()))

    settings = PrusaCoreOneSettings(
        host="http://printer.local",
        api_key="token",
        http_timeout_s=1.0,
        file_action_limit=2,
        state_transition_timeout_s=1.0,
        status_poll_interval_s=0.1,
        safe_to_unload_bed_c=35.0,
        safe_to_touch_nozzle_c=50.0,
        max_nozzle_target_c=300.0,
        max_bed_target_c=120.0,
        serial_enabled=True,
        serial_port="/dev/ttyACM0",
        serial_baud=115200,
        serial_timeout_s=1.0,
        notebook_dir=None,
        notebook_download_enabled=True,
        notebook_lookahead_pct=5.0,
        enable_experimental_tuning=False,
    )

    writer = PrusaSerialWriter(settings)
    payload = writer.preflight()

    assert payload["ok"] is True
    assert payload["command"] == "M400"


def test_prusa_serial_writer_reports_helper_failure(monkeypatch):
    class _FakePort:
        def __init__(self):
            self.rts = None

        def reset_input_buffer(self):
            raise RuntimeError("boom")

        def write(self, payload: bytes):
            return len(payload)

        def flush(self):
            return None

        def readline(self):
            return b""

        def close(self):
            return None

    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=lambda *args, **kwargs: _FakePort()))

    settings = PrusaCoreOneSettings(
        host="http://printer.local",
        api_key="token",
        http_timeout_s=1.0,
        file_action_limit=2,
        state_transition_timeout_s=1.0,
        status_poll_interval_s=0.1,
        safe_to_unload_bed_c=35.0,
        safe_to_touch_nozzle_c=50.0,
        max_nozzle_target_c=300.0,
        max_bed_target_c=120.0,
        serial_enabled=True,
        serial_port="/dev/ttyACM0",
        serial_baud=115200,
        serial_timeout_s=1.0,
        notebook_dir=None,
        notebook_download_enabled=True,
        notebook_lookahead_pct=5.0,
        enable_experimental_tuning=False,
    )

    writer = PrusaSerialWriter(settings)
    try:
        writer.send_command("M221")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected serial failure to raise")


def test_prusa_serial_writer_query_command_reads_from_fd_when_available(monkeypatch):
    class _FakePort:
        def __init__(self):
            self.rts = None
            self._lines = [b"echo:E0 Flow: 95%\n", b"ok\n"]

        def reset_input_buffer(self):
            return None

        def write(self, payload: bytes):
            return len(payload)

        def flush(self):
            return None

        def readline(self):
            return self._lines.pop(0) if self._lines else b""

        def close(self):
            return None

    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=lambda *args, **kwargs: _FakePort()))

    settings = PrusaCoreOneSettings(
        host="http://printer.local",
        api_key="token",
        http_timeout_s=1.0,
        file_action_limit=2,
        state_transition_timeout_s=1.0,
        status_poll_interval_s=0.1,
        safe_to_unload_bed_c=35.0,
        safe_to_touch_nozzle_c=50.0,
        max_nozzle_target_c=300.0,
        max_bed_target_c=120.0,
        serial_enabled=True,
        serial_port="/dev/ttyACM0",
        serial_baud=115200,
        serial_timeout_s=1.0,
        notebook_dir=None,
        notebook_download_enabled=True,
        notebook_lookahead_pct=5.0,
        enable_experimental_tuning=False,
    )

    writer = PrusaSerialWriter(settings)
    lines = writer.query_command("M221")

    assert lines == ["echo:E0 Flow: 95%", "ok"]
