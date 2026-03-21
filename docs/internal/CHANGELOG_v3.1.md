# CHANGELOG v3.1

> Historical release notes. This changelog describes a past transition and may reference intermediate architecture states that are no longer current.

## P0-1
- **Bug ID:** `P0-1`
- **File(s) changed:** `wallee/engine/dispatch.py`, `wallee/tools/decorator.py`, `wallee/tools/registry.py`, `tests/test_engine.py`
- **What was wrong:** The engine executed actuator tools before approval and then executed them a second time after dispatch, so side effects could happen pre-approval or twice.
- **What you changed:** Reordered engine gates so approval/deadline/queue checks happen before execution, added an explicit side-effect-free precheck hook, and updated tests to assert a single execution.
- **Risk:** Tools that relied on execution-time validation now need explicit `precheck_fn` wiring if they must be rejected before dispatch rather than fail during execution.

## P0-2
- **Bug ID:** `P0-2`
- **File(s) changed:** `wallee/ledger/db.py`, `tests/test_ledger.py`
- **What was wrong:** Re-proposing the same tool and params deleted prior terminal actions, which silently destroyed ledger history.
- **What you changed:** Preserved terminal rows, rejected only active duplicates, and generated retry-specific idempotency keys for later re-proposals.
- **Risk:** Any downstream code that assumed idempotency keys never gain a retry suffix must now treat them as opaque identifiers.

## P0-3
- **Bug ID:** `P0-3`
- **File(s) changed:** `wallee/config.py`, `wallee/human/telegram.py`, `wallee/main.py`, `tests/test_telegram.py`, `tests/test_config.py`
- **What was wrong:** The Telegram bot trusted any inbound update and never checked whether the sender or chat matched the configured operator.
- **What you changed:** Added chat/user authorization checks, config support for allowed Telegram user IDs, and tests covering DM and group authorization paths.
- **Risk:** Group-chat deployments now need `TELEGRAM_ALLOWED_USER_IDS` configured if they want specific operator-only access instead of chat-wide access.

## P2-1
- **Bug ID:** `P2-1`
- **File(s) changed:** `wallee/config.py`, `wallee/agent/parser.py`, `wallee/agent/loop.py`, `wallee/human/cli.py`, `wallee/human/telegram.py`, `wallee/main.py`, `tests/test_config.py`, `tests/test_telegram.py`
- **What was wrong:** Timing-sensitive values such as human-signal TTLs, parser wait bounds, approval timeout, and last-decision TTL were scattered and too short for slower 30–120s agent cycles.
- **What you changed:** Centralized those defaults in config, wired them through main/parser/CLI/Telegram/agent loop, and raised human-facing TTLs and approval timeout to safer defaults.
- **Risk:** Deployments that depended on the old short expirations will now retain intents, urgent flags, images, and approval waits longer unless their environment overrides the new config values.

## P2-2
- **Bug ID:** `P2-2`
- **File(s) changed:** `wallee/agent/loop.py`, `tests/test_agent_loop.py`
- **What was wrong:** `CALL_HUMAN` decisions did not record an episode boundary even though the ledger’s episode query treats `CALL_HUMAN` as a reset point.
- **What you changed:** Recorded `CALL_HUMAN` events in the ledger from the agent loop and added a test that verifies the boundary event is persisted.
- **Risk:** Any tooling that implicitly assumed `CALL_HUMAN` would stay invisible in the events table now sees those operator-escalation entries.

## P0-4
- **Bug ID:** `P0-4`
- **File(s) changed:** `wallee/device_packs/prusa_link/actuators.py`
- **What was wrong:** PrusaLink actuators still relied on execution-time whiteboard validation, so after the engine safety fix they could be diary-written and dispatched before those checks rejected them.
- **What you changed:** Added side-effect-free precheck helpers for all stateful PrusaLink actuators and wired them through each tool’s metadata.
- **Risk:** The precheck helpers must stay behaviorally aligned with the actuator bodies, or a future code change could let the two validation paths drift.

