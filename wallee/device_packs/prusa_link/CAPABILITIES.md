# Prusa Core One+ Capability Map

**Generated:** 2026-03-15
**Printer:** Prusa Core One+ (COREONE)
**Firmware:** Prusa-Firmware-Buddy 6.4.0+1.LOCAL
**Serial:** <REDACTED>
**UUID:** <REDACTED>
**MAC:** <PRINTER_MAC>
**Interfaces:** HTTP (<PRINTER_IP>:80), USB Serial (/dev/ttyACM0 @ 115200), UDP Metrics (→Pi:8514), UDP Syslog (→Pi:13514)

---

## 1. Network Topology

| Interface | Address | Notes |
|-----------|---------|-------|
| Printer HTTP | <PRINTER_IP>:80 | Only open TCP port |
| Pi eth0 | <PI_ETH_IP> | Wired to printer |
| Pi wlan0 | <PI_WIFI_IP> | WiFi (SSH) |
| Pi tailscale0 | <VPN_IP> | VPN |
| Printer USB | /dev/ttyACM0 | CDC ACM, 115200 baud, USB 2.0 Full-Speed (12 Mbps) |
| Metrics stream | Printer → Pi:8514/UDP | InfluxDB line protocol, ~50 packets/sec, ~970 bytes each |
| Syslog stream | Printer → Pi:13514/UDP | Serial output mirrored over network, ~1 packet/sec |

**Open ports (TCP):** 80 only. No HTTPS, no IPP, no JetDirect.
**Open ports (UDP):** 67 (DHCP server), 68 (DHCP client), 5353 (mDNS — no services advertised).
**Outbound UDP from printer:** Metrics to Pi eth0:8514 (continuous, high-rate), syslog to Pi eth0:13514 (periodic). Configured in printer Settings → Network → Metrics/Logging.

---

## 2. Authentication

| Method | Header/Mechanism | Notes |
|--------|-----------------|-------|
| API Key | `X-Api-Key: <your-api-key>` | Primary method |
| HTTP Digest | `WWW-Authenticate: Digest realm="Printer API"` | Also supported |
| No auth | → 401 Unauthorized | All endpoints require auth |

---

## 3. PrusaLink HTTP API (v1)

### 3.1 Status & Info (read-only)

| Endpoint | Method | Status | Response |
|----------|--------|--------|----------|
| `/api/version` | GET | 200 | `{"api":"2.0.0","server":"2.1.2","nozzle_diameter":0.40,"text":"PrusaLink","hostname":"prusa-core-one","capabilities":{"upload-by-put":true}}` |
| `/api/v1/status` | GET | 200 | **Primary polling endpoint.** Printer state, all temps (nozzle/bed actual+target), axis positions (x/y/z), flow %, speed %, fan RPMs (hotend/print). During printing: includes `job` object with id, progress, time_remaining, time_printing. |
| `/api/v1/info` | GET | 200 | `nozzle_diameter`, `mmu` (false), `serial`, `hostname`, `min_extrusion_temp` (170) |
| `/api/v1/storage` | GET | 200 | Storage list: one USB storage at `/usb/`, read-write |
| `/api/v1/transfer` | GET | 204 | No active transfer (empty when idle) |

**`/api/v1/status` response structure** (key fields):
```json
{
  "printer": {
    "state": "PRINTING|IDLE|PAUSED|FINISHED|ATTENTION|ERROR",
    "temp_nozzle": 215.2,
    "target_nozzle": 215.0,
    "temp_bed": 60.1,
    "target_bed": 60.0,
    "axis_x": 120.0,
    "axis_y": 100.0,
    "axis_z": 5.2,
    "flow": 100,
    "speed": 100,
    "fan_hotend": 6039,
    "fan_print": 5200
  },
  "job": {
    "id": 42,
    "state": "PRINTING",
    "progress": 42,
    "time_remaining": 3600,
    "time_printing": 2400,
    "file": {
      "name": "BENCHY~2.BGC",
      "display_name": "Benchy Rules.bgcode",
      "refs": {"icon": "...", "thumbnail": "...", "download": "..."}
    }
  },
  "storage": {"path": "/usb/", "name": "usb"}
}
```

**IMPORTANT:** The `job` object is only present when a job is active. During IDLE/FINISHED, it's absent. The `printer` section always exists. Axis positions appear in FINISHED state but may be absent during other states.

### 3.2 Job Control

| Endpoint | Method | Status | Body | Effect |
|----------|--------|--------|------|--------|
| `/api/v1/job` | GET | 200 / 204 | — | Job info (200 during print, 204 when idle) |
| `/api/v1/job` | PUT | 204 | `{"command":"PAUSE"}` | Pause print |
| `/api/v1/job` | PUT | 204 | `{"command":"RESUME"}` | Resume print |
| `/api/v1/job` | DELETE | 204 | — | Cancel print |
| `/api/v1/job` | POST | 204 | — | Purpose unclear (accepted) |

