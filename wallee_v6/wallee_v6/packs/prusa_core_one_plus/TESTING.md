# Prusa CORE One/+ Testing

Use two loops:

1. **fast unit tests** for design correctness
2. **real-printer smoke tests** for supported-surface reality

Status vocabulary used in this document:

- `implemented`: coded and unit-tested in this repository
- `direct-hardware-proven`: direct operator-only write observed on real
  hardware and verified through `GET /api/v1/status`
- `managed-wallee-proven`: deterministic engine dispatch observed on real
  hardware and verified through `GET /api/v1/status`, with no LLM action choice
- `planner-enabled`: admitted by config and policy in the live frontier

## 1. Fast unit tests

Run the pack test set:

```bash
pytest -q tests/test_prusa_core_one_adapters.py \
          tests/test_prusa_core_one_job_notebook.py \
          tests/test_prusa_core_one_driver.py \
          tests/test_prusa_core_one_pack.py
```

Run the full repository suite:

```bash
pytest -q
```

### What these tests cover

- HTTP file-tree flattening and path quoting
- environment loading
- deterministic notebook grounding
- status normalization
- driver lifecycle operations
- bounded trim write + verify logic for speed, flow, nozzle, and bed
- pack normalization
- frontier generation
- operator-only action exposure and reset actions
- notebook auto-build and persistence
- registry load path

## 2. Real-printer smoke tests

Always test on a low-value print first.

### Phase A: status and notebook only

```bash
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke status
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke files
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke build-notebook "Benchy Rules.bgcode"
```

Pass criteria:

- status returns sane lifecycle and temperatures
- files returns the expected printable inventory
- notebook build writes `<job_hash>.notebook.json`

### Phase B: lifecycle control

Start a print from storage, then pause and resume it.

```bash
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke start "Benchy Rules.bgcode"
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke pause
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke resume
```

Pass criteria:

- printer enters `PRINTING`
- printer enters `PAUSED`
- printer returns to `PRINTING`

### Phase C: direct operator-only trim proofs

Only when the printer is actively printing. Verification is always through
`GET /api/v1/status`.

```bash
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke status
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke trim-speed
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke status
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke trim-flow
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke status
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke trim-nozzle-up
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke status
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke trim-nozzle-down
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke status
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke trim-bed-up
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke status
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke trim-bed-down
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke status
```

Pass criteria:

- each write is accepted by the printer
- post-state changes on the supported surface only
- the print remains active
- each family has a reset path and repeated post-read checks

### Phase D: managed operator-triggered trim proofs

Run these through Wallee without using planner choice.

```bash
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-trim-speed
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-reset-speed
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-trim-flow
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-reset-flow
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-trim-nozzle-up
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-trim-nozzle-down
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-trim-bed-up
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-trim-bed-down
```

Pass criteria:

- deterministic engine dispatch succeeds with a bounded action ID only
- verification succeeds from `GET /api/v1/status` only
- reset works for each family
- repeated post-read checks stay stable
- managed proof does not automatically make a family planner-enabled

### Phase E: controlled multi-family session

When you want one operator-only session that can progress across multiple
families, use the fixed order:

1. speed
2. nozzle
3. flow
4. bed

```bash
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke managed-proof-session
```

Hard-stop rules:

- if any family fails trim verification, reset verification, repeatability, or
  leaves an ambiguous state, the session stops immediately
- the next family is not attempted until the current family verifies and resets
  cleanly
- this session does not change planner exposure

### Phase F: planner-driven trim experiments

All four trim families are currently planner-enabled.

```bash
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke phase1-speed-once
```

Behavior:

- if `printing_phase != active_printing`, the run is suppressed
- if suppressed during `startup_printing`, the helper saves a startup trace
  under `docs/evidence/prusa_core_one_plus/phase1/startup_traces`
- if the planner proposes anything other than the single bounded speed trim, the
  run is suppressed and the reason is saved
- every run writes a permanent artifact under
  `docs/evidence/prusa_core_one_plus/phase1/runs`

Controlled mixed-family variants are also available:

```bash
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke phase1-speed-flow-session
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke phase1-speed-nozzle-session
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke phase1-speed-bed-session
python -m wallee_v6.packs.prusa_core_one_plus.hardware_smoke phase1-all-families-session
```

These runs keep the same conservative rules:

- one bounded planner action at a time
- supported-surface verification after each action
- operator-only reverse reset at the end
- hard stop on the first ambiguity or verification/reset failure

Pass criteria:

- the autonomy boundary remains `active_printing` only
- `active_printing` may come from either `progress > 0` or advancing
  `job_time_printing_s` across consecutive polls for the same job
