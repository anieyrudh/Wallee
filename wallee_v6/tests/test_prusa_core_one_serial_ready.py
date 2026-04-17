from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from wallee_v6.packs.prusa_core_one_plus import serial_ready


def test_ensure_serial_ready_returns_without_recovery(monkeypatch):
    settings = SimpleNamespace(serial_enabled=True, serial_port="/dev/ttyACM0")
    monkeypatch.setattr(
        serial_ready,
        "_status_payload",
        lambda _settings: {"job_active": False, "lifecycle": "STOPPED", "job_id": None},
    )
    monkeypatch.setattr(
        serial_ready,
        "_tty_device_info",
        lambda _port: {"interface_name": "1-2:1.0", "usb_device_name": "1-2"},
    )
    monkeypatch.setattr(
        serial_ready,
        "_serial_preflight",
        lambda _settings: {"ok": True, "command": "M400", "latency_ms": 12.3},
    )

    payload = serial_ready.ensure_serial_ready(settings)

    assert payload["ok"] is True
    assert payload["recovery_performed"] is False
    assert payload["preflight"]["command"] == "M400"
    assert [step["step"] for step in payload["recovery_steps"]] == ["initial_preflight"]


def test_ensure_serial_ready_recovers_after_usb_reset(monkeypatch):
    settings = SimpleNamespace(serial_enabled=True, serial_port="/dev/ttyACM0")
    monkeypatch.setattr(
        serial_ready,
        "_status_payload",
        lambda _settings: {"job_active": False, "lifecycle": "STOPPED", "job_id": None},
    )
    monkeypatch.setattr(
        serial_ready,
        "_tty_device_info",
        lambda _port: {"interface_name": "1-2:1.0", "usb_device_name": "1-2"},
    )
    attempts = {"count": 0}

    def fake_preflight(_settings):
        attempts["count"] += 1
        if attempts["count"] < 4:
            raise TimeoutError("serial port did not become writable before deadline")
        return {"ok": True, "command": "M400", "latency_ms": 101.0}

    monkeypatch.setattr(serial_ready, "_serial_preflight", fake_preflight)
    monkeypatch.setattr(serial_ready, "_port_holder_pids", lambda _port: [101, 202])
    killed: list[int] = []
    monkeypatch.setattr(serial_ready, "_kill_holder_pids", lambda pids: killed.extend(pids) or pids)
    cdc_calls: list[str] = []
    monkeypatch.setattr(
        serial_ready,
        "_recover_cdc_acm",
        lambda info: cdc_calls.append(info["interface_name"]) or {"step": "cdc_acm_rebind", "ok": True},
    )
    usb_calls: list[str] = []
    monkeypatch.setattr(
        serial_ready,
        "_recover_usb_authorized",
        lambda info: usb_calls.append(info["usb_device_name"]) or {"step": "usb_authorized_reset", "ok": True},
    )
    monkeypatch.setattr(serial_ready, "_wait_for_port", lambda _port, timeout_s=10.0: True)

    payload = serial_ready.ensure_serial_ready(settings)

    assert payload["ok"] is True
    assert payload["recovery_performed"] is True
    assert killed == [101, 202]
    assert cdc_calls == ["1-2:1.0"]
    assert usb_calls == ["1-2"]
    assert payload["preflight"]["latency_ms"] == 101.0
    assert [step["step"] for step in payload["recovery_steps"]] == [
        "initial_preflight",
        "kill_port_holders",
        "post_holder_cleanup_preflight",
        "cdc_acm_rebind",
        "wait_for_port_after_cdc_acm",
        "post_cdc_acm_preflight",
        "usb_authorized_reset",
        "wait_for_port_after_usb_reset",
        "post_usb_reset_preflight",
    ]


def test_ensure_serial_ready_requires_idle_printer(monkeypatch):
    settings = SimpleNamespace(serial_enabled=True, serial_port="/dev/ttyACM0")
    monkeypatch.setattr(
        serial_ready,
        "_status_payload",
        lambda _settings: {"job_active": True, "lifecycle": "PRINTING", "job_id": 7},
    )

    with pytest.raises(RuntimeError, match="requires an idle printer"):
        serial_ready.ensure_serial_ready(settings)


def test_tty_device_info_prefers_symlink_interface_name(monkeypatch, tmp_path):
    tty_name = "ttyACM0"
    tty_dir = tmp_path / "sys" / "class" / "tty" / tty_name
    tty_dir.mkdir(parents=True)
    resolved_interface = tmp_path / "devices" / "pci0000:00" / "usb1" / "1-2" / "1-2:1.0"
    resolved_interface.mkdir(parents=True)
    device_symlink = tty_dir / "device"
    device_symlink.symlink_to(resolved_interface)

    monkeypatch.setattr(serial_ready, "Path", lambda raw="": (tmp_path / raw.lstrip("/")) if isinstance(raw, str) else Path(raw))
    monkeypatch.setattr(serial_ready.os, "readlink", lambda path: "../../../1-2:1.0")

    info = serial_ready._tty_device_info(f"/dev/{tty_name}")

    assert info["interface_name"] == "1-2:1.0"
    assert info["usb_device_name"] == "1-2"
    assert info["device_symlink_target"] == "../../../1-2:1.0"


def test_recover_cdc_acm_rejects_invalid_interface_name():
    with pytest.raises(RuntimeError, match="invalid interface_name"):
        serial_ready._recover_cdc_acm({"interface_name": "device"})