**WARNING:** PUT and DELETE return 204 even when there's no active job. They do NOT return an error. Precondition checks in Wallee actuators are essential.

### 3.3 G-code Injection

| Endpoint | Method | Status | Body | Effect |
|----------|--------|--------|------|--------|
| `/api/v1/gcode` | POST | 204 | `{"command":"M105"}` | Execute arbitrary G-code |
| `/api/v1/gcode` | POST | 400 | non-JSON | Bad request |
| `/api/v1/gcode` | GET | 405 | — | POST only |

**This enables temperature control, homing, and any G-code command via HTTP.** No need for serial to set temperatures. However, there is no way to read the G-code response via this endpoint — it's fire-and-forget.

### 3.4 File Management

| Endpoint | Method | Status | Notes |
|----------|--------|--------|-------|
| `/api/v1/files/usb` | GET | 200 | Full directory listing (8.3 names + display_name) |
| `/api/v1/files/usb/MMU3` | GET | 200 | Subfolder listing works recursively |
| `/api/v1/files/usb/{file}` | GET | 200 | Single file metadata (name, size, type, refs) |
| `/api/v1/files/usb/{file}` | PUT | 501* | Upload (requires actual file content, not empty) |
| `/api/v1/files/usb/{file}` | HEAD | 200 | File existence check |
| `/api/v1/files` | GET | 403 | Must specify storage path |
| `/api/v1/files/local` | GET | 403 | No local storage on Core One |
| `/api/v1/files/sdcard` | GET | 403 | No SD card on Core One |

File names use 8.3 format internally (e.g., `BENCHY~2.BGC`). The `display_name` field has the full name (e.g., `Benchy Rules.bgcode`).

### 3.5 Thumbnails & Downloads

| Endpoint | Method | Content-Type | Notes |
|----------|--------|-------------|-------|
| `/thumb/l/usb/{file}` | GET | image/png | Large thumbnail (~50KB) |
| `/thumb/s/usb/{file}` | GET | 404 | Small thumbnails NOT available on Core One |
| `/usb/{file}` | GET | application/octet-stream | Full file download (gcode/bgcode) |

### 3.6 OctoPrint Compatibility Layer

| Endpoint | Method | Status | Notes |
|----------|--------|--------|-------|
| `/api/printer` | GET | 200 | Telemetry: temp-bed, temp-nozzle, print-speed, z-height, material. State flags: operational, paused, printing, etc. |
| `/api/job` | GET | 200 | estimatedPrintTime, file info, completion (0.0-1.0 float) |
| `/api/job` | POST | 409 | Returns 409 Conflict when no active job (better error than v1) |
| `/api/files` | GET | 200 | Nested file structure with origin, refs, thumbnails |
| `/api/files/local` | GET | 200 | Maps local → USB |
| `/api/files/usb/{file}` | GET | 200 | Single file metadata |
| `/api/settings` | GET | 200 | Returns `{"printer": {}}` (mostly empty) |

**OctoPrint endpoints NOT implemented:** `/api/connection`, `/api/system`, `/api/system/commands`, `/api/login`, `/api/printer/command`, `/api/printer/tool`, `/api/printer/bed`, `/api/printer/sd`.

### 3.7 Endpoints That Don't Exist (404)

`/api/v1/temperatures`, `/api/v1/temperatures/nozzle`, `/api/v1/temperatures/bed`, `/api/v1/cameras`, `/api/v1/cameras/snap`, `/api/v1/update`, `/api/v1/network`, `/api/v1/log`, `/api/v1/settings`, `/api/v1/system`, `/api/v1/printer`, `/api/v1/print`, `/api/v1/nozzle`, `/api/v1/bed`, `/api/v1/fan`, `/api/v1/mesh`, `/api/v1/power`, `/api/v1/firmware`, `/api/v2/*` (no v2 API exists)

---

## 4. USB Serial Interface

### 4.1 Connection Details

| Field | Value |
|-------|-------|
| Port | `/dev/ttyACM0` |
| Symlink | `/dev/serial/by-id/usb-Prusa_Research__prusa3d.com__Original_Prusa_COREONE_<REDACTED>-if00` |
| Baud | 115200 |
| Driver | cdc_acm |
| Permissions | `crw-rw---- root:dialout` |
| USB VID:PID | 2c99:001f |

### 4.2 Temperature Sensors (M105)

The Core One has **4 temperature sensors** (not 2):

| Key | Meaning | Typical Range | Controllable? |
|-----|---------|---------------|---------------|
| `T` | Hotend (nozzle) | 20-300°C | Yes (target) |
| `B` | Heated bed | 20-120°C | Yes (target) |
| `X` | Chamber/enclosure | 20-50°C | **Yes — active control!** |
| `A` | Ambient sensor | 20-45°C | No (read-only) |

PWM output channels:

| Key | Meaning | Range |
|-----|---------|-------|
| `@` | Hotend heater power | 0-127 |
| `B@` | Bed heater power | 0-127 |
| `C@` | Chamber control (heater/fan) | 0-127 |
| `HBR@` | Heatbreak fan | 0-255 |

