# Wallee — Architecture Reference

Quick-start reference for developers and AI agents working on the Wallee codebase.

---

## System diagram

```
                          ┌────────────────────┐
                          │  OpenRouter (LLM)   │
                          │  Gemini 3.1 Pro     │
                          │  + response healing │
                          │  + prompt caching    │
                          └─────────▲──────────┘
                                    │ HTTPS (structured JSON output)
                                    │
┌───────────────────────────────────┼─────────────────────────────────┐
│  Raspberry Pi 5                   │                                 │
│                                   │                                 │
│  ┌─────────────────────────────┐  │                                 │
│  │     Agent Loop (thread)     │──┘                                 │
│  │  prompt.py → llm_client.py  │                                    │
│  │  parser.py → loop.py        │                                    │
│  │  change_detector.py         │                                    │
│  │  + vision (camera frames)   │                                    │
│  └─────────┬───────────────────┘                                    │
│            │ propose                                                │
│            ▼                                                        │
│  ┌─────────────────┐     ┌──────────────────────────────┐          │
│  │  Ledger (SQLite) │◄───►│      Engine (thread)         │          │
│  │  actions         │     │  Gate 0: ESTOP               │          │
│  │  approvals       │     │  Gate 1: Queue guard          │          │
│  │  events          │     │  Gate 2: Deadline             │          │
│  └────────┬────────┘     │  Gate 3: Approval             │          │
│           │               │  Gate 4: TOCTOU (precheck)    │          │
│           │               │  Gate 5: Dispatch             │          │
│           │               └───────────┬──────────────────┘          │
│           │                           │ execute                     │
│  ┌────────▼────────┐                 │                              │
│  │  Diary (SQLite)  │◄───────────────┘                              │
│  │  per-device      │                                               │
│  │  idempotency     │                                               │
│  └─────────────────┘                                                │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                     Whiteboard (Redis)                        │   │
│  │  SET key value EX ttl  |  LPUSH key:history  |  LTRIM        │   │
│  │  ~80 keys  |  ring buffers with timestamps  |  trends        │   │
│  └──────────────────────────▲───────────────────────────────────┘   │
│                              │ publish                              │
│  ┌───────────────────────────┼──────────────────────────────────┐   │
│  │              Device Packs (sensor threads)                    │   │
│  │                                                               │   │
│  │  host_pi          sysfs → cpu_temp, system_stats, usb, net   │   │
│  │  prusa_link       HTTP  → printer state, temps, job, files   │   │
│  │  prusa_metrics    UDP   → temps, electrical, fans, position, │   │
│  │                           filament, enclosure, firmware       │   │
│  │  prusa_serial     USB   → endstops (M119), diagnostic gcode  │   │
│  │  pi_cameras       HTTP  → nozzle cam (ustreamer), buddy cams │   │
│  │                           (RTSP via ffmpeg, auto-discovered)  │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────────────────────┐  ┌──────────────────────────────┐ │
│  │  Safety Kernel (thread)     │  │  Human Interface              │ │
│  │  - agent/engine heartbeat   │  │  - CLI (REPL)                 │ │
│  │  - overcurrent (oc_nozz,    │  │  - Telegram (async bot)       │ │
│  │    oc_inp)                  │  │  - Dashboard (HTTP+WS 8081/2) │ │
│  │  - ESTOP flag               │  │  - call_human fallback chain  │ │
│  └─────────────────────────────┘  └──────────────────────────────┘ │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
         │              │              │
         ▼              ▼              ▼
   Prusa Core One+   3DO Nozzle    Buddy WiFi
   (HTTP :80)        Endoscope     Cameras
   (UDP  :8514)      (ustreamer    (RTSP, MAC
   (USB  serial)      localhost)    88:49:2d)
```

---

## File tree

