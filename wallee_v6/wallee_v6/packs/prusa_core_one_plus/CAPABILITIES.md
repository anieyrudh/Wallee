# Prusa CORE One/+ Capability Notes

This file is the capability contract for this pack.

Status terms used below:

- `implemented`: coded and unit-tested in this repository
- `direct-hardware-proven`: direct operator-only write observed on real hardware
  and verified through `GET /api/v1/status`
- `managed-wallee-proven`: deterministic Wallee dispatch observed on real
  hardware and verified through `GET /api/v1/status`, with no LLM action choice
- `planner-enabled`: admitted by config and policy in the live frontier

## 1. Supported read surfaces

### HTTP status and job

Used for:

- lifecycle
- active job presence
- progress
- current file identity
- observable speed / flow / target temperatures when present
- post-write verification

### HTTP files and file metadata

Used for:

- file inventory
- requested file resolution
- printable metadata
- download path discovery

### Raw G-code download

Used for:

- deterministic job section parsing
- baseline notebook build

### Serial writes

Used for:

- bounded live tuning writes only

Not used for:

- truth
- free-form control
- planner language

## 2. Canonical Proof Matrix

| family | implemented | direct-hardware-proven | managed-wallee-proven | planner-enabled | default policy |
| --- | --- | --- | --- | --- | --- |
| speed | yes | yes | yes | yes | planner-visible |
| flow | yes | yes | yes | yes | planner-visible |
| nozzle temp | yes | yes | yes | yes | planner-visible |
| bed temp | yes | yes | yes | yes | planner-visible |

The managed proofs are operator-triggered only. They use bounded action IDs,
deterministic engine dispatch, supported-surface verification only, a reset
path, and repeated post-read checks.

Controlled planner proofs now also exist for:

- single-family reset-backed sessions for speed, flow, nozzle temp, and bed temp
- pairwise mixed-family sessions:
  - `speed + flow`
  - `speed + nozzle temp`
  - `speed + bed temp`
- one bounded all-families session with all four planner-enabled trim families

These planner proofs are still conservative:

- one bounded action at a time
- supported-surface verification after every action
- reverse operator-only reset stack at the end of the session
- hard stop on the first ambiguity, verification drift, or reset failure

Limited live normal-runtime observations also now exist under:

- [live_runtime/README.md](/Users/anieyrudh/Desktop/Wallee2/wallee_v6/docs/evidence/prusa_core_one_plus/live_runtime/README.md)

Those runs use the actual runtime entrypoint, not the smoke harness:

- `python -m wallee_v6.main --once --goal "..."`

Observed normal-runtime outcomes now include:

- weak-evidence `NO_ACTION`
- single-family speed success
- no repeated speed trim after a recent verified effect
- mixed `speed + flow` choosing one family only
- mixed `flow + nozzle` choosing one family only

## 3. Direct-Fact Print Phases

The pack now derives an internal printing-phase fact from supported surfaces
only:

- `startup_printing`
  - `lifecycle == PRINTING`
  - `job_active == true`
  - known job identity is missing, or
  - `job_progress_pct` is missing or `<= 0.0`, and
  - printer-reported `job_time_printing_s` is not advancing across consecutive
    polls
- `active_printing`
  - `lifecycle == PRINTING`
  - `job_active == true`
  - known job identity is present, and either:
  - `job_progress_pct > 0.0`
  - or printer-reported `job_time_printing_s` is advancing across consecutive
    polls
- `not_printing`
  - anything else

This distinction exists to keep the speed family off the startup-printing path
without adding timer heuristics. The speed action remains planner-enabled, but
it is internally blocked until the printer reports `active_printing`.

Startup-surface inspection on April 12, 2026 showed one useful supporting fact
from `GET /api/v1/job`: `job.file.display_name` is present from the first
startup sample even when `GET /api/v1/status` omits `status.job.file`. The live
pack now uses `/api/v1/job` to ground `current_file` and job identity during
startup. The live gate now admits `active_printing` when either
`job_progress_pct > 0.0` or the printer-reported `job_time_printing_s` counter
advances across consecutive polls for the same known job identity. No
wall-clock timer is used.

## 4. Family Operating Contracts

### Speed

- bounded step: `M220 S(current±5)`, clamped to `75..125`
- block states: `not_printing`, `startup_printing`, serial writer unavailable,
  `speed_pct` missing, `speed_pct <= 75`, `speed_pct >= 125`, or
  `job_progress_pct <= 0`
- verification field: `speed_pct`
- reset action: `A_PRUSA_OPERATOR_RESTORE_SPEED_DEFAULT` via `M220 S100`
- planner exposure status: planner-enabled
- autonomy boundary: `active_printing` only
- planner admission rule: require `job_progress_pct > 0` even when
  `active_printing` is opened by advancing `job_time_printing_s`
- suppression facts:
  - `printer_1.speed_autonomy_boundary`
  - `printer_1.speed_autonomy_eligible`
  - `printer_1.speed_autonomy_blockers`

