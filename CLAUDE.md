# CLAUDE.md — Wallee

## What this is

Wallee is an autonomous 3D printer operator. An LLM proposes actions. Deterministic code decides if they're safe. A safety kernel watches independently. The LLM is **untrusted** — it can propose, never execute directly.

Hardware: Raspberry Pi 5 (8GB), Prusa Core One+, 3DO nozzle camera, Buddy WiFi cameras.

## Architecture (read this first)

```
LLM (OpenRouter, GPT 5.4)
        ↓ proposes (JSON)
Agent Loop (thread) ─── reads ──→ Whiteboard (Redis, ~80 live keys)
        ↓ writes proposal
Ledger (SQLite WAL)
        ↓ polls
Engine (thread) ── 6 gates ──→ Tool executes on hardware
        ↑ independent
Safety Kernel (thread) ── heartbeats + overcurrent + ESTOP
```

**Gate sequence:** ESTOP → Queue guard → Deadline → Approval → TOCTOU precheck → Dispatch

**ESTOP bypasses the engine entirely.** Sends M25 directly to printer via `wallee/safety/estop.py`. Telegram, CLI, and safety kernel all use this shared helper.

## Rules for coding agents

**Read before editing.** Read the file you're changing + its tests before making any edit.

**Verify after editing.** Run the relevant grep/test to confirm your change landed. Don't report "done" without verification output.

**Never do these things:**
- Add dangerous default params to actuators (e.g., `target=0`). All hardware actuators must reject missing required params.
- Put dynamic content in the system message (defeats prompt caching). Camera frames, whiteboard data, episode → user message only.
- Put static content in the user message. SOUL.md, LEARNED.md, tool list → system message only.
- Import LangChain, LangGraph, or similar frameworks. The architecture is intentionally simple.
- Add `requires_approval=True` to any tool without explicit instruction. Currently all tools are `requires_approval=False`.
- Use `KEYS *` in Redis. Use `SCAN` instead.

**Common mistakes agents make on this codebase:**
- Editing code but not updating the corresponding test → Codex audit catches the mismatch
- Reporting stale test counts from local env instead of running fresh
- Claiming a fix is "done" when grep shows the old code is still there
- Adding `os.environ.get()` instead of using the `cfg` config object
- Duplicating code across files instead of extracting to shared module
- Forgetting that pause/resume uses M25/M24 via `POST /api/v1/gcode`, NOT `PUT /api/v1/job`

## File layout

```
wallee/
├── agent/
│   ├── loop.py              # Agent loop: read state → build prompt → call LLM → route decision
│   ├── prompt.py            # Cached prefix (system) + dynamic suffix (user) prompt builder
│   ├── parser.py            # Parse LLM JSON → Decision objects, validate, clamp intervals
│   ├── llm_client.py        # OpenRouter client: structured output, validation, self-healing retry
│   └── change_detector.py   # Detect external whiteboard changes between cycles
├── engine/
│   ├── dispatch.py          # Engine: poll ledger, run 6-gate sequence, dispatch tools
│   └── reconcile.py         # Crash recovery: resolve in-flight actions on boot
├── safety/
│   ├── kernel.py            # Watchdog: heartbeats + overcurrent + ESTOP monitoring
│   └── estop.py             # Direct M25 to printer (shared by Telegram, CLI, kernel)
├── ledger/
│   ├── db.py                # SQLite WAL: proposals, approvals, episodes, events
│   └── diary.py             # Per-device-group idempotency for crash recovery
├── bus/
│   ├── network.py           # HTTP client for PrusaLink
│   ├── serial.py            # USB serial for G-code
│   └── udp_listener.py      # UDP InfluxDB metrics parser (port 8514)
├── whiteboard/
│   └── client.py            # Redis client: SET with TTL, ring buffers, trends
├── tools/
│   ├── decorator.py         # @tool decorator
│   ├── registry.py          # Auto-discovers device packs, registers tools, sensor wake triggers
│   └── builtins/
│       ├── call_human_tool.py   # Escalate to human (Telegram-wired via injection in main.py)
│       ├── discover.py          # Probe all buses for connected hardware
│       ├── remember.py          # Persist observations to OBSERVATIONS.md
│       ├── web_search.py        # Separate OpenRouter call with web search enabled
│       ├── differential.py      # Rate of change for whiteboard keys
│       ├── trends.py            # Trend direction + magnitude
│       └── sensor_history.py    # Raw ring buffer values
├── device_packs/
│   ├── host_pi/             # CPU temp, system stats, USB, network
│   ├── prusa_link/          # HTTP API: printer state, temps, job, files + actuators
│   ├── prusa_metrics/       # UDP: 62 InfluxDB metrics at 50pkt/s
│   ├── prusa_serial/        # USB serial: endstops, diagnostic G-code
│   └── pi_cameras/          # Nozzle cam (ustreamer), buddy cams (RTSP auto-discovery)
├── human/
│   ├── telegram.py          # Telegram bot: commands, approvals, intents, ESTOP
│   ├── cli.py               # CLI REPL: intents, approvals, ESTOP
│   └── call_human.py        # Fallback chain: Telegram → CLI → durable outbox
├── ui/
│   └── dashboard.py         # Read-only web dashboard at :8081
├── knowledge/               # Human-curated (checked into git)
│   ├── SOUL.md              # Agent identity, rules, confidence framework
│   └── LEARNED.md           # 3D printing domain knowledge
├── config.py                # All config from .env with typed defaults
└── main.py                  # Boot sequence: start all threads, wire dependencies
```