```
wallee/
├── agent/
│   ├── loop.py              # Main agent loop — read state, call LLM, route decisions
│   ├── prompt.py            # Build system prompt + vision messages for LLM
│   ├── parser.py            # Parse LLM JSON → Decision, clamp intervals by printer state
│   ├── llm_client.py        # OpenRouter HTTP client — structured output + plugins
│   └── change_detector.py   # Detect external whiteboard changes between cycles
├── engine/
│   ├── dispatch.py          # Engine — poll ledger, run 6-gate sequence, dispatch tools
│   └── reconcile.py         # Crash recovery — resolve in-flight actions on boot
├── safety/
│   └── kernel.py            # Watchdog — heartbeats + overcurrent + ESTOP monitoring
├── ledger/
│   ├── db.py                # SQLite WAL — proposals, approvals, episodes, events
│   ├── diary.py             # Per-device-group idempotency DB for crash recovery
│   └── migrations/
│       ├── 001_init.sql     # Schema: actions, approvals, events tables
│       └── 002_add_observation.sql  # Add observation column to actions
├── whiteboard/
│   └── client.py            # Redis wrapper — TTL, ring buffers, timestamps, trends
├── bus/
│   ├── network.py           # HTTPClient for PrusaLink (GET/POST/PUT/DELETE)
│   ├── serial.py            # SerialBus — open-send-read-close, blacklist, garble detection
│   └── udp_listener.py      # UDP receiver + InfluxDB line protocol parser + MetricsBuffer
├── device_packs/
│   ├── host_pi/             # 4 sensors: cpu_temp, system_stats, usb, network
│   ├── prusa_link/          # 3 sensors + 12 actuators via PrusaLink HTTP API
│   ├── prusa_metrics/       # 8 sensors via UDP metrics stream (port 8514)
│   ├── prusa_serial/        # 2 actuators via USB serial (endstops + diagnostic gcode)
│   └── pi_cameras/          # 2 sensors: nozzle camera + buddy cameras (auto-discovered)
├── tools/
│   ├── registry.py          # Discovers packs, registers tools, runs sensor background threads
│   ├── decorator.py         # @tool decorator — attaches metadata (kind, refresh_hz, etc.)
│   └── builtins/            # 6 built-in tools: discover, call_human, trends, differential, history, remember
├── knowledge/
│   ├── SOUL.md              # Agent mission briefing + decision heuristics
│   └── LEARNED.md           # Printing knowledge + troubleshooting playbook
├── human/
│   ├── cli.py               # Interactive CLI — intent, approve, reject, status, ESTOP
│   ├── telegram.py          # Telegram bot — commands, photos, inline keyboards, auth
│   └── call_human.py        # Fallback chain: Telegram → CLI → durable outbox
├── ui/
│   └── dashboard.py         # Read-only web dashboard — inline HTML/CSS/JS, WebSocket push
├── config.py                # .env loader, typed Config dataclass with all defaults
└── main.py                  # Boot sequence — correct startup order, signal handling
```

---

## How a single agent cycle works

```
1. READ STATE
   └── whiteboard.read_all_with_trends()
       Returns ~80 keys + trend annotations for numeric histories

2. READ EPISODE
   └── ledger.current_episode()
       Actions since last WAIT/CALL_HUMAN (max 12)

3. CHECK INTENT
   └── whiteboard.read("human.intent")
       Skip if already responded to this exact intent string

4. DETECT EXTERNAL CHANGES
   └── change_detector.detect(state, episode)
       Compare to previous snapshot, flag changes not caused by Wallee

5. BUILD PROMPT
   └── prompt.build_prompt(state, episode, intent, knowledge, tools, time, changes)
       Assembles: changes → SOUL.md → LEARNED.md → whiteboard → intent → episode → tools → instructions

6. BUILD MESSAGES (with vision)
   └── prompt.build_messages(system_prompt, state)
       Adds camera frames (max 2) + human photo as image_url content blocks

7. CALL LLM
   └── llm_client.call(prompt, messages)
       POST to OpenRouter with structured output schema + plugins
       Retries 3x on network errors with 2s backoff

8. PARSE RESPONSE
   └── parser.parse_llm_output(raw, printer_state)
       JSON → Decision(type, tool, params, reason, check_after_s, message, severity)
       Clamps check_after_s: 30-120s (active), 30-300s (idle)
       Bad JSON → WAIT with default interval

9. ROUTE DECISION
   ├── ACTION → ledger.propose(tool, params, reason, device_group, ...)
   │            Engine picks it up on next poll (0.5s)
   ├── WAIT   → ledger.record_wait(reason)
   │            Sleep for check_after_s (in 0.5s chunks)
   └── CALL_HUMAN → ledger.record_call_human(message)
                     call_human_fn(message, severity) → Telegram/CLI/outbox

10. PUBLISH ACTIVITY
    └── whiteboard.publish("agent.last_decision", summary)
        whiteboard.r.lpush("agent.activity_log", entry)
```

---

## How the engine processes a proposal

