# V7 Execution Plan — the general-purpose physical-agent harness

Status: approved direction (July 2026). Successor to
[REFACTOR_EXECUTION_PLAN.md](REFACTOR_EXECUTION_PLAN.md); design rationale in
[V7_DESIGN_SKETCH.md](V7_DESIGN_SKETCH.md) and evidence in
[DESIGN_RESEARCH_2026-07.md](DESIGN_RESEARCH_2026-07.md). This document is
written to be executed by an agent session with no other context: every
milestone names its files, tests, gates, and exit criteria.

---

## 0. How to execute this plan (read first, follow always)

1. **Branch discipline.** All v7 work happens on a dedicated branch cut from
   `main` AFTER the v6.6 hardware sign-off merges (suggested:
   `v7/mainline`; one child branch per milestone if preferred). Never push
   to `main`; never open a PR unless asked.
2. **The method is contract-first, always.** Every structural change lands
   behind the pinned behavior: full suite green, goldens byte-identical
   unless the milestone explicitly schedules a re-bless, cassettes
   byte-stable unless it schedules a re-record. Re-bless/re-record are
   separate commits titled `[re-bless] …` / `[re-record] …`, never mixed
   with source changes. This plan schedules exactly TWO re-records (Phase 0
   transport; M1 capability keys) and TWO re-blesses (M1; M2 only if the
   split leaks — it should not).
3. **Not-yet-true invariants** are `xfail(strict=True)` with reasons
   referencing `docs/V7_EXECUTION_PLAN.md §N` (see V7-I1 for the checker
   migration). Every new invariant is added to `tests/contract/MANIFEST`
   in the same commit.
4. **Run `./scripts/gate.sh` before every push.** If a gate and this plan
   conflict, the gate wins; fix the plan reference in the same commit and
   note it.
5. **When reality contradicts this plan** (an API changed, a file moved),
   prefer the smallest faithful adaptation, record it in the commit
   message, and update this document's relevant line in the same commit.
   Do not silently deviate.
6. **Anti-scope (never do these under this plan):** no message bus; no
   custom OS beyond the image pipeline in §8; no vector/graph memory; no
   runtime self-modification; no streaming control decisions; no second
   database; no Rust beyond the sentinel workspace; no free-form args
   channel in PlanIR; capability schemas stay declarative data (enums,
   bounds, floors — never expressions or code).

## 1. Goals and non-negotiables

v7 turns the proven single-printer runtime into a general-purpose harness:
any machine expressible as capabilities, N machines, provable safety, and
authoring ergonomics competitive with the best integration ecosystems —
while keeping all six v6 promises and their executable enforcement intact
throughout. The six promises, the contract suite, the ratchets, the drills,
and gate.sh are load-bearing during every milestone; they are the reason
this plan can be executed by an agent at all.

## 2. Architecture end-state

```
wallee/
  kernel/            trusted computing base (target <5k LOC, absolute caps)
    models.py        PlanIR, LegalAction, ActionRun, contracts (from models.py)
    engine.py        gates: validate, approve, dispatch, verify, replan
    runtime_db.py    SQLite: plans, action_runs, exec_journal, approvals,
                     events, agent_memory (v7.x), schema_migrations
    capabilities.py  CapabilityRegistry (loads capabilities/*.yaml)
    capabilities_gen.py  GENERATED types (scripts/gen_capabilities.py)
    factkeys.py      FactKey registry (generated section + accessors)
    safety_client.py heartbeat/latch/request client (protocol §5.2)
    predicates.py, locks.py, coerce.py, ids.py, runtime_control.py
  cortex/            planner harness (iterates fast; kernel gates everything)
    world.py         WorldCompiler + WorldPacket assembly
    world_view.py    prompt-view compilation (decision signals; from models.py)
    planner.py, llm_transport.py, openrouter_observability.py
    planner_eval.py, memory.py (v7.x)
  ops/               composition + operator surfaces
    cli.py, composition.py, loop.py, artifacts.py, archive.py
    operator_cli.py, gateway/ (M3: telegram.py, webhook.py)
  packs/             sim_printer, sim_arm, prusa_core_one_plus,
                     moonraker (M3), + SDK templates under sdk/
  sdk/               pack scaffold templates + certify implementation
capabilities/        versioned capability schemas (YAML, frozen once published)
sentinel/            Rust cargo workspace (M2)
schemas/             generated JSON schemas (now incl. safety + capabilities)
deploy/systemd/      wallee-main@.service, wallee-safety@.service (templated)
image/               rpi-image-gen configuration (M3)
docs/SAFETY_PROTOCOL.md   versioned protocol spec (M2)
```

