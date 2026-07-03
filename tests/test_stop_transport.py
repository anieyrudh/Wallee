"""Unit tests for the generic machine-stop transport."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


from wallee.stop_transport import execute_stop

SIM_PROFILE = {
    "transport": "file",
    "primary_request": {"method": "POST", "path": "/sim/stop", "json": {"command": "STOP"}},
}


def test_file_transport_appends_stop_record(tmp_path):
    outcome = execute_stop(SIM_PROFILE, data_dir=tmp_path)
    assert outcome.ok
    log = tmp_path / "safety" / "stop_commands.jsonl"
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    assert lines[0]["request"] == SIM_PROFILE["primary_request"]

    execute_stop(SIM_PROFILE, data_dir=tmp_path)
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


def test_none_transport_is_a_safe_noop(tmp_path):
    outcome = execute_stop({"transport": "none"}, data_dir=tmp_path)
    assert not outcome.ok
    assert "no-op" in outcome.detail


def test_http_transport_missing_host_fails_cleanly(tmp_path):
    outcome = execute_stop(
        {"transport": "http", "host_env": "NOT_SET_HOST", "primary_request": {"method": "POST", "path": "/stop"}},
        data_dir=tmp_path,
        env={},
    )
    assert not outcome.ok
    assert "no control host" in outcome.detail


class _StopHandler(BaseHTTPRequestHandler):
    received: list[str] = []

    def do_POST(self):
        _StopHandler.received.append(self.path)
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


def test_http_transport_hits_primary_endpoint(tmp_path):
    _StopHandler.received = []
    server = HTTPServer(("127.0.0.1", 0), _StopHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        outcome = execute_stop(
            {
                "transport": "http",
                "host_env": "STOP_HOST",
                "primary_request": {"method": "POST", "path": "/api/v1/gcode", "json": {"command": "M25"}},
            },
            data_dir=tmp_path,
            env={"STOP_HOST": f"127.0.0.1:{port}"},
        )
    finally:
        thread.join(timeout=2)
        server.server_close()

    assert outcome.ok, outcome.detail
    assert _StopHandler.received == ["/api/v1/gcode"]


def test_transport_never_raises(tmp_path):
    # A malformed profile must yield a failed outcome, not an exception.
    outcome = execute_stop({"transport": "http", "primary_request": "not-a-dict"}, data_dir=tmp_path, env={"H": "x"})
    assert not outcome.ok