**M105 response format:**
```
ok T:215.00/215.00 B:60.00/60.00 X:36.00/36.00 A:25.00/0.00 @:80 B@:50 C@:31 HBR@:255
```

**CRITICAL:** The HTTP API (`/api/v1/status`) only reports `temp_nozzle`, `target_nozzle`, `temp_bed`, `target_bed`. It does **NOT** report chamber or ambient temperatures. Serial M105 is the only source for chamber and ambient data.

### 4.3 Working G-code Commands

| Command | Response | Data Provided |
|---------|----------|---------------|
| `M105` | Temps | All 4 temps + targets + PWM outputs |
| `M114` | Position | `X:242.00 Y:-9.00 Z:168.04 E:-46.47 Count A:23300 B:25100 Z:67215` |
| `M115` | Firmware | Firmware name, version, machine type, UUID, all capabilities |
| `M27` | SD status | `Not SD printing` or print progress |
| `M73` | Progress | `M73 Progress: 42%; Change: 0m;` |
| `M220` | Speed | Returns `ok` (speed factor, no echo unless modified) |
| `M221` | Flow | `E0 Flow: 100%` |
| `M119` | Endstops | All 6 endstop states (x/y/z min/max) |
| `M503` | Settings | Full stored settings dump (steps/mm, PID, jerk, accel, etc.) |
| `M20` | File list | SD/USB file listing in 8.3 format |
| `M155 S1` | Auto-report | Enable auto temp reporting every 1s (same format as M105 without `ok` prefix) |
| `M155 S0` | Disable | Stop auto temp reporting |
| `M862.1 Q` | Nozzle dia | `M862.1 T0 P0.40 A0 F1` |
| `M862.2 Q` | FW code | `M862.2 P310` |
| `M862.3 Q` | Model | `M862.3 P "COREONE"` |
| `M862.4 Q` | Board | `M862.4 P640` |
| `M862.5 Q` | Nozzle type | `M862.5 P2` |
| `M900` | Linear adv. | `Advance K 0=0.00 1=0.00 ...` |
| `M970` | Phase step | `phstep: active, M970 X1 Y1` |

### 4.4 Firmware Capabilities (from M115)

| Capability | Supported | Notes |
|------------|-----------|-------|
| AUTOREPORT_TEMP | Yes | M155 S1 works |
| PRINT_JOB | Yes | Job control via G-code |
| AUTOLEVEL | Yes | Bed leveling |
| Z_PROBE | Yes | Probe available |
| LEVELING_DATA | Yes | Can read mesh data |
| VOLUMETRIC | Yes | Volumetric extrusion |
| SOFTWARE_POWER | Yes | Software power control |
| THERMAL_PROTECTION | Yes | Thermal runaway protection active |
| CHAMBER_TEMPERATURE | Yes | Chamber is a controllable zone |
| SERIAL_XON_XOFF | No | |
| BINARY_FILE_TRANSFER | No | |
| EEPROM | No | Uses flash instead |
| PROGRESS | No | Use M73 |
| AUTOREPORT_SD_STATUS | No | |
| EMERGENCY_PARSER | No | |

### 4.5 Stored Machine Parameters (from M503)

| Parameter | Value | Meaning |
|-----------|-------|---------|
| M92 X100 Y100 Z400 E380 | steps/mm | CoreXY + direct drive |
| M203 X350 Y350 Z35 E100 | mm/s | Max feedrates |
| M201 X10000 Y10000 Z1000 E6000 | mm/s² | Max acceleration |
| M204 P10000 R6000 T10000 | mm/s² | Print/Retract/Travel accel |
| M301 P14 I1 D100 | — | Hotend PID |
| M304 P50 I0.77 D30 | — | Bed PID |
| M906 X539 Y539 Z600 T0 E450 | mA | Stepper current |
| M914 X-2 Y-2 Z4 | — | StallGuard thresholds |
| M603 L20 U105 | mm | Filament load/unload lengths |
| M200 D1.75 | mm | Filament diameter |
| M970 X1 Y1 | — | Phase stepping active |

### 4.6 CRITICAL: Serial Reliability Issues

**The USB serial connection is unstable.** The firmware has an aggressive handshake loop:

1. Opening the serial port triggers repeated M115 identification dumps every ~2-3 seconds
2. Many lines arrive **garbled/corrupted** (characters dropped, compressed gibberish)
3. After several dumps, the USB CDC connection **disconnects** (SerialException)
4. The USB device disappears from `/dev/` for ~8 seconds, then reconnects
5. This cycle repeats indefinitely

**Impact on Wallee:**
- Commands DO get responses in the ~1-3 second window before the next disconnect
- Polling with explicit M105 is more reliable than M155 auto-reporting
- A robust serial driver must handle reconnection loops and filter M115 spam
- Setting `dsrdtr=False, dtr=False, rts=False` helps but does not eliminate the issue

### 4.7 BLACKLISTED Commands