**Runtime-generated files** (in `WALLEE_DATA_DIR`, default `/var/lib/wallee/`):
- `OBSERVATIONS.md` — persistent cross-job memory (written by remember tool)
- `JOB_CONTEXT.md` — per-print context (created on PREPARING, deleted on FINISHED)
- `job_thumbnail.png` — reference image from PrusaLink (per-print lifecycle)
- `ledger.db` — action history
- `diary_*.db` — idempotency databases

## Tool inventory

### Sensors (background publishers, no gates)

| Tool | Pack | refresh_hz | Publishes |
|------|------|-----------|-----------|
| read_cpu_temp | host_pi | 1.0 | host.cpu_temp |
| read_system_stats | host_pi | 0.5 | host.cpu_percent, memory_percent, disk_percent, uptime_hours |
| read_usb_devices | host_pi | 0.1 | host.usb_devices |
| read_network_interfaces | host_pi | 0.2 | host.network_interfaces |
| read_printer_state | prusa_link | 0.5 | printer.state, job_state, temps, speed, flow, progress |
| read_printer_info | prusa_link | 0.1 | printer.firmware, model, serial, nozzle_diameter |
| read_file_list | prusa_link | 0.02 | printer.files |
| read_prusalink_dashboard | prusa_link | 0.1 | prusalink.* (material, z_height, flags) |
| read_job_phase | prusa_link | 1.0 | job.phase, job.phase_detail, job.time_in_phase_s |
| read_temperatures | prusa_metrics | 0.3 | printer.temp_* (nozzle, bed, chamber, heatbreak, board, mcu) |
| read_electrical | prusa_metrics | 0.3 | printer.volt_*, curr_*, oc_* |
| read_fans | prusa_metrics | 0.5 | printer.fan_* |
| read_position | prusa_metrics | 5.0 | printer.pos_*, ipos_* |
| read_filament | prusa_metrics | 1.0 | printer.fsensor_* |
| read_enclosure | prusa_metrics | 0.3 | printer.door_sensor, temp_chamber |
| read_firmware_health | prusa_metrics | 0.2 | printer.heap_*, cpu_usage, stepper_stall |
| read_print_state | prusa_metrics | 0.3 | printer.is_printing, print_filename, heater_enabled, pwm_* |
| read_nozzle_camera | pi_cameras | 1.0 | camera.nozzle_frame, nozzle_status |
| read_buddy_cameras | pi_cameras | 0.1 | camera.buddy_count, buddy{n}_status, buddy{n}_frame |

### Actuators (on-demand, go through engine gates)

