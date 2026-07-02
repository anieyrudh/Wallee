# Wallee — Refactor Execution Plan

Date: 2026-07-02
Companion to: [`docs/PROJECT_ANALYSIS.md`](PROJECT_ANALYSIS.md) (the findings this plan resolves; section references like "§3.1" point there).

How this plan was produced: three competing high-level strategies (safety-first incremental, lean-consolidation-first, contract-first strangler) were designed independently and scored by a three-judge panel; the winner was elaborated into five parallel workstream designs (CI/CD, safety correctness, consolidation, lean-down, validation & drift prevention), each grounded in the actual code; three adversarial critics (completeness vs. the findings ledger, promise-fidelity including transient migration windows, feasibility/sequencing) then reviewed the combined plan. **Every critic finding rated critical or major is resolved in this document** — the Integration Decisions table (§2) and the phase plans below are the reconciled, single source of truth.

---

## 0. Goals and non-negotiables

**Goal:** one lean, installable package (~17–18k production LOC, down from ~31.5k across two trees) that actually implements the promises the repo makes, with CI/CD and machine-enforced contracts that make the observed drift classes (safety code present but unwired; claims true only on one laptop; docs diverging from code) structurally impossible to merge.

**The promise — six invariants that must hold at every phase boundary, each becoming a named required CI check:**

| # | Invariant | End-state proof |
|---|---|---|
| P1 | The LLM proposes; deterministic code validates and dispatches. No path from model output to a driver. | `safety-invariants` + `adversarial-gates` |
| P2 | Closed, bounded action space (frontier/PlanIR) validated outside the prompt. | `safety-invariants` |
| P3 | An independent safety layer can stop the machine even if the runtime dies. | `safety-invariants` (SIGKILL test) + hardware-smoke evidence |
| P4 | Device packs own all hardware behavior; core stays generic. | `repo-contract` (core-purity check) |
| P5 | Durable audit trail: propose→gate→dispatch→result persisted; crash-recoverable. | `safety-invariants` (journal/reconcile tests) |
| P6 | Human approval gates hazardous actions; a human can always ESTOP. | `safety-invariants` (approval/latch tests) |

**Honesty rule for transitions (from the promise-fidelity critique):** phase-boundary claims are scoped per tree. Where a promise is *not yet* held (e.g., legacy promise 3 is only partial until the pre-freeze batch; v6 promise 6 narrows at cutover unless the remote channel lands), the gap is stated in the phase's exit criteria and tracked as a strict-xfail test with an issue link — never implied green.

**Strategy (judge-selected, 2:1): "Pin, Prove, Promote, Prune, Police" — a contract-first strangler.**
Tree decision: `wallee_v6` wins. Legacy `wallee/` is hotfixed (it is the only tree that currently holds the promises end-to-end and may be running on real hardware), mined for its three portable organs (ESTOP transport, startup reconcile, thin Telegram gateway), then archived to a `legacy/v5` branch — *only after* the v6 safety-invariant suite is fully green and a signed hardware session validates the new watchdog's physical stop path. `wallee_v6` is then promoted to the repo root as the single package `wallee` (src-layout, one `pyproject.toml`, `uv.lock`).

---

## 1. Phase map

| Phase | Name | Contents (summary) | Exit gate | Calendar (solo, part-time — re-baselined per feasibility critique) |
|---|---|---|---|---|
| 0 | Stop the bleeding | Day-1 safety hotfixes (both trees) + minimal reconcile + truth-green CI + branch protection + redactions | CI green & required; deployed tree materially safer | 1.5–2 weeks (day-1 sub-milestone: hotfix PR + minimal CI) |
| 1 | Pin | Safety-invariant suite (all 6 promises, strict-xfail), clock seam, transport seam + cassettes, schema generation, goldens, evidence decision | Contract suite is a required check; every promise green or xfail-with-issue | 2 weeks (hard time-box) |
| 2 | Prove | v6 safety made real (interlock, watchdog, approvals, reconcile); legacy pre-freeze batch; freeze legacy | Zero xfails in safety-promise files; SIGKILL test green; signed hardware session; `legacy/v5-final` tagged | 2–3 weeks |
| 3 | Promote | One tree/one package; Prusa evicted from core; injection hardening; docs consolidation; remote-channel gate; Pi cutover runbook; `v6.6.0` | Core-purity check green; suite green post-move; remote ESTOP restored or attended-only rule signed | 3 weeks |
| 4 | Prune | God-file decomposition, dedupe, logging, DB hygiene, typed facts, mypy-strict core | No production file >800 lines; goldens byte-identical; mypy green | 2–3 weeks |
| 5 | Police | Final gate set, coverage floor, live-eval lane, seeded-violation drill, ROADMAP | Drill passes (all 7 seeded PRs fail on the named check); required-check set locked | 1–2 weeks |