## P1-1
- **Bug ID:** `P1-1`
- **File(s) changed:** `wallee/ledger/db.py`, `tests/test_ledger.py`
- **What was wrong:** A single SQLite connection was shared across multiple threads without synchronization, which could interleave operations or raise connection-level errors under load.
- **What you changed:** Wrapped all ledger SQLite access in an `RLock` and added a concurrent proposal test to verify the shared connection stays usable.
- **Risk:** The ledger now serializes DB access, so very high write rates may queue behind the lock instead of running concurrently.

## P1-2
- **Bug ID:** `P1-2`
- **File(s) changed:** `wallee/engine/dispatch.py`, `wallee/main.py`, `tests/test_engine.py`
- **What was wrong:** Approval-required actions entered `WAITING_APPROVAL` without notifying Telegram, so remote operators often never saw the request before it timed out.
- **What you changed:** Added an engine-level approval notifier hook, wired it to Telegram startup, and covered one-shot notification and notifier-failure behavior with tests.
- **Risk:** If a deployment swaps in a custom notifier, notifier exceptions are now logged and suppressed, so operators must watch logs if approvals stop appearing.

## P0-5
- **Bug ID:** `P0-5`
- **File(s) changed:** `wallee/engine/dispatch.py`, `wallee/human/telegram.py`, `wallee/safety/kernel.py`, `tests/test_engine.py`, `tests/test_telegram.py`, `tests/test_safety_kernel.py`
- **What was wrong:** CLI and Telegram published different ESTOP keys and no runtime component consumed the Telegram path, so an operator could believe ESTOP was active while the engine kept dispatching actions.
- **What you changed:** Standardized on `safety.estop`, blocked engine dispatch when it is set, and made the safety kernel alert on active ESTOP with deduplicated recovery-aware behavior.
- **Risk:** Any external tooling still writing or reading `human.estop` must be updated to `safety.estop` or it will no longer trigger the canonical stop path.

## P1-3
- **Bug ID:** `P1-3`
- **File(s) changed:** `wallee/whiteboard/client.py`, `wallee/tools/registry.py`, `tests/test_whiteboard.py`, `tests/test_registry.py`
- **What was wrong:** Whiteboard publishes and sensor multi-key snapshots were written as separate Redis commands, so concurrent writers could leave the current value, history ring, and sibling keys temporarily out of sync.
- **What you changed:** Added transactional `publish()` and `publish_many()` helpers backed by Redis pipelines and switched sensor loops to publish multi-key payloads atomically.
- **Risk:** Any custom whiteboard wrappers that only implemented `publish()` now need a compatible `publish_many()` method if they are used with sensor loops.

## P1-4
- **Bug ID:** `P1-4`
- **File(s) changed:** `wallee/whiteboard/client.py`, `tests/test_whiteboard.py`
- **What was wrong:** `read_all()` used `KEYS *`, which can block Redis and stall the dashboard or agent snapshot path under larger keyspaces.
- **What you changed:** Replaced the full-keyspace scan with incremental `SCAN` iteration and added a regression test that fails if `KEYS *` is called again.
- **Risk:** `SCAN` does not guarantee ordering, so any downstream code that accidentally depended on deterministic read-all key order must sort keys explicitly.

## P2-3
- **Bug ID:** `P2-3`
- **File(s) changed:** `wallee/ledger/db.py`, `wallee/agent/prompt.py`, `tests/test_ledger.py`, `tests/test_prompt.py`
- **What was wrong:** The current episode query and prompt formatter could pull in arbitrarily many actions plus raw `result_json` and `error_json`, causing token bloat after long runs.
- **What you changed:** Bounded episode retrieval to recent actions only and summarized long reasons, result payloads, error payloads, and state JSON before inserting them into the prompt.
- **Risk:** Older actions beyond the new episode cap are no longer shown verbatim to the LLM, so workflows that implicitly relied on very long action history may need explicit summarization later.

