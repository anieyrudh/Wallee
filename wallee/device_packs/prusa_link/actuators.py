"""Actuator tools for Prusa printer via PrusaLink HTTP API (/api/v1/* only).

Uses only /api/v1/* endpoints. No OctoPrint compatibility endpoints.
Each actuator has precondition checks against whiteboard discrete states.
"""

import logging

from wallee.tools.decorator import tool
from wallee.device_packs.prusa_link.sensors import _get_http

logger = logging.getLogger(__name__)


def _read_state(whiteboard, key: str):
    return whiteboard.read(key) if whiteboard else None


def _ok() -> dict:
    return {"status": "ok"}


def _precheck_pause_print(whiteboard=None, **kwargs) -> dict:
    phase = _read_state(whiteboard, "job.phase")
    if phase == "PREPARING":
        return {"error": "Cannot pause during PREPARING phase — firmware rejects it (405). Wait for PRINTING phase."}
    job_state = _read_state(whiteboard, "printer.job_state")
    if job_state != "PRINTING":
        return {"error": f"Cannot pause: job is {job_state}, not PRINTING"}
    return _ok()


def _precheck_resume_print(whiteboard=None, **kwargs) -> dict:
    job_state = _read_state(whiteboard, "printer.job_state")
    if job_state != "PAUSED":
        return {"error": f"Cannot resume: job is {job_state}, not PAUSED"}

    state = _read_state(whiteboard, "printer.state")
    if state not in ("PAUSED", "ATTENTION", "READY", None):
        return {"error": f"Cannot resume: printer state is {state}"}
    return _ok()


def _precheck_cancel_print(whiteboard=None, **kwargs) -> dict:
    job_state = _read_state(whiteboard, "printer.job_state")
    if job_state in ("IDLE", None):
        return {"error": f"Cannot cancel: no active job (state={job_state})"}
    return _ok()


def _precheck_start_print(whiteboard=None, file_path: str = "", **kwargs) -> dict:
    job_state = _read_state(whiteboard, "printer.job_state")
    if job_state not in ("IDLE", None):
        return {"error": f"Cannot start print: job state is {job_state}, not IDLE"}

    printer_state = _read_state(whiteboard, "printer.state")
    if printer_state == "ERROR":
        return {"error": "Cannot start print: printer in ERROR state"}

    if not file_path:
        return {"error": "file_path is required"}
    return _ok()


def _precheck_set_temperature(whiteboard=None, target=None, heater: str = "nozzle", **kwargs) -> dict:
    if target is None:
        return {"error": "target temperature is required"}
    target = float(target)
    gcode_map = {"nozzle": "M104", "bed": "M140", "chamber": "M141"}
    if heater not in gcode_map:
        return {"error": f"Unknown heater: {heater}. Must be 'nozzle', 'bed', or 'chamber'"}

    limits = {"nozzle": (0, 300), "bed": (0, 120), "chamber": (0, 50)}
    low, high = limits[heater]
    if target < low or target > high:
        return {"error": f"{heater.title()} target {target}C outside safe range ({low}-{high})"}

    printer_state = _read_state(whiteboard, "printer.state")
    if printer_state == "ERROR":
        return {"error": "Cannot set temp: printer in ERROR state"}
    return _ok()


def _precheck_idle_motion(whiteboard=None, action: str = "move", **kwargs) -> dict:
    state = _read_state(whiteboard, "printer.state")
    if state not in ("IDLE", "FINISHED", None):
        return {"error": f"Cannot {action}: printer state is {state}, must be IDLE"}
    return _ok()


def _precheck_set_speed_factor(whiteboard=None, percent=None, **kwargs) -> dict:
    if percent is None:
        return {"error": "percent is required"}
    percent = int(percent)
    if percent < 10 or percent > 200:
        return {"error": f"Speed factor {percent}% outside bounds (10-200)"}

    state = _read_state(whiteboard, "printer.state")
    if state != "PRINTING":
        return {"error": f"Cannot set speed: printer state is {state}, must be PRINTING"}
    return _ok()


def _precheck_set_flow_factor(whiteboard=None, percent=None, **kwargs) -> dict:
    if percent is None:
        return {"error": "percent is required"}
    percent = int(percent)
    if percent < 10 or percent > 150:
        return {"error": f"Flow factor {percent}% outside bounds (10-150)"}

    state = _read_state(whiteboard, "printer.state")
    if state != "PRINTING":
        return {"error": f"Cannot set flow: printer state is {state}, must be PRINTING"}
    return _ok()


