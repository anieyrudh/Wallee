# Prusa Core One+ Setup

This checklist is for running the V6 Prusa pack on a real edge host.

## 1. Required interfaces

At minimum:

- PrusaLink / local HTTP API reachable from the Wallee host
- API key for the printer

Optional:

- UDP metrics stream enabled on the printer
- USB serial connected for diagnostics only

## 2. Environment variables

For a starting template, see [`.env.example`](/Users/anieyrudh/Desktop/Wallee2/wallee_v6/.env.example).

### Required

```bash
export WALLEE_ENABLED_PACKS=prusa_core_one_plus
export WALLEE_SIMULATION=0
export PRUSA_CORE_ONE_HOST=http://192.168.0.195
export PRUSA_CORE_ONE_API_KEY=your_prusalink_api_key
```

Exact v5-compatible aliases are supported for bring-up only:

```bash
export PRUSALINK_HOST=http://192.168.0.195
export PRUSALINK_API_KEY=your_prusalink_api_key
```

Precedence rule:

- if both canonical and alias envs are set, `PRUSA_CORE_ONE_*` wins
- alias support is limited to host and API key
- `DEVICE_PACKS` is not used by v6

### Recommended

```bash
export PRUSA_CORE_ONE_ENABLE_METRICS=1
export PRUSA_CORE_ONE_METRICS_BIND_HOST=0.0.0.0
export PRUSA_CORE_ONE_METRICS_PORT=8514
```

### Optional serial diagnostics

```bash
export PRUSA_CORE_ONE_ENABLE_SERIAL=1
export PRUSA_CORE_ONE_SERIAL_PORT=/dev/ttyACM0
export PRUSA_CORE_ONE_SERIAL_BAUD=115200
```

## 3. Why serial is optional

Serial is used only for diagnostics in this pack.
If serial is unstable or unavailable, leave it disabled.
The pack will continue to function through HTTP.

## 4. Metrics configuration on the printer

Enable the printer's metrics push stream and point it at the edge host IP and
port configured in `PRUSA_CORE_ONE_METRICS_PORT`.

The pack will degrade cleanly if metrics are unavailable, but the dashboard and
future trend logic will be poorer.

## 5. Safety notes

- The pack assumes HTTP is the authoritative control surface.
- The pack does **not** treat UDP metrics or serial diagnostics as safety
  critical.
- The pack computes `safe_to_unload` conservatively from bed/nozzle temperature
  proxies because the printer does not expose direct part temperature.
- `STOP_PROCESS` and `START_PROCESS` are marked medium hazard and require
  approval.

## 6. Operational smoke test

After setting the environment variables:

```bash
python -m wallee_v6.main --once --goal "Inspect printer state"
```

Expected result:

- the pack loads
- raw state is published
- the world packet contains `printer_1.*` facts
- no serial failure should prevent startup

## 7. Troubleshooting

### HTTP unreachable

Symptoms:

- `printer_1.raw.connected = false`
- `printer_1.raw.health = OFFLINE`

Check:

- host/IP reachable from the Pi
- API key correct
- printer HTTP service enabled

### No metrics

Symptoms:

- `printer_1.raw.metrics_available = false`

Check:

- printer metrics export enabled
- target IP/port correct
- UDP not blocked locally

### Serial failures

Symptoms:

- `printer_1.raw.serial_diag_available = false`

Action:

- disable serial diagnostics unless chamber/ambient readings are truly needed
- treat serial as optional until the USB CDC path is proven stable on your host
