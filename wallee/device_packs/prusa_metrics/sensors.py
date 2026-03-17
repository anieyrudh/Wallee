"""Sensor tools for Prusa metrics UDP stream.

Push model: a single UDPListener thread receives all metrics and populates a
shared MetricsBuffer. These sensor tools read from that buffer — they do NOT
poll the printer. The registry calls them on schedule to publish to whiteboard.
"""

import logging

from wallee.bus.udp_listener import MetricsBuffer, UDPListener
from wallee.tools.decorator import tool

logger = logging.getLogger(__name__)

# Shared listener and buffer — initialized once, used by all sensors
_listener: UDPListener | None = None
_buffer: MetricsBuffer | None = None


def get_buffer() -> MetricsBuffer | None:
    """Get the shared metrics buffer, starting the listener if needed."""
    global _listener, _buffer
    if _buffer is None:
        _buffer = MetricsBuffer()
        _listener = UDPListener(bind_addr="0.0.0.0", port=8514, buffer=_buffer)
        _listener.start()
        logger.info("Prusa metrics UDP listener started")
    return _buffer


def _field(key: str, field: str = "v") -> int | float | str | bool | None:
    """Shorthand to read a field from the shared buffer."""
    buf = get_buffer()
    if buf is None:
        return None
    return buf.get_field(key, field)


# ---------------------------------------------------------------------------
# Sensor tools — each reads from the shared MetricsBuffer
# ---------------------------------------------------------------------------

@tool(kind="sensor", refresh_hz=0.3, history_depth=30)
def read_temperatures() -> dict:
    """Read all printer temperatures from metrics stream.

    Sources: temp_noz, temp_bed, chamber_temp, temp_hbr, temp_brd, temp_mcu,
    plus targets ttemp_noz and ttemp_bed.
    """
    buf = get_buffer()
    if buf is None:
        return {"error": "Metrics buffer not available"}

    result = {}

    # Nozzle
    v = buf.get_field("temp_noz,a=1,n=0", "value")
    if v is not None:
        result["printer.temp_nozzle"] = round(float(v), 2)
    tv = buf.get_field("ttemp_noz,a=1,n=0", "value")
    if tv is not None:
        result["printer.target_nozzle"] = int(tv)

    # Bed
    v = buf.get_field("temp_bed", "v")
    if v is not None:
        result["printer.temp_bed"] = round(float(v), 2)
    tv = buf.get_field("ttemp_bed", "v")
    if tv is not None:
        result["printer.target_bed"] = int(tv)

    # Chamber
    v = buf.get_field("chamber_temp", "v")
    if v is not None:
        result["printer.temp_chamber"] = round(float(v), 2)

    # Heatbreak
    v = buf.get_field("temp_hbr,a=1,n=0", "value")
    if v is not None:
        result["printer.temp_heatbreak"] = round(float(v), 2)

    # Board
    v = buf.get_field("temp_brd", "v")
    if v is not None:
        result["printer.temp_board"] = round(float(v), 2)

    # MCU
    v = buf.get_field("temp_mcu", "v")
    if v is not None:
        result["printer.temp_mcu"] = int(v)

    return result if result else {"error": "No temperature metrics received yet"}


@tool(kind="sensor", refresh_hz=0.3, history_depth=30)
def read_electrical() -> dict:
    """Read electrical measurements — safety-critical for detecting faults.

    Sources: volt_bed, volt_nozz, curr_nozz, oc_nozz (overcurrent), oc_inp.
    """
    buf = get_buffer()
    if buf is None:
        return {"error": "Metrics buffer not available"}

    result = {}
    for key, wb_key in [
        ("volt_bed", "printer.volt_bed"),
        ("volt_nozz", "printer.volt_nozzle"),
        ("curr_nozz", "printer.curr_nozzle"),
        ("oc_nozz", "printer.oc_nozzle"),
        ("oc_inp", "printer.oc_input"),
    ]:
        v = _field(key)
        if v is not None:
            result[wb_key] = round(float(v), 4) if isinstance(v, float) else v

    return result if result else {"error": "No electrical metrics received yet"}


