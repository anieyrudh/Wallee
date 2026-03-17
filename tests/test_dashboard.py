"""Tests for dashboard server."""

import json
import threading
import time
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
        assert "Print Status" in DASHBOARD_HTML
        assert "Temperatures" in DASHBOARD_HTML
        assert "Electrical" in DASHBOARD_HTML
        assert "Nozzle Camera" in DASHBOARD_HTML

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
