# Wallee Architecture Audit
## Date: 2026-03-21

## Codebase Stats

- Scope read for this audit: all readable text files under `wallee/` (`138` files), plus root [`AGENTS.md`](/Users/anieyrudh/Desktop/Wallee2/AGENTS.md) and [`.env.example`](/Users/anieyrudh/Desktop/Wallee2/.env.example).
- Python files: `67`
- Python LOC: `9,657`
- Registered runtime tools: `39`
  - Sensors: `19`
  - Actuators: `20`
- Replay scenarios: `60`
- SQL migrations: `3`
- Knowledge files under `wallee/knowledge/`: `5` runtime text files plus `.gitkeep`
- Test suite status: `520 passed in 36.41s`
- Lint snapshot: `ruff check wallee` reports `29` issues, mostly unused imports / style, plus one real startup bug in `main.py`

## Architecture Overview

### Actual runtime topology

The current implementation is a single Python process with multiple threads:

1. `main.py` loads config, connects Redis, configures helpers.
2. Safety kernel starts in a thread.
3. Ledger opens SQLite and runs boot-time reconcile once.
4. Tool registry loads built-ins and selected device packs.
5. Engine starts in a thread and polls the ledger.
6. Sensor publisher threads start.
7. Agent loop starts in a thread and calls OpenRouter.
8. Dashboard starts as HTTP + websocket server.
9. Telegram bot starts in its own thread if configured.
10. CLI runs in the foreground if a TTY exists.

### Actual data flow

```text
Sensors / camera capture / UDP listener
  -> Redis whiteboard
  -> Agent reads full whiteboard snapshot + trends
  -> Agent builds system prompt + user message
  -> OpenRouter LLM returns JSON
  -> Parser -> Decision
  -> Agent writes ledger proposals for ACTION / ACTION_CHAIN
  -> Engine polls PROPOSED rows
  -> Gates run in code
  -> Tool executes against PrusaLink / serial / local filesystem / network
  -> Result persisted to diary + ledger

Side channels:
- CALL_HUMAN bypasses the engine and sends directly from the agent thread
- Safety kernel reads Redis and can call `estop_printer()` directly
- Dashboard reads Redis and ledger directly, but has no write path
```

### Trust-boundary reality check

The implementation mostly keeps physical hardware actions behind the engine, but it is not as strict as the ground-truth spec:

- The agent deletes and publishes multiple whiteboard keys directly, not just `agent.heartbeat`.
- `CALL_HUMAN` causes direct side effects from the agent thread and does not go through the engine.
- The safety kernel is not an independent process; it is a thread inside the same process as the agent and engine.

## Module-by-Module Documentation

### 2a. Agent loop (`wallee/agent/`)

#### `loop.py`: exact cycle

One `run_once()` cycle does this, in order:

1. `whiteboard.read_all_with_trends()`
2. `_handle_job_phase_transition(state)`
3. `episode = ledger.current_episode()`
4. `_scan_episode_for_rejections(episode)` to publish `agent.cooldown`
5. Read `human.image`
6. Read `human.intent`, but suppress it if identical to `_last_responded_intent`
7. If `agent.awaiting_feedback` exists and intent is `great|ok|failed`, call `remember()` directly and clear feedback state
8. If `job.phase == IDLE`, `print.queue` is non-empty, and bed temp is below `35C`, auto-propose `start_print` without the LLM
9. `ExternalChangeDetector.detect(state, episode, ledger)`
10. If an external change includes `printer.state ... PAUSED`, publish `agent.external_pause`
11. `_load_knowledge()`
12. Read `human.pending_callout`
13. Build system prompt
14. Build dynamic user message
15. Build `messages`; include a human-sent photo only for that cycle
16. Capture stale-check sentinels:
    - `human.intent`
    - `human.pending_callout`
    - `printer.state`
17. Call `llm.call(...)`
18. Re-read those three sentinels; if any changed, treat the decision as stale
19. If stale and retries < 3: discard decision and return early
20. Parse JSON via `parse_llm_output(...)`
21. Apply `_check_cooldown(...)`
22. Apply `_check_oscillation(...)`
23. `_route_decision(...)`
24. Set next sleep based on WAIT vs non-WAIT
25. Delete `human.intent` after consuming it
26. Clear `agent.external_pause` if a human replied
27. Delete `human.image` after one use

`run()` wraps this in a loop with a separate heartbeat thread and sleeps on an event, so Telegram / CLI / selected sensor changes can wake the agent early.