Import boundaries (machine-enforced, §9): `kernel` imports only stdlib +
pydantic (+ jsonschema/yaml for registry loading); `cortex` may import
`kernel`; `ops` may import both; `packs` import `kernel` only. `main.py`
remains the stable entry shim.

## 3. Integration decisions (single source of truth)

| # | Decision |
|---|---|
| V7-I1 | **Plan-reference migration**: `scripts/check_contract_manifest.py`'s `XFAIL_REASON` regex extends to accept `docs/REFACTOR_EXECUTION_PLAN.md` **or** `docs/V7_EXECUTION_PLAN.md`; `tests/contract/conftest.py` `PLAN` switches to this document in the first v7 commit. Old xfail reasons are not rewritten. |
| V7-I2 | **Capability schemas are data, frozen by version.** `capabilities/<name>@<major>.yaml`. Published versions never change (a content hash per schema is pinned in `capabilities/LOCK.json`; the sync check fails on drift). Changes = new `@N+1` file. Packs declare `capabilities: [process_control@1, …]` in manifest.yaml. |
| V7-I3 | **Safety floors live in capability schemas.** Each verb in a schema carries `hazard_floor` and `approval_required_floor`. The kernel enforces max(schema floor, pack-declared value) — a pack may tighten, never loosen. The repo-contract CANCEL regex check is retired only after the schema-floor contract test is green (M1 exit). |
| V7-I4 | **FactKey migration is a ratchet, not a big bang.** Canonical key form becomes `<device_id>.<capability>.<field>` (builtin/runtime facts use the reserved `runtime` capability namespace). The grammar additionally reserves `site` as a non-device first segment for future factory-level facts (inventory, cross-machine state); nothing emits it before the fleet lane — it is reserved now so the M1 registry and checker never need re-cutting (see ROADMAP "microfactory horizon"). A generated `LEGACY_KEY_ALIASES` map covers the old `printer_1.*` keys; `.legacy-fact-keys-allowlist.json` (shrink-only, checker `scripts/check_fact_keys.py`) drives alias usage to zero. The prompt view emits canonical keys from M1 — that is the scheduled M1 re-record + re-bless. |
| V7-I5 | **Kernel/cortex split is pure movement.** File moves + import updates + shims only; behavior-identical; goldens byte-identical; `wallee.models`, `wallee.engine`, etc. remain importable as deprecation shims re-exporting from their new homes for one milestone, then are removed in M3 (tests migrate at M2). |
| V7-I6 | **The safety plane is a versioned file protocol** (`docs/SAFETY_PROTOCOL.md`, v1). The Python watchdog and the Rust sentinel are two implementations of the same spec, proven by ONE shared conformance suite (`tests/safety_protocol/`) parametrized over implementations. Deployment default becomes the sentinel; the Python watchdog remains supported for sim/tests and non-Rust hosts. Never two enforcers on one data-dir. |
| V7-I7 | **Sentinel dependency ceiling**: ≤5 crates (serde, serde_json, + minimal HTTP client; sd_notify hand-rolled over `NOTIFY_SOCKET`), no async runtime, `cargo-deny` clean, vendored lockfile. Static binaries for aarch64 + x86_64 are release artifacts. |
| V7-I8 | **Secondary sentinel is strictly optional** (user decision): `WALLEE_SECONDARY_SENTINEL=auto|off|required`, default `auto`. Its absence NEVER blocks startup, never trips, never degrades dispatch — it only sets a recorded posture fact (`runtime.safety.channels = 1|2`) surfaced in runtime_state and the outbox on startup. `required` is opt-in for owners who want hard enforcement. The secondary's authority is the MACHINE stop only — never host power, never host shutdown. Protocol v1 RESERVES the serial framing (§5.2) so Stage 1 needs no rework; hardware implementation itself is v7.x. |
| V7-I9 | **Second pack family is Moonraker (Klipper)**, sim-first, before any Bambu work: open documented HTTP+websocket API, no cloud coupling, largest self-hosted install base. It implements `process_control@1`, `thermal_setpoint@1`, `scalar_telemetry@1`. Raw G-code is never planner-visible; adapters may use it internally to realize bounded verbs. |
| V7-I10 | **Gateway is approval-only pipes.** Inbound vocabulary is exactly `APPROVE <run_id> <args_hash>`, `REJECT <run_id>`, `ESTOP`, `STATUS` (strict regex, no NL parsing). Startup refuses an empty/group allowlist. No goal or plan text ever enters from the gateway. Adapters are stateless; state lives in the existing outbox/approval/request files. |
| V7-I11 | **Transport hardening lands in Phase 0 on v6.6** (pre-M1): session_id, require_parameters, models[] (≤3), the deterministic `response.model` allowlist gate (violation = planner failure feeding the existing retry/degrade path), and journaling of `cached_tokens`/`cache_write_tokens`/`reasoning_tokens`/`cost`. One `[re-record]` for the payload fingerprint change. |
| V7-I12 | **Memory ships dark.** The `agent_memory` table and deterministic writers land in v7.x behind `WALLEE_MEMORY=off` (default). It flips on only after the planner-eval + replay-corpus A/B shows a measured improvement; the A/B harness is part of the deliverable. Retrieval is deterministic (scope filter + recency, BM25 at most); content passes `_sanitize_prompt_text` and renders in a fenced block. |
| V7-I13 | **Versioning**: `7.0.0a1` at M1 exit, `7.0.0a2` at M2 exit, `7.0.0b1` at M3 exit, `7.0.0` after a hardware session on the capability-retrofitted Prusa pack. Each milestone leaves `main`-mergeable state. |
| V7-I14 | **Repair-within-frontier is table-driven and conservative**: only big→small substitution within the same family and direction, never for approval-gated verbs, journaled as `repaired_from`. Anything else stays a refusal. |

