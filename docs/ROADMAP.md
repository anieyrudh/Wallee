# Roadmap

> **v7 direction is now planned**: see [V7_EXECUTION_PLAN.md](V7_EXECUTION_PLAN.md)
> (executable milestones) and [V7_DESIGN_SKETCH.md](V7_DESIGN_SKETCH.md)
> (rationale). Items below that the v7 plan absorbs are marked there;
> this file remains the ledger for anything not yet scheduled.

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

## Remaining Prune backlog (Phase 4 items behind the ratchets)

`main.py` was split (cli / composition / loop / artifacts / archive behind the
entrypoint e2e suite); seven files remain over the 800-line cap, enforced
shrink-only by `scripts/check_file_sizes.py` until each lands its planned
split: `hardware_smoke.py` (→ `smoke/` package via the `_ManagedFamilySpec`
table), `pack.py` (→ state_normalizer / frontier / realize / tuning_policy),
`job_notebook.py`, `models.py` (contracts vs prompt-view compilation),
`vision.py`, `planner_eval.py`, and `driver.py` (whose six `_set_*` clones
also await the per-family spec-table dedupe, Prune item 5). Each split must
show byte-identical goldens; the cap flips to absolute when the allowlist
empties. Also pending from the Prune list: module loggers + print-to-handler
(item 7), generated typed prompt-view models (item 4), and the typed FactKey
registry (item 14).

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
- **Authenticated dashboard.** The unauthenticated, unwired `dashboard.py`
  was deleted in Phase 4 (Integration Decision I-8) and `fastapi`/`uvicorn`
  dropped from the dependencies. A designed, authenticated read surface is
  future work.

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

## The microfactory horizon (end-state assessment, 2026-07)

The stated end goal is Wallee as the operating layer of a microfactory: N
heterogeneous machines under one roof. **Verdict: this does not change the v7
design — it layers on top of it.** Industrial automation already settled this
decomposition (cell controller vs. supervisory operations, the ISA-95 shape,
independently mirrored by the Viam / FDM Monster / Prusa Connect convergence
in [DESIGN_RESEARCH_2026-07.md](DESIGN_RESEARCH_2026-07.md)): each Wallee
instance is a **cell controller** — locally safe, locally gated, fully
functional with the supervisor absent — and a microfactory OS is a
**supervisory layer above** the cells, not a change inside them. The
propose/dispose architecture recurses cleanly: a factory-level planner
proposing dispatches against a factory-level frontier, disposed by the same
deterministic gate/journal/approval machinery one level up.

What the supervisory layer eventually adds — all **above** the kernel, none
inside it, each behind a trigger:

| Concern | Shape | Trigger |
|---|---|---|
| Job/order scheduling + dispatch | Advisory coordinator (v7.x fleet lane) grows into a dispatcher whose dispatches are **proposals into each machine's existing goal → approval pipeline** | N≥3 and real queue pain |
| Material/inventory state | New fact domain under the reserved `site.` key namespace (V7-I4), not a new mechanism | First real material-tracking need |
| Cross-machine workflows | Orchestration of per-machine jobs (print → remove → post-process) at the supervisory level | First genuinely multi-machine job |
| Multi-operator authorization | Operator identity on approval records + per-machine/verb roles; additive to the journal schema and the V7-I10 gateway grammar | Second regular operator |
| Staged fleet updates | RAUC A/B (already planned) + canary-machine rollout procedure | First fleet-wide update |
| Factory observability | Read-only aggregation over per-machine journals/events (the coordinator's planned shape) | With the coordinator |

Commitments held **now** so none of the above ever requires surgery:

1. **The coordinator stays advisory and separate** (already a v7 rule). When
   it becomes a dispatcher there is still exactly one command path into a
   machine: its own gated pipeline. Never a second path.
2. **`site.` is a reserved fact-key namespace** from M1 (V7-I4) so
   factory-level facts slot into the registry without re-cutting it.
3. **Goals/jobs stay structured records and approvals stay attributable**, so
   a scheduler can emit goals and RBAC can bind to approvals without schema
   surgery.
4. **Safety authority is per-machine, permanently.** A factory ESTOP is a
   fan-out to per-machine stops; there is no central safety authority whose
   failure is shared across cells.

The single item where the design would genuinely have to evolve — recorded
here so it is a decision, not a surprise: a **shared-workspace actuator** (an
arm or gantry serving multiple cells) breaks the per-machine safety premise,
because one actuator's motion crosses cell boundaries. That day needs
hardware-grade zoned interlocks (light curtains / estop groups wired across
the affected cells) and a new capability family designed with the same rigor
as the safety protocol — it is hardware-driven and cannot be pre-built in
software. Trigger: the first shared actuator, not before.