@tool(kind="sensor", refresh_hz=0.5, history_depth=10)
def read_fans() -> dict:
    """Read fan state, PWM, and measured RPM for all fans."""
    buf = get_buffer()
    if buf is None:
        return {"error": "Metrics buffer not available"}

    result = {}

    # Heatbreak fan
    m = buf.get("fan,fan=heatbreak")
    if m:
        result["printer.fan_heatbreak_state"] = m.fields.get("state", 0)
        result["printer.fan_heatbreak_pwm"] = m.fields.get("pwm", 0)
        result["printer.fan_heatbreak_rpm"] = m.fields.get("measured", 0)

    # Print fan
    m = buf.get("fan,fan=print")
    if m:
        result["printer.fan_print_state"] = m.fields.get("state", 0)
        result["printer.fan_print_pwm"] = m.fields.get("pwm", 0)
        result["printer.fan_print_rpm"] = m.fields.get("measured", 0)

    # XBE fans (enclosure)
    for n in (1, 2, 3):
        m = buf.get(f"xbe_fan,fan={n}")
        if m:
            result[f"printer.xbe_fan_{n}_pwm"] = m.fields.get("pwm", 0)
            result[f"printer.xbe_fan_{n}_rpm"] = m.fields.get("rpm", 0)

    return result if result else {"error": "No fan metrics received yet"}


@tool(kind="sensor", refresh_hz=5.0, history_depth=10)
def read_position() -> dict:
    """Read toolhead position (mm) and stepper positions (steps) at 5Hz."""
    buf = get_buffer()
    if buf is None:
        return {"error": "Metrics buffer not available"}

    result = {}
    for key, wb_key in [
        ("pos_x", "printer.pos_x"),
        ("pos_y", "printer.pos_y"),
        ("pos_z", "printer.pos_z"),
        ("ipos_x", "printer.ipos_x"),
        ("ipos_y", "printer.ipos_y"),
        ("ipos_z", "printer.ipos_z"),
    ]:
        v = _field(key)
        if v is not None:
            result[wb_key] = round(float(v), 3) if isinstance(v, float) else v

    return result if result else {"error": "No position metrics received yet"}


@tool(kind="sensor", refresh_hz=1.0, history_depth=10)
def read_filament() -> dict:
    """Read filament sensor data for jam/runout detection.

    Fields: st (state: 2=present), f (flow count), r (rotation), ri (rotation increment).
    """
    buf = get_buffer()
    if buf is None:
        return {"error": "Metrics buffer not available"}

    m = buf.get("fsensor,n=0")
    if m is None:
        return {"error": "No filament sensor metrics received yet"}

    return {
        "printer.fsensor_state": m.fields.get("st", 0),
        "printer.fsensor_flow": m.fields.get("f", 0),
        "printer.fsensor_rotation": m.fields.get("r", 0),
        "printer.fsensor_rotation_inc": m.fields.get("ri", 0),
    }


@tool(kind="sensor", refresh_hz=0.3, history_depth=5)
def read_enclosure() -> dict:
    """Read enclosure sensors: door state and chamber temperature."""
    buf = get_buffer()
    if buf is None:
        return {"error": "Metrics buffer not available"}

    result = {}
    v = _field("door_sensor")
    if v is not None:
        result["printer.door_sensor"] = v

    v = _field("chamber_temp")
    if v is not None:
        result["printer.temp_chamber"] = round(float(v), 2)

    return result if result else {"error": "No enclosure metrics received yet"}


@tool(kind="sensor", refresh_hz=0.2, history_depth=5)
def read_firmware_health() -> dict:
    """Read firmware health metrics: memory, CPU, stepper stalls."""
    buf = get_buffer()
    if buf is None:
        return {"error": "Metrics buffer not available"}

    result = {}

    m = buf.get("heap")
    if m:
        result["printer.heap_free"] = m.fields.get("free", 0)
        result["printer.heap_total"] = m.fields.get("total", 0)

    v = _field("cpu_usage")
    if v is not None:
        result["printer.cpu_usage"] = v

    v = _field("stp_stall")
    if v is not None:
        result["printer.stepper_stall"] = v

    return result if result else {"error": "No firmware health metrics received yet"}


@tool(kind="sensor", refresh_hz=0.3, history_depth=0)
def read_print_state() -> dict:
    """Read print state flags from metrics stream.

    Sources: is_printing, print_filename, heater_enabled.
    """
    buf = get_buffer()
    if buf is None:
        return {"error": "Metrics buffer not available"}

    result = {}
    v = _field("is_printing")
    if v is not None:
        result["printer.is_printing"] = bool(v)

    v = _field("print_filename")
    if v is not None:
        result["printer.print_filename"] = v

    v = _field("heater_enabled")
    if v is not None:
        result["printer.heater_enabled"] = bool(v)

    v = _field("nozzle_pwm")
    if v is not None:
        result["printer.pwm_nozzle"] = v

    v = _field("bed_pwm")
    if v is not None:
        result["printer.pwm_bed"] = v

    return result if result else {"error": "No print state metrics received yet"}