## 4. Phase 0 — hardening on v6.6 (no v7 structure; start immediately after merge)

Deliverables (each its own commit; order as listed):

1. **Transport upgrade** (V7-I11) in `wallee/llm_transport.py` + `planner.py`
   + `config.py` (`WALLEE_PLANNER_FALLBACK_MODELS`,
   `WALLEE_ALLOWED_PLANNER_MODELS` defaulting to primary+fallbacks) +
   engine-side model gate; `[re-record]` cassettes; unit tests for the gate
   (allowlisted model passes; fallback outside allowlist → planner failure
   path, event recorded, NO dispatch).
2. **Red-team lane**: `tests/redteam/` — generators producing (a) hostile
   PlanIR mutations (unicode confusables in ids, zero-width chars, 10k-char
   `why`, nested JSON in string fields, id lookalikes), (b) poisoned
   observation fields (vision summary/notes carrying instructions),
   (c) malformed tuning choices. Property for every case: zero
   `exec_journal` rows, zero DISPATCHED runs. Emits
   `.runtime/redteam_report.json` (corpus size, block rate); CI
   `adversarial-gates` job runs it and **fails under 100% block rate**.
   Add the block-rate table to `docs/VALIDATION.md`.
3. **Pack SDK v1**: `wallee pack new <name>` (templates under
   `wallee/sdk/templates/`: manifest, four-method pack with TODOs, sim
   driver, conformance test stub, README/CAPABILITIES stubs) and
   `wallee pack certify <path>` (manifest schema, repo-contract rules,
   generic frontier contract checks — every candidate action bounded,
   typed, hazard-classed, verify/expected_delta present — plus the
   adversarial corpus against the pack's frontier). Certify runs for all
   in-tree packs in the `repo-contract` CI job. `docs/PACK_AUTHORING.md`
   walks the sim-first flow.

Exit: gate.sh green; red-team report shows 100%; certify green for the
three in-tree packs; cassette re-record committed separately.

## 5. Milestone M1 — the capability system

### 5.1 Capability schema format

`capabilities/process_control@1.yaml` (canonical example; the generator
enforces this shape):

```yaml
capability: process_control
version: 1
description: Start/pause/resume/cancel a long-running physical process.
facts:
  state:        {type: enum, values: [IDLE, RUNNING, PAUSED, FINISHED, ERROR, UNKNOWN]}
  progress_pct: {type: number, min: 0, max: 100, nullable: true}
  active:       {type: boolean}
verbs:
  pause:  {hazard_floor: LOW,    approval_floor: false, args: {}}
  resume: {hazard_floor: LOW,    approval_floor: false, args: {}}
  cancel: {hazard_floor: MEDIUM, approval_floor: true,  args: {}}
prompt:
  summary_fields: [state, progress_pct]
```

Initial vocabulary (M1 ships exactly these five; more only with a device
that needs them): `process_control@1`, `thermal_setpoint@1` (bounded-trim
grammar: families, directions, magnitudes, per-family bounds — this absorbs
the TuningDescriptor redesign), `motion_enable@1`, `camera_observe@1`,
`scalar_telemetry@1`.

`scripts/gen_capabilities.py` (pattern-copy of gen_schemas.py): generates
`wallee/kernel/capabilities_gen.py` (typed verb/fact classes),
`schemas/capability_*.schema.json`, and verifies `capabilities/LOCK.json`
hashes (V7-I2). `--check` in CI (fold into the `schema-sync` job); `--write`
locally. Hand-editing generated files is a seeded-drill violation (§9).

### 5.2 Work items, in order

1. Generator + the five schemas + LOCK + sync check + drill seed.
2. `CapabilityRegistry` in kernel: loads schemas, validates each pack
   manifest's `capabilities:` list, exposes typed verb/fact lookups.
   Manifest schema gains the `capabilities` field (regenerate).
3. **FactKey registry** (`wallee/factkeys.py` for now; moves in M2):
   `FactKey(device, capability, field)` with `.s` string form; generated
   canonical keys from schemas; `LEGACY_KEY_ALIASES`;
   `scripts/check_fact_keys.py` ratchet + `.legacy-fact-keys-allowlist.json`
   seeded with current counts.
4. Retrofit the three packs to declare capabilities and emit canonical
   FactKeys from `normalize()` (Prusa: `process_control@1` +
   `thermal_setpoint@1` + `camera_observe@1` + `scalar_telemetry@1`;
   sim packs minimally `process_control@1` + `scalar_telemetry@1`).
5. Frontier compilation + engine floors: `candidate_actions` output is
   validated against the declared capability verb schemas
   (unknown verb for the pack's declared capabilities = load error);
   hazard/approval floors enforced per V7-I3.
6. Prompt view emits canonical keys; `tests/replay/keymap.py` maps to
   canonical keys; **the scheduled `[re-record]` + `[re-bless]`** land
   here as two separate commits.
7. Prusa decision-signal compilation moves behind a
   `planner_signals(world)` pack hook typed by capability (this empties
   most of the core-purity allowlist; ratchet the counts down in the same
   commits that move code).

### 5.3 M1 validation

New contract invariants (add to MANIFEST; all green at exit — no xfails):
`test_capability_schemas_are_frozen` (LOCK hash),
`test_pack_manifests_declare_valid_capabilities`,
`test_frontier_verbs_conform_to_capability_schemas`,
`test_hazard_and_approval_floors_come_from_schemas` (a pack attempting to
loosen a floor is refused at load),
`test_prompt_view_contains_only_canonical_fact_keys`,
`test_legacy_fact_key_ratchet_is_enforced`.
Plus: full suite, goldens re-blessed once, cassettes re-recorded once,
replay corpus green through the updated keymap, core-purity total
materially down (target: `models.py`/`planner.py` entries at 0; only
`planner_eval.py` remainder allowed, with its number recorded here at
exit).

Exit criteria: all of §5.3; `wallee pack certify` green for all packs
against capability conformance; `7.0.0a1` tagged on the branch.

## 6. Milestone M2 — kernel/cortex split + safety protocol + Rust sentinel

### 6.1 Split mechanics

Pure `git mv` + import rewrite per the §2 layout, executed exactly like the
v6 promotion (ordered specific renames, then generic; hand-fix only
path-derivation logic), with deprecation shims per V7-I5. Goldens must be
byte-identical — no re-bless is scheduled for M2; if one appears necessary,
stop and re-examine the change, it means behavior leaked.
`scripts/check_import_boundaries.py` (AST-walk of import statements against
the §2 matrix) lands in the same PR, wired into `repo-contract` and the
drill (§9). File-size allowlist entries re-key to new paths with equal or
lower numbers.

### 6.2 Safety protocol spec v1 (`docs/SAFETY_PROTOCOL.md`)

Documents, with JSON schemas generated into `schemas/safety_*.schema.json`:

- **Files** under `<data_dir>/safety/`: `heartbeat.json` (liveness = file
  mtime; body `{wall_ts, cycle_index, job_active}`), `profile.json` (array
  of stop profiles: `{transport: http|file, primary_request, host_env,
  key_env}`), `estop.latch.json` (present ⇔ engaged; `{reason, wall_ts,
  source}`), `estop.request.json` / `estop.clear.json` (consume-once).
- **Semantics table**: trip sequence (write latch atomically → execute
  every profile stop → re-issue while latched and job active), staleness
  (`now - mtime > timeout` AND `job_active` → stop+latch; idle → escalate
  only), clear (explicit deletion only; no TTL anywhere), boot grace,
  resend interval.
- **Conformance levels**: MUST items testable by the shared suite.
- **Reserved (V7-I8)**: secondary-channel serial framing — newline-delimited
  `WLE1 BEAT <cycle> <job_active> <profile_sha256>` at each cycle;
  handshake `WLE1 HELLO <protocol_version>`; the spec reserves it, v7.x
  implements it.

`tests/safety_protocol/` — the ONE suite, parametrized over drivers:
`PythonWatchdogDriver` (in-process, fake clock) and `SentinelBinaryDriver`
(spawns the built binary against a tmp data-dir, manipulates real files,
asserts on real effects incl. the file-transport stop log). Cases: stale
heartbeat during active job → stop issued + latch written; idle staleness →
escalate only; latch present at boot → honored; corrupted JSON latch →
still treated as engaged (fail-safe); request files consumed exactly once;
resend-while-latched; boot grace respected; SIGKILL-the-runtime e2e
(sentinel variant of the existing contract test).

### 6.3 `sentinel/` (Rust) — Stage 1, as approved

Cargo workspace, single binary crate `wallee-sentinel`:
`src/main.rs` (arg/env parsing — same `WALLEE_DATA_DIR` +
`WALLEE_SAFETY_*` variables as the Python watchdog, so `safety.env` is
reused unchanged), `heartbeat.rs`, `latch.rs` (atomic write via temp+rename),
`profile.rs`, `stop.rs` (http via the chosen minimal client + file
transport for sim parity), `sdnotify.rs` (hand-rolled `READY=1`/
`WATCHDOG=1` datagrams). Loop on `std::time::Instant`; no threads beyond
main unless the HTTP timeout requires one. Logging to stderr (journald
captures it). Behavior defined by §6.2 — where the Python watchdog and the
spec disagree, fix the spec or the Python first, then implement.

CI job `sentinel`: `cargo fmt --check`, `clippy -D warnings`, `cargo test`,
`cargo-deny check`, build x86_64 release, run `tests/safety_protocol/` with
`SentinelBinaryDriver` against it, cross-compile aarch64
(`aarch64-unknown-linux-gnu`, static via musl if practical) and upload the
artifact. Local runs skip the binary driver when the binary is absent
(pytest marker), CI never skips it.

Deployment: `wallee-safety@.service` `ExecStart` switches to the sentinel
binary with `WatchdogSec=10` + `Restart=always`; the Python watchdog stays
as `python -m wallee.safety_watchdog` for sim hosts. DEPLOYMENT.md gains
the sentinel install step (binary from release artifacts into
`/opt/wallee/bin/`).

### 6.4 Secondary-sentinel posture (software side only, per V7-I8)

Config + posture fact + startup outbox note land in M2 (they are ~50
lines); the loop beats the serial channel only when configured and present;
absence is silent apart from the posture fact. Contract invariant:
`test_secondary_sentinel_absence_never_blocks` — with `auto` and no
device, startup succeeds, dispatch proceeds, no trip, posture fact = 1.
The MCU firmware itself is v7.x (§8).

### 6.5 M2 validation & exit

Invariants added: `test_kernel_imports_nothing_above_it` (structural),
`test_safety_protocol_conformance[python]` + `[sentinel]` (the suite),
`test_sentinel_sigkill_e2e`, `test_secondary_sentinel_absence_never_blocks`.
Exit: goldens byte-identical through the split; both protocol drivers
green in CI; sentinel artifacts building for both targets; templated units
(`wallee-main@.service`, `wallee-safety@.service`) replacing the fixed ones
with `%i` data dirs (the fleet enabler, landed here because the unit files
are already being touched); `7.0.0a2`.

## 7. Milestone M3 — generality proof + gateway + image

1. **Moonraker pack** (V7-I9), sim-first: `packs/moonraker/` with recorded
   API fixtures driving `sim_moonraker`; certification green; goldens for
   its sim trajectories; hardware smoke doc modeled on the Prusa pack's.
   Exit proof of Pillar 1: the diff touches `packs/` and `capabilities/`
   docs only — zero core changes (enforced by review + the import/purity
   gates).
2. **Operator gateway** (V7-I10): `ops/gateway/` + `wallee-gateway`
   console script + `wallee-gateway@.service` (own user, no data-dir write
   access beyond outbox/request files). Telegram adapter first; webhook
   second. Contract invariants:
   `test_gateway_inbound_grammar_is_closed` (hostile corpus — injection
   strings, goal text, unicode tricks — produces zero effects beyond the
   four verbs), `test_gateway_refuses_group_or_empty_allowlist`,
   `test_gateway_approvals_are_args_hash_bound`.
3. **Appliance image** (`image/`): rpi-image-gen config per the accepted
   trade-offs — RO rootfs, tmpfs overlay for `/etc` `/var`, persistent
   `/var/lib/wallee` partition (SQLite WAL + safety dir), journald
   volatile, dev-mode toggle, RTC note in docs. CI `image` workflow on
   `workflow_dispatch` + release tags only (it is slow): build, loop-mount,
   assert fstab RO root + units enabled + wheel installed, upload
   `wallee-<ver>.img.xz` + SBOM. Boot-on-hardware remains a manual
   checklist item appended to DEPLOYMENT.md.
4. **Release pipeline** (`.github/workflows/release.yml`, on tag): sdist +
   wheel, sentinel binaries (both targets), image (Pi tags), SBOMs,
   SHA256SUMS; draft GitHub Release with the CHANGELOG section.

Exit: certify green for four packs; gateway invariants green; image builds
and passes the loop-mount assertions; `7.0.0b1`. `7.0.0` follows the
hardware session on the retrofitted Prusa pack (DEPLOYMENT §7 checklist,
now including one sentinel-tripped stop).

## 8. v7.x lanes (designed now, gated on triggers)

- **Fleet**: already enabled by templated units (M2); per-machine
  `/etc/wallee/<machine>/` env + `/var/lib/wallee/<machine>/` data;
  `wallee-operator --machine`. The advisory coordinator (FDM-Monster-shaped,
  SQLite, read-only pollers + request-file forwarding) only at N≥3.
- **Memory ledger** (V7-I12): migration adds `agent_memory`; writers:
  end-of-job summarizer, escalation-resolution recorder,
  `wallee-operator note`; retrieval in `cortex/memory.py`; A/B harness in
  planner_eval; ships dark.
- **Secondary sentinel hardware**: Pi Zero 2/RP2350-class firmware speaking
  the reserved serial framing; stop authority = machine only (V7-I8).
- **Repair-within-frontier** (V7-I14) + its contract tests.
- **Proposer lane** (self-evolution, per the standing rejection of anything
  autonomous): agent-drafted PRs only, gated by frozen contracts + drills,
  a statistical acceptance criterion on offline evals (PACE-style, never
  raw score-diff), and human merge. No runtime path may load learned code.

## 9. Guardrails: extensions to the existing gate set

- New checkers: `check_fact_keys.py` (M1 ratchet),
  `check_import_boundaries.py` (M2), capability LOCK verification inside
  `gen_capabilities.py --check` (M1). All three: CI (`repo-contract` /
  `schema-sync`) + `gate.sh` + pre-commit.
- **Seeded drill grows four violations** (drill must stay 7/7 → 11/11):
  hand-edited `capabilities_gen.py` → capability sync check fires;
  mutated published capability YAML → LOCK check fires;
  `from wallee.cortex…` import inside `kernel/` → boundary check fires;
  renamed `estop.latch.json` in the sentinel conformance fixture →
  protocol suite fires.
- Coverage floor ratchets up only; file-size allowlist keys migrate with
  the M2 moves at equal-or-lower values; core-purity allowlist shrinks per
  §5.2(7) with M1.
- CI end-state = current 11 checks + `sentinel` + `image`
  (dispatch/release) + `release`; the AGENTS.md gate-block is regenerated
  whenever a job is added (the sync check enforces this).

## 10. Risk register

| Risk | Mitigation |
|---|---|
| M1 re-record hides a prompt regression | Re-record is one isolated commit; replay corpus + planner-eval comparison run before/after and their deltas pasted into the PR/commit body |
| Split leaks behavior | No re-bless budget in M2 — a golden diff halts the milestone by definition |
| Rust toolchain friction for future maintenance | V7-I7 ceiling; the Python watchdog remains a first-class fallback; the conformance suite is the safety net either way |
| Capability vocabulary grows speculative entries | Rule: a schema lands only with a pack that implements it (five at M1, no more) |
| Solo-maintainer stall | Every milestone exits `main`-mergeable and independently tagged; Phase 0 items are useful even if v7 pauses forever |
| Gateway becomes an injection path | V7-I10 closed grammar + hostile-corpus invariant + own service user; any future verb addition requires a new contract test first |

## 11. Milestone exit summary

| Milestone | Ships | Proof |
|---|---|---|
| Phase 0 | transport hardening, red-team lane, pack SDK v1 | 100% block rate; certify green ×3; one re-record |
| M1 | capability system, FactKey ratchet, floors-from-schemas, signal hook | §5.3 invariants green, no new xfails; purity allowlist collapsed; `7.0.0a1` |
| M2 | kernel/cortex split, safety protocol v1, Rust sentinel, templated units, secondary-sentinel posture | goldens byte-identical; conformance suite green ×2 implementations; `7.0.0a2` |
| M3 | Moonraker pack, gateway, image, release pipeline | zero-core-change second pack; gateway corpus green; image assertions green; `7.0.0b1` |
| 7.0.0 | — | hardware session incl. sentinel-tripped stop |
