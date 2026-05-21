"""Minimal Pi-side serial readiness and recovery helpers.

This module stays intentionally small so it can be used before live runs without
pulling in the full hardware_smoke surface. It focuses on one problem:
ensuring the bounded serial writer can open `/dev/ttyACM*` and send a harmless
command before Wallee starts a live print-control session.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from .adapters import PrusaCoreOneSettings, PrusaLinkHttpClient, PrusaSerialWriter
from .driver import status_to_snapshot


def _status_payload(settings: PrusaCoreOneSettings) -> dict[str, Any]:
    http = PrusaLinkHttpClient(settings)
    snapshot = status_to_snapshot(http.get_status(), job=http.get_job(), info=http.get_info())
    return {
        "lifecycle": snapshot.lifecycle.value,
        "job_active": snapshot.job_active,
        "job_id": snapshot.job_id,
        "current_file": snapshot.current_file,
        "speed_pct": snapshot.speed_pct,
        "flow_pct": snapshot.flow_pct,
        "nozzle_target_c": snapshot.nozzle_target_c,
        "bed_target_c": snapshot.bed_target_c,
    }


def _serial_preflight(settings: PrusaCoreOneSettings) -> dict[str, Any]:
    writer = PrusaSerialWriter(settings)
    try:
        return writer.preflight()
    finally:
        writer.close()


def _tty_device_info(serial_port: str) -> dict[str, str]:
    device_symlink = Path("/sys/class/tty") / Path(serial_port).name / "device"
    try:
        raw_target = os.readlink(device_symlink)
    except OSError:
        raw_target = ""

    device_link = device_symlink.resolve()
    interface_name = Path(raw_target).name if raw_target else device_link.name
    if not interface_name or interface_name == "device":
        interface_name = device_link.name

    usb_device_name = interface_name.split(":", 1)[0] if ":" in interface_name else device_link.parent.name
    usb_device = device_link.parent
    return {
        "tty_name": Path(serial_port).name,
        "interface_name": interface_name,
        "usb_device_name": usb_device_name,
        "device_symlink": str(device_symlink),
        "device_symlink_target": raw_target,
        "device_link": str(device_link),
        "usb_device_path": str(usb_device),
    }


def _run_command(argv: list[str], *, timeout_s: float = 15.0, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout_s)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed rc={result.returncode}: {argv}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def _sudo_sysfs_write(path: str, value: str) -> None:
    _run_command(
        [
            "sudo",
            "sh",
            "-lc",
            f"printf '%s' {json.dumps(value)} > {path}",
        ]
    )


def _port_holder_pids(serial_port: str) -> list[int]:
    result = _run_command(["lsof", "-t", serial_port], check=False)
    pids: list[int] = []
    for line in result.stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            pid = int(text)
        except ValueError:
            continue
        pids.append(pid)
    return sorted(set(pids))


def _kill_holder_pids(pids: list[int]) -> list[int]:
    killed: list[int] = []
    current_pid = os.getpid()
    for pid in pids:
        if pid == current_pid:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        killed.append(pid)
    if killed:
        time.sleep(1.0)
    return killed


def _wait_for_port(serial_port: str, *, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    path = Path(serial_port)
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.25)
    return path.exists()


def _recover_cdc_acm(info: dict[str, str]) -> dict[str, Any]:
    interface_name = info["interface_name"]
    if not interface_name or interface_name == "device" or ":" not in interface_name:
        raise RuntimeError(f"Refusing cdc_acm recovery with invalid interface_name={interface_name!r}")
    _sudo_sysfs_write("/sys/bus/usb/drivers/cdc_acm/unbind", interface_name)
    time.sleep(1.0)
    _sudo_sysfs_write("/sys/bus/usb/drivers/cdc_acm/bind", interface_name)
    return {"step": "cdc_acm_rebind", "interface_name": interface_name, "ok": True}


def _recover_usb_authorized(info: dict[str, str]) -> dict[str, Any]:
    base = f"/sys/bus/usb/devices/{info['usb_device_name']}/authorized"
    _sudo_sysfs_write(base, "0")
    time.sleep(2.0)
    _sudo_sysfs_write(base, "1")
    return {"step": "usb_authorized_reset", "usb_device_name": info["usb_device_name"], "ok": True}


def ensure_serial_ready(settings: PrusaCoreOneSettings) -> dict[str, Any]:
    if not settings.serial_enabled or not settings.serial_port:
        raise RuntimeError("Serial readiness requires PRUSA_CORE_ONE_ENABLE_SERIAL=1 and PRUSA_CORE_ONE_SERIAL_PORT")
    status = _status_payload(settings)
    if status.get("job_active"):
        raise RuntimeError("Serial readiness requires an idle printer so recovery does not perturb an active job")

    info = _tty_device_info(settings.serial_port)
    attempts: list[dict[str, Any]] = []

    def try_preflight(label: str) -> dict[str, Any] | None:
        try:
            payload = _serial_preflight(settings)
        except Exception as exc:
            attempts.append({"step": label, "ok": False, "error": repr(exc)})
            return None
        attempts.append({"step": label, "ok": True, "preflight": payload})
        return payload

    payload = try_preflight("initial_preflight")
    if payload is not None:
        return {
            "ok": True,
            "status": status,
            "preflight": payload,
            "recovery_performed": False,
            "recovery_steps": attempts,
            "serial_port": settings.serial_port,
            "tty_device": info,
        }

    holder_pids = _port_holder_pids(settings.serial_port)
    killed_pids = _kill_holder_pids(holder_pids)
    attempts.append(
        {
            "step": "kill_port_holders",
            "ok": True,
            "holder_pids": holder_pids,
            "killed_pids": killed_pids,
        }
    )
    payload = try_preflight("post_holder_cleanup_preflight")
    if payload is not None:
        return {
            "ok": True,
            "status": status,
            "preflight": payload,
            "recovery_performed": True,
            "recovery_steps": attempts,
            "serial_port": settings.serial_port,
            "tty_device": info,
        }

    attempts.append(_recover_cdc_acm(info))
    port_present = _wait_for_port(settings.serial_port)
    attempts.append({"step": "wait_for_port_after_cdc_acm", "ok": port_present, "serial_port": settings.serial_port})
    payload = try_preflight("post_cdc_acm_preflight")
    if payload is not None:
        return {
            "ok": True,
            "status": status,
            "preflight": payload,
            "recovery_performed": True,
            "recovery_steps": attempts,
            "serial_port": settings.serial_port,
            "tty_device": info,
        }

    attempts.append(_recover_usb_authorized(info))
    port_present = _wait_for_port(settings.serial_port)
    attempts.append({"step": "wait_for_port_after_usb_reset", "ok": port_present, "serial_port": settings.serial_port})
    payload = try_preflight("post_usb_reset_preflight")
    if payload is not None:
        return {
            "ok": True,
            "status": status,
            "preflight": payload,
            "recovery_performed": True,
            "recovery_steps": attempts,
            "serial_port": settings.serial_port,
            "tty_device": info,
        }

    return {
        "ok": False,
        "status": status,
        "recovery_performed": True,
        "recovery_steps": attempts,
        "serial_port": settings.serial_port,
        "tty_device": info,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ensure the Prusa serial control surface is ready before live runs.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Read compact status over PrusaLink")
    sub.add_parser("preflight", help="Run one minimal serial preflight with no recovery")
    sub.add_parser("ensure", help="Recover stale serial/device state if needed, then preflight")
    args = parser.parse_args(argv)

    settings = PrusaCoreOneSettings.from_env()
    if args.command == "status":
        print(json.dumps(_status_payload(settings), indent=2, sort_keys=True))
        return 0
    if args.command == "preflight":
        print(
            json.dumps(
                {
                    "status": _status_payload(settings),
                    "preflight": _serial_preflight(settings),
                    "serial_port": settings.serial_port,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    payload = ensure_serial_ready(settings)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