#### Episode construction and formatting

- Source: `Ledger.current_episode()` in [`wallee/ledger/db.py`](/Users/anieyrudh/Desktop/Wallee2/wallee/ledger/db.py)
- Boundary: all actions after the most recent `WAIT` or `CALL_HUMAN` event in the `events` table
- Limits:
  - normal: last `12` actions
  - fallback if no current episode: last `5` actions
- Order in prompt: chronological
- Prompt formatting:
  - `REJECTED`: `!! YOUR ACTION REJECTED: ...`
  - `FAILED`: `!! YOUR ACTION FAILED: ...`
  - `DONE`: `OK: tool -> result`
  - other statuses: `[STATUS] tool -- reason`

#### `prompt.py`: exact prompt contents

System prompt order:

1. `SOUL.md`
2. `LEARNED.md`
3. `OBSERVATIONS.md`
4. available actuator tools
5. `JOB_CONTEXT.md` if present
6. fixed JSON-output instructions

Dynamic user message order:

1. pending callout banner (`PENDING`, `ACKNOWLEDGED`, or `No pending escalation.`)
2. phase banner from `job.phase`, `job.phase_detail`, `job.time_in_phase_s`
3. compact vision summary from top three non-zero `vision.nozzle.*` scores
4. external changes banner
5. whiteboard dump
6. human intent
7. recent actions / episode
8. timestamp

Explicit prompt exclusions:

- `camera.*_frame`
- `human.image`
- keys in `_SKIP_KEYS`:
  - `host.usb_devices`
  - `host.network_interfaces`
  - `agent.last_decision`
  - `agent.heartbeat`
  - `engine.heartbeat`
- suffixes in `_SKIP_SUFFIXES`:
  - `_diameter`
  - `.files`
  - `.firmware`
  - `.serial`
  - `.model`
- strings longer than `100` chars
- trend keys as standalone rows; trends are inlined on the base key only

Important inclusion / exclusion facts:

- `HARDWARE.md` is loaded in `loop.py` but never inserted into the prompt.
- `REFERENCE.md` is never inserted into the prompt.
- `BOOTSTRAP_NOTES.md` is never loaded at runtime.

#### `llm_client.py`

- Default model: `openai/gpt-5.4`
- Runtime model: `cfg.openrouter_model`
- Endpoint: OpenRouter chat completions
- Output contract: strict JSON schema with types `ACTION`, `ACTION_CHAIN`, `WAIT`, `CALL_HUMAN`
- Token limit: `max_tokens=512`
- Sampling: `temperature=0.7` default, `top_p=0.9`
- Plugins:
  - `response-healing`
- Retry behavior:
  - network retryable exceptions: connect / read / write / pool / connect timeout
  - retries: `3`
  - backoff: `2s`, then `4s`
- Validation retry:
  - one extra retry if output is structurally invalid or references unknown tools
  - correction prompt is appended after the bad answer
- Fallback behavior:
  - on transport failure: return a hardcoded WAIT JSON string
  - on validation failure after retry: return a hardcoded WAIT JSON string

#### `parser.py`

- Never raises on malformed input
- Strips Markdown code fences before parsing
- Invalid JSON -> safe WAIT
- Non-dict JSON -> safe WAIT
- Unknown `type` -> safe WAIT
- Missing tool on ACTION -> downgraded WAIT
- Missing / invalid `actions` on ACTION_CHAIN -> downgraded WAIT
- Missing message on CALL_HUMAN -> downgraded WAIT
- `params` parsing:
  - accepts dict
  - accepts JSON string encoding a dict
  - anything else -> `{}`
- `check_after_s` is clamped:
  - active states (`PRINTING`, `PAUSED`, `ATTENTION`, `PREPARING`): min `10`, max `30`
  - idle-ish states: min `10`, max `120`
  - default `15`

#### `change_detector.py`

- Keeps previous whiteboard snapshot in RAM
- Only tracks keys that appear in a tool's `state_effects`
- Agent-caused change detection uses:
  - current episode tools in `DISPATCHED` or `DONE`
  - recent `DONE` actions in the ledger within `30s`
- External changes are returned as strings like `key changed: old -> new`
- Untracked keys are ignored completely

#### Cooldown logic