### Flow

- bounded step: `M221 S(current±5)`, clamped to `75..125`
- block states: not `PRINTING`, serial writer unavailable, `flow_pct` missing,
  `flow_pct <= 75`, or `flow_pct >= 125`
- verification field: `flow_pct`
- reset action: `A_PRUSA_OPERATOR_RESTORE_FLOW_DEFAULT` via `M221 S100`
- planner exposure status: planner-enabled
- planner admission facts:
  - `printer_1.flow_shadow_eligible`
  - `printer_1.flow_shadow_blockers`
  - `printer_1.flow_shadow_actions`

### Nozzle Temp

- bounded step: `M104 S(current+5)` or `M104 S(current-5)`
- block states: not `PRINTING`, serial writer unavailable,
  `nozzle_target_c` missing, minimum extrusion floor reached, or maximum nozzle
  target reached
- verification field: `nozzle_target_c`
- reset action: paired bounded opposite action
- planner exposure status: planner-enabled
- planner admission facts:
  - `printer_1.nozzle_shadow_eligible`
  - `printer_1.nozzle_shadow_blockers`
  - `printer_1.nozzle_shadow_actions`

### Nozzle Shadow Readiness

Readiness facts are published and used to gate planner admission:

- `printer_1.nozzle_shadow_eligible`
- `printer_1.nozzle_shadow_blockers`
- `printer_1.nozzle_shadow_actions`

These use direct facts only:

- `lifecycle`
- `job_active`
- `printing_phase`
- `live_tuning_available`
- `nozzle_target_c`
- `min_extrusion_temp_c`

If nozzle is not eligible, `printer_1.nozzle_shadow_blockers` records the
direct-fact reason as a pipe-delimited string.

### Bed Temp

- bounded step: `M140 S(current+5)` or `M140 S(current-5)`, clamped to
  `job/material default ±15C`
- block states: not `PRINTING`, serial writer unavailable, `bed_target_c`
  missing, bed floor reached, or bed ceiling reached
- verification field: `bed_target_c`
- reset action: paired bounded opposite action
- planner exposure status: planner-enabled
- planner admission facts:
  - `printer_1.bed_shadow_eligible`
  - `printer_1.bed_shadow_blockers`
  - `printer_1.bed_shadow_actions`

## 5. Planner-visible actions

### Always admitted when legal

- `PAUSE_PROCESS`
- `RESUME_PROCESS`
- `STOP_PROCESS`
- `START_PROCESS`
- builtin wait-for-cool
- builtin call-human

### Planner-enabled live tuning families

- `A_PRUSA_TRIM_SPEED_DOWN_SMALL`
  - command: `M220 S(current-5)` within `75..125`
  - verify: HTTP speed = explicit target
  - reset: `A_PRUSA_OPERATOR_RESTORE_SPEED_DEFAULT`
  - status: implemented, direct-hardware-proven, managed-wallee-proven, and
    planner-enabled
  - Phase 1 autonomy boundary: only admit planner-driven speed trim once
    `printer_1.printing_phase == active_printing`
- `A_PRUSA_TRIM_SPEED_UP_SMALL`
  - command: `M220 S(current+5)` within `75..125`
  - verify: HTTP speed = explicit target
  - reset: `A_PRUSA_OPERATOR_RESTORE_SPEED_DEFAULT`
  - status: implemented and planner-enabled
- `A_PRUSA_TRIM_FLOW_DOWN_SMALL`
  - command: `M221 S(current-5)` within `75..125`
  - verify: flow readback = explicit target
  - reset: `A_PRUSA_OPERATOR_RESTORE_FLOW_DEFAULT`
  - status: implemented, direct-hardware-proven, managed-wallee-proven, and
    planner-enabled
  - planner admission facts: only admit planner-driven flow trim when
    `printer_1.flow_shadow_eligible == true`
- `A_PRUSA_TRIM_FLOW_UP_SMALL`
  - command: `M221 S(current+5)` within `75..125`
  - verify: flow readback = explicit target
  - reset: `A_PRUSA_OPERATOR_RESTORE_FLOW_DEFAULT`
  - status: implemented and planner-enabled
- `A_PRUSA_TRIM_NOZZLE_DOWN_SMALL`
  - command: `M104 S(current-5)`
  - verify: HTTP nozzle target changed
  - reset: `A_PRUSA_TRIM_NOZZLE_UP_SMALL`
  - status: implemented, direct-hardware-proven, managed-wallee-proven, and
    planner-enabled
  - planner admission facts: only admit planner-driven nozzle trim when
    `printer_1.nozzle_shadow_eligible == true`
- `A_PRUSA_TRIM_NOZZLE_UP_SMALL`
  - command: `M104 S(current+5)`
  - verify: HTTP nozzle target changed
  - reset: `A_PRUSA_TRIM_NOZZLE_DOWN_SMALL`
  - status: implemented, direct-hardware-proven, managed-wallee-proven, and
    planner-enabled
  - planner admission facts: only admit planner-driven nozzle trim when
    `printer_1.nozzle_shadow_eligible == true`
