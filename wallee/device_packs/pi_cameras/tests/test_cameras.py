"""Tests for pi_cameras sensor tools."""

import base64
import httpx
import pytest
from unittest.mock import patch

import wallee.device_packs.pi_cameras.sensors as cam_mod
from wallee.device_packs.pi_cameras.sensors import (
    read_nozzle_camera,
    read_buddy_cameras,
    discover_buddy_cameras,
    discover_nozzle_camera_port,
    STALE_THRESHOLD,
)

FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 500 + b"\xff\xd9"
FAKE_JPEG_2 = b"\xff\xd8\xff\xe0" + b"\x01" * 500 + b"\xff\xd9"


@pytest.fixture(autouse=True)
def reset_state():
    old_nozzle = cam_mod._nozzle_client
    old_nozzle_port = getattr(cam_mod, "_nozzle_port", None)
    old_discovered_port = getattr(cam_mod, "_discovered_nozzle_port", None)
    cam_mod._stale_state = {}
    cam_mod._discovered_buddies = []
    cam_mod._last_discovery = 0
    cam_mod._last_nozzle_discovery = 0
    cam_mod._discovered_nozzle_port = None
    cam_mod._nozzle_port = None
    yield
    cam_mod._nozzle_client = old_nozzle
    cam_mod._nozzle_port = old_nozzle_port
    cam_mod._discovered_nozzle_port = old_discovered_port


def _inject_nozzle(handler):
    cam_mod._nozzle_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="http://localhost:8083",
    )


class TestNozzleCamera:
    @patch("wallee.device_packs.pi_cameras.sensors.httpx.get")
    def test_discovers_nozzle_camera_port(self, mock_get):
        def handler(url, timeout):
            if url.endswith(":8083/snapshot"):
                return httpx.Response(200, content=FAKE_JPEG, headers={"content-type": "image/jpeg"})
            raise httpx.ConnectError("nope")

        mock_get.side_effect = handler
        assert discover_nozzle_camera_port(force=True) == "8083"

    def test_live_frame(self):
        cam_mod._discovered_nozzle_port = "8083"
        cam_mod._nozzle_port = "8083"
        _inject_nozzle(lambda r: httpx.Response(200, content=FAKE_JPEG))
        result = read_nozzle_camera()
        assert result["camera.nozzle_status"] == "live"
        assert result["camera.nozzle_port"] == "8083"
        assert "camera.nozzle_frame" in result

    def test_offline_on_error(self):
        _inject_nozzle(lambda r: httpx.Response(503, content=b""))
        result = read_nozzle_camera()
        assert result["camera.nozzle_status"] == "offline"

    def test_stale_detection(self):
        cam_mod._discovered_nozzle_port = "8083"
        cam_mod._nozzle_port = "8083"
        _inject_nozzle(lambda r: httpx.Response(200, content=FAKE_JPEG))
        for _ in range(STALE_THRESHOLD + 1):
            result = read_nozzle_camera()
        assert result["camera.nozzle_status"] == "stale"

    def test_metadata(self):
        meta = read_nozzle_camera._tool_meta
        assert meta["kind"] == "sensor"
        assert meta["refresh_hz"] == 1.0


class TestBuddyDiscovery:
    @patch("wallee.device_packs.pi_cameras.sensors.subprocess.run")
    def test_finds_cameras_by_mac(self, mock_run):
        mock_run.return_value = type("R", (), {
            "stdout": "192.168.0.194 dev eth0 lladdr 88:49:2d:a2:d3:a8 REACHABLE\n"
                      "192.168.0.200 dev eth0 lladdr 88:49:2d:a2:c1:ce STALE\n"
                      "192.168.0.195 dev eth0 lladdr f0:24:f9:c6:ee:2d REACHABLE\n",
            "returncode": 0,
        })()
        ips = discover_buddy_cameras()
        assert "192.168.0.194" in ips
        assert "192.168.0.200" in ips
        assert "192.168.0.195" not in ips  # printer, not a buddy cam

    @patch("wallee.device_packs.pi_cameras.sensors.subprocess.run")
    def test_no_cameras(self, mock_run):
        mock_run.return_value = type("R", (), {
            "stdout": "192.168.0.195 dev eth0 lladdr f0:24:f9:c6:ee:2d REACHABLE\n",
            "returncode": 0,
        })()
        ips = discover_buddy_cameras()
        assert ips == []

    @patch("wallee.device_packs.pi_cameras.sensors.subprocess.run")
    def test_caches_results(self, mock_run):
        mock_run.side_effect = [
            type("R", (), {"stdout": "default via 192.168.0.1 dev eth0\n", "returncode": 0})(),
            type("R", (), {"stdout": "", "returncode": 0})(),
            type("R", (), {
                "stdout": "192.168.0.194 dev eth0 lladdr 88:49:2d:a2:d3:a8 REACHABLE\n",
                "returncode": 0,
            })(),
        ]
        discover_buddy_cameras()
        discover_buddy_cameras()
        assert mock_run.call_count == 3
        assert mock_run.call_args_list[0].args[0] == ["ip", "route", "show", "default"]
        assert mock_run.call_args_list[2].args[0] == ["ip", "neigh", "show"]


class TestBuddyCameraSensor:
    @patch("wallee.device_packs.pi_cameras.sensors.discover_buddy_cameras", return_value=[])
    def test_no_buddies(self, mock_disc):
        result = read_buddy_cameras()
        assert result["camera.buddy_count"] == 0
        assert result["camera.buddy1_status"] == "offline"

    @patch("wallee.device_packs.pi_cameras.sensors._capture_rtsp_jpeg", return_value=FAKE_JPEG)
    @patch("wallee.device_packs.pi_cameras.sensors.discover_buddy_cameras", return_value=["192.168.0.194"])
    def test_one_buddy(self, mock_disc, mock_rtsp):
        result = read_buddy_cameras()
        assert result["camera.buddy_count"] == 1
        assert result["camera.buddy1_status"] == "live"
        assert result["camera.buddy1_ip"] == "192.168.0.194"
        assert "camera.buddy1_frame" in result
        assert result["camera.buddy2_status"] == "offline"

    def test_metadata(self):
        meta = read_buddy_cameras._tool_meta
        assert meta["kind"] == "sensor"
        assert meta["refresh_hz"] == 0.1
