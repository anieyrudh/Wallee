"""Built-in tool: discover connected hardware."""

from wallee.tools.decorator import tool


@tool(kind="actuator", requires_approval=False)
def discover_hardware(whiteboard=None, **kwargs) -> dict:
    """Scan all buses, enumerate devices, match to packs, update HARDWARE.md.

    v1: Returns currently loaded device pack info.
    Future: active bus scanning (I2C, SPI, USB).
    """
    # This is a placeholder — real implementation scans buses
    # and updates HARDWARE.md. For now, return a status message.
    return {
        "status": "success",
        "message": "Hardware discovery is a manual process in v1. Check HARDWARE.md.",
    }