| Command | Reason |
|---------|--------|
| `M997` | **DANGEROUS:** Triggers firmware update mode, USB disappears for ~8s |
| `M112` | Emergency stop — only via safety kernel, never via agent |
| `M502` | Factory reset stored settings |
| `M500` | Save settings to flash — risk of corrupting stored config |

### 4.8 Commands That Don't Work

`PRUSA Rev` (old firmware syntax), `M116`, `M552`, `M592`, `M78`, `D3`, `D9`

---

## 5. UDP Metrics Stream (Port 8514)

### 5.1 Protocol

The printer pushes telemetry via UDP to `<PI_ETH_IP>:8514` (Pi eth0). Each UDP packet (~960-1000 bytes) contains one message block.

**Format:** Syslog RFC 5424 header wrapping InfluxDB line protocol metrics.

**Header format:**
```
<14>1 - MAC_ADDR buddy - - - msg=SEQ,tm=TIMESTAMP,v=4 FIRST_METRIC_LINE
```

Fields:
- `<14>` — Syslog priority (facility=user, severity=info)
- `<PRINTER_MAC>` — Printer MAC address (minus F0:24:F9)
- `buddy` — Application name (Prusa Buddy firmware)
- `msg=53110` — Sequence number (monotonically increasing)
- `tm=1309866606` — Firmware monotonic timestamp (microseconds)
- `v=4` — Protocol version

**Metric line format (InfluxDB line protocol):**
```
metric_name[,tag=val] field=value timestamp_offset
```

Timestamps are microsecond offsets relative to the message `tm` value.
Integer values are suffixed with `i` (e.g., `v=44i`). Floats are bare (e.g., `v=24.062366`).
Strings are quoted (e.g., `v="M114"`). Boolean true is `v=T`.

### 5.2 Complete Metric Inventory (62 metrics)

**Temperatures (6):**

| Metric | Tags | Fields | Example | Notes |
|--------|------|--------|---------|-------|
| `temp_noz` | `n=0,a=1` | `value=float` | `value=215.20` | Nozzle temp (°C) |
| `temp_bed` | — | `v=float` | `v=60.05` | Bed temp (°C) |
| `temp_hbr` | `n=0,a=1` | `value=float` | `value=33.30` | Heatbreak temp (°C) |
| `temp_mcu` | — | `v=int` | `v=44i` | MCU temp (°C). ~50 Hz! |
| `temp_brd` | — | `v=float` | `v=38.69` | Board temp (°C) |
| `chamber_temp` | — | `v=float` | `v=27.80` | Chamber temp (°C) |

**Target temperatures (2):**

| Metric | Tags | Fields | Example |
|--------|------|--------|---------|
| `ttemp_noz` | `n=0,a=1` | `value=int` | `value=215i` |
| `ttemp_bed` | — | `v=int` | `v=60i` |

**Heater PWM (2):**

| Metric | Fields | Example |
|--------|--------|---------|
| `nozzle_pwm` | `v=int` | `v=80i` |
| `bed_pwm` | `v=int` | `v=50i` |

**Fan metrics (7):**

| Metric | Tags | Fields | Example |
|--------|------|--------|---------|
| `fan` | `fan=heatbreak` | `state=int,pwm=int,measured=int` | `state=1,pwm=200,measured=6039` |
| `fan` | `fan=print` | `state=int,pwm=int,measured=int` | `state=1,pwm=255,measured=5200` |
| `fan_speed` | — | `v=int` | `v=5200i` |
| `fan_hbr_speed` | — | `v=int` | `v=6039i` |
| `print_fan_act` | — | `v=int` | `v=5200i` |
| `hbr_fan_act` | — | `v=int` | `v=6039i` |
| `xbe_fan` | `fan=1\|2\|3` | `pwm=int,rpm=int` | `pwm=128i,rpm=3000i` |

**Position (6):**

| Metric | Fields | Example | Notes |
|--------|--------|---------|-------|
| `pos_x` | `v=float` | `v=242.000000` | mm, ~0.4 Hz |
| `pos_y` | `v=float` | `v=-9.000000` | mm |
| `pos_z` | `v=float` | `v=4.000000` | mm |
| `ipos_x` | `v=int` | `v=-8i` | Steps (raw stepper position) |
| `ipos_y` | `v=int` | `v=8i` | Steps |
| `ipos_z` | `v=int` | `v=1600i` | Steps |

**Electrical measurements (7):**

| Metric | Fields | Example | Notes |
|--------|--------|---------|-------|
| `volt_bed` | `v=float` | `v=24.062366` | Bed voltage (V) |
| `volt_bed_raw` | `v=int` | `v=669i` | ADC raw |
| `volt_nozz` | `v=float` | `v=0.036022` | Nozzle heater voltage (V, 0 when off) |
| `volt_nozz_raw` | `v=int` | `v=1i` | ADC raw |
| `curr_nozz` | `v=float` | `v=0.047021` | Nozzle current (A) |
| `curr_nozz_raw` | `v=int` | `v=14i` | ADC raw |
| `curr_inp_raw` | `v=int` | `v=495i` | Input current raw |