- Trigger source: `_scan_episode_for_rejections()`
- It only publishes cooldown for rejections classified as human rejections
- Classification heuristic:
  - rejection reason comes from `error_json`
  - if reason contains any engine-style substrings (`TOCTOU`, `expired`, `not approved`, `timeout`, `outside bounds`, etc.), it is treated as engine-caused
  - otherwise it is treated as human-caused
- Effect:
  - publish `agent.cooldown = {"tool": ..., "until": now+90, ...}` with TTL `90`
  - future ACTION / ACTION_CHAIN decisions proposing that tool are converted to WAIT for `30s`
- Clear condition:
  - TTL expiry only
  - there is no explicit acknowledgement / clear path

#### Oscillation detector

- Keeps last two decisions in `_recent_decisions`
- Detects only `A -> B -> A` across three consecutive action decisions
- WAIT in the middle does not count as oscillation
- On trigger:
  - clear history
  - force WAIT for `60s`

Important limitation:

- ACTION chains are keyed as `ACTION_CHAIN:` because the detector uses `decision.tool`, which is empty for chains. Different chains are therefore indistinguishable.

#### Stale-decision check

- Pre-call snapshot: `human.intent`, `human.pending_callout`, `printer.state`
- Post-call snapshot: same three keys
- If any differ, the LLM answer is discarded
- Discard budget: `3`
- After `3` discards, the agent proceeds with the stale answer anyway

Important limitation:

- It does not watch the full world; only those three keys participate.
- On stale discard, the cycle returns early without routing a decision.

### 2b. Engine (`wallee/engine/`)

#### Exact gate order in `dispatch.py`

The real gate order is:

1. Unknown tool reject
2. `gate_bypass` fast-path
3. Chain predecessor check
4. `safety.estop` reject
5. `agent.external_pause` reject for `resume_print`
6. Queue guard (`ledger.has_inflight(device_group)`)
7. Deadline / proposal age
8. Approval
9. TOCTOU precheck
10. Diary write + dispatch
11. Success / failure persistence

Differences from the ground-truth spec:

- Queue guard runs before TOCTOU, not after it.
- Approval runs before TOCTOU, not after deadline then before dispatch.
- Reconcile is not part of the engine loop.

#### Gate bypass

Current `gate_bypass=True` tools:

- `call_human`
- `discover_hardware`
- `remember`
- `web_search`
- `lookup_issue`
- `get_sensor_history`

What bypass means in code:

- no chain predecessor check
- no ESTOP check
- no queue guard
- no deadline check
- no approval check
- no TOCTOU check
- no diary write

Note: the comment in `dispatch.py` says bypass skips all gates "except ESTOP above", but the actual ESTOP check is below the bypass branch, so bypassed tools also skip ESTOP.

#### Approval flow

- Tool-level approval is declared in decorator metadata
- Only two current tools require approval:
  - `cancel_print`
  - `start_print`
- Approval process:
  - engine sees `requires_approval`
  - if no approval row exists, mark action `WAITING_APPROVAL`
  - call `approval_notifier`
  - engine keeps polling `WAITING_APPROVAL`
  - approval timeout expires after `cfg.engine_approval_timeout_s` (`300s` by default from config)
  - latest approval row wins

#### Action-chain flow

- The agent proposes one ledger row per chain step with shared `chain_id` and increasing `chain_seq`
- Engine behavior:
  - step `0` processes normally
  - later steps are skipped while predecessors are still `PROPOSED`, `WAITING_APPROVAL`, `DISPATCHED`, or `UNKNOWN`
  - later steps are rejected as `chain_skipped` if any predecessor is `FAILED` or `REJECTED`

The chain is therefore serialized by polling, not by an explicit queue object.

### 2c. Safety kernel (`wallee/safety/`)

#### What it monitors

- `agent.heartbeat`
- `engine.heartbeat`
- `printer.oc_nozzle`
- `printer.oc_input`
- `safety.estop`

#### ESTOP behavior

- Human side:
  - CLI and Telegram publish `safety.estop=True` with TTL
  - both also call `estop_printer()` directly
- Safety kernel side:
  - if `safety.estop` is present, call `estop_printer(PRUSALINK_HOST, PRUSALINK_API_KEY)`
  - send critical call_human alert

`estop_printer()` itself:

1. try `POST /api/v1/gcode {"command": "M25"}`
2. if that fails, try `DELETE /api/v1/job`

#### Independence assessment

No, the safety kernel is not truly independent in the current code:

- It is started as a thread in `main.py`, not a separate process.
- It shares the same process, interpreter, and failure domain as the agent and engine.
- ESTOP is a network API call to PrusaLink, not a hardware relay or local interlock.

A crash in the main process can kill the safety kernel too.

### 2d. Device packs (`wallee/device_packs/`)

#### `host_pi`

- Interfaces:
  - local sysfs
  - `psutil`
  - subprocess fallbacks (`lsusb`, `system_profiler`)
- Sensors:
  - `read_cpu_temp`: `1.0Hz`
  - `read_system_stats`: `0.5Hz`
  - `read_usb_devices`: `0.1Hz`
  - `read_network_interfaces`: `0.2Hz`
- Actuators: none

#### `prusa_link`

- Interface: local HTTP via `wallee.bus.network.HTTPClient`
- Sensors:
  - `read_printer_state`: `0.5Hz`
  - `read_printer_info`: `0.1Hz`
  - `read_file_list`: `0.02Hz` (`~50s`)
  - `read_job_phase`: `1.0Hz`
- Actuators:
  - `pause_print`
  - `resume_print`
  - `cancel_print`
  - `start_print`
  - `set_temperature`
  - `home_axes`
  - `disable_motors`
  - `set_speed_factor`
  - `set_flow_factor`
  - `set_position`
  - `extrude`
  - `retract`

#### `prusa_metrics`

- Interface: UDP push on port `8514`, parsed as syslog-wrapped InfluxDB line protocol
- Push model:
  - one background `UDPListener`
  - sensor functions read a shared `MetricsBuffer`
- Sensors:
  - `read_temperatures`: `0.3Hz`
  - `read_electrical`: `0.3Hz`
  - `read_fans`: `0.5Hz`
  - `read_position`: `5.0Hz`
  - `read_filament`: `1.0Hz`
  - `read_enclosure`: `0.3Hz`
  - `read_firmware_health`: `0.2Hz`
  - `read_print_state`: `0.3Hz`
- Actuators: none

#### `prusa_serial`

- Interface: USB serial, open-send-read-close
- Actuators:
  - `read_endstops`
  - `send_gcode` (restricted diagnostic allowlist only)
- Sensors: none

#### `pi_cameras`

- Interfaces:
  - nozzle camera: local HTTP snapshot from `ustreamer`
  - buddy cameras: RTSP + auto-discovery via ARP/neighbor table
  - vision analysis: OpenRouter multimodal call
- Sensors:
  - `read_nozzle_camera`: `1.0Hz`
  - `read_buddy_cameras`: `0.1Hz`
  - `read_vision_analysis`: `0.1Hz`
- Actuators: none

#### `vision_analysis.py`

Prompt structure:

1. camera geometry paragraph for `nozzle` or `buddy`
2. scores-only instruction prompt
3. optional `Current print phase: <phase>`
4. one or two frames:
   - previous frame (~10s ago) if available
   - current frame

Prompt behavior:

- output must be JSON only
- scores `0.0` to `1.0`
- categories:
  - `normal`
  - `stringing`
  - `spaghetti`
  - `blob`
  - `warping`
  - `underextrusion`
  - `overextrusion`
  - `layer_shift`
  - `bed_adhesion_ok`
  - `burn_marks`
  - `confidence`

Score-to-status computation:

- max defect > `0.7` -> `DEFECT:<name>`
- max defect > `0.4` -> `POSSIBLE:<name>`
- else -> `NORMAL`

Temporal comparison:

- previous frame per camera is stored in `_prev_frames`
- prompt explicitly asks the vision model to compare previous vs current frame

Hysteresis:

- if current combined status is `NORMAL`
- but prior whiteboard `vision.status` was `DEFECT:*` or `POSSIBLE:*`
- and current `vision.nozzle.normal < 0.8`
- then keep a degraded state as `FADING:<previous_defect>`
- confidence is floored at `0.4`

Phase awareness:

- analysis is skipped unless phase is `PRINTING`, `PREPARING`, or `PAUSED`
- phase string is appended to the vision prompt

### 2e. Tools (`wallee/tools/`)

#### Registered tool inventory

