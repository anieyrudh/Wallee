# Wallee — Comprehensive Codebase Analysis (2026-07)

*A multi-agent audit of every branch, framed as a Rumsfeld matrix (known knowns /
known unknowns / unknown knowns / unknown unknowns), with verified defects, a
security/privacy exposure list, and forward proposals.*

Method: ~19 parallel deep-reader/reviewer agents mapped the full history and all
branches; every high/critical finding was then re-checked by independent
adversarial verifiers and by direct reading of the cited source. Findings below
are labelled **[verified]** where a verifier or direct code read confirmed them.

---

## 0. The one thing to read first

**`main` is not the head of development, and it carries every serious defect this
audit found.** The real current state of the project lives on
**`v6.6/refactored`** (37 commits ahead of `main`, **0 behind** — `main` is a
strict ancestor, so it fast-forwards cleanly). That branch is a completed,
contract-first hardening refactor that **fixes essentially every P0 defect listed
in this report**, and it carries the forward plan (roadmap + v7 design). Its merge
is deliberately gated on **one thing that has not happened: an attended physical
hardware sign-off** on the Pi (`DEPLOYMENT.md §7`).

So the headline is not "Wallee is broken." It is:

> The dangerous version is the one that is public and tagged as the mainline; the
> safe version is finished but unmerged, waiting on a hardware session only you can
> run. The single highest-value action is to close that gate and merge.

Everything else in this document is either (a) evidence for that claim, (b)
exposure that is live on the public `main` **right now** and worth fixing
regardless of the merge, or (c) forward-looking proposals for v6.6 → v7.

---

## 1. Branch & version map (ground truth)

| Branch | Rel. to `main` | What it is | State |
|---|---|---|---|
| `main` | — | Pre-refactor **dual tree**: legacy `wallee/` (v5) + `wallee_v6/` (v6.5). HEAD `3eb2691`. | Public mainline. Carries all defects below. |
| `v6.6/refactored` | +37 / −0 | Completed single-tree `wallee/` refactor: independent watchdog, latched ESTOP + stop-transport, approval resume, crash reconcile, injection hardening, 11 CI checks, 7/7 seeded-violation drill, full docs, **and** the v7 plans. | ✅ Complete. Awaiting hardware sign-off, then FF-merge → tag `v6.6.0`. |
| `claude/refactor-phase-0` | +35 | Old name for `v6.6/refactored` (renamed). Slated for deletion. | Redundant. |
| `claude/project-analysis-improvements-4ay1eo` | +2 | The two seed commits (`PROJECT_ANALYSIS.md`, `REFACTOR_EXECUTION_PLAN.md`) that became the refactor plan; both now live on `v6.6/refactored`. | Redundant, delete. |
| `v5`, `codex/v6`, `codex/v6.5`, `codex/raeanne` | behind / historical | Superseded generation snapshots. | Archive/delete refs. |

The refactor and the v7 plans were authored by AI coding-agent sessions (Codex on
the v6/v6.5 lines; Claude on the July refactor line) with you squash-merging. This
is not stated in the public README but is visible throughout the history and in
`pyproject` metadata — worth knowing because it shapes the "two-agent workflow"
recommendations in §7.

---

## 2. Rumsfeld matrix

### 2a. Known knowns — what is solidly true and (mostly) proven

- **The thesis is implemented, not just claimed.** v6.5's core loop —
  world-packet compilation → closed **frontier of legal actions** → strict-JSON
  `PlanIR` from the planner → deterministic approve/execute/verify/replan over a
  single SQLite runtime DB with a **dual commit barrier** (`action_runs` logical
  state vs `exec_journal` physical-side-effect record; `DISPATCHED` before pack
  execution; `IN_FLIGHT` before any hardware side effect) — genuinely exists and
  is unit-tested. The `IN_FLIGHT`-before-side-effect barrier and `expected_delta`
  verification are byte-identical between v6.5 and the refactor. **[verified]**
- **The Prusa pack folds three vendor interfaces into one device abstraction:**
  HTTP as authoritative read/lifecycle control, bounded serial live-tuning writes
  (`M220`/`M221`/`M104`/`M140`, plus experimental `M204`/`M572`), a read-only
  G-code "job notebook" for context, UDP/FFF metrics kept out of the critical
  path. Four trim families (speed / flow / nozzle temp / bed temp) are the
  planner-enabled, hardware-proven surface.