- planner-driven speed trim itself still requires `job_progress_pct > 0`
- suppression reasons are explicit and saved
- the planner frontier should reflect the currently admitted bounded trim families

## 3. Current trim-family status

- speed: implemented, direct-hardware-proven, managed-wallee-proven,
  planner-enabled
- flow: implemented, direct-hardware-proven, managed-wallee-proven,
  planner-enabled
- nozzle temp: implemented, direct-hardware-proven, managed-wallee-proven,
  planner-enabled
- bed temp: implemented, direct-hardware-proven, managed-wallee-proven,
  planner-enabled

## 4. Mixed-family planner proof status

- pairwise controlled mixed-family planner sessions passed for:
  - `speed + flow`
  - `speed + nozzle temp`
  - `speed + bed temp`
- one bounded all-families planner session passed with:
  - speed
  - flow
  - nozzle temp
  - bed temp
- these proofs are session-harness proofs, not unconstrained autonomy proofs

Recent artifacts:

- `wallee_v6/docs/evidence/prusa_core_one_plus/phase1/runs/2026-04-13T125852-phase1-speed-flow-session.json` (evidence artifact — not yet committed to this repository)
- `wallee_v6/docs/evidence/prusa_core_one_plus/phase1/runs/2026-04-13T125957-phase1-speed-nozzle-session.json` (evidence artifact — not yet committed to this repository)
- `wallee_v6/docs/evidence/prusa_core_one_plus/phase1/runs/2026-04-13T130055-phase1-speed-bed-session.json` (evidence artifact — not yet committed to this repository)
- `wallee_v6/docs/evidence/prusa_core_one_plus/phase1/runs/2026-04-13T130243-phase1-all-families-session.json` (evidence artifact — not yet committed to this repository)

Planner-hardening eval artifacts:

- `wallee_v6/docs/evidence/prusa_core_one_plus/planner_eval/planner-baseline.json` (evidence artifact — not yet committed to this repository)
- `wallee_v6/docs/evidence/prusa_core_one_plus/planner_eval/comparison-report.md` (evidence artifact — not yet committed to this repository)
- `wallee_v6/docs/evidence/prusa_core_one_plus/planner_eval/cases` (evidence artifact — not yet committed to this repository)

## 5. Limited live runtime observations

Use the actual runtime entrypoint for bounded live observation:

```bash
python -m wallee_v6.main --once --goal "Reduce print speed a little while keeping the current print running."
```

Current normal-runtime evidence:

- `wallee_v6/docs/evidence/prusa_core_one_plus/live_runtime/README.md` (evidence artifact — not yet committed to this repository)
- `wallee_v6/docs/evidence/prusa_core_one_plus/live_runtime/RUNBOOK.md` (evidence artifact — not yet committed to this repository)
- `wallee_v6/docs/evidence/prusa_core_one_plus/live_runtime/runs/2026-04-13T092859-runtime-weak-evidence-no-action.json` (evidence artifact — not yet committed to this repository)
- `wallee_v6/docs/evidence/prusa_core_one_plus/live_runtime/runs/2026-04-13T093016-runtime-single-family-speed.json` (evidence artifact — not yet committed to this repository)
- `wallee_v6/docs/evidence/prusa_core_one_plus/live_runtime/runs/2026-04-13T093051-runtime-repeat-speed-after-effect.json` (evidence artifact — not yet committed to this repository)
- `wallee_v6/docs/evidence/prusa_core_one_plus/live_runtime/runs/2026-04-13T093136-runtime-mixed-family-speed-flow.json` (evidence artifact — not yet committed to this repository)
- `wallee_v6/docs/evidence/prusa_core_one_plus/live_runtime/runs/2026-04-13T093445-runtime-mixed-family-flow-nozzle.json` (evidence artifact — not yet committed to this repository)

Observed behavior in the normal runtime path:

- weak-evidence goals preferred `NO_ACTION`
- repeated speed trim was not selected after a recent verified effect
- mixed-family goals still produced one-family-only decisions
- cleanup restored baseline after each bounded observation set

## 6. What not to do during testing

- do not expose arbitrary raw G-code to the planner just to debug faster
- do not use serial readback text as truth
- do not use planner choice for the managed trim proofs in this phase
- do not run the first tuning tests on a valuable print

## 7. Failure triage

### If status fails

Stop. Fix PrusaLink reachability or credentials first.

### If notebook build fails

Check file download support and file metadata path before touching control.

### If lifecycle control works but a trim family fails

Check:

- serial port permissions
- serial device path
- whether HTTP status actually exposes the target fact on this printer/firmware

### If a managed proof writes but does not verify

Treat it as not yet ready for planner admission. Do not promote it by argument.
