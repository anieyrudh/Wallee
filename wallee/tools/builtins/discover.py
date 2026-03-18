"""Built-in tool: discover connected hardware."""

import os

from wallee.tools.decorator import tool


@tool(kind="actuator", requires_approval=False)
def discover_hardware(whiteboard=None, **kwargs) -> dict:
    """Run lightweight local discovery for camera and printer-related hardware."""
    findings = {
        "configured_device_packs": [p.strip() for p in os.environ.get("DEVICE_PACKS", "").split(",") if p.strip()],
    }

    try:
        from wallee.device_packs.prusa_serial.actuators import _find_prusa_port
        findings["printer.serial_port"] = _find_prusa_port()
    except Exception as e:
        findings["printer.serial_port_error"] = str(e)

    try:
        from wallee.device_packs.pi_cameras.sensors import discover_nozzle_camera_port, discover_buddy_cameras
        findings["camera.nozzle_port"] = discover_nozzle_camera_port(force=True)
        findings["camera.buddy_ips"] = discover_buddy_cameras()
    except Exception as e:
        findings["camera.discovery_error"] = str(e)

    if whiteboard is not None:
        if findings.get("camera.nozzle_port") is not None:
            whiteboard.publish("camera.nozzle_port", findings["camera.nozzle_port"], ttl=300)
        if "camera.buddy_ips" in findings:
            whiteboard.publish("camera.buddy_ips", findings["camera.buddy_ips"], ttl=300)
        if findings.get("printer.serial_port"):
            whiteboard.publish("printer.serial_port", findings["printer.serial_port"], ttl=300)

    return {
        "status": "success",
        "findings": findings,
        "message": "Local hardware discovery completed.",
    }
