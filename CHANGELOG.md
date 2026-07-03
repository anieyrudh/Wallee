# Changelog

All notable changes to this repository are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions track the maintained `wallee_v6/` line (SemVer, 6.x).

This file starts at the Phase-0 stabilization of the refactor described in
[`docs/REFACTOR_EXECUTION_PLAN.md`](docs/REFACTOR_EXECUTION_PLAN.md); earlier
history lives in the per-version changelogs under `docs/internal/`.

## [Unreleased]

### Phase 5 — Police (the permanent end-state)

- **Canonical CI**: `tests` (matrix, 72% coverage floor), `gates`
  (pre-commit set), `mypy` (strict scope on the safety core),
  `safety-invariants`, `adversarial-gates`, `sim-evals`, `cassette-replay`,
  `schema-sync`, `docs-contract`, `repo-contract`, `pip-audit` — plus
  CodeQL, grouped dependabot, and the weekly `live-eval` trend lane
  (`scripts/run_live_eval.py` over the translated corpus; never a merge
  gate).
- **Fault-injection lane** (`tests/sim_evals/`): mid-trajectory faults
  degrade safely. The lane immediately caught (and this release fixes) the
  engine reporting a FAILED action in `executed_action_ids`.
- **Seeded-violation drill**: 7/7 named guards fired
  (`docs/internal/DRILL_2026-07.md`), including one honest catch of the
  drill's own bad seeding.
- **One command surface**: `./scripts/gate.sh` mirrors CI; the PR template
  checkboxes mirror the required checks; `AGENTS.md` is the single
  authoritative agent guide (gate table kept in sync with ci.yml by
  `scripts/gen_agents_gate_block.py`); CLAUDE/CODEX are pointers; stale
  agent guides renamed into `docs/history/` with a placement rule in
  `check_docs.py`.
- **Docs consolidation**: root README and ARCHITECTURE rewritten for the
  single tree (v5 architecture archived to history); new
  `docs/VALIDATION.md` (the honest ledger, incl. the 47/60 live baseline),
  `docs/GLOSSARY.md`, `docs/SCHEMAS.md`.

### Phase 4 — Prune (one tree, lean-down behind stable contracts)

- **Legacy v5 tree retired** (`wallee/` v5, root `tests/`,
  requirements files): archived in history with a full disposition register
  (`docs/internal/RETIRED_FINDINGS.md`); the `legacy/v5` branch +
  `legacy/v5-final` tag are cut from the last pre-deletion `main` commit at
  merge time. The 60-scenario replay corpus was translated to v6
  world-packet key-space FIRST (`tests/replay/keymap.py`) and now runs as a
  model-independent gate-assertion suite plus live planner-eval cases; its
  honest 47/60 baseline is preserved.
- **Single package**: `wallee_v6/` promoted to the root `wallee` package
  (pure `git mv` + mechanical rename; goldens/cassettes byte-identical;
  `rg wallee_v6` = 0 outside history docs). `pyproject` is the one spec:
  name `wallee`, version 6.6.0, entry points `wallee` / `wallee-operator` /
  `wallee-watchdog`.
- **Dead code deleted**: the unauthenticated dashboard (with
  fastapi/uvicorn), the RedisWhiteboard placeholder, the legacy Pi launch
  script. `ControlLease` kept and unit-tested (it is load-bearing).
- **Dedupe behind goldens**: one `wallee/coerce.py` for the scalar/fact
  helpers (five-plus clones removed), one run-scope builder, the
  `pack._realize` if-chain replaced by a spec table. The core-purity
  ratchet enforced the boundary during its own refactor (342 → 337).
- **main.py split** (1,461 → 51-line shim over cli / composition / loop /
  artifacts / archive), proven by the entrypoint e2e suite; the 800-line
  file cap lands as a shrink-only ratchet (`scripts/check_file_sizes.py`)
  with the remaining seven oversized files recorded per-file in ROADMAP.
- **Persistent-failure trip**: N consecutive crashed cycles now stop the
  machine and escalate (once per incident, re-armed on operator clear) —
  closing the analysis finding that the counter was computed but unused.
- **Bounded whiteboard change ring** (was an unbounded slow leak).

### Phase 3 — Promote (device-boundary, injection hardening, remote stop, docs)

- **Duplicate device-id claims are rejected.** The registry refuses to load two
  packs that both claim the same device id instead of silently merging them.
- **Prompt frontier == executable frontier.** The engine refuses any planned
  action that was never offered to the planner (truncated frontier included),
  so the prompt view and the executable set cannot diverge.