def _precheck_set_position(whiteboard=None, x=None, y=None, z=None, **kwargs) -> dict:
    if x is None and y is None and z is None:
        return {"error": "at least one of x, y, z is required"}
    if x is not None:
        x = float(x)
        if x < 0 or x > 252:
            return {"error": f"X={x} outside bounds (0-252mm)"}
    if y is not None:
        y = float(y)
        if y < 0 or y > 220:
            return {"error": f"Y={y} outside bounds (0-220mm)"}
    if z is not None:
        z = float(z)
        if z < 0 or z > 220:
            return {"error": f"Z={z} outside bounds (0-220mm)"}

    state = _read_state(whiteboard, "printer.state")
    if state not in ("IDLE", "FINISHED", None):
        return {"error": f"Cannot move: printer state is {state}, must be IDLE. Moving during a print would destroy it."}
    return _ok()


def _precheck_extrusion(action: str, whiteboard=None, length_mm=None, **kwargs) -> dict:
    if length_mm is None:
        return {"error": "length_mm is required"}
    length_mm = float(length_mm)
    if length_mm <= 0 or length_mm > 100:
        return {"error": f"{action.title()} length {length_mm}mm outside bounds (0-100)"}

    state = _read_state(whiteboard, "printer.state")
    if state not in ("IDLE", "FINISHED", None):
        return {"error": f"Cannot {action}: printer state is {state}, must be IDLE"}

    nozzle_temp = _read_state(whiteboard, "printer.temp_nozzle")
    if nozzle_temp is not None and nozzle_temp < 170:
        return {"error": f"Cannot {action}: nozzle temp {nozzle_temp}C < 170C min_extrusion_temp"}
    return _ok()


def _precheck_extrude(whiteboard=None, length_mm=None, **kwargs) -> dict:
    return _precheck_extrusion("extrude", whiteboard=whiteboard, length_mm=length_mm, **kwargs)


def _precheck_retract(whiteboard=None, length_mm=None, **kwargs) -> dict:
    return _precheck_extrusion("retract", whiteboard=whiteboard, length_mm=length_mm, **kwargs)


@tool(kind="actuator", requires_approval=False, max_proposal_age_ms=15000, precheck_fn=_precheck_pause_print, state_effects=["printer.state", "printer.job_state"])
def pause_print(whiteboard=None, **kwargs) -> dict:
    """Pause the current print via G-code M25 (SD card pause).

    Uses G-code injection instead of PUT /api/v1/job because Core One+
    firmware returns 405 on the REST endpoint during many states.
    """
    http = _get_http()
    if http is None:
        return {"error": "PrusaLink not configured"}

    job_state = whiteboard.read("printer.job_state") if whiteboard else None
    if job_state != "PRINTING":
        return {"error": f"Cannot pause: job is {job_state}, not PRINTING"}

    return _gcode(http, "M25", "pause_print")


@tool(kind="actuator", requires_approval=False, max_proposal_age_ms=30000, precheck_fn=_precheck_resume_print, state_effects=["printer.state", "printer.job_state"])
def resume_print(whiteboard=None, **kwargs) -> dict:
    """Resume a paused print via G-code M24 (SD card resume)."""
    http = _get_http()
    if http is None:
        return {"error": "PrusaLink not configured"}

    job_state = whiteboard.read("printer.job_state") if whiteboard else None
    if job_state != "PAUSED":
        return {"error": f"Cannot resume: job is {job_state}, not PAUSED"}

    state = whiteboard.read("printer.state") if whiteboard else None
    if state not in ("PAUSED", "ATTENTION", "READY", None):
        return {"error": f"Cannot resume: printer state is {state}"}

    return _gcode(http, "M24", "resume_print")