## P2-4
- **Bug ID:** `P2-4`
- **File(s) changed:** `wallee/agent/loop.py`, `tests/test_agent_loop.py`
- **What was wrong:** The agent parser clamped `WAIT.check_after_s`, but the main loop ignored it and always slept a fixed `poll_interval`, so the LLM’s requested cadence had no effect.
- **What you changed:** Stored the parsed next-cycle delay, made the run loop sleep for `WAIT.check_after_s`, and chunked long sleeps so `stop()` still interrupts promptly.
- **Risk:** Long `WAIT` intervals now actually slow agent polling, so any hidden assumptions that the loop always re-runs at `poll_interval` may surface in downstream integrations.

## P0-6
- **Bug ID:** `P0-6`
- **File(s) changed:** `wallee/device_packs/prusa_serial/actuators.py`, `wallee/device_packs/prusa_serial/tests/test_serial.py`
- **What was wrong:** `send_gcode` accepted arbitrary operator/LLM-supplied commands and relied on a small blacklist, which still left motion and state-changing G-code reachable.
- **What you changed:** Replaced the blacklist-only gate with a strict single-command diagnostic allowlist, rejected multiline/parameterized input, and added regression tests for blocked unsafe commands.
- **Risk:** Any workflow that previously used `send_gcode` for non-diagnostic commands must now move to dedicated actuator tools or it will be rejected.

## P2-5
- **Bug ID:** `P2-5`
- **File(s) changed:** `wallee/agent/change_detector.py`, `tests/test_change_detector.py`
- **What was wrong:** The change detector treated failed/rejected tools as if Wallee had executed them and also treated read-only `send_gcode` as capable of causing printer-side state changes.
- **What you changed:** Limited episode attribution to successfully dispatched/done tools and removed `send_gcode` from the state-changing G-code tool set, with tests covering both regressions.
- **Risk:** If any real state-changing tool is left in a non-`DONE`/`DISPATCHED` status longer than expected, the detector may now flag the resulting printer change as external until status transitions complete.

## P3-1
- **Bug ID:** `P3-1`
- **File(s) changed:** `wallee/ui/dashboard.py`, `tests/test_dashboard.py`
- **What was wrong:** The dashboard HTTP thread could stay blocked in `handle_request()` after `stop()`, leaving sockets open long enough to cause bind conflicts on restart.
- **What you changed:** Added reusable/timeout-based HTTP server cleanup, joined dashboard threads on stop, and added a restart-on-same-port regression test.
- **Risk:** Dashboard shutdown is now stricter about joining its worker threads, so any future long-running handler work could make `stop()` wait until that work finishes.

## P3-2
- **Bug ID:** `P3-2`
- **File(s) changed:** `wallee/ui/dashboard.py`, `tests/test_dashboard.py`
- **What was wrong:** The dashboard UI was functional but sparse, with limited at-a-glance state, plain camera presentation, and little visual hierarchy for live operations.
- **What you changed:** Added a richer hero/header, summary stat cards, cleaner card styling, explicit camera status overlays/placeholders, and light HTML smoke coverage for the new overview elements.
- **Risk:** The refreshed layout adds more CSS and DOM structure, so any external custom styling or DOM scraping that depended on the previous exact markup may need updates.

## P2-6
- **Bug ID:** `P2-6`
- **File(s) changed:** `wallee/agent/change_detector.py`, `tests/test_change_detector.py`
- **What was wrong:** The change detector still contained `printer.last_gcode` and `printer.cmdcnt` branches even though nothing in the codebase publishes those keys, creating dead logic and misleading implied coverage.
- **What you changed:** Removed the unpublished-metric checks and replaced them with a regression test that verifies those stray keys do not generate fake external-change alerts.
- **Risk:** If those metrics are wired in later, the detector will need an explicit reintroduction of that logic rather than silently benefiting from stale dead code.

