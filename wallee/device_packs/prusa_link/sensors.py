"""Sensor tools for Prusa printer via PrusaLink HTTP API (/api/v1/* only).

HTTP sensors provide:
- Job progress and state (metrics stream lacks progress %)
- Temperatures as fallback (metrics stream stops sending temp_bed, chamber_temp
  during IDLE state, but the HTTP API always reports them)
- Printer identity (once on startup)
- File listing
"""

import logging
import os
import time as _time

from wallee.tools.decorator import tool

logger = logging.getLogger(__name__)

import threading as _threading

_http = None
_http_lock = _threading.Lock()


def _get_http():
    """Get or create the shared HTTP client from env config. Thread-safe."""
    global _http
    if _http is None:
        with _http_lock:
            if _http is None:  # Double-checked locking
                host = os.environ.get("PRUSALINK_HOST", "").strip()
                api_key = os.environ.get("PRUSALINK_API_KEY", "").strip()
                if not host:
                    logger.warning("PRUSALINK_HOST not set, prusa_link sensors disabled")
                    return None
                from wallee.bus.network import HTTPClient
                base_url = host if host.startswith("http") else f"http://{host}"
                _http = HTTPClient(base_url=base_url, api_key=api_key, timeout=5.0)
                logger.info(f"PrusaLink HTTP client initialized: {base_url}")
    return _http


@tool(kind="sensor", refresh_hz=0.5, history_depth=0)
def read_printer_state() -> dict:
    """Read printer state and job progress from /api/v1/status.

    The metrics stream has is_printing but NOT job progress percentage,
    time_remaining, or detailed job state. This sensor fills that gap.
    """
    http = _get_http()
    if http is None:
        return {"error": "PrusaLink not configured"}

    status = http.get("/api/v1/status")
    if "error" in status:
        return {"printer.api_error": status["error"]}

    printer = status.get("printer", {})
    result = {
        "printer.state": printer.get("state", "UNKNOWN"),
    }

    # Temperatures — HTTP API always reports these, even during IDLE.
    # The metrics stream stops sending temp_bed/ttemp_bed/chamber_temp when idle.
    # These serve as a reliable fallback.
    for api_key, wb_key in [
        ("temp_nozzle", "printer.temp_nozzle"),
        ("target_nozzle", "printer.target_nozzle"),
        ("temp_bed", "printer.temp_bed"),
        ("target_bed", "printer.target_bed"),
    ]:
        val = printer.get(api_key)
        if val is not None:
            result[wb_key] = round(float(val), 1)

    # Speed and flow factors
    for api_key, wb_key in [
        ("speed", "printer.speed"),
        ("flow", "printer.flow"),
    ]:
        val = printer.get(api_key)
        if val is not None:
            result[wb_key] = val

    job = status.get("job", {})
    if job:
        job_state = job.get("state") or printer.get("state", "UNKNOWN")
        result["printer.job_state"] = job_state
        result["printer.job_progress"] = job.get("progress", 0)
        time_remaining = job.get("time_remaining")
        if time_remaining is not None:
            result["printer.job_time_remaining_s"] = time_remaining
        time_printing = job.get("time_printing")
        if time_printing is not None:
            result["printer.job_time_printing_s"] = time_printing
    else:
        result["printer.job_state"] = "IDLE"

    return result


@tool(kind="sensor", refresh_hz=0.1, history_depth=0)
def read_printer_info() -> dict:
    """Read printer identity from /api/v1/info and /api/version. Called rarely."""
    http = _get_http()
    if http is None:
        return {"error": "PrusaLink not configured"}

    info = http.get("/api/v1/info")
    if "error" in info:
        return {"printer.api_error": info["error"]}

    version = http.get("/api/version")
    firmware = "unknown"
    model = info.get("hostname", "unknown")
    if isinstance(version, dict) and "error" not in version:
        firmware = version.get("server", version.get("text", "unknown"))
        model = version.get("hostname", model)

    return {
        "printer.firmware": firmware,
        "printer.model": model,
        "printer.serial": info.get("serial", "unknown"),
        "printer.nozzle_diameter": info.get("nozzle_diameter", "unknown"),
    }


@tool(kind="sensor", refresh_hz=0.02, history_depth=0)
def read_file_list() -> dict:
    """Read file listing from USB storage. Very slow refresh (every 50s)."""
    http = _get_http()
    if http is None:
        return {"error": "PrusaLink not configured"}

    files = http.get("/api/v1/files/usb")
    if "error" in files:
        return {"printer.api_error": files["error"]}

    # Extract just names and types for whiteboard
    file_list = []
    children = files.get("children", [])
    for f in children:
        entry = {
            "name": f.get("display_name", f.get("name", "?")),
            "type": f.get("type", "UNKNOWN"),
        }
        if "size" in f:
            entry["size"] = f["size"]
        file_list.append(entry)

    return {"printer.files": file_list}


# Module-level state for phase tracking
_phase_state = {"phase": "IDLE", "entered": 0.0}


@tool(kind="sensor", refresh_hz=1.0, history_depth=10)
def read_job_phase() -> dict:
    """Derive high-level print phase from PrusaLink status.

    Publishes: job.phase, job.phase_detail, job.time_in_phase_s

    Phases: IDLE, PREPARING, PRINTING, PAUSED, FINISHED, ERROR
    PREPARING = printer says PRINTING but progress == 0 (still purging/heating)
    """
    global _phase_state

    http = _get_http()
    if http is None:
        return {"error": "PrusaLink not configured"}

    status = http.get("/api/v1/status")
    if "error" in status:
        return {"error": status["error"]}

    printer = status.get("printer", {})
    job = status.get("job", {})

    state = printer.get("state", "IDLE")
    progress = float(job.get("progress", 0) or 0)
    temp_nozzle = float(printer.get("temp_nozzle", 0) or 0)
    target_nozzle = float(printer.get("target_nozzle", 0) or 0)
    temp_bed = float(printer.get("temp_bed", 0) or 0)
    target_bed = float(printer.get("target_bed", 0) or 0)

    has_job = state in ("PRINTING", "PAUSED") or progress > 0

    if state in ("ERROR", "ATTENTION"):
        phase = "ERROR"
    elif state == "PAUSED":
        phase = "PAUSED"
    elif state == "FINISHED":
        phase = "FINISHED"
    elif not has_job and state in ("IDLE", "READY"):
        phase = "IDLE"
    elif has_job and progress == 0:
        phase = "PREPARING"
    elif has_job:
        phase = "PRINTING"
    else:
        phase = "IDLE"

    # Track time in phase
    now = _time.time()
    if _phase_state["entered"] == 0.0:
        _phase_state = {"phase": phase, "entered": now}
    elif phase != _phase_state["phase"]:
        _phase_state = {"phase": phase, "entered": now}
    time_in_phase = int(now - _phase_state["entered"])

    # Detail string
    if phase == "PREPARING":
        detail = f"Heating nozzle ({temp_nozzle:.0f}/{target_nozzle:.0f}C)"
    elif phase == "PRINTING":
        detail = f"Printing at {progress:.0f}%"
    elif phase == "FINISHED":
        detail = "Print complete, cooling"
    elif phase == "PAUSED":
        detail = f"Paused at {progress:.0f}%"
    elif phase == "ERROR":
        detail = state
    else:
        detail = "No active job"

    return {
        "job.phase": phase,
        "job.phase_detail": detail,
        "job.time_in_phase_s": time_in_phase,
    }