@tool(kind="actuator", requires_approval=True, max_proposal_age_ms=30000, precheck_fn=_precheck_cancel_print, state_effects=["printer.state", "printer.job_state"])
def cancel_print(whiteboard=None, **kwargs) -> dict:
    """Cancel the current print. Irreversible.

    Tries DELETE /api/v1/job first (standard PrusaLink v1). If the firmware
    returns 405 (common on Core One+ in transitional states), falls back to
    M603 abort G-code via POST /api/v1/gcode.
    """
    http = _get_http()
    if http is None:
        return {"error": "PrusaLink not configured"}

    job_state = whiteboard.read("printer.job_state") if whiteboard else None
    if job_state in ("IDLE", None):
        return {"error": f"Cannot cancel: no active job (state={job_state})"}

    # Primary: DELETE /api/v1/job (standard PrusaLink v1)
    result = http.delete("/api/v1/job")
    if "error" not in result:
        return {"status": "success", "action": "cancel_print", "method": "DELETE"}

    # Fallback: M603 abort G-code (works in all states on Core One+)
    logger.warning(f"cancel_print: DELETE /api/v1/job failed ({result.get('error', '?')}), trying M603 G-code abort")
    gcode_result = http.post("/api/v1/gcode", json_body={"command": "M603"})
    if "error" not in gcode_result:
        return {"status": "success", "action": "cancel_print", "method": "M603"}

    # Both failed — return the original error
    return {"error": f"cancel_print failed: DELETE returned {result.get('error', '?')}, M603 returned {gcode_result.get('error', '?')}"}


@tool(kind="actuator", requires_approval=True, max_proposal_age_ms=60000, precheck_fn=_precheck_start_print, state_effects=["printer.state", "printer.job_state"])
def start_print(whiteboard=None, file_path: str = "", **kwargs) -> dict:
    """Start printing a file via POST /api/v1/files/{path}/pprint.

    Args:
        file_path: Path on USB storage (e.g., '/usb/BENCHY~2.BGC').
    """
    http = _get_http()
    if http is None:
        return {"error": "PrusaLink not configured"}

    job_state = whiteboard.read("printer.job_state") if whiteboard else None
    if job_state not in ("IDLE", None):
        return {"error": f"Cannot start print: job state is {job_state}, not IDLE"}

    printer_state = whiteboard.read("printer.state") if whiteboard else None
    if printer_state == "ERROR":
        return {"error": "Cannot start print: printer in ERROR state"}

    if not file_path:
        return {"error": "file_path is required"}

    # PrusaLink v1 start print endpoint
    # Strip leading /usb/ if present, then POST to /api/v1/files/{path}/pprint
    clean_path = file_path.lstrip("/")
    if clean_path.startswith("usb/"):
        clean_path = clean_path[4:]
    endpoint = f"/api/v1/files/usb/{clean_path}/pprint"

    result = http.post(endpoint)
    if "error" in result:
        return result

    return {"status": "success", "action": "start_print", "file_path": file_path}


@tool(kind="actuator", requires_approval=False, max_proposal_age_ms=15000, precheck_fn=_precheck_set_temperature, state_effects=["printer.target_nozzle", "printer.target_bed", "printer.target_chamber"])
def set_temperature(whiteboard=None, target=None, heater: str = "nozzle", **kwargs) -> dict:
    """Set target temperature via POST /api/v1/gcode. Fire-and-forget.

    Uses G-code injection: M104 (nozzle), M140 (bed), M141 (chamber).
    Confirmation comes from the metrics stream showing targets change.

    Args:
        target: Target temperature in Celsius (required).
        heater: 'nozzle', 'bed', or 'chamber'.
    """
    if target is None:
        return {"error": "target temperature is required"}
    target = float(target)

    http = _get_http()
    if http is None:
        return {"error": "PrusaLink not configured"}

    gcode_map = {"nozzle": "M104", "bed": "M140", "chamber": "M141"}
    if heater not in gcode_map:
        return {"error": f"Unknown heater: {heater}. Must be 'nozzle', 'bed', or 'chamber'"}

    limits = {"nozzle": (0, 300), "bed": (0, 120), "chamber": (0, 50)}
    lo, hi = limits[heater]
    if target < lo or target > hi:
        return {"error": f"{heater.title()} target {target}C outside safe range ({lo}-{hi})"}

    printer_state = whiteboard.read("printer.state") if whiteboard else None
    if printer_state == "ERROR":
        return {"error": "Cannot set temp: printer in ERROR state"}

    gcode = f"{gcode_map[heater]} S{target}"
    result = http.post("/api/v1/gcode", json_body={"command": gcode})
    if "error" in result:
        return result

    return {"status": "success", "action": "set_temperature", "heater": heater, "target": target}