| Tool | Pack | has_precheck | Required params |
|------|------|-------------|-----------------|
| pause_print | prusa_link | Yes | (none) |
| resume_print | prusa_link | Yes | (none) |
| cancel_print | prusa_link | Yes | (none) |
| start_print | prusa_link | Yes | path |
| set_temperature | prusa_link | Yes | target, heater |
| home_axes | prusa_link | Yes | (none) |
| disable_motors | prusa_link | Yes | (none) |
| set_speed_factor | prusa_link | Yes | percent |
| set_flow_factor | prusa_link | Yes | percent |
| set_position | prusa_link | Yes | at least one of x/y/z |
| extrude | prusa_link | Yes | length_mm |
| retract | prusa_link | Yes | length_mm |
| read_endstops | prusa_serial | No | (none) |
| send_gcode | prusa_serial | No | gcode (allowlisted) |
| call_human | builtin | No | message |
| discover_hardware | builtin | No | (none) |
| remember | builtin | No | observation |
| web_search | builtin | No | query |
| trends | builtin | No | key |
| differential | builtin | No | key |
| get_sensor_history | builtin | No | key |

All actuators have `requires_approval=False`.

## Prompt structure (caching)

**System message (cached prefix — static within a job):**
1. SOUL.md
2. LEARNED.md
3. OBSERVATIONS.md (if exists)
4. Tool list with parameter descriptions
5. JOB_CONTEXT.md (if active job)
6. Job thumbnail (if available) — "the model being printed"
7. Static instruction: "Respond with JSON."

**User message (dynamic suffix — changes every cycle):**
1. Pending callout status (FIRST — anti-hallucination)
2. Phase banner: `PHASE: PRINTING (52%) — 340s`
3. External changes (if any)
4. Whiteboard sensor data (text, no images)
5. Recent actions episode
6. Live camera frames (vision blocks)

## LLM output schema

```json
{
  "type": "ACTION | ACTION_CHAIN | WAIT | CALL_HUMAN",
  "observation": "required — what you see",
  "reasoning": "required — why you chose this",
  "tool": "for ACTION",
  "params": {},
  "actions": [{"tool": "...", "params": {}, "reasoning": "..."}],
  "message": "for CALL_HUMAN",
  "severity": "info | warning | critical",
  "check_after_s": 30
}
```

Validation + self-healing retry: if malformed, client retries once with error as correction. If retry also fails, returns safe WAIT. ACTION_CHAIN max: 5 steps. Failed step → remaining steps SKIPPED.

## Configuration

```bash
# Required
OPENROUTER_API_KEY=
OPENROUTER_MODEL=openai/gpt-5.4
PRUSALINK_HOST=192.168.0.195
PRUSALINK_API_KEY=
REDIS_URL=redis://localhost:6379

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_ALLOWED_USER_IDS=

# Paths
WALLEE_DATA_DIR=/var/lib/wallee
WALLEE_LOG_DIR=/var/log/wallee

# Cycle timing
AGENT_MIN_CHECK_INTERVAL_S=10
AGENT_MAX_CHECK_INTERVAL_S=30
AGENT_MAX_CHECK_INTERVAL_IDLE_S=120
AGENT_DEFAULT_CHECK_INTERVAL_S=15

# Device packs
DEVICE_PACKS=host_pi,prusa_link,prusa_metrics,prusa_serial,pi_cameras
```

## PrusaLink quirks

- **Pause/resume:** M25/M24 via `POST /api/v1/gcode`. NOT `PUT /api/v1/job` (405 on Core One+).
- **HTTP API says PRINTING during purge.** Use `job.phase` not raw `printer.state`.
- **Serial disconnects every 1-3s.** Normal Core One+ behavior. Not an error.
- **UDP metrics on port 8514.** InfluxDB line protocol in Syslog RFC 5424.
- **Thumbnail:** `GET /thumb/l/usb/{filename}` or `/thumb/s/usb/{filename}`.

## Event-driven wake

Agent wakes immediately on: `printer.state` change, `printer.fsensor_state` change, human intent (Telegram/CLI), ESTOP. Implementation: `threading.Event.wait(timeout)`. Cannot preempt in-flight LLM call.

## Testing and deployment

```bash
pytest                          # Full suite (baseline: ~491 tests)
pytest wallee/device_packs/prusa_link/tests/  # Specific pack

# Deploy to Pi
rsync -avz --exclude .venv --exclude __pycache__ --exclude .env \
  --exclude '*.pyc' --exclude '*.db' --exclude wallee-audit \
  . b0@192.168.0.188:~/wallee/

# Clean slate before test runs
ssh b0@192.168.0.188 'redis-cli FLUSHALL && rm -f /var/lib/wallee/*.db'
```

Pi: `b0@192.168.0.188` | Printer: `192.168.0.195` | Dashboard: `:8081`