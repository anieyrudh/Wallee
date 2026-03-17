"""Prusa serial device pack — USB serial for G-code commands and endstops."""

PACK_META = {
    "name": "prusa_serial",
    "description": "Prusa printer USB serial for G-code commands (actuator-only)",
    "bus": "serial",
    "discovery_match": {"type": "usb", "vid": "2c99", "pid": "001f"},
}