# ---------------------------------------------------------------------------
# G-code injection actuators (fire-and-forget via POST /api/v1/gcode)
# ---------------------------------------------------------------------------

def _gcode(http, command: str, action: str, **extra) -> dict:
    """Send a G-code command via HTTP. Shared helper."""
    result = http.post("/api/v1/gcode", json_body={"command": command})
    if "error" in result:
        return result
    return {"status": "success", "action": action, "gcode": command, **extra}


@tool(kind="actuator", requires_approval=False, max_proposal_age_ms=30000, precheck_fn=lambda whiteboard=None, **kwargs: _precheck_idle_motion(whiteboard=whiteboard, action="home", **kwargs))
def home_axes(whiteboard=None, **kwargs) -> dict:
    """Home all axes via G28. Printer must be IDLE — never home during a print."""
    http = _get_http()
    if http is None:
        return {"error": "PrusaLink not configured"}

    state = whiteboard.read("printer.state") if whiteboard else None
    if state not in ("IDLE", "FINISHED", None):
        return {"error": f"Cannot home: printer state is {state}, must be IDLE"}

    return _gcode(http, "G28", "home_axes")


@tool(kind="actuator", requires_approval=False, max_proposal_age_ms=15000, precheck_fn=lambda whiteboard=None, **kwargs: _precheck_idle_motion(whiteboard=whiteboard, action="disable motors", **kwargs))
def disable_motors(whiteboard=None, **kwargs) -> dict:
    """Disable stepper motors via M18. Idempotent, safe. Printer must be IDLE."""
    http = _get_http()
    if http is None:
        return {"error": "PrusaLink not configured"}

    state = whiteboard.read("printer.state") if whiteboard else None
    if state not in ("IDLE", "FINISHED", None):
        return {"error": f"Cannot disable motors: printer state is {state}, must be IDLE"}

    return _gcode(http, "M18", "disable_motors")


@tool(kind="actuator", requires_approval=False, max_proposal_age_ms=15000, precheck_fn=_precheck_set_speed_factor, state_effects=["printer.speed"])
def set_speed_factor(whiteboard=None, percent=None, **kwargs) -> dict:
    """Set print speed factor (M220). Only during printing. Bounds: 10-200%."""
    if percent is None:
        return {"error": "percent is required"}
    percent = int(percent)

    http = _get_http()
    if http is None:
        return {"error": "PrusaLink not configured"}

    if percent < 10 or percent > 200:
        return {"error": f"Speed factor {percent}% outside bounds (10-200)"}

    state = whiteboard.read("printer.state") if whiteboard else None
    if state != "PRINTING":
        return {"error": f"Cannot set speed: printer state is {state}, must be PRINTING"}

    return _gcode(http, f"M220 S{percent}", "set_speed_factor", percent=percent)


@tool(kind="actuator", requires_approval=False, max_proposal_age_ms=15000, precheck_fn=_precheck_set_flow_factor, state_effects=["printer.flow"])
def set_flow_factor(whiteboard=None, percent=None, **kwargs) -> dict:
    """Set flow/extrusion factor (M221). Only during printing. Bounds: 10-150%."""
    if percent is None:
        return {"error": "percent is required"}
    percent = int(percent)

    http = _get_http()
    if http is None:
        return {"error": "PrusaLink not configured"}

    if percent < 10 or percent > 150:
        return {"error": f"Flow factor {percent}% outside bounds (10-150)"}

    state = whiteboard.read("printer.state") if whiteboard else None
    if state != "PRINTING":
        return {"error": f"Cannot set flow: printer state is {state}, must be PRINTING"}

    return _gcode(http, f"M221 S{percent}", "set_flow_factor", percent=percent)


