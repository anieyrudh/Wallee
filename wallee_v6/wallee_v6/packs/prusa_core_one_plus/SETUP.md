# Prusa CORE One/+ Setup

This checklist is for running the redesigned Prusa pack on a real edge host.

Status terms used here:

- `implemented`: coded and unit-tested in this repository
- `direct-hardware-proven`: direct operator-only write observed on real
  hardware and verified through `GET /api/v1/status`
- `managed-wallee-proven`: deterministic engine dispatch observed on real
  hardware and verified through `GET /api/v1/status`, with no LLM action choice
- `planner-enabled`: admitted by config and policy in the live frontier

This setup guide enables the managed proof path. It does not, by itself, make a
family planner-enabled.

## 1. Required prerequisites

- a reachable PrusaLink host for the target printer
- a valid PrusaLink API key if the host requires it
- local USB serial access **only if** you want live tuning writes
- a sacrificial or low-value print for first hardware smoke tests

## 2. Minimum environment

```bash
export WALLEE_ENABLED_PACKS=prusa_core_one_plus
export WALLEE_SIMULATION=0

export PRUSA_CORE_ONE_HOST=http://<PRINTER_IP>
export PRUSA_CORE_ONE_API_KEY=your_prusalink_api_key
export PRUSA_CORE_ONE_NOZZLE_CAMERA_DEVICE_PATH=/dev/v4l/by-id/usb-3DO_3DO_NOZZLE_CAMERA_V2_3DO-video-index0
```

Exact aliases are still supported:

- `PRUSALINK_HOST` -> `PRUSA_CORE_ONE_HOST`
- `PRUSALINK_API_KEY` -> `PRUSA_CORE_ONE_API_KEY`

If both are present, `PRUSA_CORE_ONE_*` wins.

For this frozen CORE One/+ baseline, keep both Wallee and `crowsnest` bound to
the stable by-id nozzle camera path above rather than `/dev/video0`.

## 3. Enable the grounded notebook

```bash
export PRUSA_CORE_ONE_NOTEBOOK_DIR=/var/lib/wallee/prusa_notebooks
export PRUSA_CORE_ONE_ENABLE_GCODE_DOWNLOAD=1
export PRUSA_CORE_ONE_NOTEBOOK_LOOKAHEAD_PCT=5.0
```

What happens:

- the pack builds a baseline notebook automatically
- it writes `<job_hash>.notebook.json` into the notebook directory
- if a matching `<job_hash>.notes.json` exists, the pack merges it

## 4. Enable bounded tuning writes

```bash
export PRUSA_CORE_ONE_ENABLE_SERIAL=1
export PRUSA_CORE_ONE_SERIAL_PORT=/dev/ttyACM0
export PRUSA_CORE_ONE_SERIAL_BAUD=115200
export PRUSA_CORE_ONE_SERIAL_TIMEOUT_S=1.0
```

This enables the serial writer needed for all bounded trim families and their
reset paths.

## 5. Operator-managed proof commands

Do not widen planner exposure here. These commands stay operator-triggered even
after managed proof passes.

```bash
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-trim-flow
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-reset-flow
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-trim-nozzle-up
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-trim-nozzle-down
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-trim-bed-up
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-trim-bed-down
```

Speed, flow, nozzle temp, and bed temp are all now planner-enabled.

## 6. Cooling and safety thresholds

```bash
export WALLEE_SAFE_TO_UNLOAD_TEMP_C=35
export PRUSA_CORE_ONE_SAFE_NOZZLE_TOUCH_C=50
```

## 7. Quick connectivity checks

### Status

```bash
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke status
```

### File inventory

```bash
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke files
```

### Notebook build for one known file

```bash
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke build-notebook "Benchy Rules.bgcode"
```

## 8. First-run expectations

On a healthy setup you should see:

- status JSON with lifecycle and current temperatures
- printable files listed from printer storage
- a notebook JSON file written into the notebook directory

If status works but serial does not, lifecycle control and notebook building can
still run. Only live tuning will stay unavailable.
