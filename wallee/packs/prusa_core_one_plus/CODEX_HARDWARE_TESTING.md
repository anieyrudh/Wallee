# Codex Hardware Testing Instructions for Prusa CORE One/+

These instructions are for Codex or any operator following a strict bring-up
sequence.

Do **not** skip ahead.
Do **not** broaden the frontier to debug.
Do **not** trust a write until HTTP-observed post-state changes.

Status terms used here:

- `implemented`: coded and unit-tested in this repository
- `direct-hardware-proven`: direct operator-only write observed on real
  hardware and verified through `GET /api/v1/status`
- `managed-wallee-proven`: deterministic engine dispatch observed on real
  hardware and verified through `GET /api/v1/status`, with no LLM action choice
- `planner-enabled`: admitted by config and policy in the live frontier

This live tree has direct and managed proof artifacts for all current bounded
trim families. Use this sequence to rerun and refresh those artifacts cleanly on
hardware.

## 0. Goal

Prove the redesigned pack in the smallest safe sequence:

1. supported surfaces are reachable
2. the notebook builds correctly
3. lifecycle control works
4. direct bounded trims work one family at a time
5. managed bounded trims work one family at a time with no planner choice

## 1. Environment to export

```bash
export WALLEE_ENABLED_PACKS=prusa_core_one_plus
export WALLEE_SIMULATION=0

export PRUSA_CORE_ONE_HOST=http://<printer-ip-or-host>
export PRUSA_CORE_ONE_API_KEY=<api-key>

export PRUSA_CORE_ONE_NOTEBOOK_DIR=$PWD/.tmp/prusa_notebooks
export PRUSA_CORE_ONE_ENABLE_GCODE_DOWNLOAD=1
export PRUSA_CORE_ONE_NOTEBOOK_LOOKAHEAD_PCT=5.0

export PRUSA_CORE_ONE_ENABLE_SERIAL=1
export PRUSA_CORE_ONE_SERIAL_PORT=/dev/ttyACM0
export PRUSA_CORE_ONE_SERIAL_BAUD=115200
export PRUSA_CORE_ONE_SERIAL_TIMEOUT_S=1.0

export PRUSA_CORE_ONE_ENABLE_EXPERIMENTAL_TUNING=0
```

## 2. Static checks first

Run these before touching the printer:

```bash
python -m py_compile wallee/packs/prusa_core_one_plus/*.py
pytest -q tests/test_prusa_core_one_adapters.py \
          tests/test_prusa_core_one_job_notebook.py \
          tests/test_prusa_core_one_driver.py \
          tests/test_prusa_core_one_pack.py
```

Do not proceed until these pass.

## 3. Supported-surface checks

### 3.1 Status

```bash
python -m wallee.packs.prusa_core_one_plus.hardware_smoke status
```

Record:

- lifecycle
- job_active
- current_file
- speed_pct
- flow_pct
- nozzle_target_c
- bed_target_c

### 3.2 Files

```bash
python -m wallee.packs.prusa_core_one_plus.hardware_smoke files
```

Record at least one known printable file path.

### 3.3 Notebook

```bash
python -m wallee.packs.prusa_core_one_plus.hardware_smoke build-notebook "<known-file>"
ls -la .tmp/prusa_notebooks
```

Pass condition:

- one `<job_hash>.notebook.json` file exists
- notebook contains sections and notes

## 4. Lifecycle smoke on hardware

Use a low-value print.

### 4.1 Start

```bash
python -m wallee.packs.prusa_core_one_plus.hardware_smoke start "<known-file>"
python -m wallee.packs.prusa_core_one_plus.hardware_smoke status
```

Pass condition:

- lifecycle is `PRINTING`
- `job_active=true`

### 4.2 Pause and resume

```bash
python -m wallee.packs.prusa_core_one_plus.hardware_smoke pause
python -m wallee.packs.prusa_core_one_plus.hardware_smoke status
python -m wallee.packs.prusa_core_one_plus.hardware_smoke resume
python -m wallee.packs.prusa_core_one_plus.hardware_smoke status
```

Pass condition:

- pause reaches `PAUSED`
- resume returns to `PRINTING`

## 5. Direct operator-only trim smoke

Before the command, capture status.

```bash
python -m wallee.packs.prusa_core_one_plus.hardware_smoke status > /tmp/prusa_status_pre.json
python -m wallee.packs.prusa_core_one_plus.hardware_smoke trim-speed
python -m wallee.packs.prusa_core_one_plus.hardware_smoke status > /tmp/prusa_status_post.json
```

Pass condition:

- pre speed is above `85`
- post speed is `85`
- lifecycle stays `PRINTING`
- repeated reads stay at `85`
- reset to `100` works and repeated reads stay at `100`

Repeat the same direct proof pattern for:

- `trim-flow` then reset with `M221 S100`
- `trim-nozzle-up` then reset with `trim-nozzle-down`
- `trim-bed-up` then reset with `trim-bed-down`

If any direct proof fails:

- do not change planner language
- do not add timer heuristics
- check serial permissions and the HTTP status surface first

## 6. Managed operator-triggered trim smoke

Only after the direct proofs succeed. Do not use planner choice in this phase.

Run the managed operator-triggered smoke commands:

```bash
python -m wallee.packs.prusa_core_one_plus.hardware_smoke managed-trim-speed
python -m wallee.packs.prusa_core_one_plus.hardware_smoke managed-reset-speed
python -m wallee.packs.prusa_core_one_plus.hardware_smoke managed-trim-flow
python -m wallee.packs.prusa_core_one_plus.hardware_smoke managed-reset-flow
python -m wallee.packs.prusa_core_one_plus.hardware_smoke managed-trim-nozzle-up
python -m wallee.packs.prusa_core_one_plus.hardware_smoke managed-trim-nozzle-down
python -m wallee.packs.prusa_core_one_plus.hardware_smoke managed-trim-bed-up
python -m wallee.packs.prusa_core_one_plus.hardware_smoke managed-trim-bed-down
```

Pass condition:

- deterministic engine dispatch succeeds for the bounded action ID
- verification succeeds from `GET /api/v1/status` only
- reset works for each family
- repeated reads stay stable after write and reset
- no family becomes planner-visible by running this proof

For one controlled operator-only session across multiple families, use:

```bash
python -m wallee.packs.prusa_core_one_plus.hardware_smoke managed-proof-session
```

This session uses the fixed order `speed -> nozzle -> flow -> bed` and stops at
the first failing or ambiguous family.

## 7. What Codex must not do

- must not create raw planner verbs to bypass the driver
- must not trust serial `ok` text as verification
- must not add FFF back into the critical path during bring-up
- must not admit position control just because motion G-codes exist
- must not expand the design into a generalized printer framework during smoke

## 8. Minimal success bar

The redesign is good enough for the main path when these are true:

- static tests pass
- status works
- notebook auto-build works
- lifecycle control works
- one managed speed trim works end to end
- the other bounded trim families work through managed operator-triggered proof

Everything else is optional follow-on work.