| Name | Pack | Kind | Approval | Bypass | State effects | Params |
|---|---|---:|---:|---:|---|---|
| `call_human` | builtin | actuator | no | yes | — | `message`, `severity` |
| `cancel_print` | prusa_link | actuator | yes | no | `printer.state`, `printer.job_state` | — |
| `disable_motors` | prusa_link | actuator | no | no | — | — |
| `discover_hardware` | builtin | actuator | no | yes | — | — |
| `extrude` | prusa_link | actuator | no | no | — | `length_mm`, `feedrate` |
| `get_sensor_history` | builtin | actuator | no | yes | — | `key`, `depth` |
| `home_axes` | prusa_link | actuator | no | no | — | — |
| `lookup_issue` | builtin | actuator | no | yes | — | `query` |
| `pause_print` | prusa_link | actuator | no | no | `printer.state`, `printer.job_state` | — |
| `read_endstops` | prusa_serial | actuator | no | no | — | — |
| `remember` | builtin | actuator | no | yes | — | `observation` |
| `resume_print` | prusa_link | actuator | no | no | `printer.state`, `printer.job_state` | — |
| `retract` | prusa_link | actuator | no | no | — | `length_mm`, `feedrate` |
| `send_gcode` | prusa_serial | actuator | no | no | — | `command` |
| `set_flow_factor` | prusa_link | actuator | no | no | `printer.flow` | `percent` |
| `set_position` | prusa_link | actuator | no | no | — | `x`, `y`, `z` |
| `set_speed_factor` | prusa_link | actuator | no | no | `printer.speed` | `percent` |
| `set_temperature` | prusa_link | actuator | no | no | `printer.target_nozzle`, `printer.target_bed`, `printer.target_chamber` | `target`, `heater` |
| `start_print` | prusa_link | actuator | yes | no | `printer.state`, `printer.job_state` | `file_path` |
| `web_search` | builtin | actuator | no | yes | — | `query` |
| `read_buddy_cameras` | pi_cameras | sensor | — | — | — | — |
| `read_cpu_temp` | host_pi | sensor | — | — | — | — |
| `read_electrical` | prusa_metrics | sensor | — | — | — | — |
| `read_enclosure` | prusa_metrics | sensor | — | — | — | — |
| `read_fans` | prusa_metrics | sensor | — | — | — | — |
| `read_filament` | prusa_metrics | sensor | — | — | — | — |
| `read_file_list` | prusa_link | sensor | — | — | — | — |
| `read_firmware_health` | prusa_metrics | sensor | — | — | — | — |
| `read_job_phase` | prusa_link | sensor | — | — | — | — |
| `read_network_interfaces` | host_pi | sensor | — | — | — | — |
| `read_nozzle_camera` | pi_cameras | sensor | — | — | — | — |
| `read_position` | prusa_metrics | sensor | — | — | — | — |
| `read_print_state` | prusa_metrics | sensor | — | — | — | — |
| `read_printer_info` | prusa_link | sensor | — | — | — | — |
| `read_printer_state` | prusa_link | sensor | — | — | — | — |
| `read_system_stats` | host_pi | sensor | — | — | — | — |
| `read_temperatures` | prusa_metrics | sensor | — | — | — | — |
| `read_usb_devices` | host_pi | sensor | — | — | — | — |
| `read_vision_analysis` | pi_cameras | sensor | — | — | — | — |

#### Registered but likely low-usage tools

Static references are weak evidence because the LLM can call tools dynamically, but these have very little direct code/test/scenario evidence:

- `discover_hardware`
- `extrude`
- `retract`
- `read_vision_analysis` as an explicit symbol

#### Referenced but not registered

The spec and stale compiled artifacts imply these built-ins should exist, but there is no source implementation and they are not registered:

- `git_pull`
- `trends`
- `differential`

### 2f. Human interface (`wallee/human/`)

#### Telegram

Commands:

- `/start`
- `/status`
- `/urgent`
- `/estop`
- `/snapshot`
- `/approve <id>`
- `/reject <id>`
- `/help`
- `/queue [files...]`

Approval flow:

- Engine calls `approval_notifier`
- Telegram sends message with inline keyboard
- callback writes approval row to ledger
- engine re-polls and resumes gating

Images:

- inbound photo:
  - highest-res Telegram photo is downloaded
  - optionally resized with PIL to max `1024x1024`
  - base64 JPEG published to `human.image`
  - caption becomes `human.intent` if present
- outbound `/snapshot`:
  - reads `camera.*_frame` from whiteboard
  - sends live frames only

Retry logic:

- all outbound Telegram sends use `_send_with_retry()`
- retries: `3`
- backoff: `1s`, then `2s`
- synchronous `send()` waits up to `60s`

Authorization:

- enforced by chat ID and optionally allowed user IDs

#### CLI