**Safety/overcurrent (2):**

| Metric | Fields | Example | Notes |
|--------|--------|---------|-------|
| `oc_nozz` | `v=int` | `v=0i` | Nozzle overcurrent flag (0=OK) |
| `oc_inp` | `v=int` | `v=0i` | Input overcurrent flag (0=OK) |

**Filament sensor (1):**

| Metric | Tags | Fields | Example |
|--------|------|--------|---------|
| `fsensor` | `n=0` | `st=int,f=int,r=int,ri=int` | `st=2i,f=1057502i,r=27225i,ri=1308458i` |

Fields: `st`=state (2=filament present), `f`=flow count, `r`=rotation count, `ri`=rotation increment.

**Enclosure (1):**

| Metric | Fields | Example | Notes |
|--------|--------|---------|-------|
| `door_sensor` | `v=int` | `v=11i` | Door state (11=closed, likely bitmask) |

**Firmware internals (13):**

| Metric | Tags | Fields | Notes |
|--------|------|--------|-------|
| `maintask_loop` | — | `v=T` (boolean) | Main loop heartbeat, ~50 Hz |
| `gcode` | — | `v=string` | Last G-code command executed |
| `cmdcnt` | — | `v=int` | G-code command counter |
| `sdpos` | — | `v=int` | SD card byte position (-1 when idle) |
| `stp_stall` | — | `v=int` | Stepper stall detection counter |
| `heap` | — | `free=int,total=int` | Firmware memory (bytes) |
| `cpu_usage` | — | `v=int` | CPU utilization % |
| `gui_loop_dur` | — | `v=int` | GUI render time (ms) |
| `esp_out` | — | `sent=int` | ESP WiFi module bytes sent |
| `esp_in` | — | `recv=int` | ESP WiFi module bytes received |
| `adj_z` | — | `v=float` | Z-axis adjustment (mm) |
| `heater_enabled` | — | `v=int` | Heater enable flag |
| `is_printing` | — | `v=int` | Print active flag |

**Printer identity (2, sent once at start):**

| Metric | Fields | Example |
|--------|--------|---------|
| `fw_version` | `v=string` | `v="6.4.0+1.LOCAL"` |
| `buddy_revision` | `v=string` | `v="48"` |
| `buddy_bom` | `v=string` | `v="48"` |

**Internal bookkeeping (6):**

| Metric | Tags | Fields | Notes |
|--------|------|--------|-------|
| `print_filename` | — | `v=string` | Current print file ("" when idle) |
| `cur_mmu_imp` | — | `v=float` | MMU impedance/current |
| `points_dropped` | — | `v=int` | Metrics points dropped (overflow) |
| `store_bytes` | — | `v=int` | Metrics store size |
| `store_items` | — | `v=int` | Metrics store count |
| `store_migrations` | — | `v=int` | Metrics store migrations |
| `runtime` | `n=default` | `u=int` | Uptime (seconds) |
| `stack` | `n=default\|metric_` | `t=int,m=int` | Stack usage tracking |

### 5.3 Metrics Rate

| Metric | Approximate Rate |
|--------|-----------------|
| `maintask_loop`, `temp_mcu` | ~50 Hz (highest frequency) |
| `ipos_x/y/z`, `pos_x/y/z` | ~5 Hz |
| `fsensor` | ~1 Hz |
| `fan_speed`, `heap`, `stp_stall` | ~0.5 Hz |
| `temp_noz`, `temp_bed`, `temp_brd`, `temp_hbr` | ~0.3 Hz |
| `volt_*`, `curr_*`, `door_sensor`, `pwm` | ~0.3 Hz |
| `xbe_fan`, `chamber_temp` | ~0.3 Hz |
| `fw_version`, `buddy_*` | Once at startup |

Total: ~50 packets/sec, ~300KB/sec sustained.

### 5.4 Key Data Exclusive to Metrics Stream

These are NOT available via HTTP API or serial polling:

| Data | Metric | Safety Value |
|------|--------|-------------|
| Bed voltage | `volt_bed` | Detect PSU or wiring issues |
| Nozzle voltage/current | `volt_nozz`, `curr_nozz` | Detect heater failure or short |
| Input current | `curr_inp_raw` | Detect power supply overload |
| Overcurrent flags | `oc_nozz`, `oc_inp` | **Direct safety signal** — firmware-detected overcurrent |
| Door sensor | `door_sensor` | Enclosure open/closed state |
| Filament sensor | `fsensor` | Flow/rotation for jam/runout detection |
| Board temp | `temp_brd` | Electronics thermal monitoring |
| MCU temp | `temp_mcu` | Firmware thermal monitoring (at 50 Hz!) |
| Stepper stall | `stp_stall` | Detect mechanical issues |
| Heap/CPU | `heap`, `cpu_usage` | Firmware health monitoring |
| Print filename | `print_filename` | Know what's printing without HTTP call |
| Is printing flag | `is_printing` | Instant print state without HTTP call |

