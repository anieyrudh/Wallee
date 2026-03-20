"""Built-in tool: discover connected hardware and persist HARDWARE.md."""

import os
from datetime import datetime
from pathlib import Path

from wallee.tools.decorator import tool

_KNOWLEDGE_DIR = Path(__file__).parent.parent.parent / "knowledge"


def _write_hardware_summary(findings: dict) -> Path:
    hardware_path = _KNOWLEDGE_DIR / "HARDWARE.md"
    hardware_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Hardware Discovery",
        "",
        f"Last updated: {datetime.now().isoformat()}",
        "",
    ]
    for key in sorted(findings):
        lines.append(f"- **{key}**: {findings[key]}")
    hardware_path.write_text("\n".join(lines) + "\n")
    return hardware_path


@tool(kind="actuator", requires_approval=False, gate_bypass=True)
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

    hardware_path = _write_hardware_summary(findings)

    return {
        "status": "success",
        "findings": findings,
        "hardware_path": str(hardware_path),
        "message": "Local hardware discovery completed.",
    }