Total: **12–16 part-time weeks** (the strategy's original 8–12 was rejected as ~2× optimistic by the feasibility critique). Every phase ends with an annotated tag; hardware-smoke checklists are required at exactly three points — Phase-2 exit, `legacy/v5-final`, and every `v6.6.0+` release tag (not every boundary; scoped per critique).

---

## 2. Integration decisions (single source of truth)

The five workstreams were designed in parallel and disagreed on ~10 shared artifacts. These are the binding resolutions; where any workstream detail conflicts with this table, the table wins.

| # | Artifact | Decision |
|---|---|---|
| I-1 | Root `pyproject.toml` | One spec: `name="wallee"`, `version="6.6.0"`, hatchling backend, uv lockfile, dependency-groups for dev. Entry points: `wallee = "wallee.main:main"` (NOT `wallee.cli` — that module doesn't exist until Phase 4; `main.py` remains a stable shim after the split), `wallee-operator`, `wallee-watchdog = "wallee.safety.watchdog:main"`. **No `httpx`** (see I-2). **No `fastapi`/`uvicorn`** (see I-8). Author field fixed for citability. One Pillow ceiling: `<12` until a tested bump. |
| I-2 | Watchdog & ESTOP transport | WS2's architecture at WS3's paths: `src/wallee/safety/watchdog.py` (stdlib-only: `json/os/time/urllib.request/pathlib/signal`) + `src/wallee/safety/stop_transport.py` (generic, stdlib). Packs contribute **declarative stop-profile data** via manifest (`safety_profile`: host env-var name, primary/fallback request dicts); packs ship **no transport code**. The systemd unit execs the `wallee-watchdog` console script, so the Phase-3 rename is a no-op for deployed units. |
| I-3 | Latch ownership | Watchdog-owned latch dir (`wallee-safety:wallee-safety`, `0755`); the runtime user can **read but not write/unlink** `estop.latch.json`. Heartbeat + `estop.request.json` live in a runtime-writable dir. `operator_cli clear-estop --confirm` goes through the watchdog's validated clear entry point (sudo rule or socket/file handshake), never a direct unlink. Contract test asserts the runtime user gets permission-denied on latch removal. |
| I-4 | Watchdog timing | Two-signal design (resolves the 15 s-timeout vs 20 s-LLM-call false-trip contradiction): (a) fast process-liveness beat from a dedicated thread (detects process death, ~15 s horizon); (b) loop-progress beat (cycle counter in heartbeat payload, beaten from the work loop incl. failure/replan paths) with a **derived** wedge horizon `max(90 s, 2 × planner_request_timeout × retry_attempts)`, asserted by a unit test computed from `Config`. Stop policy: process-death during active job → stop; wedge during active job → stop; idle → escalate only. |
| I-5 | Interlock lifecycle | One source of truth: `SafetyKernel.trip()` **writes the durable latch file**; the in-process gate and `poll()` derive engagement from latch-file presence + in-memory state; trips survive restart; clearing requires the explicit operator command (file removal via watchdog + `kernel.clear()` signal). Contract tests: trip → restart → still blocked; clear → same process resumes without restart. Interlock/latch re-checked **per action iteration** in multi-action sequences, not just at cycle entry. |
| I-6 | CI job inventory & required checks | One canonical table (§6), owned by the CI workstream, merging both proposals. One coverage-ratchet helper (`scripts/_ratchet.py`) shared by coverage floor, size allowlist, and purity allowlist. One seeded-drill list (7 cases, §8). `strict_required_status_checks: false` + a documented break-glass procedure (edit the in-repo ruleset JSON via PR — auditable) so a red external check can't force ad-hoc protection disabling. |
| I-7 | xfail policing | ONE mechanism: `strict=True` xfails with issue-URL reasons + `tests/contract/MANIFEST` + an offline grep rule in `check_repo_contract.py`. The `--runxfail` flip and the `gh api` open-issue check are **cut**. Zero-xfail gates are scoped **per file** with a milestone map encoded in the checker: `test_safety_invariants.py` → zero xfails at Phase-2 exit; `test_core_genericity.py` (core purity, device-id collision, frontier mismatch) → zero xfails at Phase-3 exit. This prevents the Phase-2 gate from turning permanently red on Phase-3-scheduled tests. |
| I-8 | v6 `dashboard.py` | Deleted in the Phase-3 promotion window (it is dead code with an unauthenticated design and a direct `_conn` reach-in); `fastapi`/`uvicorn` dropped from dependencies in the same PR. A designed, authenticated dashboard is ROADMAP work. |
| I-9 | Agent/local entry point | `./scripts/gate.sh` only (Makefile cut). It runs exactly the CI jobs in order; pre-commit runs the fast subset. AGENTS.md, CONTRIBUTING.md, and the PR template reference only `gate.sh`; a generated `<!--gate-block-->` is kept byte-identical to `ci.yml` by a meta-check. |
| I-10 | Claims machinery | `claims.yaml`/`check_claims.py` is **cut** (over-engineering for a solo repo, per feasibility critique). Doc honesty is carried by: C2 fact markers (numeric claims generated from the live system), C6 evidence refs (hash-verified artifacts behind every `*-proven` cell), and the six promises being literal required-check names. The safety-ack label gate and CODEOWNERS are also cut (theater at 0 required reviews). |
| I-11 | Tuning grammar | One module `src/wallee/tuning.py` in **core** (WS4's placement wins — `planner_eval.py` must import it), structured `TuningDescriptor` metadata on `LegalAction` replaces both string-parsing conventions (`TUNE_` prefix and `_TRIM_` id-parsing). |
| I-12 | `planner_eval.py` | Split (unblocks the Phase-3 core-purity gate, which otherwise fails on its 109 Prusa references): generic scoring engine stays in core; the Prusa `ACTION_CATALOG`/case corpus moves to `tests/evals/corpus/` + the pack. Until the split lands, `check_core_purity.py` carries it on the shrink-only allowlist. `explain_planner_preference.py` follows the corpus. |
| I-13 | Promotion mechanics | Three separate CI-green PRs, not one: (a) delete legacy tree (Phase-2 exit, after the archive branch is cut), (b) pure `git mv` + blanket `\bwallee_v6\b → wallee` rewrite over py/yaml/toml/service files (the three-sed variant is rejected — it verifiably misses string-form module refs like `patch("wallee_v6.engine.time.monotonic")`), gated by `rg wallee_v6` returning zero hits, (c) packaging/CI/config changes. Untracked `egg-info` handled with plain `rm` + `.gitignore`. Required-check rename uses temporary alias jobs so main never becomes unmergeable. |
| I-14 | Vision free-text in guards | An explicit **behavior-change PR** (Phase 3, its own golden re-bless): deterministic suppression guards consume the structured `finding_types` enum only; all substring matching on model-generated summary text is deleted. The adversarial regression asserting "no deterministic guard reads model free-text" activates only after this PR. |

---

## 3. Phase 0 — Stop the bleeding (1.5–2 weeks; day-1 sub-milestone)

### Day 1–2: the safety hotfix PRs (both trees)

Legacy (`wallee/`) — one PR:
1. **Latch ESTOP**: remove the TTL on `safety.estop` (`human/telegram.py:422`, `human/cli.py:144`, `config.py:87,134`); engine gate and kernel treat it as latched until an explicit human clear command (new `/estop_clear` — see item 3).
2. **Approval gate order**: skip the deadline gate on approved re-processing (`engine/dispatch.py:139-160,213-217`) so post-30 s approvals execute; pin with a test. Add a pinning test that every `requires_approval` tool has a precheck (so approved-late dispatch always re-validates against hardware-adjacent state).
3. **Telegram group-chat guard** (critical, from the completeness critique): refuse to start when `TELEGRAM_CHAT_ID` is a group and `TELEGRAM_ALLOWED_USER_IDS` is empty (`telegram.py:71-77` — today an empty allowlist authorizes *everyone* in the group). `/estop_clear` additionally requires a non-empty explicit allowlist. Test: group chat id + empty allowlist → constructor raises.
4. **Fault-monitor parsing**: JSON-decode before the `!= "0"` comparison (`safety/kernel_main.py:123`).
5. **Dashboard**: bind `127.0.0.1` at **both** default sites (`main.py:212` and `ui/dashboard.py:510`), host configurable, shared-token check on the WS handshake (~20 lines).
6. **Secrets off argv**: pass API keys to the kernel subprocess via environment (`main.py:130-140`).

v6 (`wallee_v6/`) — one PR:
7. **Planner NameError**: import the three `_action_*_from_id` helpers into `planner.py` (bug at `planner.py:385-387`); add a `tuning_choice` regression test; fix all 27 `ruff check wallee_v6` findings.
8. **Journal-wipe flip WITH minimal reconcile in the same PR** (critical fix from the promise-fidelity critique — flipping the default alone leaves crash-residue `DISPATCHED` rows whose `required_locks` permanently filter PAUSE/CANCEL out of every future frontier via `planning_context._active_locks()`): default `WALLEE_FLUSH_STATE_ON_START=0` **and** a ~30-line boot sweep — `DISPATCHED → UNKNOWN` (+ CRITICAL event + outbox escalation), `PROPOSED/AUTHORIZED → ABORTED`. Test: crash-simulated `DISPATCHED` row → restart → pause/cancel still present in the compiled frontier. (Full reconcile with wall-clock timestamps still lands in Phase 2.)
9. **Transition-table fix hoisted** (from the feasibility critique — Phase-1 goldens crash without it): allow `PROPOSED/AUTHORIZED → REPLAN_REQUIRED` in `runtime_db._ALLOWED_TRANSITIONS` (the engine already performs both at `engine.py:274,297`; today they raise `ValueError`); pin with two tests.
10. **Laptop-path tests**: derive script paths from `Path(__file__).resolve().parents[1]` in the three failing tests; `--ssh-key` default → required arg; also fix the out-of-repo fixture fallback at `tests/test_prusa_core_one_job_notebook.py:111` (same defect class, missed by the original plan).

### Day 2–5: truth-green CI + protection

11. `ci.yml` rewrite: `legacy-tests` (root `pytest.ini` scoping collection to `tests/` + `wallee/`), `v6-tests` (`pip install -e "wallee_v6[dev]" && pytest wallee_v6/tests && ruff check wallee_v6`), `docs-contract` (existing checkers). De-flake or quarantine the named flaky legacy tests (OS-assigned ports + readiness polling replace hardcoded ports 18765-18767 and `sleep(0.5)`; the 17 timing sleeps get event-based sync or the specific tests leave the required job with a tracking issue) — a flaky *required* check trains re-run-until-green, the exact habit this plan exists to kill.
12. Fix the ~36 author-laptop-path links; `scripts/check_forbidden_patterns.py` lands (laptop paths, RFC1918/Tailscale-CGNAT IPs — docs use `192.0.2.x`/`<PRINTER_IP>` placeholders, MAC/serial formats, ssh-key paths, `sk-or-`) as pre-commit hook + CI job.
13. **Redactions**: printer serial/UUID/MAC/Tailscale IP out of `prusa_link/CAPABILITIES.md`; operator SSH/IP details out of `docs/internal/CLAUDE.md`; parameterize hardcoded printer IPs in v6 docs.
14. **Branch protection ON** via an in-repo ruleset JSON: PRs required, approvals 0, no direct pushes, no bypass actors; required checks = `legacy-tests`, `v6-tests (3.11)`, `docs-contract` (one Python leg required now; 3.12 runs `continue-on-error` for a week, then promoted — neither leg has ever been proven anywhere).
15. Interim README note (the two-tree "which tree do I want?" table + Redis prerequisite) — the front door stays wrong for weeks otherwise; the full rewrite is Phase 3.
16. Week-1 **evidence decision** (now-or-never): attempt recovery of `wallee_v6/docs/**` and `docs/evidence/**` from the author's machine. Whatever is recovered is committed post-redaction; every `*-proven` claim whose artifact is not recovered is downgraded to `implemented` in the same commit.
17. Tag `v6.5.0` (annotated; hardware evidence explicitly starts at Phase 2 — the tag-gating workflow doesn't exist yet, and that is stated rather than implied).
18. `dependabot.yml` (pip ×2 + actions), CHANGELOG.md created with a retroactive 6.5.0 entry naming the safety hotfixes; `wallee_v6/pyproject.toml` version → `6.5.0`, author field fixed.

**Phase-0 exit (honestly scoped):** CI green and required on both trees; deployed legacy holds P1, P2, P5, P6 plus latched human-ESTOP; **legacy P3 is explicitly partial** (side-thread heartbeats, unsupervised kernel, no Redis-loss behavior — scheduled in the pre-freeze batch, tracked as dated items); v6 gaps are machine-tracked from Phase 1.

---

## 4. Phase 1 — Pin (2-week hard time-box)

Priority order inside the box (pre-declared cut line, per the feasibility critique — must-haves first):

1. **Safety-invariant suite** `wallee_v6/tests/contract/` — all six promises present from day one; not-yet-true invariants are `xfail(strict=True, reason="<issue URL>")`. Runs as the named required check `safety-invariants` with `--strict-markers` and a **fail-on-zero-collection guard from day one** (not deferred to the Phase-5 drill). The full invariant list is §5. Genericity invariants (core purity, device-id collision, prompt/executable-frontier equivalence) live in a separate `test_core_genericity.py` with a Phase-3 milestone (per I-7).
2. **Injectable clock seam** (`now_fn`/`mono_fn` on `SafetyKernel`, the latch predicate, approvals, watchdog; `HumanGateway` already has `now_ms`) — a named deliverable, because at least four specified tests silently depend on it; without it the v6 ESTOP-latch test cannot detect the original 600 s-TTL bug class. The latch test advances a fake clock 24 h and asserts still-latched; an AST/grep guard asserts latch-clearing code contains no time comparison.
3. **Transport seam + cassettes**: extract the planner's HTTP block behind a `PlannerTransport` protocol (live/recording/replay); **header allowlist patch to `openrouter_observability.py` lands before any cassette is recorded** (allowlist: `x-request-id`, provider, content-type, ratelimit; recording refuses to write on any `sk-or-`/`Bearer` substring); cassettes committed under `tests/fixtures/cassettes/` with fingerprint matching — any prompt/payload change turns CI red until deliberately re-recorded. CI job `cassette-replay` replays through the *production* planner.
4. **Schema generation**: `scripts/gen_schemas.py --check|--write` derives `plan_ir.schema.json` (adds `$id`/version, `maxItems`/`uniqueItems` on `sequence`), `world_packet.schema.json` (from pydantic; kills the 540-line hand dict), `manifest.schema.json` (tightened `additionalProperties`). Reconcile the three horizon limits to one `Config`-sourced constant and give `last_result` a typed sub-model (closing the one `additionalProperties: true` hole). CI job `schema-sync`.
5. **Characterization goldens**: plan→gate→dispatch→journal traces over sim packs via the existing whole-runtime `tmp_path` fixture. Goldens pin exact shapes; invariants pin properties; re-blessing only in separate `[re-bless]` commits (enforced per-commit, not per-PR-diff, so the sanctioned separate-commit workflow actually passes the check).
6. **Entrypoint-level e2e tests** (from the promise-fidelity critique — the original bugs were all *wiring* omissions in `main` that 262 green unit tests missed): run the packaged CLI as a subprocess (`python -m wallee_v6.main --cycles 2 --simulate ...`) and assert from the DB/outbox/files that (a) a stale heartbeat blocks dispatch and escalates, (b) a present `estop.request.json` trips and blocks, (c) a recorded approval resumes execution, (d) the heartbeat file's mtime advances every cycle. These are the structural defense against "safety component present but unwired in main" recurring through the Phase-4 split.
7. `planner_eval` made CI-runnable offline (case corpus committed in-repo; transport-injectable; phantom `docs/evidence` root removed) — slips to Phase 3 if the box is hit.

**Exit:** `safety-invariants`, `schema-sync`, `cassette-replay`, `gates` (pre-commit job) required; every promise green or strict-xfail with an issue; anything unpinned past the box moves to the Phase-5 backlog *by decision, not silence*.

---

## 5. Phase 2 — Prove (the safety build-out)

### v6: flip the xfails green

| Item | Fix | Proven by |
|---|---|---|
| Interlock wired | Check latch/interlock at `Engine._execute_actions` entry **and per action iteration**; act on `poll()` in the loop; `trip()` writes the durable latch (I-5) | `test_tripped_interlock_blocks_dispatch`, `test_latch_written_between_actions_blocks_next_action`, trip→restart→still-blocked |
| Stop callback | Port `wallee/safety/estop.py` as pack **stop-profile data** + generic stdlib `stop_transport` (I-2); first-ever `register_interlock_callback` caller | `test_interlock_trip_invokes_pack_stop_callback` |
| Watchdog process | `src/.../safety/watchdog.py` per I-2/I-3/I-4: file heartbeats (mtime = truth), latch honored by both enforcers, stop resent every 30 s while latched during active jobs, `sd_notify` watchdog-of-the-watchdog, boot grace, **boot-time stop-transport preflight** (authenticated reachability dry-check + escalation — a wrong API key must not be discovered at trip time) | `test_watchdog_stops_machine_when_agent_sigkilled` (SIGKILLs the **real CLI subprocess**, file transport), idle-no-stop test, resend test, HTTP-transport test against a local stub server |
| Heartbeat semantics | Beats from the work loop on every pass **including failure/replan paths**; two-signal budget per I-4 | `test_heartbeat_beaten_on_failure_paths`, config-derived budget test |
| Approval resume | Cycle-start scan of `WAITING_APPROVAL` runs; resume routes **through the shared gated executor** (interlock + lease + locks — not a re-implemented tail); expired-while-down approvals abort immediately as `approval_expired_before_execution` with a re-approval escalation; approvals table gains `CHECK(decision IN ('APPROVE','REJECT'))` and a guard refusing approvals for runs not in `WAITING_APPROVAL` | End-to-end approve→execute test; `test_approved_hazardous_action_executes_exactly_once`; `test_human_estop_available_while_waiting_approval` |
| Reconcile (full) | Port of legacy `reconcile.py` over `exec_journal`; wall-clock columns beside monotonic (DB migration lands first — WS4 item 1) | `test_restart_with_in_flight_escalates_to_human`, `test_journal_records_wall_clock` |
| Hazard-class floor | Pack-conformance rule: START/CANCEL/STOP/RESUME-class verbs must be ≥ MEDIUM hazard (auto-approve-LOW defaults on, so a mislabeled control verb silently bypasses P6) | conformance test |
| systemd | Real `wallee-safety.service` (Type=notify, hardened, `EnvironmentFile=`), `wallee-main.service` with full ExecStart args (goal/mode from the EnvironmentFile — the unit must actually boot), `Requires=wallee-safety.service` | deploy-doc smoke item: `systemctl start` succeeds on the Pi |

Also: runtime writes a boot check that the watchdog's liveness/version file exists and is not the placeholder — "watchdog actually running and current" becomes a machine check, not a checklist line.

### Legacy: the pre-freeze batch (the tree is deployed until Phase 3)

1. Kernel acts on heartbeat loss during active jobs (stop + outbox), heartbeats/progress counters from the work loops; kernel subprocess supervised.
2. **Redis-loss degraded mode** (from the completeness critique): sustained Redis connectivity failure during an active job is treated like heartbeat loss — direct PrusaLink stop + a local latch file (Redis-independent) + outbox escalation; tested with a fakeredis connection kill.
3. Human ESTOP routed through the pack `SafetyProfile` (not raw env), so the stop path matches the active pack.
4. **Cheap injection hardening for the freeze window** (rather than silent risk acceptance): `remember` becomes approval-gated; `web_search`/tool results rendered inside explicit untrusted-data fences; filename length/charset clamp before prompt inclusion.

### Freeze

Zero xfails in the safety-promise files + SIGKILL e2e green in CI + **signed hardware session on the Pi** (SIGKILL `wallee-main` mid-test-print → physical pause within budget; latch blocks resume; `clear-estop` restores; cold-start operator ESTOP works) → tag `legacy/v5-final`, cut branch `legacy/v5` with a tombstone README, and **delete `wallee/` + legacy root tests from main** (this is promotion PR (a) per I-13). The archive PR description carries the **retired-findings disposition register**: every legacy-only finding from the analysis (§5.1 legacy schema items, §5.2 Redis-whiteboard items, §5.5 parser bugs, §6.2 legacy correctness bugs, §4.2 singletons/layering) listed as "known, retired with the tree" — so "all issues resolved" is auditable, not implied.

---

## 6. Phase 3 — Promote (one tree, one package, Prusa out of core)

### Mechanics (three PRs per I-13)

- Pure move: `git mv wallee_v6/wallee_v6 → src/wallee`, tests/schemas/knowledge/deploy/scripts relocated; blanket `\bwallee_v6\b → wallee` rewrite; `rg wallee_v6` = 0 hits gate; full pre-move suite + goldens byte-identical post-move on the same commit.
- Packaging: the one `pyproject.toml` (I-1) + `uv.lock`; `requirements*.txt` deleted; `config.py` path resolution via `importlib.resources`; check rename via temporary alias jobs, then the ruleset JSON swap is applied by `gh api` from the in-repo file, then aliases removed.
- A compatibility note ships in the release: anything importing `wallee_v6` (open branches, the Pi venv, other checkouts) breaks deliberately; the runbook covers the Pi.

### De-Prusa-fication (each move behind the pinned contracts)

1. `TuningDescriptor` grammar module (I-11); both old detectors deleted.
2. Pack interface additions in `packs/base.py`: `planner_signals(world) -> DecisionSignals` hook absorbs the ~700 lines of `printer_1.*` compilation out of `models.py`; `plan_policies` (suppression rules, terminal guard) move from `planner.py`/`main.py` into the pack **as data/policy objects**, not free-floating keyword matchers.
3. **I-14 behavior-change PR**: deterministic guards consume `finding_types` only; summary-substring matching deleted; goldens re-blessed deliberately.
4. **Prompt/executable frontier unification** (owner assigned per the completeness critique): `validate_plan` restricted to the prompt-visible frontier (or truncation removed so they're identical); flips the Phase-1 xfail.
5. Device-id parameterization in `engine.py`/`planning_context.py`/`main.py`; registry rejects duplicate `DEVICE_ID` claims (`claimed[device_id] = pack_id` — the truthiness-accident snippet from the draft is corrected); sim/real packs get distinct ids in tests.
6. **v6 input sanitization** (owner assigned): filenames, notebook notes, and vision text are length/charset-clamped at pack `normalize()` ingestion and rendered only inside fenced/typed fields in the prompt view — then the adversarial assertions activate.
7. `planner_eval` split per I-12; `check_core_purity.py` lands **in the same PR as the first eviction** with a shrink-only allowlist; exit proof: `rg 'printer_1|PRUSA|_TRIM_' src/wallee --glob '!**/packs/**'` = 0.

### The remote-channel gate (from both critiques — promise 6 must not silently narrow to "human with an SSH session")

Before `v6.6.0` is tagged, one of:
- the thin Telegram gateway lands (`gateways/telegram.py`, ~150 lines over `HumanGateway`: approval push, approve/reject replies, `/estop` writing `estop.request.json`; the group-chat allowlist guard is a precondition), **or**
- an authenticated webhook/ntfy push notifier for outbox files + an ESTOP webhook, **or**
- a signed "attended-operation only until the gateway lands" rule in the release notes and hardware checklist.

### Docs consolidation + deployment

- README rewritten single-tree; `docs/ARCHITECTURE.md` (v6 pipeline) written; GLOSSARY, SCHEMAS reference, VALIDATION (including the honest 47/60 replay history); **SECURITY.md threat model** (prompt injection via filenames/notebook/web results; LAN exposure of PrusaLink/dashboard; Telegram hardening; disclosure expectations) — owner assigned per the completeness critique.
- Colliding stale `docs/internal/AGENTS.md`/`CLAUDE.md` renamed to `docs/history/*_v4_legacy.md` **at promotion time** (agents keep loading them until they physically cannot); a docs rule forbids `AGENTS.md|CLAUDE.md|CODEX.md` outside root + history. Remaining doc/code contradictions get owners here: "retried once" vs default-3, the trim-envelope discrepancy (both from config/pack-spec-derived fact markers).
- 60-scenario replay corpus translated to world-packet key-space (`tests/replay/keymap.py`), becoming (i) a model-independent gate-assertion suite (required) and (ii) planner-eval cases — **conversion completes before the legacy tree leaves main** (ordering per the promise critique; it is a checklist item in the archive PR).
- **Pi cutover runbook** (`docs/DEPLOYMENT.md`, from the feasibility critique): legacy services stopped+disabled (not removed) → env files provisioned → v6 units installed (never two watchdogs co-resident) → hardware checklist → legacy uninstall only after `v6.6.0` sign-off; rollback = re-enable legacy units from a `legacy/v5` checkout kept on the Pi.

Tag `v6.6.0` — first GitHub Release.

---

## 7. Phase 4 — Prune (lean-down behind stable contracts)

Ordered by value/risk (characterization-first: every split PR shows byte-identical goldens; re-blesses are separate commits — exactly three are expected: INSERT-not-REPLACE, coercion strictness, schema regeneration):

| # | Item | LOC Δ | Risk |
|---|---|---:|---|
| 1 | DB hygiene: `schema_migrations` table (transactional, versioned), indexes (`action_runs(status)`, `(run_scope, updated_ts_ms)`, `approvals(action_run_id)`, `exec_journal(action_run_id)`, `events(ts_ms)`), `INSERT`-not-REPLACE for plans (+ IntegrityError test), wall-clock journal columns (lands early — Phase 2 reconcile consumes it) | +90 | Low |
| 2 | Dedupe: one `util/coerce.py` (`_float_or_none` ×5+), one run-scope builder, one `_normalize_*`, one pipe-split | −230 | Low |
| 3 | Ring-buffer `InMemoryWhiteboard._changes` | +5 | Low |
| 4 | Generated schema + typed prompt-view models (hand dict deleted) | −450 | Med |
| 5 | `driver.py`: six `_set_*` clones → one `_set_scalar` + per-family spec table | −160 | Low |
| 6 | `pack._realize` 30-branch if-chain → dispatch table | −110 | Low |
| 7 | Logging: module loggers everywhere, stdout tee becomes a handler, DB event stream unchanged for audit; driver/pack layers get diagnostics | +80 | Low-Med |
| 8 | Act on `consecutive_cycle_failures`: `>=` threshold with a fired-flag re-armed on interlock clear (the `==`-fires-once bug from the draft is corrected) → trip + outbox | +35 | Low |
| 9 | Dead code: v6 `dashboard.py` (per I-8), `RedisWhiteboard` placeholder; `runtime_control.py` gets unit tests **or** is deleted in this sweep (decision recorded — it is currently the one zero-test module nothing mentions) | −120 | Low |
| 10 | `main.py` → `cli.py` + `composition.py` + `loop.py` + `artifacts.py` + `archive.py` (`main.py` stays as the stable entry shim); the entrypoint e2e suite (Phase 1 item 6) is what proves the split drops no wiring | −150 | Med |
| 11 | `models.py` 1,662 → ~420 (signal compilation left in Phase 3; contracts remain) | −100 | Med |
| 12 | `pack.py` 3,247 → `state_normalizer` / `frontier` / `realize` / `tuning_policy` (+ the six shadow-state clones parameterized) | −650 | **High** |
| 13 | `hardware_smoke.py` 4,827 → `smoke/` package via the existing `_ManagedFamilySpec` table (one parametrized phase-1 runner, benchy, audit report, CLI) | −2,300 | Med |
| 14 | Typed `FactKey` registry (ends stringly-typed fact keys and alias-probing; predicates fail closed on typos today — this makes the failure loud instead of silent) | +180 | Med |

Net: **21.3k → ~17.3k production LOC**; test code deliberately grows (~+800 beyond the Phase-1 suites) — enforcement moving from prose into executable code is the point. File-size cap (800 lines) flips from shrink-only allowlist to absolute; mypy strict on the core package with `py.typed`; ruff ramp completes (`E,F,B,I,UP,SIM,C4,RUF,T201` — T201 makes stray `print()` a lint error after item 7).

---

## 8. Phase 5 — Police (the permanent end-state)

**Canonical CI (required checks on `main`; the one merged table per I-6):**

| Check | Contents | Required from |
|---|---|---|
| `tests` (3.11, 3.12) | L0 unit + L2 sim-integration + goldens + entrypoint e2e; coverage ratchet inside | Phase 0 (as `legacy-tests`/`v6-tests`) |
| `safety-invariants` | `tests/contract/` — the six promises; zero-collection guard; per-file xfail milestones | Phase 1 |
| `gates` | pre-commit run --all-files (ruff lint+format, forbidden-patterns, secrets-in-fixtures, generated-artifact `--check`s) | Phase 1 |
| `schema-sync` | `gen_schemas.py --check` | Phase 1 |
| `cassette-replay` | offline replay through the production planner | Phase 1 |
| `mypy` | strict-modules scope (safety/engine/runtime_db first) → whole core at Phase 4 | Phase 2 |
| `docs-contract` | `check_docs.py` v2 (markdown links + backtick path refs + absolute-path ban) + `gen_doc_facts.py --check` (counts generated from the live registry/pytest into fact markers) | Phase 0 (v2 at Phase 1) |
| `repo-contract` | `check_repo_contract.py` v2: v6 pack conformance (manifest schema-valid, README/CAPABILITIES mention rule, hazard-class floor, duplicate-device-id), file-size ratchet, core purity, evidence integrity (`check_evidence.py`: every `*-proven` cell carries a hash-verified committed artifact ref; orphan detector), xfail/MANIFEST hygiene, gate-block sync | Phase 3 |
| `adversarial-gates` | hostile-PlanIR corpus + translated hazard scenarios + injection strings: the **gates** must refuse every one, model-independent | Phase 3 |
| `sim-evals` | fault-injection trajectory evals over the sim printer, offline lane (deterministic/cassette-driven; pass^k belongs to the live lane only — measuring pass^5 on a deterministic planner measures nothing) | Phase 5 |
| `pip-audit` | dependency vulnerabilities | Phase 5 |

Plus: CodeQL default setup (near-zero cost, from the completeness critique) and ruff's S-rules instead of a separate bandit. Scheduled workflows: weekly live-eval lane (budget-capped, `OPENROUTER_API_KEY` secret, N-sample pass-rates with trend history) and dependabot.

**Seeded-violation drill (exit ritual):** seven deliberately red PRs — an 801-line core file; `printer_1` in `engine.py`; an F821; a laptop path in a test; a hardware serial in docs; **deletion of `test_safety_invariants.py`** (proves the zero-collection guard); a golden hand-edited inside a source-touching commit. Each must fail on the *named* check; run URLs recorded; then closed unmerged. A guard that has never fired is a claim, not a control.

**Guidelines end-state (for AI-agent contributors who follow instructions literally):**
- ONE authoritative root `AGENTS.md` (CLAUDE.md/CODEX.md = 3-line pointers): the six non-negotiables each citing its CI check name; the `<!--gate-block-->` (generated from `ci.yml`, byte-identical or CI fails); hard rules — never mix golden/cassette edits with source commits; never touch `tests/contract/` xfails without an issue link; never mark `*-proven` without an `evidence:` ref; never write literal IPs/serials/paths; extraction PRs carry no logic changes; regenerate (never hand-edit) generated artifacts.
- `./scripts/gate.sh` is the only local command surface (I-9); CONTRIBUTING.md and the PR template reference it exclusively; the PR template checkboxes mirror required-check names one-to-one.
- Version-pinning parity: the exact ruff version pinned identically in dev-deps, pre-commit rev, and CI; dependabot bumps all three together or goes red.
- **`docs/ROADMAP.md`** (from the completeness critique — deferral must be a recorded decision, not silence): the P3 research upgrades from the analysis (local Pi safety-critic + conservative-enum degraded mode; hybrid local-CNN→VLM vision with bounding-box cross-check; forecast-residual telemetry lane + G-code static screening gate; LTL/MLTL invariants over PlanIR + conformal approval escalation; MCP device-pack servers), the full Telegram port and event-driven wake, the authenticated dashboard, and the two largely-pre-satisfied P1 items with their small remainders (structured outputs: v6 already sends strict `response_format` + `cache_control` — remaining: `require_parameters`/model filtering and reasoning-field-first evaluation; reliability: cache-aware fallback chains, OTel GenAI spans, per-cycle output-sanity alerts).

---

## 9. Risk register

| Risk | Mitigation |
|---|---|
| Refactor ships a regression the unit suite misses | Characterization goldens + cassette fingerprints + entrypoint e2e; extraction PRs must be byte-identical; re-blesses are separate reviewed commits |
| Safety work itself introduces a transient hole | The two known instances are already fixed in-plan (journal-flip + same-PR reconcile; latch-dir permissions); phase exits state per-tree promise scope honestly; hardware sign-off gates the freeze |
| Watchdog false-trips erode operator trust | Two-signal timing derived from Config with a unit test; idle → escalate-only policy; resend-while-latched bounded to active jobs |
| Promotion bricks the deployed Pi | Console-script systemd entry (rename-proof), cutover runbook with disabled-not-removed legacy units and a rollback checkout, alias CI jobs across the check rename |
| Solo-maintainer stall mid-migration | Every phase boundary is independently shippable and tagged; Phase-1 box has a pre-declared cut line; the plan survives pausing after any phase |
| AI-agent contributors reintroduce drift | Branch protection with no bypass from day 2; identical local/CI gates (`gate.sh`); forbidden-pattern hooks; generated docs/schemas; the seeded drill proves the guards fire |
| A required check is red for external reasons | `strict` off + documented break-glass via in-repo ruleset JSON PR (auditable), never ad-hoc protection disabling |

---

## 10. What this plan deliberately does NOT do

- No orchestration framework, no second database, no DSL, no streaming planner — the existing AGENTS.md anti-scope list stands.
- No `claims.yaml` meta-registry, no CODEOWNERS/label gates, no LLM-judged safety verdicts, no pass^k over deterministic components (cut as solo-repo over-engineering; the named-check + fact-marker + evidence-ref machinery carries the honesty load).
- No big-bang rewrite: legacy is archived intact and hot-fixed, not rewritten; v6 is refactored behind pinned behavior, never redesigned mid-flight.
- No hardware claims from CI: sim proves logic; only committed, hash-referenced hardware-smoke evidence proves `*-proven` rows, and the release checklist samples physical truth at the three points that matter.