- **The lineage is unusually well-documented and was acted on.** v3.1 fixed 18
  catalogued bugs; v4 came out of a same-day live-debug session; the 2026-03-21
  architecture audit found 5 P1 trust-boundary violations, most of which the v5
  line genuinely fixed (e.g. the safety kernel became a real subprocess).
- **The refactor works as a body of code.** In a fresh venv the `v6.6/refactored`
  suite passes (**422 passed + 1 deliberate xfail**), all gate scripts pass, the
  seeded-violation drill fires 7/7, and all 8 safety-critical invariants survive
  the split/dedupe — several strengthened. **[verified via independent run]**
- **The safety *model* is sound in the abstract.** "LLM proposes, deterministic
  code disposes"; safety lives outside the planner; the frontier is the planner
  boundary. Where it breaks (below) is in *enforcement wiring*, not in the design.

### 2b. Known unknowns — gaps the project already acknowledges

These are honestly recorded in `README`/`SECURITY.md`/`ROADMAP.md`/pack docs:

- **ESTOP is software-only** — a stop request over the same network control path,
  not a hardware relay or power interlock. Heaters stay energized on a stuck stop.
- **Safety depends on the host stack** (Redis reachability, network path to the
  machine) — "independent of the agent" but not of the broader system.
- **Hardware sign-off has not happened.** Every "stops the machine" claim on the
  refactor is *stops the simulated machine*. This gates the v6.6.0 merge/tag.
- **Planner "proofs" are session-harness proofs, not unconstrained-autonomy
  proofs** — the pack docs say so explicitly.
- **Deliberately excluded from the planner:** position control, arbitrary raw
  G-code, raw numeric targets, `M900`/`M201`/`M205`, FFF/UDP in the critical path.
- **Replay baseline is 47/60**, with a recorded failure taxonomy (telemetry-first
  reasoning, chain-completeness misses, under-reaction bias).
- **Roadmap deferrals (all recorded):** core-purity eviction not yet zero (337
  device refs still in core); 7 files over the 800-line cap behind a shrink-only
  ratchet; vision `FindingType` enum cannot distinctly express detachment /
  collision / jam (they ride on the model tagging `spaghetti`); sim-eval fault
  lane and live `pass^k` lane; Telegram notification port + event-driven wake;
  authenticated dashboard; hatchling/uv packaging migration.
- **v7 open risks the plan itself names:** M1 re-record hiding a prompt
  regression; kernel/cortex split leaking behavior; Rust-sentinel toolchain
  friction for a solo maintainer; capability-vocabulary scope creep; gateway
  becoming an injection path; solo-maintainer serialization stall.

### 2c. Unknown knowns — things the code embodies but nobody wrote down

