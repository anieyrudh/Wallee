# Wallee — Comprehensive Project Analysis & Improvement Plan

Date: 2026-07-02
Scope: full repository (`wallee/` legacy v5 line, `wallee_v6/` current v6.5 line, docs, CI, process), plus a survey of 2024–2026 research and tooling advances the project could adopt.

Method: parallel deep-dives over both source trees, the test suites, CI, and all documentation; test suites were actually executed; every high-severity claim below was verified directly against code (file:line citations throughout); the research section was compiled from primary sources with second-source verification of surprising claims.

---

## 1. Executive summary

Wallee's **architecture is genuinely strong — and now matches the published state of the art**. The "LLM proposes / deterministic code disposes" split, the closed action frontier + Plan IR in v6, the ledger/diary crash-safety design, and the independent-safety-kernel concept are exactly the patterns that 2025–2026 research (RoboGuard, AgentSpec, SELP) and emerging standards (ISO/IEC TR 5469:2024, EU Machinery Regulation 2023/1230) recommend. The process design — PR/issue templates, custom doc-contract checkers, honest limitation notes, replay-harness eval writeups — is unusually thoughtful for a solo project.

The problem is **implementation drift: several of the repo's central safety and quality claims are currently untrue in code**:

1. **The v6.5 safety kernel is decorative.** `register_interlock_callback` is never called anywhere, `safety.poll()`'s result is discarded (`wallee_v6/wallee_v6/main.py:1151`), nothing checks `interlock.engaged` before dispatch, there is no ESTOP transport at all in v6, and the shipped `wallee-safety.service` literally runs a placeholder `time.sleep(10**9)` one-liner.
2. **Approvals are broken in both trees, in different ways.** In v6, `WAITING_APPROVAL` runs are never resumed — approval-gated actions (CANCEL, START) can never execute via the planner loop. In legacy, the deadline gate runs before the approval gate against the original proposal timestamp, so any approval arriving after `max_proposal_age_ms` (~30 s for `cancel_print`) is rejected as `expired` — the human approves and the action silently dies.
3. **Legacy ESTOP auto-expires.** `safety.estop` is written with a 600 s TTL; an emergency stop that silently un-latches after 10 minutes is a safety-critical mis-design.
4. **CI has been red on `main` since the v6.5 merge** — both jobs. Root `pytest -q` fails at collection on `wallee_v6/tests`, and the repo's own `scripts/check_docs.py` exits 1 with ~36 errors: README/ARCHITECTURE link to `<author-laptop-path>/...` — absolute paths on the author's laptop. The `wallee_v6` tree — "the latest maintained implementation" — has **never been covered by CI, lint, or type-checking**, and it shows: `ruff check wallee_v6` finds 27 errors including a real production `NameError` (`planner.py:385-387` calls three helpers that are never imported).
5. **The v6 evidence base is missing.** `wallee_v6/docs/` (architecture doc, ADRs, the `docs/evidence/**` artifacts underwriting the pack's "proven" status matrix) does not exist in the repo; three v6 tests hardcode the author's laptop paths and fail on any other machine.

None of this diminishes the design contribution — but for a repo that positions itself as a citable safety architecture, the gap between claim and code is the most important thing to close. The good news: nearly all P0 items are days, not months, of work.

---

## 2. Repository overview

| | `wallee/` (legacy, v5 line) | `wallee_v6/` (current, v6.5) |
|---|---|---|
| Model | One-action-per-cycle agent loop | Compile → plan (Plan IR) → validate → execute |
| State | Redis whiteboard + SQLite ledger/diary | In-memory whiteboard + SQLite runtime DB |
| LLM output | Strict JSON parsed from text | JSON Schema + pydantic-validated Plan IR |
| Safety | Separate watchdog subprocess + software ESTOP | In-process kernel (unwired), no ESTOP |
| Human path | Telegram + CLI + outbox | Operator CLI + outbox |
| Size / tests | ~10.2k LOC, 517 tests (green) | ~21.3k LOC, 262 tests (3 red) |
| CI / lint | Covered | **Not covered** |
| Packaging | requirements.txt (unbounded floors) | pyproject.toml (bounded ranges) |

The two trees share no code; both independently implement whiteboard, ledger, safety, human gateway, LLM client, and dashboard. Legacy has capabilities v6 lost (Telegram, ESTOP transport, startup reconcile, event-driven wake); v6 has capabilities legacy lacks (closed frontier, Plan IR, post-action verification, prompt-contract tests).

---

## 3. Critical findings (P0)

### 3.1 v6 safety kernel does not stop anything
- `SafetyKernel.register_interlock_callback` (`wallee_v6/wallee_v6/safety.py:32`) has **zero callers**; `trip()` therefore has no physical effect.
- `safety.poll()` is called once per cycle and its return value discarded (`main.py:1150-1152`); neither the loop nor `Engine._execute_actions` ever checks `safety.interlock.engaged` before dispatch.
- The heartbeat is beaten by the same thread being watched (`engine.py:203,214,330`), and failure/replan paths `break` before the beat — so the interlock would trip spuriously (3 s timeout vs 5 s poll interval, `config.py:138-139`)… and then nothing would happen anyway.
- `deploy/systemd/wallee-safety.service:8` ships `python -c "...print('Safety kernel placeholder running'); time.sleep(10**9)"`.
- v6 has no ESTOP transport at all (no direct HTTP stop, no serial `M112`, no GPIO). The safest action (CANCEL) is approval-gated — and approvals are broken (§3.2).

**Fix:** check `interlock.engaged` in `Engine._execute_actions` (`engine.py:243`) and abort with an event; act on `poll()`; port legacy `wallee/safety/estop.py` into the Prusa pack as the registered trip callback; beat the heartbeat on every engine pass including failure paths; replace the placeholder service with a real independent watchdog process (heartbeat via file/DB, stop via direct PrusaLink call).

### 3.2 Approval workflows are dead ends (both trees)
- **v6:** `engine.py:281-291` sets `WAITING_APPROVAL` on a fresh `action_run_id`, immediately checks `has_valid_approval` (which can never be true for a brand-new run id), and breaks. No code path ever revisits blocked runs (`WAITING_APPROVAL` appears only at `engine.py:281`, `models.py:40`, `runtime_db.py:33,37`). `operator_cli.py approve` records approvals nothing consumes.
- **Legacy:** Gate 2 (deadline) runs before Gate 3 (approval) (`wallee/engine/dispatch.py:139-160`) and re-evaluates age against the original `created_mono` when a `WAITING_APPROVAL` action is re-processed after approval (`dispatch.py:213-217`). With `cancel_print` at `max_proposal_age_ms=30000` (`prusa_link/actuators.py:200`) and a 300 s approval window (`config.py:20`), **any approval later than ~30 s is rejected as expired**.

**Fix (v6):** on cycle start, scan `WAITING_APPROVAL` runs, re-check approval, resume dispatch or expire to `ABORTED`; add an end-to-end test. **Fix (legacy):** skip the deadline gate on approved re-processing, or reset the deadline clock at approval time.

### 3.3 Legacy ESTOP is a TTL'd flag, and heartbeat loss triggers nothing
- `/estop` writes `safety.estop` with `HUMAN_ESTOP_TTL_S` = 600 s (`wallee/human/telegram.py:422`, `cli.py:144`, `config.py:87,134`). After expiry, the engine gate (`dispatch.py:120`) passes again and the kernel's alert latch resets (`safety/kernel_main.py:68-76`) — the machine can be commanded again without any human clearing the stop. ESTOP must be **latched** (no TTL), cleared only by explicit human action, with the kernel re-sending the stop while latched.
- On heartbeat loss the kernel only sets `safety.agent_stale`/`safety.engine_stale` — keys **nothing in the codebase reads** — and takes no stop action (`kernel_main.py:83-111`). Both agent and engine heartbeats are published from dedicated side threads (`agent/loop.py:325-332`, `engine/dispatch.py:56-63`), so a wedged control loop keeps "alive" forever; the watchdog only detects full process death. Publish heartbeats (or a progress counter) from the work loops themselves, and make sustained heartbeat loss during an active job trigger the stop + outbox escalation.
- `main.py` never supervises the kernel subprocess (`safety_proc` is never polled) — the watchdog can die silently. Also, API keys are passed to it on the command line (`main.py:134`), visible in `/proc/*/cmdline`.

### 3.4 v6 wipes its own crash journal, and there is no reconcile
`WALLEE_FLUSH_STATE_ON_START` defaults to `"1"` (`config.py:140-141`), so `reset_runtime_start_state()` (`main.py:243-257`) deletes all plans, action_runs, exec_journal, events, and the WAL/SHM files on every start — defeating the entire stated purpose of the journal ("did a move start before power died?", `runtime_db.py:66-69`). Legacy's `engine/reconcile.py` has no v6 equivalent (zero hits for "reconcile"). **Fix:** default the flush to off; on startup scan `exec_journal` for `IN_FLIGHT` → mark `UNKNOWN` and escalate to the human; store wall-clock alongside monotonic timestamps in the journal (monotonic values are meaningless across restarts — exactly the case the journal exists for).

### 3.5 Production `NameError` in the v6 planner
`planner.py:385-387` calls `_action_family_from_id` / `_action_direction_from_id` / `_action_magnitude_from_id`, which live in `models.py:1437-1463` but are not imported (`planner.py:17` imports only `PlanIR, WorldPacket`). Any EXECUTE plan carrying a `tuning_choice` crashes at runtime. `ruff check wallee_v6` flags all three as F821 — but ruff never runs on v6 (CI lints only `wallee scripts`, `.github/workflows/ci.yml:31`). Fix the import, add a test for `_tuning_choice_resolves_cleanly`, and point lint/CI at the tree (§7).

### 3.6 CI is red on `main`; the flagship tree has never run in CI
- Job 1: bare `pytest -q` from root fails at collection (`wallee_v6/tests/conftest.py:7` can't resolve `wallee_v6.config`; CI never installs the v6 package or its deps).
- Job 2: `scripts/check_docs.py` exits 1 with ~36 `local absolute link: <author-laptop-path>/...` errors (README.md:16-23, ARCHITECTURE.md:3, the prusa_core_one_plus pack docs).
- Three v6 tests hardcode `<author-laptop-path>/...` (`wallee_v6/tests/test_append_experiment_run.py:116,208`, `test_append_stock_experiment_run.py:35`) and `scripts/append_experiment_run.py:600` defaults `--ssh-key` to a personal key path. These have plainly never run anywhere but one laptop.

**Fix:** derive script paths from `Path(__file__)`; scope root pytest (`testpaths`/`norecursedirs`) and add a dedicated v6 CI job (`pip install -e "wallee_v6[dev]" && pytest wallee_v6/tests` + `ruff check wallee_v6`); fix the 36 links; make CI required on `main`.

### 3.7 Privacy/identifier leaks in public docs
- `wallee/device_packs/prusa_link/CAPABILITIES.md:6-21` leaks the printer **serial number, UUID, MAC address**, home-LAN topology, and a **Tailscale IP** (not under `docs/internal/`, no historical banner); `:723` leaks `/home/<user>/...` paths.
- `docs/internal/CLAUDE.md:271-277` leaks operator infrastructure (`ssh <user>@<PI_WIFI_IP>`, printer IP, `redis-cli FLUSHALL` against that host).
- Hardcoded `<PRINTER_IP>` also appears in current v6 docs (`wallee_v6/README.md:110`, pack `SETUP.md:30`).
- No API keys are leaked (verified by grep). Redact the identifiers and parameterize the IPs.

### 3.8 The unauthenticated dashboard streams cameras to the LAN (legacy)
`DashboardServer` binds `0.0.0.0` (`wallee/main.py:212`, `ui/dashboard.py:510`) with no auth or TLS on HTTP or WebSocket; the WS pushes the entire whiteboard every second (`dashboard.py:598-616`) including live camera frames, filenames, and human intents. Default to `127.0.0.1`, add token auth on the WS handshake, make bind configurable. (v6's FastAPI dashboard is currently dead code, but if wired in it has the same problem plus it reaches into `runtime_db._conn` and triggers a printer-polling `compile()` per unauthenticated request — `wallee_v6/wallee_v6/dashboard.py:25,33`.)

---

## 4. Architecture

### 4.1 The two-tree problem (the single largest ongoing tax)
Carrying two full implementations (10.2k + 21.3k LOC, 517 + 262 tests, two packaging systems with **conflicting pins** — root allows Pillow 12.x while v6 pins `<12`; root's unbounded pytest resolves 9.x violating v6's `<9`) in one repo, with docs/CI/CONTRIBUTING covering only the old one, confuses every newcomer and splits maintenance. Recommendation:

1. Declare v6.5 the only maintained line (the README already implies it).
2. Freeze `wallee/` — archive to a branch (`legacy/v5`) or keep read-only with a prominent status banner — after porting the two things v6 actually needs from it: the **ESTOP transport** (`wallee/safety/estop.py`) and **startup reconcile** (`wallee/engine/reconcile.py`). The Telegram gateway and event-driven wake are worth porting later.
3. Promote `wallee_v6` to the repo root as the installable package, one `pyproject.toml`, one environment.

### 4.2 Legacy tree — structural issues worth knowing even if frozen
- **Hardware leakage into the "generic" core:** the engine hardcodes `resume_print` (`dispatch.py:126`); the agent loop hardcodes Prusa phases, key names, and defect keywords (`loop.py:130-169,367-376,604-605`); the parser hardcodes active states (`parser.py:103`); registry wake triggers hardcode `printer.*` keys (`registry.py:200-212`). Worst instance: CLI/Telegram ESTOP reads `PRUSALINK_*` env directly (`cli.py:145`, `telegram.py:423`), bypassing the `SafetyProfile` device packs build — **with a non-Prusa pack, human ESTOP stops nothing**.
- **Global-singleton config pattern** (`configure_check_intervals`, `configure_web_search`, `configure_vision`, `set_call_human_fn`, module-level HTTP singletons) defeats testability and multi-instance use; replace with constructor injection.
- **Redis is a single point of failure** with no degraded mode: engine gates, TOCTOU prechecks, heartbeats, and ESTOP all die together. Define explicit behavior for "Redis lost while PRINTING" (e.g., direct stop after N seconds).
- **Layering violation:** engine reaches into `ledger._lock`/`ledger.conn` (`dispatch.py:42-48`).
- `main.py` is a 280-line god-function wiring everything; extract a composition root for testability.

### 4.3 v6 tree — the conceptual upgrade is real; the seams leak
The compile → frontier → Plan IR → validate → execute pipeline is a genuine improvement: the LLM's action language is closed, and legality is enforced outside the prompt. The `DISPATCHED`-before-execute / `IN_FLIGHT`-before-side-effect double barrier is correctly implemented once in `packs/base.py:99-105`, and the status state machine is enforced (`runtime_db.py:31-56`). But:

- **Prusa has colonized the core.** `models.py` — the "core data contracts" module — hardcodes ~60 `printer_1.*` fact keys and Prusa tuning families across ~700 lines of `decision_signals` compilation (`models.py:875-1600`); `planner.py:392-452` hardcodes `A_PRUSA_PAUSE` suppression policy inside the OpenRouter adapter; `engine.py:35-48,497-529`, `planning_context.py:52-64`, and `main.py:181-201,413-466,605-764` are saturated with `printer_1.*` plumbing. Move signal compilation behind a pack-provided `planner_signals(world)` hook and parameterize the device id.
- **Two competing definitions of "tuning action":** `verb.startswith("TUNE_")` (`planning_context.py:67-68`, `main.py:351,359`) vs action-id `_TRIM_` parsing (`models.py:1472`) — and they disagree for the only real pack. Unify on one grammar module.
- **Device-ID collision:** `sim_printer` and `prusa_core_one_plus` both claim `DEVICE_ID = "printer_1"`; enabling both merges their facts. Reject duplicate claims at registry load.
- **Prompt-frontier vs executable-frontier mismatch:** `prompt_view()` truncates to `frontier_limit` (`models.py:187-189`) but `validate_plan` accepts anything in the full frontier (`engine.py:127`) — the planner can be "validated" against actions it was never shown.
- **Blocking LLM calls freeze the whole loop** (20 s timeout × 3 retries) including safety heartbeats — another reason the watchdog must be out-of-process.
- **Unbounded memory:** `InMemoryWhiteboard._changes` grows forever (`whiteboard.py:49,57,68`) — a leak under `--forever`. Ring-buffer it.
- Doc/code mismatch: README/AGENTS.md say schema errors are "retried once"; the default is 3 (`config.py:127`, `main.py:267`).

---

## 5. Schema design

### 5.1 SQLite (both trees)
- **No migration tracking table in either tree.** Legacy re-executes every migration each boot and treats "duplicate column" as success (`wallee/ledger/db.py:28-41`) — multi-statement migration files silently half-apply. v6 does ad-hoc `PRAGMA table_info` + `ALTER` (`runtime_db.py:158-174`). Both need a `schema_migrations(version)` table.
- **Missing indexes.** Legacy: `approvals(action_id)`, `actions(chain_id, chain_seq)`, `events(message, ts)` (scanned every agent cycle via `db.py:230-239`), `actions(updated_ts)`. v6: `action_runs(status)`, `action_runs(run_scope, updated_ts_ms)` — all current lookups scan.
- **Unbounded growth + O(N)-per-cycle scans (legacy):** `ExternalChangeDetector` fetches *every DONE action ever* each cycle (`agent/change_detector.py:54`). Add a `since_ts` filter and a retention job.
- **Audit-hostile details:** legacy `events.action_id` is never populated (ids smuggled in message text, `db.py:127-130,193`); approvals have no state guard or uniqueness (Telegram can "approve" a DONE action, `db.py:197-210`, `telegram.py:500-522`); v6 `store_plan` uses `INSERT OR REPLACE`, silently overwriting audit records (`runtime_db.py:202`); approvals decision is unchecked TEXT.
- **Broken episode boundary (legacy):** episodes are delimited on `WAIT`/`CALL_HUMAN` events, but `record_call_human` is never called — CALL_HUMAN no longer starts an episode (`db.py:108-110,233-234` vs `loop.py:624-633`).

### 5.2 Redis whiteboard (legacy)
- Namespacing is convention-only and `read_all()` scans `*` (`whiteboard/client.py:110`) — any co-tenant app pollutes the agent's prompt. Prefix keys (`wallee:*`) and scan with a match.
- Camera frames as base64 in top-level keys ride along in every `read_all()` just to be filtered out later (`prompt.py:218-227`); move them out of the scan path.
- `read_all_with_trends` does O(keys) LRANGE calls per cycle (`client.py:124-132`); cache which keys have history.
- Three modules bypass the Whiteboard abstraction and touch `wb.r` directly (`telegram.py:623-626`, `cli.py:58`, `state_manager.py:57-63`).
- `human.urgent` is published but never consumed by agent or engine — a no-op feature beyond waking the agent.

### 5.3 v6 fact-key design
`"{device_id}.{namespace}.{fact}"` works, but keys are stringly-typed with no central registry: the same fact is probed under multiple aliases at read time (`raw_observed_at` **or** `raw.observed_at`, `models.py:1217-1219`; `requested_file` from three keys, `pack.py:942-946`); multi-value facts are pipe-joined strings re-parsed everywhere; whole JSON documents are stuffed into single string facts (`active_notes_json`, the eleven `*_json` artifact facts in `main.py:706-746`). Predicates fail closed on missing facts — good — which means a typo'd key **silently makes actions illegal**. Add a typed FactKey helper or per-pack fact-schema declaration.

### 5.4 JSON Schemas (v6)
- `plan_ir.schema.json`: good closed design, but no `$schema`/`$id`/version; `sequence` lacks `maxItems`/`uniqueItems` (only pydantic enforces them), and three different horizon limits coexist (schema ∞, pydantic 3, prompt contract 1).
- `world_packet.schema.json` is hand-maintained as an ~800-line Python dict (`models.py:235-774`) equality-tested against disk — great sync discipline, wrong source of truth. Generate it from pydantic `model_json_schema()` and delete ~540 lines. `last_result` is the one `additionalProperties: true` hole.
- `manifest.schema.json`: `detection`/`resources` accept unknown keys, so a typo'd detection key silently disables a pack.

### 5.5 Decision format (legacy)
- `params` must be a JSON *string* the parser re-parses (`llm_client.py:53,63`, `parser.py:40-58`) — double-encoding with a silent `{} `-on-failure path. Prefer a real object, or per-tool param schemas validated at parse time (tool metadata currently has none — errors surface as `TypeError` at dispatch).
- `check_after_s: null` and `severity: null` are schema-legal but crash the parser/Telegram path (`parser.py:171`, `telegram.py:177`) despite the "never raises" contract.
- The response validator extracts a `{...}` substring but returns the original text, which the parser won't extract (`llm_client.py:89-96` vs `parser.py:130-136`) — "valid" responses can still parse to WAIT.

---

## 6. Code quality

### 6.1 File-size hot spots (v6)
- `hardware_smoke.py` — **4,827 lines** whose docstring claims "intentionally small". Four near-identical per-family phase-1 experiment functions + three near-identical session runners + benchy validation + audit reporting + a giant argparse `main()`. The `_ManagedFamilySpec` table (`:40-109`) proves the parameterization already exists — apply it and split into a `smoke/` package (~2,500-line reduction).
- `pack.py` — 3,247 lines; `normalize()` is ~380 lines of sequential unpacking, `_realize()` is a 200-line if-chain of 30 branches that a dispatch table collapses to ~30 lines, six `_derive_*_shadow_state` methods are structural clones. Split into `state_normalizer.py` / `frontier.py` / `realize.py` / `tuning_policy.py`.
- `models.py` — 1,662 lines; ~700 lines of Prusa prompt-signal compilation belong in the pack.
- `main.py` — 1,251 lines mixing CLI, tee-logging, archiving, artifact assembly, and the control loop; `_world_artifact_payload` and `_write_runtime_state` duplicate payload structure field-by-field.
- `driver.py:241-536` — six clones of one read→clamp→write→verify pattern; one parametrized `_set_scalar` with a per-family spec.

### 6.2 Cross-cutting quality issues
- **No `logging` anywhere in the v6 runtime** — output is `print()` plus a global stdout tee (`main.py:33-50`); pack/driver layers have no diagnostics channel at all. Legacy logs, but without `exc_info=True` on the two most important handlers (`loop.py:865-867`, `dispatch.py:236-237`).
- **Computed-but-unused failure escalation (v6):** `consecutive_cycle_failures` is tracked and recorded but never acted on (`main.py:30,1211-1217`) — no interlock, no shutdown, no alert after persistent failure.
- **Duplication:** `_float_or_none` ×5 (`models.py:1516`, `planner.py:525`, `main.py:469,540`, `pack.py:3214`, `hardware_smoke.py:564`); `_run_scope_from_world` ≡ `_run_scope_from_facts` verbatim (`engine.py:35-48` vs `planning_context.py:52-64`); `_normalize_strength`/`_normalize_issue_level` defined in both `models.py:1393-1404` and `engine.py:539-546`; legacy precheck/execute validation duplicated verbatim per actuator with drift-prone bounds tables (`prusa_link/actuators.py:73` vs `:290`); legacy dashboard `_run_http`/`_run_http_only` copy-paste.
- **Dead code (legacy):** `Ledger.record_call_human`, `_deliver_safety_message`, the superseded `safety/kernel.py` SafetyKernel (two divergent implementations of the safety-critical component is itself a risk), `discover.py`, legacy `build_prompt`, `change_detector.format_for_prompt`, the `human.urgent` flag.
- **Notable correctness bugs (legacy) beyond §3:** the 90 s rejection cooldown can never trigger (its "human rejection" classifier excludes the only string the engine writes — `loop.py:367-386` vs `dispatch.py:159`); stale-decision discard skips the delay reset so the loop sleeps up to 120 s instead of retrying (`loop.py:810-813`); `cancel_event` is only checked *after* the blocking HTTP call — "cancel in-flight LLM call" can take 3 minutes (`llm_client.py:176-181`); `_archive_job_context` has an unbound-variable path (`loop.py:232-269`); the kernel's fault-monitor compares JSON-encoded values to the string `"0"`, so a monitor reporting `0.0` false-positives an ESTOP (`kernel_main.py:123`).
- **Typing:** both trees are annotated but nothing enforces it — no mypy/pyright config anywhere. The v6 F821s are exactly the class of bug a type-checker/linter catches. Start with `wallee_v6` (already well-annotated), add `py.typed`.
- **Magic numbers** throughout both trees (cooldowns, grace periods, clamps, vision hysteresis) — move to config/tool metadata; v6's `PLANNER_RETRY_ATTEMPTS` module constants shadow same-named config fields (`main.py:28-30` vs `config.py:127-132`).

---

## 7. Testing, CI, and engineering process

### 7.1 Current state (verified by running everything)
- Legacy: `pytest tests/ -q` → 361 passed; full legacy scope (`--ignore=wallee_v6`) → **517 passed** (README's claim is accurate but only reproducible with an undocumented ignore flag).
- v6: `pip install -e wallee_v6 && cd wallee_v6 && pytest` → **3 failed, 259 passed** (the three hardcoded-path tests, §3.6).
- v6's test architecture is notably better than legacy's: `conftest.py` boots the whole runtime against sim packs in `tmp_path` — real integration tests, no sleeps, 13.7 s. Legacy has good mocking discipline (fakeredis, patched httpx) but flaky patterns: hardcoded ports 18765-18767 + `sleep(0.5)` in `test_dashboard.py`, 17 timing sleeps across `test_agent_loop.py`/`test_registry.py` (blocks `pytest -n auto`, causes CI flakes).
- **Coverage gaps that matter:** v6 has zero tests for `dashboard.py`, `operator_cli.py`, `runtime_control.py`, `openrouter_observability.py`; exactly one safety test — and nothing tests that a tripped interlock blocks execution (nothing implements it, §3.1); no test for approval resume (path doesn't exist) or for the journal-wipe behavior.

### 7.2 CI/tooling gaps
- No v6 job (tests, lint, or contract checks), no Python version matrix (3.11 claimed, never tested), no coverage, no type checking, no security scanning (pip-audit/bandit/CodeQL), no dependabot/renovate, no pre-commit hooks (which would have stopped the `<author-laptop-path>/...` paths), no `concurrency`/`timeout-minutes`/`permissions` hardening in the workflow.
- **No ruff config anywhere** — CI runs ruff's minimal defaults (E4/E7/E9/F only). Add a `[tool.ruff]` with a real ruleset (bugbear, isort, pyupgrade) + `ruff format`.
- The custom checkers are genuinely good (`check_docs.py` link validation; `check_repo_contract.py` asserts every decorated tool is registered *and documented in the pack README*) — but they only cover the legacy tree, and `check_docs.py` only parses markdown links, so backtick references to the nonexistent `wallee_v6/docs/` escape it.

### 7.3 Eval process
- The legacy replay harness (60 scenarios, real prompt builder + parser, deterministic type/tool scoring, a model failure-taxonomy REPORT.md at 47/60) is a solid design that is: not CI-runnable (live OpenRouter, costs money), single-sample (no pass@k for a stochastic system), transport-divergent from production, and trend-blind. The v6 `planner_eval.py` (offline scoring of recorded planner artifacts) is the right direction but its case corpus lives in the uncommitted `docs/evidence/` tree.
- See §9.6 for the recommended three-layer eval stack.

### 7.4 Versioning/release process
Zero git tags, zero GitHub releases; versions are *directories*; changelogs stop at v4 (in `docs/internal/`); the only machine-readable version is `wallee_v6/pyproject.toml`'s `0.6.5`. Tag `v6.5.0` now; adopt tags + Releases + a root CHANGELOG going forward. Fix `pyproject.toml:12`'s author field ("OpenAI generated scaffold for Wallee v6.5") if the repo is meant to be citable.

---

## 8. Documentation quality

### 8.1 Broken and wrong (fix first)
- ~36 absolute `<author-laptop-path>/...` links across 6 files (README.md:16-23, ARCHITECTURE.md:3, `wallee_v6/README.md:114`, pack README/CAPABILITIES/TESTING) — the repo's own CI check enumerates every one.
- `wallee_v6/docs/` **does not exist**: the v6 architecture doc, ADRs (`AGENTS.md:50-52` tells contributors to update ADRs in a directory that isn't there), HUMAN_GUIDE, a PDF guide, and all `docs/evidence/**` artifacts underwriting the pack's "direct-hardware-proven / managed-wallee-proven" status matrix are referenced but absent. Commit them (redacted as needed) or strip every reference — as-is, the pack's central evidence-based claims are unverifiable.
- Quick-start errors: `cd Wallee2` (README.md:230; the clone dir is `wallee`); the v6 quick start never says `cd wallee_v6` so `pip install -e ".[dev]"` fails from root; Redis is required but never listed as a prerequisite in the quick start.
- Stale counts: "19 sensor publishers" is 20 (verified by loading the registry); "Mermaid blocks in root docs: 6" is 8; test counts are per-tree only and never mention v6's 262.
- Contradictions: legacy `CAPABILITIES.md` documents pause/resume via `PUT /api/v1/job` as the "Best Method" while the internal docs and shipped pack use `M25`/`M24` (the printer 405s the PUT); the same file claims approval on five tools where the code has it on two; v6 pack docs give two different trim envelopes (65..135 vs 75..125) in the same file.
- The README's flagship claim — safety kernel "runs as an independent process… if the agent crashes, safety keeps running" — must be reconciled per-tree: it is true (but partial) for legacy, currently false for v6 (§3.1).

### 8.2 Structure and gaps
- The two-tree story is incomprehensible to a newcomer: root README says "start in wallee_v6", then spends ~280 lines describing only the legacy system without labels. Rewrite around a "Which tree do I want?" table + a terminology mapping (whiteboard/ledger/episode ↔ world packet/runtime_db/PlanIR/frontier).
- `docs/internal/` contains a colliding second set of `AGENTS.md`/`CLAUDE.md` describing the *other* tree — a coding agent pointed at the repo can pick up the stale spec. Rename (`AGENTS_v4_legacy.md`) or move out.
- Missing: root CHANGELOG, roadmap, schema reference for `wallee_v6/schemas/*.json`, glossary (the v6 status vocabulary is copy-pasted into six files), deployment doc for the systemd units, docs index.
- SECURITY.md is thin for a physical-hardware project: no threat model (prompt injection via filenames/notebook/web results, LAN exposure of PrusaLink/Redis/dashboard, Telegram hardening), no disclosure SLA.
- CONTRIBUTING.md and the PR template never mention `wallee_v6` — the commands they mandate are broken on current main.
- The strongest docs — `DEVICE_PACK_GUIDE.md`, `AGENT_PROMPT.md`, `ARCHITECTURE_AUDIT.md` (exemplary honesty), the v6 pack's phased hardware-testing discipline — are genuinely excellent and worth advertising; the replay harness's 47/60 result deserves to be in the public Validation narrative rather than hidden (green pytest tables alone overstate the maturity).

---

## 9. Security and prompt-injection surfaces

Beyond §3.7/§3.8:

1. **`remember` → OBSERVATIONS.md → system prompt** is the highest-leverage injection path in the legacy design: one injected `remember()` call (steerable via web_search output or a malicious print filename) plants attacker text into the **system prompt of every future cycle**, persistently. Gate `remember` behind approval, sanitize/expire entries.
2. **`web_search` results** are rendered into the prompt unfenced (`prompt.py:250-262`); fence all tool results as explicitly untrusted data.
3. **Filenames** from the printer flow into the system prompt (JOB_CONTEXT) and v6 facts/action descriptions; sanitize length/charset.
4. **v6-specific:** G-code-derived notebook notes sit in the planner context; and the vision model's free-text summary is not only in-context but **keyword-matched by deterministic guards** (`planner.py:443-445,510-522`) — a hallucinated or injected "spaghetti" flips suppression logic. Never route model free-text into deterministic matchers; use the structured findings enum only.
5. **Telegram group-chat gap:** with a group `TELEGRAM_CHAT_ID` and no explicit allowlist, `allowed_user_ids` stays empty and any group member can approve, ESTOP, and inject intents (`telegram.py:71-77`). Refuse to start in that configuration. Note also that intent text can steer non-approval tools — e.g. `set_temperature` to 300 °C needs no approval (`actuators.py:73,267`).
6. Planner request/response payloads and **wholesale response headers** are persisted into artifacts and replay bundles (`openrouter_observability.py:61-68`, `vision.py:1062-1086`) — allow-list headers before a future auth echo lands on disk.
7. `wallee-main.service` has no `EnvironmentFile=` (can't receive keys) and can't start at all (missing required `--goal` and mode flag); no hardening directives (`ProtectSystem`, `NoNewPrivileges`).

The gate-side containment (frontier boundary, fail-closed predicates, driver-level clamps, no raw G-code from the LLM) is genuinely good and is the right place to keep fighting this battle — which is also the industry consensus (OWASP MCP03 mandates client/host-side gates; see §10.5).

---

## 10. Research & technology advances worth adopting (2024–2026)

Full citations verified against primary sources on 2026-07-02.

### 10.1 Published safety architectures now match Wallee's design — cite them and close the gaps
- **RoboGuard** (UPenn, RA-L 2026; arXiv:2503.07885) — LLM plans checked/repaired by deterministic temporal-logic synthesis; unsafe-plan execution &lt;3% under jailbreak. **AgentSpec** (ICSE 2026; arXiv:2503.18666) — a trigger/predicate/enforcement rule DSL interposed in the agent loop (Wallee's gate chain, formalized). **SELP** (ICRA 2025; arXiv:2409.19471) and **Safety Chip** (ICRA 2024; arXiv:2309.09919) — LTL verification of LLM plans.
- **Standards:** ISO/IEC TR 5469:2024 recommends exactly Wallee's pattern (non-AI backup + supervisor function); successor TS 22440 in CD stage. **EU Machinery Regulation 2023/1230 applies 2027-01-20**: ML-based *safety components* become high-risk — keeping the LLM strictly out of the safety function (and documenting that boundary) avoids the classification. Wallee's kernel is the Simplex/run-time-assurance pattern (ASTM F3269); embedded MLTL monitors (R2U2) run on Pi-class hardware.
- **Concrete adoption:** (a) add an LTL/MLTL invariant layer over Plan IR ("never heat bed while door open", "extruder temp monotone during cooldown") checked mechanically post-schema; (b) **KnowNo-style conformal escalation** (CoRL 2023; arXiv:2307.01928) — sample the model K times, build a calibrated prediction set over candidate actions, route to the human-approval gate exactly when the set is non-singleton — replacing ad-hoc ask-heuristics with a statistical guarantee; (c) document the non-AI safety boundary explicitly for TR 5469 / EU MR positioning.

### 10.2 Structured outputs: retire parse-from-text
Provider-enforced structured outputs are now universal: OpenAI strict mode (2024), **Anthropic structured outputs GA 2026-01-29** (strict tool use + JSON output format), and OpenRouter passthrough (`response_format` + `require_parameters: true`, models filtered by `supported_parameters=structured_outputs`). Local models get the same guarantee via llama.cpp JSON-schema→GBNF / llguidance / XGrammar. The constrained-decoding-hurts-reasoning result was substantially rebutted (dottxt "Say What You Mean"); best practice is a `reasoning` field *first* in the schema. **Adoption:** send the action/Plan IR schema as strict `response_format` through OpenRouter; keep the local validator as belt-and-suspenders; target the cross-provider schema subset (object root, all-required, `additionalProperties: false`, no numeric bounds — enforce ranges in the gates, where they belong anyway). This eliminates the double-encoded `params` string and the null-crash class (§5.5) outright.

### 10.3 Local/edge inference: a Pi-resident safety critic is now feasible
Qwen3.5-4B / Gemma 4 E2B class models (2026, Apache 2.0, native function calling) run at ~5–12 tok/s on Pi 5 CPU under llama.cpp; sparse MoE (Qwen3-30B-A3B compressed) hits ~8 tok/s on the 16 GB Pi 5. BFCL data shows 3–4B models are reliable on single-turn well-schema'd calls (~75–88%) but collapse on multi-turn planning — exactly the profile of a **safety-critic** ("state + proposed action → approve/risk/reason"), not a planner. **Adoption:** grammar-constrained local critic as (a) a second independent opinion on every cloud-proposed action (disagreement → approval gate), and (b) a degraded-mode fallback constrained to a conservative action enum (pause/cooldown/alert/no-op) when the cloud is unreachable — directly fixing the "Redis/cloud down = blind" gap. Skip the AI HAT+ 2 for LLM duty (CPU beats it; it's a vision accelerator); a $249 Jetson Orin Nano Super is the drop-in upgrade if critic latency binds.

### 10.4 Vision: hybrid local-filter → VLM-escalation
The 2025–2026 convergence: cheap high-recall local CNN (Obico's AGPL YOLO-family detector, or YOLOv11s ~0.83 mAP on extrusion defects; Hailo-8L-acceleratable) as a continuous per-second filter, escalating flagged frames to a frontier VLM for low-false-positive confirmation and the pause/abort decision. Wallee's exact architecture is now peer-reviewed (**LLM-3D Print**, arXiv:2408.14307, *Additive Manufacturing* 2025). VLM economics changed: Flash-class models cost ~$0.0003/frame — a 10-hour print at 1 frame/30 s is &lt;$1 — and Gemini-class models return **bounding boxes** that can be cross-checked deterministically against the expected print region. **Adoption:** local filter + multi-frame escalation + strict JSON verdict schema + box-vs-G-code cross-check; false-positive management (not cost) is now the design constraint.

### 10.5 MCP and agent frameworks: standardize the boundary, not the brain
MCP is now Linux-Foundation-governed (Dec 2025; co-founded by Anthropic, Block, OpenAI), with structured tool output (2025-06-18 spec) that is essentially Wallee's schema-validated action contract, an async Tasks primitive (2025-11-25), and a registry. Hardware-behind-MCP is an accepted pattern (Home Assistant core integration, ros-mcp-server); the 3D-printer MCP niche is weak — a control-grade PrusaLink MCP server would be best-in-class. OWASP MCP03:2025 (tool poisoning) mandates the gates stay client/host-side — Wallee's layer, unchanged. Conversely, for the control loop itself, current practice (Anthropic's agent-engineering series; the checkpointing≠replay-safe-side-effects critique of LangGraph-style frameworks; OpenAI's Agent Builder shutdown after 8 months) favors **keeping the bespoke loop + durable state**. **Adoption:** wrap device packs as MCP servers (gates stay host-side; map print jobs to Tasks; don't build on deprecated Roots/Sampling/Logging); do *not* adopt an orchestration framework.

### 10.6 Evals: three layers, pass^k, judges never grade safety
Anthropic's eval doctrine (Jan 2026) + τ²-bench (arXiv:2506.07982) map one-to-one onto Wallee: written domain policy + tools + simulated device + programmatic reward, scored **pass^k** (all k trials succeed — the right metric when one bad action burns a printer). LLM-as-judge is least reliable exactly where safety needs it (JudgeBench: best judges ~64%). Embodied-safety suites (SafeAgentBench, RoboPAIR — 100% jailbreak success against three real LLM-controlled robots) double as regression tests *for the gates*. **Adoption:** Layer 1 — VCR-style recorded-traffic replay in every CI run (zero-cost, catches prompt/payload regressions; also makes the existing 60-scenario harness CI-runnable offline). Layer 2 — a τ²-style simulated printer with fault injection, programmatic graders over emitted actions and final state, pass^k-gated in CI, with saturated cases graduating to a pinned-at-100% regression suite. Layer 3 — a permanent adversarial set (hazard-rejection + jailbreak prompts) asserting the *gates* block every one, model-independent. Inspect AI (UK AISI) is the best-fit harness; commit the v6 planner-eval corpus into the repo.

### 10.7 Reliability engineering: cache-first prompts, cache-aware fallbacks, OTel
Prompt caching is near-free at Wallee's cadence (Anthropic reads 0.1×, 5-min TTL refreshes free on every hit; OpenAI 90% cached-input discount; OpenRouter passes through + sticky routing via `session_id`): structure prompts as static-prefix (system + tool docs + few-shot) / dynamic-state-last for ~90% steady-state input-cost reduction. Fallback chains should respect cache economics (retry same provider first; fall back across models only on sustained failure — each fallback is a full-price cache rewrite; pick fallbacks on *different underlying infrastructure*, per the 2025 AWS/Cloudflare outages). Anthropic's Sept 2025 postmortem lesson — "HTTP 200 ≠ correct output" — argues for per-cycle output-sanity metrics (schema-retry rate, gate-rejection rate, critic-disagreement rate) as first-class alerts. Instrument with OTel GenAI semantic conventions (spans per cycle: `invoke_agent` → `chat` → `execute_tool`) but pin the semconv version — attributes are pre-stable and have churned.

### 10.8 Predictive telemetry: a cheaper, earlier-firing safety lane
Pi-class time-series foundation models (IBM Granite TTM ~1M params, ~95 ms/forecast on CPU; Chronos-Bolt; Chronos-2 for native multivariate hotend+bed+current joint forecasts) enable forecast-residual anomaly detection: heater output up while temperature flat = clog/thermistor detachment, **minutes before visual spaghetti exists** — as a deterministic gate input and safety-kernel trigger. Caveat (AAAI 2025 workshop result): classical detectors often match TSFMs — keep an isolation-forest/STL baseline and let Wallee's own telemetry decide. When escalating anomalies to the LLM, render telemetry as a **chart image** — MLLMs reason better over plots than number lists (arXiv:2410.05440). Also adopt LLM-ADAM's idea (arXiv:2605.03328): static screening of any G-code before dispatch as a new gate.

---

## 11. Prioritized roadmap

### P0 — safety-critical correctness (days)
1. v6: wire the interlock into dispatch; act on `poll()`; port `estop.py` as the trip callback; fix heartbeat semantics; replace the placeholder safety service with a real watchdog process. (§3.1)
2. Both trees: fix the approval dead-ends (v6 resume scan; legacy gate-order/deadline reset) + end-to-end tests. (§3.2)
3. Legacy: latch ESTOP (no TTL); kernel acts on heartbeat loss during active jobs; supervise the kernel subprocess; stop passing keys via argv; route human ESTOP through the pack SafetyProfile; fix fault-monitor value parsing. (§3.3)
4. v6: default `WALLEE_FLUSH_STATE_ON_START=0`; add startup reconcile over `exec_journal`; wall-clock timestamps in the journal. (§3.4)
5. v6: fix the planner `NameError` (import the three helpers) + test. (§3.5)

### P0 — repo health (days)
6. Fix the ~36 broken links; commit or de-reference `wallee_v6/docs/`; fix `cd Wallee2` and the v6 quick start. (§8.1)
7. Fix the three hardcoded-path tests + ssh-key default; scope root pytest; add a v6 CI job (pytest + ruff); fix the 27 ruff findings; make CI required. (§3.6, §7)
8. Redact hardware identifiers/IPs/SSH details from public docs. (§3.7)
9. Dashboard: bind localhost by default + token auth. (§3.8)

### P1 — security & schema (1–2 weeks)
10. Prompt-injection hardening: fence tool results; approval-gate `remember`; sanitize filenames; stop keyword-matching vision free-text in deterministic guards; Telegram group-chat guard. (§9)
11. Schema: migration-tracking tables; missing indexes; retention + bounded change-detector scans; approval state guards; `INSERT` not `INSERT OR REPLACE` for plans; generate `world_packet.schema.json` from pydantic; version all schemas. (§5)
12. Adopt provider-enforced structured outputs via OpenRouter (strict `response_format` + `require_parameters`), reasoning-field-first; fix the parser null-crash class. (§10.2)
13. Cache-first prompt layout + sticky routing + cache-aware fallback chain. (§10.7)

### P2 — architecture & process hygiene (weeks)
14. Decide the two-tree question: freeze/archive legacy after porting estop + reconcile (+ Telegram later); promote v6 to root; one packaging story; tag `v6.5.0`; add root CHANGELOG. (§4.1, §7.4)
15. Evict Prusa from the v6 core (pack-provided `planner_signals` hook, device-id parameterization, one tuning-action grammar, reject duplicate DEVICE_IDs). (§4.3)
16. Split the god files (`hardware_smoke.py`, `pack.py`, `models.py`, `main.py`); table-drive `_realize` and the driver setters; dedupe helpers; introduce `logging` in v6; act on `consecutive_cycle_failures`; ring-buffer the whiteboard change log. (§6)
17. Tooling: ruff config + format, mypy on v6, pre-commit (incl. a no-`/Users/`-paths hook), dependabot + pip-audit, CI matrix/coverage/hardening; de-flake legacy tests (OS-assigned ports, event-based sync). (§7)
18. Docs: two-tree README rewrite + terminology map; glossary; CONTRIBUTING/PR template covering v6; SECURITY.md threat model; schema reference; deployment doc; rename colliding internal AGENTS/CLAUDE files; surface the 47/60 replay result honestly. (§8)

### P3 — research-grade upgrades (the differentiators)
19. Three-layer eval stack (VCR replay in CI; simulated-printer pass^k evals; adversarial gate-assertion suite). (§10.6)
20. Local Pi safety-critic + conservative-enum degraded mode. (§10.3)
21. Hybrid vision (local filter → multi-frame VLM escalation with bounding-box cross-check). (§10.4)
22. Forecast-residual telemetry lane feeding the gates; G-code static screening gate. (§10.8)
23. LTL/MLTL invariants over Plan IR + KnowNo-style conformal approval escalation; position the repo against RoboGuard/AgentSpec/TR 5469 in the README's research framing. (§10.1)
24. Expose device packs as MCP servers (gates stay host-side); keep the bespoke loop. (§10.5)

---

## 12. What to preserve

Through any refactor, these are the assets: the propose→gate→dispatch ledger state machine with diary-backed reconciliation (legacy `reconcile.py` is small and correct — port it, don't lose it); the closed frontier + Plan IR + post-verification pipeline (v6); the `IN_FLIGHT`-before-side-effect barrier implemented once in `packs/base.py`; the fail-closed predicate semantics; the prompt-contract and whole-runtime-in-`tmp_path` test patterns (v6); the device-pack guide + repo-contract checker; and the culture of honest limitation notes and evidence-graded capability claims. The architecture is the contribution — the work above is about making the code deserve it.
