# Roadmap

Deferral is a recorded decision, not silence. This file lists work that is
deliberately **not** done yet, so "we shipped the safety-critical core and
parked the rest" is auditable. Items are grouped by why they were deferred.

Context: [REFACTOR_EXECUTION_PLAN.md](REFACTOR_EXECUTION_PLAN.md) and
[PROJECT_ANALYSIS.md](PROJECT_ANALYSIS.md).

## Structural — one tree, one package (DONE in Phase 4)

The legacy tree was deleted and the v6 subtree promoted to the root `wallee/`
package (Integration Decision I-13), with the retired-findings disposition
register recorded in
[`internal/RETIRED_FINDINGS.md`](internal/RETIRED_FINDINGS.md) and the replay
corpus translated first. Remaining follow-ups:

- **Cut the archive refs at merge time**: `legacy/v5` branch + `legacy/v5-final`
  tag from the last pre-deletion `main` commit, with a tombstone README on the
  branch.
- **Hardware sign-off** gates the release: the deletion is on a feature branch;
  the signed Pi session from [`DEPLOYMENT.md`](DEPLOYMENT.md) §7 must happen
  before `v6.6.0` is tagged on `main`.
- **Required-check rename** on `main` (legacy-tests drops out of the required
  set) is a repo-settings change the maintainer applies at merge.
- **Build backend**: the plan's I-1 called for hatchling + a uv lockfile; the
  promotion kept setuptools to avoid coupling the tree move to a packaging
  swap. Migrate deliberately later.

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
- **Authenticated dashboard.** `wallee/dashboard.py` is unauthenticated and
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
