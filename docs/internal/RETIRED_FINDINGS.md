# Retired findings — legacy v5 tree disposition register

The legacy tree (`wallee/`, its root `tests/`, and the legacy toolchain
files) was deleted from the mainline in Phase 4 of
[`../REFACTOR_EXECUTION_PLAN.md`](../REFACTOR_EXECUTION_PLAN.md). Per that
plan, every legacy-only finding from
[`../PROJECT_ANALYSIS.md`](../PROJECT_ANALYSIS.md) is listed here with its
disposition, so "all issues resolved" is auditable rather than implied.

**Archive**: before this branch merges to `main`, cut `legacy/v5` from the
last `main` commit that still contains the tree and tag it `legacy/v5-final`
(the tree remains reachable in history regardless). The Pi keeps a
`legacy/v5` checkout for rollback per
[`../DEPLOYMENT.md`](../DEPLOYMENT.md).

**Safety hotfixes shipped before retirement** (Phase 0, on the tree that was
deployed): latched ESTOP (no TTL expiry), approval-after-deadline fix,
Telegram group-chat allowlist guard, kernel fault-monitor JSON decode,
dashboard localhost+token bind, API key via env not argv. These landed and
are in the archive; everything below is retired *without* a fix, with the
reason recorded.

## Disposition: known, retired with the tree

Analysis §4.2 — legacy structural issues:

- Hardware leakage into the "generic" core (engine hardcoded `resume_print`;
  loop/parser/registry hardcoded Prusa phases and keys; CLI/Telegram ESTOP
  read `PRUSALINK_*` env directly, bypassing the pack `SafetyProfile`).
  v6 answer: packs own hardware; the core-purity ratchet plus the P4
  contract tests enforce the boundary.
- Global-singleton config pattern defeating testability. v6 answer:
  constructor injection throughout (`Config.from_env` + explicit wiring).
- Redis as a single point of failure with no degraded mode. v6 answer: no
  Redis; in-process whiteboard + SQLite; the independent watchdog covers
  control-plane death.
- Engine reaching into `ledger._lock`/`ledger.conn` (layering violation).
  v6 answer: `RuntimeDB` owns its connection.
- `main.py` 280-line god-function wiring. v6 answer: `build_runtime`
  composition (further split tracked in Phase 4).

Analysis §5.1 — legacy SQLite:

- Migrations re-executed each boot, "duplicate column" treated as success.
  v6 answer: transactional `schema_migrations` framework (Phase 2.4).
- Missing indexes (`approvals(action_id)`, `actions(chain_id, chain_seq)`,
  `events(message, ts)`, `actions(updated_ts)`). v6 answer: indexed at
  migration time.
- `ExternalChangeDetector` O(N)-per-cycle full-history scans. No v6
  equivalent exists.
- `events.action_id` never populated; approvals without state guard or
  uniqueness. v6 answer: full lineage contract-tested
  (`test_every_executed_action_has_full_lineage`).
- Broken episode boundary (`record_call_human` never called). No v6
  episode concept; the exec journal is the audit spine.

Analysis §5.2 — Redis whiteboard: convention-only namespacing with `*`
scans; base64 camera frames riding every read; O(keys) trend reads;
three modules bypassing the abstraction; dead `human.urgent` flag. All
retired with Redis itself.

Analysis §5.5 — legacy decision format: double-encoded `params` JSON-string
with silent `{}` fallback; `check_after_s: null` crashing the
"never-raises" parser; validator/parser extraction mismatch making valid
responses parse to WAIT. v6 answer: strict `response_format` JSON schema +
pydantic `PlanIR` validation, no free-text parsing.

Analysis §6.2 — legacy correctness bugs beyond §3: the unreachable 90 s
rejection cooldown (classifier excludes the only string the engine
writes); stale-decision discard skipping delay reset (up to 120 s stalls);
`cancel_event` checked only after the blocking HTTP call; unbound-variable
path in `_archive_job_context`; missing `exc_info=True` on the two most
important handlers. All retired with the tree.

Analysis §6.2 — legacy dead code: `Ledger.record_call_human`,
`_deliver_safety_message`, the superseded second `SafetyKernel`
implementation, `discover.py`, legacy `build_prompt`,
`change_detector.format_for_prompt`, `human.urgent`. Deleted with the tree.

Analysis §3.8 — the legacy dashboard's LAN exposure was hotfixed
(localhost + token) in Phase 0 and is now retired with the tree; the v6
dashboard surface was separately deleted as dead code (Integration
Decision I-8).

## Carried forward (not retired)

- The 60-scenario replay corpus was translated to v6 world-packet key-space
  (`tests/replay/keymap.py`) before deletion, becoming a model-independent
  gate-assertion suite plus live planner-eval cases. Its honest 47/60
  baseline is preserved in
  [`REPLAY_BASELINE_2026-03.md`](REPLAY_BASELINE_2026-03.md).
- The Telegram operator channel did not move with the tree; the v6 port is
  a ROADMAP item, and until it lands the attended-operation rule from
  [`../../SECURITY.md`](../../SECURITY.md) applies.