```
PROPOSED action arrives in ledger
         │
    Gate 0: ESTOP?
    │  safety.estop on whiteboard → REJECT
    ▼
    Gate 1: Queue guard
    │  Device group has DISPATCHED action → SKIP (retry next poll)
    ▼
    Gate 2: Deadline
    │  age_ms > max_proposal_age_ms → REJECT (expired)
    ▼
    Gate 3: Approval
    │  requires_approval=True?
    │  ├── No approval record → WAITING_APPROVAL, notify Telegram
    │  ├── REJECT decision → REJECT
    │  └── APPROVE decision → continue
    ▼
    Gate 4: TOCTOU
    │  has_precheck=True?
    │  └── precheck(whiteboard, **params) returns {"error":...} → REJECT
    ▼
    Gate 5: Dispatch
    │  diary.write_inflight(key, id, tool)  ← fsync before execution
    │  ledger.set_status(id, DISPATCHED)
    │  result = tool.execute(whiteboard, **params)
    │  ├── {"error":...} → diary.write_failed, ledger FAILED
    │  └── success → diary.write_success, ledger DONE
    ▼
    Result visible to agent on next cycle (in episode context)
```

---

## How to add a new device pack

1. Create `wallee/device_packs/<pack_name>/`
2. Add `__init__.py` with `PACK_META`:
   ```python
   PACK_META = {
       "name": "my_device",
       "description": "What this device does",
       "bus": "i2c",  # or "network", "serial", "udp", "sysfs", "gpio"
       "discovery_match": {"type": "always"},
   }
   ```
3. Add `sensors.py` with sensor tools:
   ```python
   from wallee.tools.decorator import tool

   @tool(kind="sensor", refresh_hz=1.0, history_depth=10)
   def read_my_sensor():
       """Read something from hardware."""
       return {"my_device.temperature": 25.0}
   ```
4. Add `actuators.py` with actuator tools (if any):
   ```python
   def _precheck_my_action(whiteboard=None, **kwargs):
       state = whiteboard.read("my_device.state") if whiteboard else None
       if state != "READY":
           return {"error": f"Device is {state}, not READY"}
       return {"status": "ok"}

   @tool(kind="actuator", requires_approval=True, precheck_fn=_precheck_my_action)
   def my_action(whiteboard=None, **kwargs):
       """Do something on the device."""
       # ... execute command ...
       return {"status": "success", "action": "my_action"}
   ```
5. Add `tests/` directory with tests
6. Add pack name to `DEVICE_PACKS` in `.env`
7. The registry auto-discovers `sensors.py` and `actuators.py` modules and registers all `@tool`-decorated functions

---

## How to add a new actuator tool

1. Write a precheck function (side-effect-free, checks discrete states only):
   ```python
   def _precheck_foo(whiteboard=None, **kwargs):
       # Check discrete states — NOT continuous sensor values
       state = whiteboard.read("device.state") if whiteboard else None
       if state != "EXPECTED":
           return {"error": f"Cannot foo: state is {state}"}
       return {"status": "ok"}
   ```

2. Write the actuator with the `@tool` decorator:
   ```python
   @tool(kind="actuator", requires_approval=False, max_proposal_age_ms=15000,
         precheck_fn=_precheck_foo)
   def foo(whiteboard=None, param1="default", **kwargs):
       """What this tool does. This docstring becomes the LLM-visible description."""
       # Runtime precondition check (defense in depth)
       state = whiteboard.read("device.state") if whiteboard else None
       if state != "EXPECTED":
           return {"error": f"Cannot foo: state is {state}"}

       # Execute
       result = do_the_thing(param1)
       return {"status": "success", "action": "foo"}
   ```

3. Return conventions:
   - Success: `{"status": "success", ...}`
   - Failure: `{"error": "human-readable reason"}`
   - Both are stored in the ledger and shown to the LLM

4. Set `requires_approval=True` for irreversible or high-consequence actions (cancel print, start print, etc.)

---

## How to add a new sensor tool

1. Write the sensor with the `@tool` decorator:
   ```python
   @tool(kind="sensor", refresh_hz=1.0, history_depth=10)
   def read_my_sensor():
       """What this sensor reads."""
       value = read_from_hardware()
       return {"device.key": value}
   ```

2. Key conventions:
   - Prefix with device/domain: `printer.`, `host.`, `camera.`, `env.`
   - Return a dict of `{whiteboard_key: value}` pairs
   - Multiple keys per sensor are fine (published atomically)
   - On error, return `{"error": "reason"}` (not published to whiteboard)

