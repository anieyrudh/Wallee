# Prusa Metrics Device Pack

Receives telemetry from the reference machine through a UDP push stream and republishes structured values into the whiteboard. These publishers read from a shared `MetricsBuffer`; they do not poll the machine directly.

## Interface

- Protocol: UDP
- Direction: machine pushes metrics, Wallee listens
- Listener: `0.0.0.0:8514`
- Parser: shared `UDPListener` feeding a shared `MetricsBuffer`

## Sensor publishers

| Sensor publisher | Keys | Rate | Description |
|---|---|---:|---|
| `read_temperatures` | `printer.temp_nozzle`, `printer.target_nozzle`, `printer.temp_bed`, `printer.target_bed`, `printer.temp_chamber`, `printer.temp_heatbreak`, `printer.temp_board`, `printer.temp_mcu` | `0.3 Hz` | Thermal state from metrics |
| `read_electrical` | `printer.volt_bed`, `printer.volt_nozzle`, `printer.curr_nozzle`, `printer.oc_nozzle`, `printer.oc_input` | `0.3 Hz` | Electrical health and safety signals |
| `read_fans` | `printer.fan_heatbreak_*`, `printer.fan_print_*`, `printer.xbe_fan_*` | `0.5 Hz` | Fan state, PWM, and RPM |
| `read_position` | `printer.pos_x`, `printer.pos_y`, `printer.pos_z`, `printer.ipos_x`, `printer.ipos_y`, `printer.ipos_z` | `5.0 Hz` | Toolhead and stepper position |
| `read_filament` | `printer.fsensor_state`, `printer.fsensor_flow`, `printer.fsensor_rotation`, `printer.fsensor_rotation_inc` | `1.0 Hz` | Feed and runout-related counters |
| `read_enclosure` | `printer.door_sensor`, `printer.temp_chamber` | `0.3 Hz` | Enclosure door and chamber state |
| `read_firmware_health` | `printer.heap_free`, `printer.heap_total`, `printer.cpu_usage`, `printer.stepper_stall` | `0.2 Hz` | Firmware memory and health counters |
| `read_print_state` | `printer.is_printing`, `printer.print_filename`, `printer.heater_enabled`, `printer.pwm_nozzle`, `printer.pwm_bed` | `0.3 Hz` | Coarse print-state and heater flags |

## Setup

Configure the machine to push its metrics stream to the Wallee host on UDP port `8514`. The listener starts lazily the first time one of these sensor publishers runs.

## Notes

- This pack is push-based. If the machine stops sending metrics, publishers return `"error"` entries instead of cached stale values.
- `printer.oc_nozzle` and `printer.oc_input` are consumed by the safety kernel in the reference deployment.
- Temperature, electrical, and motion keys are complementary to the HTTP control pack rather than a replacement for it.
