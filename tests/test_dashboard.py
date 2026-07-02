"""Tests for dashboard server."""

import json
import threading
import time
import urllib.error
import urllib.request

import fakeredis
import pytest

from wallee.whiteboard.client import Whiteboard
from wallee.ui.dashboard import DashboardServer, DASHBOARD_HTML


@pytest.fixture
def wb():
    return Whiteboard(_redis=fakeredis.FakeRedis(decode_responses=True))


class TestDashboardHTML:
    def test_html_contains_key_sections(self):
        assert "WALLEE" in DASHBOARD_HTML
        assert "Key Metrics" in DASHBOARD_HTML
        assert "Vision Analysis" in DASHBOARD_HTML
        assert "Agent Log" in DASHBOARD_HTML
        assert "Cameras" in DASHBOARD_HTML

    def test_html_contains_summary_cards(self):
        assert "Phase" in DASHBOARD_HTML
        assert "Progress" in DASHBOARD_HTML
        assert "Adjustments" in DASHBOARD_HTML

    def test_no_inline_event_handlers(self):
        """Ensure no onclick/onerror XSS vectors in HTML."""
        assert "onclick=" not in DASHBOARD_HTML.lower()
        assert "onerror=" not in DASHBOARD_HTML.lower()
        assert "onload=" not in DASHBOARD_HTML.lower()

    def test_uses_textcontent_not_innerhtml(self):
        """Dashboard JS uses textContent for safety, not innerHTML."""
        # innerHTML appears only in a comment explaining the approach
        js_section = DASHBOARD_HTML.split("<script>")[1].split("</script>")[0]
        # Remove comments before checking
        import re
        js_no_comments = re.sub(r'/\*.*?\*/', '', js_section, flags=re.DOTALL)
        assert ".innerHTML" not in js_no_comments
        assert "textContent" in js_section


class TestDashboardServer:
    def test_serves_html(self, wb):
        wb.publish("printer.state", "IDLE")
        server = DashboardServer(wb, host="127.0.0.1", port=18765)
        server.start()
        time.sleep(0.5)

        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18765", timeout=3)
            html = resp.read().decode()
            assert "WALLEE" in html
            assert resp.status == 200
        finally:
            server.stop()

    def test_stop_is_clean(self, wb):
        server = DashboardServer(wb, host="127.0.0.1", port=18766)
        server.start()
        time.sleep(0.3)
        server.stop()
        # Should not hang or throw

    def test_can_restart_on_same_port_after_stop(self, wb):
        server = DashboardServer(wb, host="127.0.0.1", port=18767)
        server.start()
        time.sleep(0.3)
        server.stop()

        server2 = DashboardServer(wb, host="127.0.0.1", port=18767)
        server2.start()
        time.sleep(0.3)
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18767", timeout=3)
            assert resp.status == 200
        finally:
            server2.stop()


class TestDashboardAuth:
    def test_default_host_is_loopback(self, wb):
        server = DashboardServer(wb)
        assert server.host == "127.0.0.1"

    def test_non_loopback_host_without_token_refuses_to_start(self, wb, monkeypatch):
        monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
        with pytest.raises(ValueError, match="DASHBOARD_TOKEN"):
            DashboardServer(wb, host="0.0.0.0", port=18770)

    def test_non_loopback_host_with_token_is_allowed(self, wb):
        server = DashboardServer(wb, host="0.0.0.0", port=18770, token="secret")
        assert server.token == "secret"

    def test_token_authorized_paths(self, wb):
        server = DashboardServer(wb, host="127.0.0.1", port=18770, token="secret")
        assert server._token_authorized("/?token=secret") is True
        assert server._token_authorized("/?token=wrong") is False
        assert server._token_authorized("/") is False

    def test_no_token_configured_authorizes_everything(self, wb):
        server = DashboardServer(wb, host="127.0.0.1", port=18770, token="")
        assert server._token_authorized("/") is True

    def test_http_requires_token_when_configured(self, wb):
        server = DashboardServer(wb, host="127.0.0.1", port=18771, token="secret")
        server.start()
        time.sleep(0.5)
        try:
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen("http://127.0.0.1:18771", timeout=3)
            assert excinfo.value.code == 401

            resp = urllib.request.urlopen("http://127.0.0.1:18771/?token=secret", timeout=3)
            assert resp.status == 200
            assert "WALLEE" in resp.read().decode()
        finally:
            server.stop()