- **Core/pack device-knowledge boundary is machine-enforced.**
  `scripts/check_core_purity.py` is a shrink-only ratchet (backed by
  `wallee_v6/.core-purity-allowlist.json`) that blocks new device references
  from leaking into the generic core and can only tighten. Wired into CI and
  pre-commit. The full Prusa eviction is tracked in
  [`docs/ROADMAP.md`](docs/ROADMAP.md).
- **Injection hardening (I-14).** The deterministic active-print gates consume
  the closed vision `finding_type` enum only; all substring-matching on the
  model's free-text summary is deleted, so a poisoned or hallucinated sentence
  cannot steer a gate. Proven by activated adversarial regressions in
  `test_gate_adversarial.py`.
- **Input sanitization at ingestion.** Print filenames, notebook notes, and
  vision summaries are charset/length-clamped in the pack `normalize()` step
  (and vision free text is stripped of control characters at generation) before
  they can reach the planner prompt.
- **Remote ESTOP.** `wallee-operator estop` requests a soft stop the control
  loop honors at the next cycle boundary (fires the physical stop transport and
  latches durably); `wallee-operator clear-estop` is the attended-only release.
  Covered by a P6 contract invariant.
- **Docs.** [`SECURITY.md`](SECURITY.md) gains a threat model (prompt injection,
  LAN exposure, notification hardening, ESTOP rules); new
  [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) Pi cutover runbook and
  [`docs/ROADMAP.md`](docs/ROADMAP.md) recording deferred work.

### Phase 2 — Prove (the v6 safety promises made true)

The decorative safety kernel is now a working, independent safety layer. All
Phase-2 contract invariants that were strict-xfails are green; the
safety-invariants suite has zero xfails (only three Phase-3 core-genericity
invariants remain xfailed).

- **Interlock is a real gate.** `SafetyKernel` owns a durable ESTOP latch;
  the engine checks it (and beats the heartbeat) at the top of every action
  iteration, so a stop that lands mid-sequence halts the rest. A trip
  survives restart and clears only by explicit operator action — never by
  elapsed time.
- **Physical stop transport.** A generic, stdlib-only stop transport is
  driven by declarative pack `safety_profile` data (hosts/keys by env var,
  no secrets inline); `build_runtime` registers it as the interlock trip
  callback and persists the profiles for the watchdog. sim = file transport
  (tests), Prusa = PrusaLink M25/DELETE http.
- **Independent out-of-process watchdog** (`wallee_v6.safety_watchdog`)
  replaces the `sleep(10**9)` placeholder: it consumes a file heartbeat
  beacon (mtime = truth), stops + latches on a stale heartbeat during an
  active job (escalate-only when idle), re-issues while latched, and honors
  human ESTOP requests — proven by a SIGKILL-the-runtime end-to-end test.
  Real, hardened systemd units (`Requires=` the watchdog, bootable
  ExecStart, EnvironmentFile) replace the placeholder.
- **Approvals resume.** A recorded approval is now consumed: parked
  WAITING_APPROVAL runs resume through the shared gated dispatch path
  (exactly once), unanswered approvals past the wait window abort with a
  re-approval escalation, and a tripped ESTOP disposes of pending approvals.
- **Crash recovery hardened.** exec_journal gains wall-clock timestamps
  (monotonic is meaningless across restarts); boot reconcile finalizes
  IN_FLIGHT journal rows to unknown-outcome; plans are INSERT-only
  (IntegrityError on duplicate, never a silent replace). A tracked,
  transactional `schema_migrations` framework replaces ad-hoc ALTERs and
  adds status/scope/approval/journal/event indexes.

### Phase 1 — Pin (executable contracts on current behavior)

- **Six-promise safety contract suite** (`wallee_v6/tests/contract/`, CI job
  `safety-invariants`): the README's safety claims are now executable tests.
  23 invariants hold; 14 are strict xfails, each naming the execution-plan
  item that closes it. A MANIFEST plus `scripts/check_contract_manifest.py`
  makes deleting, renaming, skipping, or weakening an invariant a red build,
  and zero-collection cannot green the job.
- **Adversarial gate corpus**: hostile PlanIRs (G-code injection in action
  ids, path traversal, hallucinated/duplicate ids, oversized sequences)
  all die at the deterministic gates with zero journal rows.
- **Injectable clock** on `SafetyKernel` (`mono_fn`) so TTL/latch properties
  are provable with a fake clock (the legacy ESTOP auto-un-latch class).