---

## 6. UDP Syslog Stream (Port 13514)

### 6.1 Protocol

The printer mirrors its USB serial output over UDP to `<PI_ETH_IP>:13514`. Each UDP packet contains one syslog-wrapped serial line.

**Format:** Syslog RFC 5424, one message per packet.

```
<14>1 - <PRINTER_MAC> buddy Marlin - - SERIAL_LINE_CONTENT
```

The app-name is `Marlin` (vs `buddy` for metrics), identifying this as the Marlin serial output channel.

### 6.2 Content

The syslog stream contains **exactly what the USB serial port would output**, including:

1. **M105 temperature responses** (with `ok` prefix when solicited, without when auto-reported):
   ```
   ok T:32.50/0.00 B:29.52/0.00 X:32.99/36.00 A:38.69/0.00 @:0 B@:0 C@:27.85 HBR@:0
   ```

2. **M114 position responses:**
   ```
   X:242.00 Y:-9.00 Z:4.00 E:0.00 Count A:23300 B:25100 Z:1600
   ```

3. **M115 firmware identification** (periodic, same spam as serial):
   ```
   FIRMWARE_NAME:Prusa-Firmware-Buddy 6.4.0+1.LOCAL (Github) SOURCE_CODE_URL:https://git...
   Cap:SERIAL_XON_XOFF:0
   Cap:EEPROM:0
   Cap:Z_PROBE:1
   ```

### 6.3 Rate

Approximately 1 message per second during idle. Rate increases during printing or when auto-reporting is enabled.

### 6.4 Critical Advantage Over USB Serial

The syslog stream provides **all the same data as USB serial** but:
- **No disconnections** — UDP is connectionless, no CDC ACM instability
- **No garbled data** — each packet is a complete, intact message
- **No M115 spam flooding** — individual packets, easy to filter
- **Can be received simultaneously** with other interfaces (no serial port locking)

**Recommendation:** Use syslog stream (port 13514) instead of USB serial for reading M105/M114 data. Only use USB serial for sending G-code commands when the `/api/v1/gcode` HTTP endpoint is insufficient (i.e., when you need to read the response).

---

## 7. Serial Test Results (2026-03-15)

### 7.1 Old Processes Killed

Previous Wallee v2.1 processes were holding the serial port:
- `wallee.prusa.observer` (PID 1142522) — killed
- `wallee.daemons.host_pi` (PID 2191) — killed
- `wallee.ui.dashboard` (PID 1361) — killed

### 7.2 Serial Test with Reconnection

The serial port disconnects every 1-3 seconds due to M115 spam. Using a reconnect-on-each-command strategy (open → flush → send → read → close → wait for USB reconnect → repeat), all commands succeeded:

```
M105: OK -> ok T:31.28/0.00 B:29.14/0.00 X:32.40/36.00 A:38.62/0.00 @:0 B@:0 C@:27.54 HBR@:0
M114: OK -> X:242.00 Y:-9.00 Z:4.00 E:0.00 Count A:23300 B:25100 Z:1600
M221: OK -> echo:E0 Flow: 100%
```

### 7.3 Serial Behavior Pattern

1. Open port → ~100ms window before M115 spam starts
2. Immediately flush input buffer and send command
3. Read response within 0.3s timeout
4. Close port before disconnect hits
5. Wait ~2s for USB reconnection before next command

This works reliably but limits throughput to ~1 command per 3 seconds.

---

## 8. Data Source Matrix

### 8.1 What's Available Where