@tool(kind="actuator", requires_approval=False, max_proposal_age_ms=30000, precheck_fn=_precheck_set_position)
def set_position(whiteboard=None, x=None, y=None, z=None, **kwargs) -> dict:
    """Move toolhead to position via G1. HIGH CONSEQUENCE — never during a print.

    Args:
        x: X position in mm (0-252).
        y: Y position in mm (0-220).
        z: Z position in mm (0-220).
    """
    if x is None and y is None and z is None:
        return {"error": "at least one of x, y, z is required"}

    http = _get_http()
    if http is None:
        return {"error": "PrusaLink not configured"}

    if x is not None:
        x = float(x)
        if x < 0 or x > 252:
            return {"error": f"X={x} outside bounds (0-252mm)"}
    if y is not None:
        y = float(y)
        if y < 0 or y > 220:
            return {"error": f"Y={y} outside bounds (0-220mm)"}
    if z is not None:
        z = float(z)
        if z < 0 or z > 220:
            return {"error": f"Z={z} outside bounds (0-220mm)"}

    state = whiteboard.read("printer.state") if whiteboard else None
    if state not in ("IDLE", "FINISHED", None):
        return {"error": f"Cannot move: printer state is {state}, must be IDLE. "
                "Moving during a print would destroy it."}

    # Build G-code with only specified axes
    parts = ["G1"]
    if x is not None:
        parts.append(f"X{x}")
    if y is not None:
        parts.append(f"Y{y}")
    if z is not None:
        parts.append(f"Z{z}")
    parts.append("F3000")
    gcode = " ".join(parts)

    result_kwargs = {}
    if x is not None:
        result_kwargs["x"] = x
    if y is not None:
        result_kwargs["y"] = y
    if z is not None:
        result_kwargs["z"] = z

    return _gcode(http, gcode, "set_position", **result_kwargs)


@tool(kind="actuator", requires_approval=False, max_proposal_age_ms=30000, precheck_fn=_precheck_extrude)
def extrude(whiteboard=None, length_mm=None, feedrate: int = 300, **kwargs) -> dict:
    """Extrude filament via G1 E{length}. Requires nozzle >= 170C.

    Args:
        length_mm: Length to extrude in mm (max 100).
        feedrate: Feedrate in mm/min (default 300).
    """
    if length_mm is None:
        return {"error": "length_mm is required"}
    length_mm = float(length_mm)

    http = _get_http()
    if http is None:
        return {"error": "PrusaLink not configured"}

    if length_mm <= 0 or length_mm > 100:
        return {"error": f"Extrude length {length_mm}mm outside bounds (0-100)"}

    state = whiteboard.read("printer.state") if whiteboard else None
    if state not in ("IDLE", "FINISHED", None):
        return {"error": f"Cannot extrude: printer state is {state}, must be IDLE"}

    nozzle_temp = whiteboard.read("printer.temp_nozzle") if whiteboard else None
    if nozzle_temp is not None and nozzle_temp < 170:
        return {"error": f"Cannot extrude: nozzle temp {nozzle_temp}C < 170C min_extrusion_temp"}

    # Set relative extrusion mode, extrude, then back to absolute
    commands = ["M83", f"G1 E{length_mm} F{feedrate}", "M82"]
    for cmd in commands:
        result = http.post("/api/v1/gcode", json_body={"command": cmd})
        if "error" in result:
            return result

    return {"status": "success", "action": "extrude", "length_mm": length_mm, "feedrate": feedrate}


@tool(kind="actuator", requires_approval=False, max_proposal_age_ms=30000, precheck_fn=_precheck_retract)
def retract(whiteboard=None, length_mm=None, feedrate: int = 300, **kwargs) -> dict:
    """Retract filament via G1 E-{length}. Requires nozzle >= 170C.

    Args:
        length_mm: Length to retract in mm (max 100).
        feedrate: Feedrate in mm/min (default 300).
    """
    if length_mm is None:
        return {"error": "length_mm is required"}
    length_mm = float(length_mm)

    http = _get_http()
    if http is None:
        return {"error": "PrusaLink not configured"}

    if length_mm <= 0 or length_mm > 100:
        return {"error": f"Retract length {length_mm}mm outside bounds (0-100)"}

    state = whiteboard.read("printer.state") if whiteboard else None
    if state not in ("IDLE", "FINISHED", None):
        return {"error": f"Cannot retract: printer state is {state}, must be IDLE"}

    nozzle_temp = whiteboard.read("printer.temp_nozzle") if whiteboard else None
    if nozzle_temp is not None and nozzle_temp < 170:
        return {"error": f"Cannot retract: nozzle temp {nozzle_temp}C < 170C min_extrusion_temp"}

    commands = ["M83", f"G1 E-{length_mm} F{feedrate}", "M82"]
    for cmd in commands:
        result = http.post("/api/v1/gcode", json_body={"command": cmd})
        if "error" in result:
            return result

    return {"status": "success", "action": "retract", "length_mm": length_mm, "feedrate": feedrate}