3. Parameters to consider:
   - `refresh_hz`: How often to poll (1.0 = every 1s, 0.1 = every 10s)
   - `history_depth`: Ring buffer size (0 = no history, 10-30 for trending)
   - `ttl_ms`: Auto-calculated as `2000 / refresh_hz` but can be overridden

---

## Known limitations and quirks

### Core One+ firmware quirks
- **PUT /api/v1/job returns 405** during many printer states. All pause/resume goes through G-code injection (M25/M24 via POST /api/v1/gcode).
- **HTTP API reports PRINTING during purge/preparation.** The precheck uses `job.phase` to distinguish PREPARING from PRINTING and blocks pause during PREPARING.
- **Metrics stream stops sending some values during IDLE.** `temp_bed` and `chamber_temp` disappear from UDP when idle. HTTP API always reports them — prusa_link sensors serve as fallback.

### USB serial
- **Serial disconnects every 1-3 seconds.** The printer's USB serial is unstable. `SerialBus` opens fresh for each command (open-send-read-close pattern) and auto-reconnects.
- **Garbled output.** USB instability causes corrupted bytes. Lines with <70% printable characters are discarded.
- **Restricted command allowlist.** `send_gcode` only permits M105, M114, M115, M119, M503. State-changing commands must use dedicated actuator tools.

### Camera system
- **Nozzle camera port auto-discovery.** Tries ports 8083, 8080, 8084, 8082, 8085, 8090 looking for a JPEG snapshot endpoint. Override with `NOZZLE_CAMERA_PORT` env var.
- **Buddy camera discovery by MAC prefix** (88:49:2d). Uses ARP table scanning with nmap ping sweep. Handles DHCP changes and plug/unplug automatically. Re-scans every 60s.
- **Stale frame detection.** If the same frame hash appears 3+ consecutive times, camera status changes to "stale".

### Agent behavior
- **Observe-only by default.** Agent does NOT propose actions unless there is an active `human.intent` or a safety emergency. This is enforced by SOUL.md, not code.
- **Intent deduplication.** Once the agent responds to an intent, it marks it as handled and won't re-present it to the LLM on subsequent cycles.
- **LLM errors → silent WAIT.** If the LLM returns empty/invalid JSON, the parser defaults to a WAIT decision. No crash, no action.
- **CALL_HUMAN dedup.** Hashes first 100 chars of each CALL_HUMAN message. If a pending callout with the same hash is still PENDING on the whiteboard, subsequent identical escalations are suppressed (agent returns WAIT instead).
- **Event-driven wake.** Agent sleep can be interrupted by Telegram intent/urgent/estop, CLI intent/estop, or printer state changes from sensors. Uses `threading.Event.wait(timeout)` instead of fixed `time.sleep()`.
- **JOB_CONTEXT.md lifecycle.** Created on PREPARING transition (or on restart if mid-print). Material detected from filename regex then PrusaLink API fallback. Archived to OBSERVATIONS.md on FINISHED. Adjustments and issues appended during the print.
- **Observation + reasoning schema.** LLM returns `observation` (what it sees) and `reasoning` (why it chose this action). Both are persisted to the ledger and shown in Telegram approval requests.

### Prompt architecture
- **Cached prefix / dynamic suffix.** System message contains static knowledge (SOUL.md, LEARNED.md, OBSERVATIONS.md, tool list, JOB_CONTEXT.md, instruction) — marked with `cache_control: ephemeral` for 90% cost reduction. User message contains dynamic per-cycle data (phase banner, whiteboard state, episode, cameras, timestamp).
- **Vision blocks.** Camera frames (max 2) and human photos are sent as `image_url` content blocks in the user message. Job thumbnail sent in the system message.

### Data directory separation
- **Generated files in WALLEE_DATA_DIR.** JOB_CONTEXT.md, OBSERVATIONS.md, job_thumbnail.png, ledger.db, diary DBs, and outbox all live in the configured data directory (default `/var/lib/wallee`), not in the source tree's `knowledge/` directory.

### System
- **All components run as threads in one process** (not separate OS processes as originally specced). Single `main.py` manages everything. If the main process crashes, the safety kernel dies with it. For production hardening, extract the safety kernel to a separate systemd-managed process.
- **Data directory fallback.** If `/var/lib/wallee` is not writable, falls back to `~/.wallee/data`.
- **Dashboard port.** HTTP on 8081, WebSocket on 8082 (to avoid conflict with camera server on 8080).