| Data Point | HTTP API | Metrics UDP | Syslog UDP | Serial | Best Source | Rate |
|------------|----------|-------------|------------|--------|-------------|------|
| Printer state | `/api/v1/status` | `is_printing` | — | — | HTTP | 0.5 Hz |
| Job state | `/api/v1/status` | — | — | M27 | HTTP | 0.5 Hz |
| Job progress % | `/api/v1/status` | — | — | M73 | HTTP | 0.5 Hz |
| Time remaining | `/api/v1/status` | — | — | — | HTTP | 0.5 Hz |
| Print filename | — | `print_filename` | — | — | **Metrics** | ~0.3 Hz |
| Nozzle temp | `/api/v1/status` | `temp_noz` | M105→T | M105→T | Metrics | ~0.3 Hz |
| Nozzle target | `/api/v1/status` | `ttemp_noz` | — | M105→T | Metrics | ~0.3 Hz |
| Bed temp | `/api/v1/status` | `temp_bed` | M105→B | M105→B | Metrics | ~0.3 Hz |
| Bed target | `/api/v1/status` | `ttemp_bed` | — | M105→B | Metrics | ~0.3 Hz |
| **Chamber temp** | — | `chamber_temp` | M105→X | M105→X | **Metrics** | ~0.3 Hz |
| **Ambient temp** | — | — | M105→A | M105→A | **Syslog** | ~1 Hz |
| **Heatbreak temp** | — | `temp_hbr` | — | — | **Metrics** | ~0.3 Hz |
| **Board temp** | — | `temp_brd` | — | — | **Metrics** | ~0.3 Hz |
| **MCU temp** | — | `temp_mcu` | — | — | **Metrics** | ~50 Hz |
| Nozzle PWM | — | `nozzle_pwm` | M105→@ | M105→@ | **Metrics** | ~0.3 Hz |
| Bed PWM | — | `bed_pwm` | M105→B@ | M105→B@ | **Metrics** | ~0.3 Hz |
| Chamber PWM | — | — | M105→C@ | M105→C@ | **Syslog** | ~1 Hz |
| Heatbreak fan PWM | — | — | M105→HBR@ | M105→HBR@ | **Syslog** | ~1 Hz |
| X/Y/Z position | `/api/v1/status`* | `pos_x/y/z` | M114 | M114 | Metrics | ~5 Hz |
| Stepper position | — | `ipos_x/y/z` | — | M114 Count | **Metrics** | ~5 Hz |
| Fan RPM (hotend) | `/api/v1/status` | `fan,fan=heatbreak` | — | — | Metrics | ~0.5 Hz |
| Fan RPM (print) | `/api/v1/status` | `fan,fan=print` | — | — | Metrics | ~0.5 Hz |
| XBE fans (1/2/3) | — | `xbe_fan,fan=N` | — | — | **Metrics** | ~0.3 Hz |
| Speed factor | `/api/v1/status` | — | — | M220 | HTTP | 0.5 Hz |
| Flow factor | `/api/v1/status` | — | — | M221 | HTTP | 0.5 Hz |
| **Bed voltage** | — | `volt_bed` | — | — | **Metrics** | ~0.3 Hz |
| **Nozzle voltage** | — | `volt_nozz` | — | — | **Metrics** | ~0.3 Hz |
| **Nozzle current** | — | `curr_nozz` | — | — | **Metrics** | ~0.3 Hz |
| **Overcurrent flags** | — | `oc_nozz`,`oc_inp` | — | — | **Metrics** | ~0.3 Hz |
| **Door sensor** | — | `door_sensor` | — | — | **Metrics** | ~0.3 Hz |
| **Filament sensor** | — | `fsensor` | — | — | **Metrics** | ~1 Hz |
| **Stepper stall** | — | `stp_stall` | — | — | **Metrics** | ~0.5 Hz |
| **Heap/CPU** | — | `heap`,`cpu_usage` | — | — | **Metrics** | ~0.5 Hz |
| Firmware version | `/api/version` | `fw_version` | M115 | M115 | HTTP | Once |
| Endstop states | — | — | — | M119 | Serial only | On demand |
| File listing | `/api/v1/files/usb` | — | — | M20 | HTTP | On demand |
| Thumbnails | `/thumb/l/usb/{file}` | — | — | — | HTTP only | On demand |

### 8.2 What Can Be Controlled

| Action | HTTP API | Serial | Best Method | Approval? |
|--------|----------|--------|-------------|-----------|
| Pause print | PUT `/api/v1/job` `{"command":"PAUSE"}` | M25 | HTTP | No (safe) |
| Resume print | PUT `/api/v1/job` `{"command":"RESUME"}` | M24 | HTTP | Yes |
| Cancel print | DELETE `/api/v1/job` | M524 | HTTP | Yes |
| Start print | POST `/api/v1/job` `{"file":"..."}` | M23+M24 | HTTP | Yes |
| Set nozzle temp | POST `/api/v1/gcode` `{"command":"M104 S215"}` | M104 S215 | Either | Yes |
| Set bed temp | POST `/api/v1/gcode` `{"command":"M140 S60"}` | M140 S60 | Either | Yes |
| Set chamber temp | POST `/api/v1/gcode` `{"command":"M141 S36"}` | M141 S36 | **G-code only** | Yes |
| Home axes | POST `/api/v1/gcode` `{"command":"M28"}` | G28 | Either | Yes |
| Set speed factor | POST `/api/v1/gcode` `{"command":"M220 S100"}` | M220 S100 | Either | Yes |
| Set flow factor | POST `/api/v1/gcode` `{"command":"M221 S100"}` | M221 S100 | Either | Yes |
| Upload file | PUT `/api/v1/files/usb/{name}` | — | HTTP only | Yes |
| Enable auto-temp | — | M155 S1 | Serial only | No |

---

## 9. Recommended Wallee Architecture

### 9.1 Three Device Packs

| Pack | Interface | Purpose | Reliability |
|------|-----------|---------|-------------|
| `prusa_link` | HTTP (<PRINTER_IP>:80) | Job control, file management, state | Stable (poll) |
| `prusa_metrics` | UDP listener (Pi:8514) | 62 telemetry metrics, safety signals | **Stable (push)** |
| `prusa_serial` | USB Serial (/dev/ttyACM0) | G-code commands, endstops | Unstable (reconnect) |

### 9.2 Why Three Packs?

