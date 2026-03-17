"""Tests for bus/network.py HTTP client."""

import httpx
import pytest

from wallee.bus.network import HTTPClient


BASE_URL = "http://testprinter"


def _mock_transport(handler):
    """Create an httpx MockTransport from a handler function."""
    return httpx.MockTransport(handler)


def _json_response(data, status_code=200):
    return httpx.Response(
        status_code,
        json=data,
        headers={"content-type": "application/json"},
    )


def _make_client(handler, api_key=""):
    """Build an HTTPClient with a mock transport."""
    client = HTTPClient(BASE_URL, api_key=api_key)
    client._client = httpx.Client(
        transport=_mock_transport(handler),
        base_url=BASE_URL,
        headers=client._headers,
    )
    return client


class TestHTTPClientGet:
    def test_get_success(self):
        def handler(request):
            assert request.url.path == "/api/v1/status"
            return _json_response({"printer": {"state": "IDLE"}})

        client = _make_client(handler)
        result = client.get("/api/v1/status")
        assert result == {"printer": {"state": "IDLE"}}
        client.close()

    def test_get_with_api_key(self):
        def handler(request):
            assert request.headers["x-api-key"] == "secret123"
            return _json_response({"ok": True})

        client = _make_client(handler, api_key="secret123")
        result = client.get("/api/v1/info")
        assert result == {"ok": True}
        client.close()

    def test_get_404(self):
        def handler(request):
            return httpx.Response(404)

        client = _make_client(handler)
        result = client.get("/api/v1/missing")
        assert "error" in result
        assert "404" in result["error"]
        client.close()

    def test_get_timeout(self):
        def handler(request):
            raise httpx.ReadTimeout("timed out")

        client = _make_client(handler)
        result = client.get("/api/v1/status")
        assert "error" in result
        assert "timed out" in result["error"]
        client.close()


class TestHTTPClientPut:
    def test_put_success(self):
        def handler(request):
            assert request.url.path == "/api/v1/job"
            import json
            body = json.loads(request.content)
            assert body["command"] == "RESUME"
            return _json_response({"status": "ok"})

        client = _make_client(handler)
        result = client.put("/api/v1/job", json_body={"command": "RESUME"})
        assert result == {"status": "ok"}
        client.close()

    def test_put_non_json_response(self):
        def handler(request):
            return httpx.Response(204, headers={"content-type": "text/plain"})

        client = _make_client(handler)
        result = client.put("/api/v1/job", json_body={"command": "PAUSE"})
        assert result["status"] == "success"
        assert result["status_code"] == 204
        client.close()


class TestHTTPClientPost:
    def test_post_success(self):
        def handler(request):
            return _json_response({"id": 42})

        client = _make_client(handler)
        result = client.post("/api/v1/job", json_body={"file": "/usb/test.gcode"})
        assert result == {"id": 42}
        client.close()


class TestHTTPClientDelete:
    def test_delete_success(self):
        def handler(request):
            assert request.method == "DELETE"
            return httpx.Response(204, headers={"content-type": "text/plain"})

        client = _make_client(handler)
        result = client.delete("/api/v1/job")
        assert result["status"] == "success"
        client.close()

    def test_delete_409_conflict(self):
        def handler(request):
            return httpx.Response(409)

        client = _make_client(handler)
        result = client.delete("/api/v1/job")
        assert "error" in result
        assert "409" in result["error"]
        client.close()
