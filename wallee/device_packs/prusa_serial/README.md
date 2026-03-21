# Prusa Serial Device Pack

Provides direct diagnostic access to the reference machine over USB serial. It is intentionally narrow: most control actions stay on the HTTP control path, while this pack handles commands that require a direct textual response.

## Interface

- Protocol: USB serial
- Discovery: `/dev/serial/by-id/*Prusa*`, fallback `/dev/ttyACM0`
- Implementation: shared `SerialBus` with allowlist and blacklist enforcement

## Actuator tools

| Tool | Params | Approval | Description |
|---|---|---:|---|
| `read_endstops` | — | no | Send `M119` and parse endstop states into `printer.endstop_*` keys |
| `send_gcode` | `command` | no | Send one allowed diagnostic G-code and return its captured response |

## Allowed diagnostic G-code

`send_gcode` only permits a read-only allowlist:

- `M105`
- `M114`
- `M115`
- `M119`
- `M503`

## Notes

- Most operational actuation stays in the HTTP control pack.
- Blacklisted destructive commands are blocked inside the serial bus.
- This pack is local-only and assumes the Wallee host has permission to access the serial device.
