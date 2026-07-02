# Changelog

All notable changes to this repository are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions track the maintained `wallee_v6/` line (SemVer, 6.x).

This file starts at the Phase-0 stabilization of the refactor described in
[`docs/REFACTOR_EXECUTION_PLAN.md`](docs/REFACTOR_EXECUTION_PLAN.md); earlier
history lives in the per-version changelogs under `docs/internal/`.

## [Unreleased]

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