- `A_PRUSA_TRIM_BED_DOWN_SMALL`
  - command: `M140 S(current-5)`
  - verify: HTTP bed target changed
  - reset: `A_PRUSA_TRIM_BED_UP_SMALL`
  - status: implemented, direct-hardware-proven, managed-wallee-proven, and
    planner-enabled
  - planner admission facts: only admit planner-driven bed trim when
    `printer_1.bed_shadow_eligible == true`
- `A_PRUSA_TRIM_BED_UP_SMALL`
  - command: `M140 S(current+5)`
  - verify: HTTP bed target changed
  - reset: `A_PRUSA_TRIM_BED_DOWN_SMALL`
  - status: implemented, direct-hardware-proven, managed-wallee-proven, and
    planner-enabled
  - planner admission facts: only admit planner-driven bed trim when
    `printer_1.bed_shadow_eligible == true`

Managed proof artifacts for the trim families live under:

- [2026-04-12-speed-managed-proof.md](/Users/anieyrudh/Desktop/Wallee2/wallee_v6/docs/evidence/prusa_core_one_plus/managed_smokes/2026-04-12-speed-managed-proof.md)
- [2026-04-12-flow-managed-proof.md](/Users/anieyrudh/Desktop/Wallee2/wallee_v6/docs/evidence/prusa_core_one_plus/managed_smokes/2026-04-12-flow-managed-proof.md)
- [2026-04-12-nozzle-temp-managed-proof.md](/Users/anieyrudh/Desktop/Wallee2/wallee_v6/docs/evidence/prusa_core_one_plus/managed_smokes/2026-04-12-nozzle-temp-managed-proof.md)
- [2026-04-12-bed-temp-managed-proof.md](/Users/anieyrudh/Desktop/Wallee2/wallee_v6/docs/evidence/prusa_core_one_plus/managed_smokes/2026-04-12-bed-temp-managed-proof.md)

Phase 1 planner experiment artifacts live under:

- `docs/evidence/prusa_core_one_plus/phase1/runs`
- `docs/evidence/prusa_core_one_plus/phase1/startup_traces`

Recent controlled mixed-family planner artifacts:

- [2026-04-13T125852-phase1-speed-flow-session.json](/Users/anieyrudh/Desktop/Wallee2/wallee_v6/docs/evidence/prusa_core_one_plus/phase1/runs/2026-04-13T125852-phase1-speed-flow-session.json)
- [2026-04-13T125957-phase1-speed-nozzle-session.json](/Users/anieyrudh/Desktop/Wallee2/wallee_v6/docs/evidence/prusa_core_one_plus/phase1/runs/2026-04-13T125957-phase1-speed-nozzle-session.json)
- [2026-04-13T130055-phase1-speed-bed-session.json](/Users/anieyrudh/Desktop/Wallee2/wallee_v6/docs/evidence/prusa_core_one_plus/phase1/runs/2026-04-13T130055-phase1-speed-bed-session.json)
- [2026-04-13T130243-phase1-all-families-session.json](/Users/anieyrudh/Desktop/Wallee2/wallee_v6/docs/evidence/prusa_core_one_plus/phase1/runs/2026-04-13T130243-phase1-all-families-session.json)

Planner-hardening eval artifacts live under:

- [planner-baseline.json](/Users/anieyrudh/Desktop/Wallee2/wallee_v6/docs/evidence/prusa_core_one_plus/planner_eval/planner-baseline.json)
- [comparison-report.md](/Users/anieyrudh/Desktop/Wallee2/wallee_v6/docs/evidence/prusa_core_one_plus/planner_eval/comparison-report.md)
- [planner_eval cases](/Users/anieyrudh/Desktop/Wallee2/wallee_v6/docs/evidence/prusa_core_one_plus/planner_eval/cases)

What these do and do not prove:

- they prove bounded mixed-family planner behavior under the session harness
- they do not prove unconstrained long-horizon autonomy without per-action
  verification and reset discipline

Startup traces remain evidence only. They do not widen planner control and they
do not change the `active_printing` gate.

## 6. Not admitted

- arbitrary raw G-code from planner
- arbitrary speed / flow / temperature numeric targets from planner
- planner-visible raw motion / position control
- FFF or any observer as a writer

## 7. Notebook capabilities

The pack automatically builds a deterministic notebook when it can identify a
printable file.

Notebook features:

- material extraction
- layer height extraction
- temperature target extraction from metadata / G-code
- layer / section map
- local watchpoints for bridge, high-flow, and tiny-layer regions
- merge path for external grounded notes

## 8. Safety and clarity rules

- supported surfaces first
- bounded actions only
- one writer path per action family
- verification from observed post-state only
- notebook is read-only context
