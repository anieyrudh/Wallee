# Roadmap

Deferral is a recorded decision, not silence. This file lists work that is
deliberately **not** done yet, so "we shipped the safety-critical core and
parked the rest" is auditable. Items are grouped by why they were deferred.

Context: [REFACTOR_EXECUTION_PLAN.md](REFACTOR_EXECUTION_PLAN.md) and
[PROJECT_ANALYSIS.md](PROJECT_ANALYSIS.md).

## Structural — one tree, one package

The repository still carries two trees (`wallee/` legacy v5 and `wallee_v6/`).
The promotion to a single root package is deferred as its own set of CI-green
PRs (Integration Decision I-13) because a whole-tree move is destructive and
must not be mixed with behavior changes:

- **Delete the legacy tree** after the hardware sign-off, once `legacy/v5-final`
  is tagged and a `legacy/v5` archive branch with a tombstone README is cut. The
  archive PR carries the retired-findings disposition register (every
  legacy-only finding, listed as "known, retired with the tree").
- **Promote `wallee_v6/` to the root** with a pure `git mv` plus a blanket
  `wallee_v6 -> wallee` rename across py/yaml/toml/service files, gated by
  `rg wallee_v6` returning zero hits (string-form module refs included). No
  behavior change in the same PR.
- **Packaging/CI/config** follow-up: entry points, workflow paths, and required
  check names (renamed via temporary alias jobs so `main` never goes
  unmergeable).

## Device-genericity — finish the Prusa eviction

`scripts/check_core_purity.py` ratchets device knowledge out of the v6 core and
blocks new leakage, but the count is not yet zero (see
`.core-purity-allowlist.json`). The remaining references live in the
decision-signal compilation, which **is** the planner's prompt. Evicting them
needs the structured `TuningDescriptor` redesign plus a cassette re-record, so
it is its own behavior-preserving PR. The end state empties the allowlist and
`test_no_core_module_references_device_ids` flips from xfail to a passing
absolute check.

## Vision vocabulary — structural catastrophic findings (extends I-14)

The deterministic active-print gates now read the closed vision `finding_type`
enum only (`residue`/`stringing`/`spaghetti`/`blob`/`unknown`), never the
model's free-text summary. That enum cannot yet express every catastrophic
failure (detachment, collision, jam) as a distinct type, so those rely on the
model tagging `spaghetti`. Extending the `FindingType` enum (and the vision
prompt, with a re-record and golden re-bless) would let the gates escalate on
those states structurally instead. Tracked as a follow-up to the I-14
behavior-change PR.

## Deferred P3 research upgrades

From the analysis, parked until the core is stable on hardware:

- Local Pi safety-critic with a conservative-enum degraded mode.
- Hybrid local-CNN -> VLM vision with bounding-box cross-check.
- Forecast-residual telemetry lane and a G-code static-screening gate.
- LTL/MLTL invariants over PlanIR plus conformal approval escalation.
- MCP device-pack servers.

## Deferred product surfaces

- **Full notification port and event-driven wake.** The operator/approval
  channel (e.g. Telegram) hardening rules are documented, but the full port and
  an event-driven (rather than polled) control-loop wake are not done.
- **Authenticated dashboard.** `wallee_v6/dashboard.py` is unauthenticated and
  not wired into the runtime; a designed, authenticated read surface is future
  work (and `fastapi`/`uvicorn` drop out of the deps until then).

## Largely-satisfied P1 items with small remainders

- **Structured outputs.** v6 already sends strict `response_format` with
  `cache_control`. Remaining: `require_parameters`/model filtering and
  reasoning-field-first evaluation.
- **Reliability.** Remaining: cache-aware fallback chains, OpenTelemetry GenAI
  spans, and per-cycle output-sanity alerts.

## Validation lanes not yet built

- `sim-evals`: fault-injection trajectory evals over the sim printer (offline,
  deterministic).
- Live `pass^k` reliability lane (only meaningful against a live planner, not a
  deterministic replay).