*(These are the most valuable output of the audit: behaviors that are true in the
code, contradict or aren't in the docs, and would surprise even the author.)*

1. **The v6.5 "one safety process" is embodied nowhere.** On `main`, the safety
   kernel is an in-process object whose heartbeats come from the very engine
   thread it is supposed to watch, and the systemd "safety" unit is a
   `time.sleep(10**9)` placeholder constructing a *separate* `SafetyKernel` in
   another process with no shared state. The v5→v6 rewrite **silently regressed
   the one property the whole project is named for** — after v5 had just fixed it.
   **[verified]**
2. **The accel tuning family is hidden from the planner's *view* only, not from
   the engine.** HEAD commit `3eb2691` strips `A_PRUSA_TRIM_ACCEL_*` from the
   prompt and the audit artifact, but the pack still emits it into
   `world.frontier`, `engine.validate_plan` checks the **full** frontier, and
   `execute_operator_action` does **no** frontier check at all — so any non-prompt
   path (direct `PlanIR`, a `tuning_choice` with `family=accel`, the operator
   engine API, a heuristic planner, a replan, a test) dispatches a **real `M204`
   serial command**. The masking is presentation-only; the audit artifact now
   asserts *less* than the engine will actually execute. **[verified]** No doc on
   `main` explains this; `CAPABILITIES.md` still describes a 60-second
   accel-suppression behavior that the code made permanent-and-invisible.
3. **v6 auto-approves all LOW-hazard actions by default**
   (`WALLEE_AUTO_APPROVE_LOW=1`). All four trim families flow through with **no
   human in the loop**; the docs describe approval gating without mentioning the
   default-open LOW path.
4. **Deterministic Prusa policy lives inside the "generic" planner adapter** —
   `_should_suppress_active_print_pause` and residue/spaghetti keyword matching
   over the model's free text. The frontier is *not* actually the only policy
   boundary; device-specific logic leaks into the core (the roadmap's 337-ref
   core-purity debt is the same story).
5. **The replay 47/60 number measures a different model over a different
   transport than production.** The harness bypasses the production `LLMClient`
   (direct OpenRouter call) and was run with `claude-opus-4-6` while production
   defaulted elsewhere. Three incompatible model histories coexist across internal
   docs and were never reconciled.
6. **The agent's long-term memory is pre-seeded with fiction.**
   `BOOTSTRAP_NOTES.md` seeds `OBSERVATIONS.md` with synthetic first-person
   "experiences," flagged only by a one-line "review before deploying" banner. The
   memory file is always loaded into the system prompt.
7. **Running the test suite writes `OBSERVATIONS.md` into the source tree** — tests
   exercise the real `remember()` path against the repo directory (gitignored, so
   the pollution is invisible).
8. **The v6 tree has no logging framework** — output is `print()` + a stdout tee;
   `consecutive_cycle_failures` is counted and persisted but **never acted on**.
9. **A philosophical reversal is undocumented:** v5's `SOUL.md` gives the agent a
   Big-Five personality and "the printer is your body" framing; v6 deliberately
   deleted persona prompts. The reversal is only visible by diffing the files.
10. **The public repo lost its own dev-safety guardrails:** v3.0 shipped
    `.claude/hooks` (block-destructive, protect-env, run-pytest); the public-launch
    scrub removed them from tracking, so the repo no longer shows how the
    agent-development environment was itself sandboxed.

### 2d. Unknown unknowns — latent defects surfaced by adversarial review

Grouped by theme; corroboration count in brackets; **[verified]** = confirmed by
an independent skeptic and/or direct code read. **Almost all are `main`-only and
fixed on `v6.6/refactored`** (fix status in §3).

- **Safety interlock is decorative on v6.5** *(7 reviewers)* **[verified].**
  `SafetyKernel.trip()` sets `interlock.engaged`, but no dispatch path ever reads
  it, `register_interlock_callback` is never called, and `poll()`'s return is
  discarded. Tripping the interlock does nothing physical. Invariant 8 ("safety
  outside the planner") is technically true and practically hollow — there is no
  stop actuator wired at all.
- **Heartbeat watchdog cannot fire in vivo** *(6)* **[verified].** `beat()` and
  `poll()` are sequential statements on the single main-loop thread; a hang in
  world-compile / planner / execute (all blocking I/O) stalls the thread *before*
  it can poll. No independent thread or process watches the clock.
- **Approval flow is a guaranteed dead-end on v6.5** *(8)* **[verified].** Every
  cycle mints a fresh `action_run_id`; approvals bind to that per-cycle id; nothing
  reloads/resumes `WAITING_APPROVAL` runs. MEDIUM-hazard actions (CANCEL /
  STOP_PROCESS / START_PROCESS) can **never execute** through the planner path.
- **Planner `NameError` on malformed-but-schema-valid output** *(5)*
  **[verified].** `_tuning_choice_resolves_cleanly` (reached from the
  malformed-EXECUTE healing path) calls `_action_family_from_id` /
  `_action_direction_from_id` / `_action_magnitude_from_id`, which are **neither
  defined nor imported** in `planner.py`. An LLM emitting both a `sequence` and a
  `tuning_choice` crashes the planner. The heuristic backend never produces that
  shape, so tests give false confidence — and the `knowledge/EXAMPLES` teach the
  pre-`tuning_action_space` contract that steers models *into* the broken shape.
- **Default `WALLEE_FLUSH_STATE_ON_START=1` wipes the crash journal every boot**
  *(6)* **[verified].** It erases `exec_journal` / approvals / `action_runs` and
  deletes the control-lock file — destroying the exact forensic record the commit
  barriers exist to provide, and breaking the single-writer lease. There is **no
  boot-time reconcile** that scans for lingering `DISPATCHED`/`IN_FLIGHT` rows, so
  a crash mid-dispatch ends in amnesia (default) or a permanent orphan (flush off).
- **v5 ESTOP is weak** *(5)* **[verified].** Software pause over the failure-prone
  network path (heaters stay energized); fired one-shot with its HTTP result
  ignored; the whiteboard flag **auto-expires after 600 s** and resume needs no
  approval; it can be defeated mid-dispatch.
- **v5 deadline gate inverts after a Pi reboot** *(1)*. `time.monotonic()` resets
  on reboot, making a stale persisted proposal's age negative → it passes the
  freshness gate and **dispatches at boot**.
- **v5 `UNKNOWN` status is a roach motel** *(1)*: neither blocks the device group
  nor ever resolves, and permanently vetoes re-proposing the same tool+params.
- **No single-instance guard on v5** *(1)*: two engine processes double-dispatch
  the same hardware action.
- **Security exposure (v5, all LAN-reachable)** — see §4.

---

## 3. Verified defect register (with fix status)

| # | Sev | Defect (all on `main`) | Verified | Fixed on `v6.6/refactored`? |
|---|---|---|---|---|
| D1 | 🔴 crit | v6 safety interlock never enforced (no consumer, no actuator) | ✅ | ✅ per-action interlock check + durable latch + generic `stop_transport.py` |
| D2 | 🔴 crit | v6 approval dead-end (WAITING_APPROVAL never resumed) | ✅ | ✅ approval resume scans WAITING_APPROVAL, executes exactly once, ESTOP-dominant |
| D3 | 🔴 crit | v6 flush-on-start wipes journal, no crash reconcile | ✅ | ✅ default→0; `reconcile_runtime_start_state` boot-sweep |
| D4 | 🟠 high | v6 planner `NameError` on malformed EXECUTE+tuning | ✅ | ✅ helpers now imported |
| D5 | 🟠 high | v6 watchdog can't fire (same-thread beat/poll; systemd stub) | ✅ | ✅ independent file-heartbeat `safety_watchdog.py` (232 lines), real systemd units |
| D6 | 🟠 high | Accel family engine-executable though hidden from prompt | ✅ | ⚠️ still plan-legal (tuning whitelist keeps it); masking still view-only |
| D7 | 🔴 crit | v5 ESTOP unlatched, auto-expires 600 s, one-shot, result ignored | ✅ | ✅ (v5 tree deleted; v6 has latched ESTOP) |
| D8 | 🟠 high | v5 Telegram group-chat auth fail-open | ✅ | ✅ Phase-0 hotfix (also fixed on `main`-targeted plan but not merged) |
| D9 | 🟠 high | v5 dashboard 0.0.0.0, no auth, no WS origin check | ✅ | ✅ deleted (I-8); runtime exposes no HTTP surface |
| D10 | 🟠 high | v5 UDP metrics + Redis unauthenticated (spoofable) | ✅ | ✅ no Redis, no UDP critical path |
| D11 | 🟠 high | Root CI can't pass: bare `pytest -q` aborts at collection; `check_docs.py` fails on 35 absolute links | ✅ | ✅ 11-check CI targeting the active tree; `gate.sh` fully green |
| D12 | 🟠 high | 35 machine-local `/Users/anieyrudh/...` links; `wallee_v6/docs/` absent | ✅ | ✅ redacted; single-tree docs exist |

**CI ground truth [verified by independent run]:** on `main`, **both** CI jobs
fail — `tests-and-lint` because bare `pytest -q` recurses into `wallee_v6/tests`
and aborts with `ModuleNotFoundError: wallee_v6.config` (no root `testpaths`
config to scope it; the real `tests/` suite is **361 passed** and `ruff` is
clean), and `docs-and-contracts` because `check_docs.py` fails on **35**
hard-coded `/Users/anieyrudh/...` links. On `v6.6/refactored`, a clean install +
`pytest` gives **422 passed / 1 xfail** at **74.06 %** coverage (72 % floor
enforced), and `scripts/gate.sh` passes every static/type/test lane end-to-end.

**The pattern is unmistakable:** `main` is the accumulation of regressions; the
refactor is the cleanup. The refactor even *strengthens* invariants beyond v6.5
(frontier validation now also rejects prompt-truncated actions; approval resume
goes through the same TOCTOU+journal tail).

### Residual issues that survive on `v6.6/refactored` (the new known-unknowns)

- **D6 accel** is still plan-legal on the branch — the frontier-visibility fix
  whitelists *all* tuning actions, so hidden accel actions remain selectable by
  non-prompt paths. Decide: fully gate it behind config+approval, or delete the
  family until it's proven.
- **Watchdog false-trip window** **[verified].** Default heartbeat timeout is
  **30 s**; the beacon file is written only *after* execution, while the planner
  budget is **3 attempts × 20 s + 2 × 1 s backoff = 62 s** — and the stale beacon
  still carries `job_active=True`, so the independent watchdog trips ESTOP on a
  slow-but-healthy print. This is the sharpest residual: fix by writing the beacon
  *before* dispatch (or with a `dispatch-in-progress` flag), or by making the
  watchdog stop-rather-than-escalate when an `IN_FLIGHT`/`UNKNOWN` row exists.
- **Watchdog trusts a stale `job_active` beacon** across a resume-then-crash
  window (the stop-vs-escalate decision).
- **~~Runtime can clear its own ESTOP latch~~ — CHECKED AND REFUTED [verified].**
  A dedicated verifier traced this and it does **not** hold: the latch
  (`data_dir/safety/estop.latch.json`) is cleared only by explicit operator paths
  (`operator-cli clear-estop`, the watchdog `--clear --confirm`, or consuming an
  operator-written `estop.clear.json`); a crash-restart **re-engages** it
  (`_load_latch` at construction), and `reset_runtime_start_state` deletes the DB
  files but *not* the latch (and is off by default). Design intent I-3 is upheld.
  Kept here as an explicit "we looked, it's fine" so it isn't re-raised.
- **Cassettes are synthetic** (`"recorded":"synthetic"`) — the planner-eval CI
  lane has never run against a live model; the live `pass^k` lane isn't built.
- **`planner_eval.py` retains ~172 device-specific references** — the instrument
  that gates all quality decisions is itself the most device-coupled code.

---

## 4. Security & privacy exposure live on public `main` right now

Independent of the merge, these are worth addressing because the repo is public:

- **Operator network identity was committed in the clear** (now redacted in the
  working tree — see the scrub commit): a Tailscale VPN IP, an SSH target, and a
  destructive `redis-cli FLUSHALL` one-liner in `docs/internal/CLAUDE.md`, plus the
  real printer/Pi LAN IPs and device MACs across `prusa_link`/`pi_cameras` docs and
  tests. **[verified in git history]** These were already redacted on the refactor
  branch; the scrub replaces them on `main` with RFC-5737 documentation values and
  `<PLACEHOLDER>` tokens. **They remain in git *history* until a history rewrite is
  run** — rotate the Tailscale address regardless.
- **v5 attack surface (all LAN-reachable, all fail-open):** unauthenticated
  dashboard on `0.0.0.0` leaking camera frames + operator photos + intents;
  UDP metrics accepted from any source (spoofable safety-relevant telemetry);
  Redis whiteboard with no auth (poison it → trigger physical actions); Telegram
  group-chat fail-open (any group member gets `/estop` + approvals).
- **Prompt-injection surface:** job names, G-code comments/metadata in the
  notebook, filenames from PrusaLink, `web_search`/`lookup_issue` content, human
  Telegram messages, and the self-written `OBSERVATIONS.md` all reach the prompt.
  v6.5's deterministic gates read model free text (residue/spaghetti keyword
  matching) — a poisoned input can influence a frontier-legal choice. The refactor
  hardens this (Phase 3.3: gates read the closed `finding_type` enum only,
  free-text clamped at ingestion), but a couple of ingestion paths (notebook note
  text, `raw_http_error` fact) were flagged as not yet clamped even there.

**Recommendation:** even before merging v6.6, scrub the four secrets from `main`
*and history* (they're already redacted on the branch), or accelerate the merge so
the redacted tree becomes canonical. Rotate the Tailscale key and any PrusaLink
API key that shared a doc with these.

---

## 5. Proposals — novel / more optimal execution

*(Beyond "merge v6.6," which is #0. Each says why it beats the status quo.)*

**P1 — Close the hardware-sign-off gate deliberately, don't let it rot.** The
entire program (v6.6 merge → tag → v7 cut) is serialized behind one attended Pi
session that has no date. Write the `DEPLOYMENT.md §7` checklist as a literal
scripted session (cold start; real ESTOP timing under a live print; watchdog stop
during motion; crash-restart reconcile), run it once, record the traces as the
first *real* (non-synthetic) evidence bundle, and merge. This is the critical path
for everything.

**P2 — Make the accel decision explicit.** Masking-without-enforcement is the worst
state: the audit trail understates what's executable. Either (a) delete the accel
family until it has managed-proof evidence, or (b) gate it behind
`enable_experimental` **and** approval **and** a real frontier-enforcement check in
`execute_operator_action`. Pick one and document it.

**P3 — Hardware ESTOP is the only fix for the scenario the README admits is
uncovered.** A normally-open relay/SSR on the printer mains behind a retriggerable
hardware watchdog that the safety process must pulse via Pi GPIO. Pulses stop
(process dead, kernel hung, SD dead) → relay drops → printer de-energizes. This is
the one change that converts "software pause" into actual safety, and it's cheap
(~$5 relay HAT + a 555 or watchdog board). Sequence it *after* the independent
file-heartbeat safety process the refactor already built.

**P4 — Test the state machine, not just examples.** Add (a) **property-based tests**
(Hypothesis) over the runtime-DB transition table asserting no path reaches `DONE`
without `DISPATCHED` and rejected transitions never mutate rows; (b) a
**crash-injection harness** with labelled kill-points (post-DISPATCHED/pre-execute,
post-IN_FLIGHT/pre-realize, mid-realize, pre-verify) that boots a fresh runtime and
asserts the reconcile pass leaves no orphan and never double-fires. This class of
test would have caught D2/D3/D5 mechanically; the refactor added reconcile but a
crash-injection harness *proves* it.

**P5 — Planner eval in CI with recorded transcripts; live model nightly only.**
The cassettes are synthetic today. Persist every real OpenRouter request/response
as a replay bundle (vision already does this), replay them deterministically in CI,
and run the live `pass^k` lane nightly. Otherwise the LLM path — the whole point of
the system — has zero regression coverage.

**P6 — Model tiering with deterministic escalation.** Route the dominant regime
(healthy print, NO_ACTION or a single small trim) to a flash-class model, and
escalate to a stronger tier only on machine-checkable events the runtime already
computes (verification mismatch, vision fault, replan). Pair with a **context/cache
diet:** split job-stable content (notebook globals, baselines, tuning bounds) into a
fourth cache-keyed message so only deltas + frontier ride uncached each cycle. This
cuts per-cycle cost without touching the safety envelope.

**P7 — Add a cost/latency budget.** Nothing today caps or alerts on per-cycle LLM
cost or a stall; `consecutive_cycle_failures` is counted but never acted on. Add a
budget ceiling, an output-sanity alert, and a fallback-to-heuristic path when
OpenRouter is down mid-print.

**P8 — Restore the telemetry lane v5 had and v6 dropped.** Filament-runout, door,
and stall sensors are real catastrophic-failure signals that the vision
`FindingType` enum can't express. Extend the enum (detachment / collision / jam /
layer-shift / warping) so the deterministic gates can escalate structurally instead
of relying on the model tagging `spaghetti`.

---

## 6. v7 assessment (the plan is docs-only today)

The v7 design (capability vocabulary → kernel/cortex split + versioned safety
protocol + Rust `wallee-sentinel` → generality via a second device family) is
coherent and the microfactory analysis (each Wallee = an ISA-95 cell controller;
a factory OS is a supervisory layer *above* cells, never a second command path
into a machine) is genuinely good systems thinking. Concerns worth resolving
*before* execution starts:

- **M1 is a make-or-break mega-milestone.** One re-record is asked to absorb both
  the FactKey canonicalization (mechanical, keymap-verifiable) *and* the unbuilt
  `TuningDescriptor` thermal-grammar redesign (a semantic prompt change). **Split
  it** — land the canonicalization first, then the grammar change — the "exactly
  two re-records" constraint trades real risk for narrative tidiness.
- **The Rust sentinel contradicts the project's own research**, which rejected a
  same-host Rust rewrite on safety grounds. Either re-scope M2 to *protocol spec +
  conformance suite only* and defer the binary to a v7.x lane triggered by a real
  appliance image, or write down the override explicitly.
- **Capability schemas get frozen (`LOCK.json`) at n=1 real device family.**
  Freezing the vocabulary before a *non-printer* capability has ever run
  end-to-end risks baking in printer-shaped assumptions. Prove the schema shape on
  a $5 relay/GPIO capability *before* the freeze.
- **Untested-assumption watch:** OpenRouter stays cheap/available/honest (the model
  allowlist checks the *label the router reports*, not provenance — a lying router
  defeats it); LLM coding-agent economics stay favorable (the plan assumes agent
  sessions remain the execution engine); Prusa/PrusaLink API stability for v7's
  life; SD-card WAL wear on the single writable partition. None are budgeted.

---

## 7. Recommended sequencing for a solo maintainer (Claude + local Codex)

**Days 0–30 — make the claimed system real (≈zero new architecture):**
close the hardware-sign-off gate (P1) and **merge `v6.6/refactored` → `main`, tag
`v6.6.0`**; cut the `legacy/v5` archive ref + `legacy/v5-final` tag; fix the
`main`-protection ruleset to name the real CI checks (it currently names deleted
checks and would deadlock PRs); scrub the four secrets from `main`+history and
rotate keys (§4); decide accel (P2).

**Days 30–60 — prove the safety machinery:** crash-injection + property tests (P4);
recorded-transcript planner eval in CI (P5); close the watchdog false-trip and
ESTOP-latch-clear windows (§3 residuals).

**Days 60–90 — real safety + efficiency:** hardware ESTOP relay (P3); model tiering
+ cache diet (P6); cost/latency budget (P7). Then cut `v7/mainline` and start M1
(split per above).

**Two-agent hygiene (Claude + Codex working in parallel).** The tri-file guidance
(`AGENTS.md` canonical; `CLAUDE.md`/`CODEX.md` thin pointers) is the right shape —
but on `main` `AGENTS.md` is **dangerously stale**: it mandates "serial
diagnostics-only" while the shipped pack's headline feature is six serial
tuning-write families, and "UDP metrics advisory" when there's no UDP surface. The
refactor fixes this (generated, byte-identical-checked gate block). Concretely:
1. **Make guidance truthful, then machine-enforce it** — the refactor's approach of
   generating the `AGENTS.md` gate block from CI and failing the build on drift is
   the model; extend it to the Prusa/serial claims.
2. **Two ownership lanes with a CODEOWNERS-style map:** Lane A =
   planner/knowledge/schemas/prompt-shape (+ contract tests); Lane B =
   engine/runtime_db/safety/packs. The **schemas are the inter-lane contract**;
   cross-lane diffs require the full invariant suite + an ADR. This lets Claude and
   Codex work concurrently without stepping on the safety spine.
3. **The seeded-violation drill is your regression net for the guardrails
   themselves** — re-run it whenever a check script changes.

---

## 8. Open questions the analysis could not answer (for you)

1. **Is the hardware sign-off scheduled?** It's the critical path for literally
   everything downstream. If the Pi/printer isn't available soon, we should
   re-scope the gate (e.g. merge behind a feature flag) and record that decision.
2. **Codex's lane:** which parts do you want your local Codex instance to own vs
   Claude? The Lane A/B split above is a proposal, not a decision.
3. **Accel:** delete, or gate-and-keep? (P2)
4. **Secrets on `main`:** scrub history now (rewrites hashes, affects anyone who
   cloned) or just accelerate the merge and rotate keys? (§4)
5. **Scope:** is the near-term goal still "one Prusa, done well," or are you ready
   to spend the v7 generality budget (second device family, capability freeze,
   Rust sentinel)?

---

*Generated by a multi-agent audit (map → adversarial review → independent
verification). High/critical findings were confirmed by independent skeptics and
direct source reads; see the defect register for per-item verification status.*
