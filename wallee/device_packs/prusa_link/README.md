# Prusa Link Device Pack

Connects Wallee to a Prusa machine through the local PrusaLink HTTP API. This pack is responsible for machine state, job metadata, file listing, and most control actions in the reference deployment.

## Interface

- Protocol: HTTP
- Auth: `X-Api-Key`
- Base endpoint: `http://$PRUSALINK_HOST`
- Primary routes used: `/api/v1/status`, `/api/v1/info`, `/api/v1/files/usb`, `/api/v1/job`, `/api/v1/gcode`

## Sensor publishers

These run in the background. The LLM never calls them directly.

| Sensor publisher | Keys | Rate | Description |
|---|---|---:|---|
| `read_printer_state` | `printer.state`, `printer.temp_nozzle`, `printer.target_nozzle`, `printer.temp_bed`, `printer.target_bed`, `printer.speed`, `printer.flow`, `printer.job_state`, `printer.job_progress`, `printer.job_time_remaining_s`, `printer.job_time_printing_s` | `0.5 Hz` | Current machine and active-job status |
| `read_printer_info` | `printer.firmware`, `printer.model`, `printer.serial`, `printer.nozzle_diameter` | `0.1 Hz` | Machine identity and static metadata |
| `read_file_list` | `printer.files` | `0.02 Hz` | USB file inventory available for `start_print` |
| `read_job_metadata` | `job.filename`, `job.material` | `0.2 Hz` | Generic job metadata for prompt context and summaries |
| `read_job_phase` | `job.phase`, `job.phase_detail`, `job.time_in_phase_s` | `1.0 Hz` | High-level phase derivation from the HTTP status endpoint |

## Actuator tools

These are actuator tools the LLM can propose. The engine validates them before dispatch.

| Tool | Params | Approval | Description |
|---|---|---:|---|
| `pause_print` | — | no | Pause the current job via `M25` |
| `resume_print` | — | no | Resume the current job via `M24` |
| `cancel_print` | — | yes | Cancel the current job; falls back from REST delete to abort G-code |
| `start_print` | `file_path` | yes | Start a file from USB storage |
| `set_temperature` | `target`, `heater` | no | Set nozzle, bed, or chamber target |
| `set_speed_factor` | `percent` | no | Set print speed multiplier |
| `set_flow_factor` | `percent` | no | Set extrusion/flow multiplier |
| `home_axes` | — | no | Home all axes while idle |
| `disable_motors` | — | no | Release steppers while idle |
| `set_position` | `x`, `y`, `z` | no | Move the head while idle |
| `extrude` | `length_mm`, `feedrate` | no | Extrude filament while idle and hot |
| `retract` | `length_mm`, `feedrate` | no | Retract filament while idle and hot |

## Callbacks

This pack also registers device callbacks for:

- `on_job_start`: refresh `job.filename` and `job.material`
- `on_idle_check`: optionally auto-propose `start_print` from `print.queue`
- `get_job_context`: add filename and material to `JOB_CONTEXT.md`
- `get_safety_profile`: expose stop paths and fault-monitor keys to the safety kernel

## Setup

See [SETUP.md](SETUP.md) for network configuration, API-key setup, environment variables, and expected whiteboard outputs.

## Related docs

- [CAPABILITIES.md](CAPABILITIES.md)
- [Device Pack Guide](../DEVICE_PACK_GUIDE.md)