1. **Metrics stream is the richest data source.** 62 metrics pushed at ~50 packets/sec. Includes safety-critical data (voltage, current, overcurrent flags, door sensor) that no other interface exposes.
2. **Metrics are push-based.** No polling needed. The printer sends data continuously. Lower latency, lower load.
3. **HTTP is stable for state/control.** Job state, file management, and G-code injection via `/api/v1/gcode`.
4. **Serial is only needed for G-code commands that need responses** (e.g., M119 endstops). Most data previously requiring serial is now available via metrics or syslog.
5. **Syslog stream (port 13514) replaces serial for reading** M105/M114 data. Same content, no USB instability.

### 9.3 Data Flow

```
prusa_link (HTTP, poll-based) ───────────────────────────────
  Sensors:
    read_printer_state  → printer.state, printer.job_state, printer.job_progress
    read_printer_info   → printer.firmware, printer.model, printer.serial
    read_file_list      → printer.files
  Actuators:
    pause_print, resume_print, cancel_print, start_print
    set_temperature (via /api/v1/gcode M104/M140)
    upload_file

prusa_metrics (UDP:8514, push-based) ───────────────────────
  Sensors (all passive — listen to UDP stream):
    temperatures        → printer.temp_nozzle, temp_bed, temp_chamber,
                          temp_heatbreak, temp_board, temp_mcu
    targets             → printer.target_nozzle, target_bed
    heater_pwm          → printer.pwm_nozzle, pwm_bed
    fans                → printer.fan_heatbreak, fan_print, xbe_fan_1/2/3
    position            → printer.pos_x/y/z (at 5Hz!)
    electrical          → printer.volt_bed, volt_nozz, curr_nozz
    safety_flags        → printer.oc_nozz, oc_inp (overcurrent)
    filament            → printer.fsensor_state, fsensor_flow
    enclosure           → printer.door_sensor
    firmware_health     → printer.heap_free, cpu_usage, stp_stall
    print_state         → printer.is_printing, print_filename

prusa_serial (USB, command-response) ────────────────────────
  Actuators (send G-code, read response):
    send_gcode          → generic G-code execution (requires_approval=True)
    read_endstops       → M119 (only available via serial)
  NOTE: Most read-only data now comes from metrics/syslog instead.
```

### 9.4 Syslog Listener (Port 13514)

The syslog stream on port 13514 provides a **stable alternative to USB serial for reading**:
- Same M105/M114 data as serial, delivered over UDP
- No USB disconnections, no garbled data
- Can be parsed for chamber temp (X:), ambient temp (A:), and all PWM values
- Should be used as a **fallback** if the metrics stream misses data (e.g., ambient temp is only in syslog, not metrics)

### 9.5 Bus Implementations Needed

| Bus | File | Purpose |
|-----|------|---------|
| `bus/network.py` | HTTP client | Already implemented |
| `bus/udp_listener.py` | UDP socket listener | NEW: receive metrics/syslog |
| `bus/serial.py` | Serial with reconnection | NEW: G-code command sender |

### 9.6 Serial Driver Requirements

Given the instability documented in §4.6, the serial driver (`bus/serial.py`) must:

1. **Auto-reconnect** when `/dev/ttyACM0` disappears and reappears
2. **Filter M115 spam** — discard unsolicited firmware identification blocks
3. **Use explicit polling** (M105) rather than M155 auto-reporting
4. **Timeout-based reads** — never block waiting for serial data
5. **Mutex** — only one command in-flight at a time
6. **Garble detection** — discard lines that don't match expected response format
7. **Detect device by USB VID:PID** (2c99:001f) rather than assuming /dev/ttyACM0
8. **Open-send-read-close pattern** — close port after each command to avoid disconnect crashes

### 9.7 Blacklisted G-codes

These must NEVER be sent by the agent or engine:

| Command | Reason |
|---------|--------|
| M997 | Triggers firmware update mode |
| M112 | Emergency stop — safety kernel only |
| M502 | Factory reset settings |
| M500 | Write to flash — risk of corruption |

---

## 10. Old Infrastructure (Killed)

Previous Wallee v2.1 processes were running on the Pi and holding the serial port. All killed on 2026-03-15:

| Process | PID | Status |
|---------|-----|--------|
| `wallee.prusa.observer` | 1142522 | **Killed** — was polling HTTP every 2s + reading serial |
| `wallee.daemons.host_pi` | 2191 | **Killed** — host daemon with service management |
| `wallee.ui.dashboard` | 1361 | **Killed** — web dashboard on port 8106 |

Config was at `/home/<user>/.config/wallee/prusa_readonly.json`. Output was written to `/tmp/wallee/`.

**Serial port is now free** for the new Wallee system.

---

## 11. File System on Printer USB

21 files on USB storage, including:
- `.bgcode` files (Prusa binary G-code — primary format)
- `.gcode` files (legacy text G-code)
- `MMU3/` subfolder (Multi Material Unit files)
- `FIRMWARE.BBF` (firmware binary)
- `LOG.TXT`, `COREON~1.TXT` (log files)

File names are 8.3 internally with `display_name` for human-readable names.
