# Host Pi Device Pack

Publishes local host health and inventory information from the machine running Wallee. This pack is hardware-facing only in the sense that it reads the host system; it does not control any external device.

## Interface

- Local sysfs
- `psutil`
- Platform tools such as `lsusb` or `system_profiler`

## Sensor publishers

| Sensor publisher | Keys | Rate | Description |
|---|---|---:|---|
| `read_cpu_temp` | `host.cpu_temp` | `1.0 Hz` | CPU temperature |
| `read_system_stats` | `host.cpu_percent`, `host.memory_percent`, `host.disk_percent`, `host.uptime_hours` | `0.5 Hz` | Host load, memory, disk, and uptime |
| `read_usb_devices` | `host.usb_devices` | `0.1 Hz` | Connected USB inventory |
| `read_network_interfaces` | `host.network_interfaces` | `0.2 Hz` | Network interface status and addresses |

## Setup

No special network setup is required. The runtime host only needs access to its own local system telemetry. On Linux, sysfs paths provide the preferred data; on macOS development machines, the pack falls back to `psutil` and system tools where available.