- **Planner transport seam + cassette replay** (CI job `cassette-replay`):
  the HTTP exchange is injectable; recorded traffic replays offline through
  the production planner including schema validation and suppression
  policies. Request fingerprints turn any prompt/payload drift into a red
  check until the corpus is deliberately re-recorded.
- **Header allowlist** in `normalize_openrouter_metadata`: no persistence
  path (artifacts, replay bundles, cassettes) can store provider auth
  echoes or cookies.
- **Schema-sync gate** (`scripts/gen_schemas.py`, CI job `schema-sync`):
  all three JSON schemas are pinned to their code sources; every pack
  manifest validates against the manifest schema; the pydantic-side plan
  bounds (3-action horizon, duplicate rejection) are asserted to exist.
- **Characterization goldens**: exact plan→gate→dispatch→journal traces
  over the sim packs, normalized deterministically; re-blessing is a
  deliberate separate `[re-bless]` commit.
- **Entrypoint e2e tests**: `python -m wallee_v6.main` runs as a subprocess
  and must demonstrate boot reconcile, durable-state survival, and cycle
  wiring — the structural defense against "component exists but main never
  calls it", the original v6 failure mode.

### Phase 0 — Stop the bleeding (safety hotfixes, truth-green CI, redactions)

Safety correctness (legacy `wallee/`):
- ESTOP now **latches**: `/estop` (Telegram) and `estop` (CLI) publish
  `safety.estop` with no TTL. Previously the emergency stop silently expired
  after 600 s, after which the machine could be commanded again with no human
  ever clearing it. New `/estop_clear` and `estop_clear` commands are the only
  way to release the latch.
- Operator approvals arriving after `max_proposal_age_ms` (~30 s for
  `cancel_print`) now execute instead of being rejected as `expired`: the
  deadline gate no longer re-ages a `WAITING_APPROVAL` proposal on approved
  re-processing. The approval-timeout loop still bounds the wait and the TOCTOU
  precheck still re-validates before dispatch.
- The Telegram bot refuses to start with an empty allowlist on a group/empty
  chat id (previously every member of a group chat could approve actions,
  inject intents, and trigger ESTOP).
- The safety kernel JSON-decodes fault-monitor values before judging them,
  fixing a false-positive ESTOP when a monitor reported `0.0`.
- The read-only dashboard binds `127.0.0.1` by default (configurable via
  `DASHBOARD_HOST`); non-loopback binds require `DASHBOARD_TOKEN`, which gates
  both the HTTP page and the websocket that streams camera frames and full
  runtime state.
- The safety-kernel subprocess receives its API key via the environment, not
  argv (argv is world-readable through `/proc/<pid>/cmdline`).

Safety correctness (current `wallee_v6/`):
- `WALLEE_FLUSH_STATE_ON_START` now defaults to **0**. The exec journal exists
  to answer "did a side effect start before the crash?"; wiping it on every
  boot erased exactly the evidence crash recovery needs.
- New startup reconcile sweeps crash residue: `DISPATCHED` rows from a dead run
  are marked `UNKNOWN` with a require-ack operator escalation, and stale
  `PROPOSED`/`AUTHORIZED` rows are aborted. Without this, `DISPATCHED` residue
  held its locks forever and silently filtered pause/cancel-class actions out
  of every future frontier.
- Fixed a production `NameError`: the planner called three tuning-grammar
  helpers it never imported, crashing any `EXECUTE` plan carrying a
  `tuning_choice`.
- The run-state machine now permits `PROPOSED/AUTHORIZED → REPLAN_REQUIRED`,
  which the engine already performed on its lock-conflict and TOCTOU paths;
  both previously raised `ValueError` at runtime.

Repository health:
- Fixed the ~36 documentation links that pointed at absolute paths on the
  author's laptop; `scripts/check_docs.py` passes again.
- Redacted leaked hardware/network identifiers (printer serial/UUID/MAC,
  Tailscale VPN IP, operator SSH host and home-LAN topology) from public docs;
  parameterized illustrative IPs to RFC 5737 TEST-NET placeholders.
- Fixed three tests and one script that hardcoded the author's laptop paths so
  the `wallee_v6` suite runs on any machine.
- Cleared all `ruff` findings in `wallee_v6` (unused imports, undefined names,
  write-only locals).
- Corrected stale docs claims (sensor count 19 → 20, per-tree test counts,
  Mermaid block count, quick-start clone directory, Redis prerequisite) and
  reframed the README around the two-tree reality.
- `wallee_v6` package version normalized to `6.5.0`; author metadata corrected.

Added regression tests for every safety fix above.