## P3-3
- **Bug ID:** `P3-3`
- **File(s) changed:** `wallee/whiteboard/client.py`, `wallee/tools/builtins/differential.py`, `tests/test_whiteboard.py`, `tests/test_builtins.py`
- **What was wrong:** The `differential` tool assumed a fixed 1 second spacing between samples, so its reported rate could be materially wrong for slower or faster sensors.
- **What you changed:** Added timestamp history alongside value history in the whiteboard and made `differential` compute rate from the real newest-to-oldest sample span when available.
- **Risk:** The whiteboard now stores an extra Redis list per historied key, which slightly increases memory usage for history-enabled sensors.

## P3-4
- **Bug ID:** `P3-4`
- **File(s) changed:** `wallee/ui/dashboard.py`
- **What was wrong:** The dashboard websocket server used the deprecated legacy `websockets.server.serve` API, emitting runtime deprecation warnings on every test run.
- **What you changed:** Switched the dashboard to the current `websockets.serve` entry point so live updates use the non-deprecated async server API.
- **Risk:** If the deployed websocket library is much older than the test environment, it must still expose `websockets.serve` or dashboard startup will need a compatibility shim.

## P3-5
- **Bug ID:** `P3-5`
- **File(s) changed:** `tests/test_llm_client.py`, `wallee/bus/serial.py`
- **What was wrong:** The last two failing tests lagged behind current behavior: the LLM client test still expected the old `json_object` response format, and the serial bus reported `pyserial not installed` before surfacing the clearer no-port condition.
- **What you changed:** Updated the LLM test to assert the current `json_schema` payload shape and reordered serial no-port detection so missing hardware is reported before import-related failures.
- **Risk:** If future serial behavior intentionally depends on import validation before port discovery, this error ordering will need to be revisited along with its tests.

## P2-7
- **Bug ID:** `P2-7`
- **File(s) changed:** `wallee/tools/builtins/discover.py`, `wallee/device_packs/pi_cameras/sensors.py`, `wallee/device_packs/pi_cameras/tests/test_cameras.py`, `tests/test_builtins.py`
- **What was wrong:** The built-in `discover_hardware` tool was still a placeholder, and the nozzle camera path depended on a hardcoded/default localhost port instead of actually probing for the live streamer.
- **What you changed:** Replaced discovery with real local probing for the Prusa serial port, nozzle camera port, and buddy camera IPs, then published the discovered values back to the whiteboard with regression tests.
- **Risk:** Port probing now performs extra localhost HTTP checks during discovery, so unusually slow or non-camera services on the scanned ports could slightly delay discovery or produce false positives if they mimic JPEG responses.

## P2-8
- **Bug ID:** `P2-8`
- **File(s) changed:** `wallee/agent/prompt.py`, `tests/test_prompt.py`
- **What was wrong:** The prompt builder included self-referential runtime keys like `agent.last_decision`, which let the LLM read its own prior summaries back from the whiteboard and fixate on stale narratives.
- **What you changed:** Added internal heartbeat and self-summary keys to the prompt skip list and covered the exclusion with a regression test.
- **Risk:** If an operator or future subsystem expected those internal agent meta-keys to remain visible in the raw prompt text, that debugging workflow now has to read them directly from Redis or the dashboard instead.

## P3-6
- **Bug ID:** `P3-6`
- **File(s) changed:** `wallee/config.py`, `wallee/agent/llm_client.py`, `wallee/main.py`, `.env.example`, `tests/test_config.py`, `tests/test_llm_client.py`
- **What was wrong:** The default OpenRouter model and client wiring still targeted the older Gemini setup, and web grounding was not configurable from central config.
- **What you changed:** Switched the default model to `openai/gpt-5.4`, added `OPENROUTER_ENABLE_WEB_SEARCH`, and made the LLM client inject the OpenRouter web plugin when enabled.
- **Risk:** Model behavior, cost, latency, and tool-grounding characteristics may differ from the previous Gemini configuration, so prompts and operational thresholds may need retuning in production.