What it provides:

- `intent <message>`
- `urgent`
- `approve <action_id>`
- `reject <action_id>`
- `status`
- `pending`
- `history`
- `estop`
- `help`
- `quit`

How it differs from Telegram:

- local TTY only
- no images
- no retry/backoff layer
- no persistent command menu
- simpler status rendering

#### `call_human` full flow

There are two distinct paths:

1. LLM `CALL_HUMAN` decision:
   - parsed in agent
   - deduplicated against `human.pending_callout`
   - recorded in ledger via `record_call_human`
   - delivered directly via `agent.call_human_fn` if wired
   - fallback tool path is bypassed entirely
   - publishes `human.pending_callout`

2. Built-in `call_human` tool:
   - goes through engine
   - tool is `gate_bypass=True`
   - uses injected Telegram sender if configured
   - otherwise uses fallback chain:
     - Telegram x3 if callable provided
     - CLI / TTY
     - durable outbox

### 2g. Knowledge files (`wallee/knowledge/`)

Runtime token estimates (`word_count * 1.3`):

- `SOUL.md`: `~603`
- `LEARNED.md`: `~728`
- `REFERENCE.md`: `~13,254`
- `BOOTSTRAP_NOTES.md`: `~1,405`
- `OBSERVATIONS.md`: `~728`

Runtime usage:

- Prompted every cycle:
  - `SOUL.md`
  - `LEARNED.md`
  - `OBSERVATIONS.md`
  - `JOB_CONTEXT.md` if present
- Loaded but not prompted:
  - `HARDWARE.md` if present
- Not loaded:
  - `REFERENCE.md`
  - `BOOTSTRAP_NOTES.md`

Knowledge contradictions / inconsistencies:

- `REFERENCE.md` uses `call_human(..., severity="low|medium|high")`, but the schema and parser accept only `info|warning|critical`.
- `SOUL.md` says `OBSERVATIONS.md` "persists forever", but `remember()` caps it to `50` entries.
- `REFERENCE.md` relies on pseudo-variables like `temp_nozzle_target` and `temp_bed_target`; runtime prompt state uses whiteboard keys like `printer.target_nozzle` and `printer.target_bed`.

### 2h. Dashboard (`wallee/ui/`)

What it displays:

- connection state
- safety state
- phase / progress / agent / vision hero cards
- key metrics table
- camera frames
- temperature chart
- agent log
- vision analysis panel
- human section
- adjustment feed
- full whiteboard table

Data sources:

- Redis whiteboard via `read_all()`
- Redis lists:
  - `agent.activity_log`
  - `human.intent_log`
- SQLite ledger directly for recent terminal action states

Write paths:

- none in code
- current UI is read-only

### 2i. Testing (`wallee/testing/`)

#### Replay harness

How it works:

1. load knowledge
2. load tool list for prompt only
3. build real system prompt + user message
4. call OpenRouter directly
5. parse with the real parser
6. compare decision against scenario expectations

Important detail:

- It instantiates `LLMClient`, but does not use it.
- It uses a direct OpenRouter call instead because the repo's historical provider settings reportedly rejected the strict schema path.

Scenarios:

- `60` total
- Current JSON files do not carry a `category` field
- The stored [`wallee/testing/REPORT.md`](/Users/anieyrudh/Desktop/Wallee2/wallee/testing/REPORT.md) groups them as:
  - Normal operation: `8`
  - Vision defects: `12`
  - Ambiguous vision: `6`
  - Telemetry reasoning: `9`
  - Human interaction: `9`
  - Rejection learning: `6`
  - Edge cases: `7`
  - Action chains + tools: `3`

Harness model:

- current code uses `cfg.openrouter_model`
- stored baseline report says a previous live run used `anthropic/claude-opus-4-6`

### 2j. Config and boot (`wallee/config.py`, `wallee/main.py`)

#### Environment variables

Required for startup:

- `OPENROUTER_API_KEY`

Conditionally required:

- `REDIS_URL` must point to a reachable Redis instance
- `PRUSALINK_HOST` / `PRUSALINK_API_KEY` if printer packs are enabled
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` for Telegram

Other supported variables:

- `OPENROUTER_MODEL`
- `LLM_TEMPERATURE`
- `VISION_MODEL`
- `DEVICE_PACKS`
- `WALLEE_DATA_DIR`
- `WALLEE_LOG_DIR`
- all agent / engine / human TTL and interval settings
- `TELEGRAM_ALLOWED_USER_IDS`
- `DASHBOARD_PORT`

#### Actual boot order

1. config load
2. parser / observations / web search / vision configuration
3. data-dir fallback
4. Redis connection and ping
5. safety thread start
6. ledger init
7. boot-time reconcile
8. registry load
9. engine thread start
10. agent object creation
11. sensor threads start
12. agent thread start
13. dashboard start
14. Telegram bot start if configured
15. CLI or headless wait

#### Failure handling

- Missing OpenRouter key: intended `sys.exit(1)`, but current code has a local-import bug
- Redis connect failure: hard exit
- Telegram start failure: log warning, continue without Telegram
- runtime thread exceptions:
  - agent and engine loops catch and log per-cycle errors
  - safety loop catches and logs per-check errors

## Known Issues (P1/P2/P3)

### P1 Blocking / architecture-breaking

1. Safety kernel is not independent.
   - Spec says separate OS process; code runs it as a thread in `main.py`.
   - A crash in the main process can kill the safety watchdog.

2. The agent writes to the whiteboard directly beyond heartbeat.
   - Examples: clearing `human.intent`, `human.image`, `agent.external_pause`, `agent.awaiting_feedback`, publishing `human.pending_callout`.
   - This violates the stated trust boundary in `AGENTS.md`.

3. `CALL_HUMAN` bypasses the engine entirely.
   - The agent directly calls `self.call_human_fn(...)` in `_route_decision()`.
   - That is a direct LLM-triggered side effect outside the engine gate path.

4. `main.py` has a real startup bug when `OPENROUTER_API_KEY` is missing.
   - `sys` is imported globally, then imported again inside `main()`.
   - Python treats it as a local variable; the early `sys.exit(1)` path can raise `UnboundLocalError` instead.

5. The safety path still depends on PrusaLink network reachability.
   - ESTOP is HTTP to the printer, not a hardware relay.
   - This is a single point of failure during precisely the wrong failure mode.

### P2 Should fix

1. Ground-truth spec drift is substantial.
   - Threads instead of processes
   - `HARDWARE.md` not maintained
   - `REFERENCE.md` not in prompt
   - missing built-ins: `git_pull`, `trends`, `differential`
   - engine gate order does not match the documented order

2. Reconcile is only run on boot.
   - Spec says boot and periodic.
   - Current engine loop never calls `reconcile()`.

3. Type handling around whiteboard payloads is inconsistent.
   - `human.pending_callout`, `agent.awaiting_feedback`, and `print.queue` are double-JSON-encoded in some paths.
   - `agent.external_pause` is stored as `"true"` string, not boolean.
   - The code works by compensating at read time, but the contract is muddy.

4. Knowledge and schema disagree on `call_human` severities.
   - `REFERENCE.md` trains the model toward `low|medium|high`.
   - Parser / schema accept `info|warning|critical`.

5. Prompt architecture ignores `HARDWARE.md` and `REFERENCE.md`.
   - `loop.py` loads `HARDWARE.md`, but `prompt.py` never renders it.
   - `REFERENCE.md` is only reachable if the LLM explicitly calls `lookup_issue`.

6. Several lazy singletons are race-prone at startup.
   - `prusa_metrics.get_buffer()`
   - `prusa_link._get_http()`
   - `prusa_serial._get_serial()`
   - camera discovery / stale-state globals

7. Oscillation detection is too coarse for action chains.
   - all chains collapse to `ACTION_CHAIN:`

8. Stale decision checking is narrow.
   - only `human.intent`, `human.pending_callout`, and `printer.state`
   - not `job.phase`, temperatures, vision state, faults, or queue state

9. `pending_callout` acknowledgement state is effectively dead runtime behavior.
   - prompt supports `ACKNOWLEDGED`
   - runtime UI/CLI/Telegram delete the key instead of updating status

10. `call_human()` fallback does not verify durable outbox success before returning `"outbox"`.
    - this weakens the "never silently gives up" contract

11. `discover_hardware()` does not update `HARDWARE.md`.
    - it only publishes findings to Redis and returns a dict

12. Test/report mismatch in replay harness.
    - `REPORT.md` says 47/60 on a prior run
    - current local pytest suite is green
    - scenario JSONs omit category metadata even though the report groups them by category

### P3 Nice to have / cleanup

1. Runtime dead-code candidates:
   - `compute_differential()` in whiteboard client
   - `read_history_timestamps()` in whiteboard client
   - `MetricsBuffer.get_all()`
   - `Diary.get_inflight()`
   - `ExternalChangeDetector.format_for_prompt()`
   - replay harness creates `LLMClient` but never uses it

2. No `TODO`, `FIXME`, or `HACK` comments were found.

3. Lint hygiene is noisy.
   - many unused imports
   - several extraneous f-strings
   - one late module import re-export

4. `OBSERVATIONS.md` is noisy and repetitive.
   - many identical `test.gcode` completion entries
   - it inflates the system prompt every cycle

5. Dashboard visuals are read-only, but it reads ledger SQLite directly from a path inferred from env.
   - acceptable, but worth documenting as a second data source besides Redis

## Architecture Assessment

### Is the LLM truly untrusted?

Not fully.

For physical printer actions, ACTION and ACTION_CHAIN still go through the ledger and engine. That is good. But the implementation gives the agent additional direct side effects:

- direct `CALL_HUMAN` delivery
- direct whiteboard mutation / deletion
- direct internal `remember()` call during job archival

So the hardware path is mostly protected, but the broader trust boundary in `AGENTS.md` is not enforced.

### Is the safety kernel truly independent?

No.

- It is not a process.
- It shares the same Python runtime as the agent and engine.
- Its ESTOP path depends on network access to PrusaLink.

A bug or crash in the main process can prevent safety checks and ESTOP.

### Is the core hardware-agnostic?

Partially.

Good:

- device-pack separation exists
- buses are separated into `network`, `serial`, `udp_listener`

Not good:

- `AgentLoop` reaches directly into PrusaLink HTTP endpoints to fetch job filename, material, and thumbnails
- `safety.estop` is printer-specific and implemented against PrusaLink
- queue auto-start is printer-specific behavior inside the generic agent loop

That means printer-specific logic is not fully contained inside `device_packs/`.

### Token budget estimate for a typical printing cycle

Measured from the current code using real knowledge files and a representative printing scenario:

- system prompt:
  - `15,936` chars
  - `2,104` words
  - rough estimate: `~2.7k` tokens by word heuristic or `~4.0k` by char heuristic
- dynamic user message:
  - `732` chars
  - `77` words
  - rough estimate: `~100-180` tokens
- total practical prompt budget:
  - roughly `~2.8k-4.2k` input tokens for a typical nominal printing cycle

Important note:

- This is manageable because `REFERENCE.md` is not in the prompt.
- If `REFERENCE.md` were added verbatim, it would contribute another `~13.3k` estimated tokens by itself.

### Latency estimate: sensor read to action dispatch

Best case:

- sensor publishes now
- state change wakes the agent immediately
- LLM responds quickly
- engine polls within `0.5s`
- tool dispatches immediately
- total: roughly `1-3s` plus network/tool latency

Typical nominal cycle:

- vision analysis cadence: `10s`
- agent may be sleeping on a WAIT delay of `10-30s`
- engine poll interval: `0.5s`
- tool HTTP latency: `sub-second to a few seconds`
- total: roughly `10-40s` from new evidence to dispatch for non-waking signals, especially vision-only changes

Worst practical case:

- stale-check discards up to `3` LLM outputs
- long WAIT interval in idle-ish states can be `120s`
- OpenRouter call timeout is `60s`

The system is therefore responsive to explicit wake events, but not low-latency for passive vision or telemetry drift.

## Recommendations

1. Split safety, engine, and agent into real OS processes first. This is the most important architectural correction.
2. Enforce the trust boundary in code: the agent should not mutate arbitrary whiteboard keys or directly deliver side effects.
3. Move all printer-specific HTTP fetches out of `AgentLoop` and into device-pack tools or sensor publishers.
4. Implement the missing documented built-ins or remove them from the spec and stale artifacts.
5. Align knowledge with the parser/schema, especially severity levels and supported tool parameters.
6. Decide whether `REFERENCE.md` should be prompt-resident, retrieval-only, or summarized into a smaller operational playbook.
7. Make reconcile periodic again, or explicitly document boot-only recovery if that is the intended design.
8. Normalize whiteboard payload typing so publishers never double-encode JSON.
9. Tighten concurrency around lazy singletons in device packs.
10. Add targeted tests for `agent/`, `engine/`, `safety/`, `ledger/`, `whiteboard/`, `human/`, `ui/`, and `main.py`, which currently have much weaker direct coverage than device packs.
