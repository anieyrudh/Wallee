# Wallee — Ground Truth Specification

**Status:** Normative. If implementation conflicts with this document, this document wins.
**Audience:** Coding agents (Claude Code) and developers.
**Hardware:** Raspberry Pi 5 (8GB), Raspberry Pi OS Bookworm.
**LLM Provider:** OpenRouter → google/gemini-3.1-pro-preview.

> If anything is ambiguous, implement the safer interpretation and surface the ambiguity as an issue.

---

## 1. What Wallee is

Wallee is an agentic policy loop for autonomous hardware control. An LLM reasons about sensor data and proposes actions. Deterministic code decides whether those actions are safe to execute. A safety kernel watches independently.

The LLM is untrusted. It can propose. It cannot dispatch, approve, or bypass.

---

## 2. Axioms

1. **Actions are irreversible.** Treat every physical action as a one-way door.
2. **LLM can be wrong.** Treat LLM output as untrusted input.
3. **Crashes are guaranteed.** RAM is not state. Only disk (Ledger) survives.
4. **Latency can be unbounded.** Use monotonic deadlines, not assumptions.
5. **LLM chooses policy; deterministic code enforces safety.** Safety is in code, never in prompts or markdown.

---

## 3. Architecture overview

```
┌─────────────────────────────────────────────────────────┐
│                    Raspberry Pi 5                        │
│                                                         │
│  ┌─────────────┐  ┌──────────┐  ┌──────────────────┐   │
│  │ Agent Loop   │  │ Engine   │  │ Safety Kernel     │   │
│  │ (Python thd) │  │ (Python  │  │ (Python thread,   │   │
│  │              │  │  thread) │  │  independent)     │   │
│  │ Calls LLM    │  │ Gates +  │  │ Heartbeats +      │   │
│  │ via OpenRouter│  │ dispatch │  │ overcurrent +     │   │
│  │ + vision     │  │          │  │ ESTOP monitoring   │   │
│  └──────┬───────┘  └────┬─────┘  └────────┬──────────┘  │
│         │               │                  │             │
│         │ proposals      │ dispatch         │ monitors    │
│         ▼               ▼                  ▼             │
│  ┌──────────────────────────────────────────────────┐   │
│  │              Device Packs (5 packs)               │   │
│  │  host_pi | prusa_link | prusa_metrics |           │   │
│  │  prusa_serial | pi_cameras                        │   │
│  └──────────────────────┬───────────────────────────┘   │
│                         │ bus frameworks                 │
│                         ▼                                │
│  ┌──────────────────────────────────────────────────┐   │
│  │   sysfs | HTTP | UDP:8514 | Serial | Camera HTTP  │   │
│  └──────────────────────┬───────────────────────────┘   │
│                         ▼                                │
│                  Physical Hardware                        │
│    Prusa Core One+ (PrusaLink HTTP + metrics UDP +       │
│    USB serial) + 3DO nozzle cam + Buddy WiFi cams        │
│                                                         │
│  ┌────────────────┐  ┌────────────────────┐             │
│  │ Whiteboard     │  │ Ledger + Diary     │             │
│  │ (Redis)        │  │ (SQLite WAL)       │             │
│  │ Live state     │  │ Action history     │             │
│  │ + ring buffers │  │ + crash recovery   │             │
│  └────────────────┘  └────────────────────┘             │
│                                                         │
│  ┌─────────────────────────────────────────────────┐    │
│  │ Human I/F: CLI + Telegram + Dashboard (read-only)│    │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
         │                              │
         ▼                              ▼
   Cloud LLM (OpenRouter)        Prusa Printer
   google/gemini-3.1-pro-preview  (PrusaLink HTTP + UDP + serial)
```

### Data flows
- **Down:** Agent proposes → Engine gates → Tool executes → Hardware
- **Sideways:** Everything publishes to Whiteboard → Agent reads it
- **Up (vision):** Camera frames + human photos → base64 on whiteboard → image_url blocks in LLM messages
- **Independent:** Safety kernel monitors heartbeats + overcurrent + ESTOP, calls human on failure

### Trust boundary
The Agent is UNTRUSTED. It may:
- Read whiteboard (all state)
- Read knowledge files
- Write proposals to ledger
- Publish agent.heartbeat to whiteboard

It may NOT:
- Call tool functions directly
- Write to whiteboard (except heartbeat + agent.last_decision + agent.activity_log)
- Approve actions
- Bypass the engine

---

## 4. Terminology

- **Tool** — the overarching name. A piece of Python code that interfaces with hardware or performs an action.
- **Sensor tool** — a tool that reads hardware on a schedule and publishes to the whiteboard. Runs in background. LLM reads output, never calls it. Also called a "publisher."
- **Actuator tool** — a tool that performs an action on demand. LLM proposes, engine dispatches through gates.
- **Built-in tool** — a tool always available regardless of hardware (discover_hardware, call_human, trends, differential, get_sensor_history, remember).
- **Device pack** — a folder containing sensor tools + actuator tools + tests for one specific hardware device.

Do NOT use the word "skill." Always say "tool."

---

## 5. Tool contracts

### 5.1 Sensor tool (publisher)

```python
@tool(kind="sensor", refresh_hz=1.0, history_depth=10)
def read_cpu_temp():
    """Read CPU temperature from thermal zone."""
    raw = THERMAL_ZONE.read_text().strip()
    temp_c = int(raw) / 1000.0

    if temp_c < -40 or temp_c > 120:
        raise ValueError(f"CPU temp {temp_c} outside sane range")

    return {"host.cpu_temp": round(temp_c, 1)}
```

**Required fields:**
| Field | Type | Description |
|-------|------|-------------|
| `kind` | `"sensor"` | Identifies this as a publisher |
| `refresh_hz` | float | Read frequency. Publisher decides based on hardware. |
| `history_depth` | int | Ring buffer size. 0 = no history. |

**Auto-calculated:**
| Field | Formula | Override? |
|-------|---------|-----------|
| `ttl_ms` | `int((1000 / refresh_hz) * 2)` | Yes, via decorator param |

**Optional fields:**
| Field | Type | Description |
|-------|------|-------------|
| `safety_limits` | dict | Limits for safety kernel. System-level only. |
| `bus` | str | Which bus framework ("i2c", "uart", "gpio", etc.) |
| `address` | Any | Bus-specific address for discovery matching |

**Runtime behavior:**
The registry runs each sensor in a background thread at `1/refresh_hz` interval. Each returned key:value pair is published atomically to the whiteboard via `publish_many()` with the computed TTL. If `history_depth > 0`, each value is LPUSH'd to a Redis list `{key}:history` and LTRIM'd to `history_depth`. Timestamps are stored in `{key}:history_ts`.

The LLM never calls sensor tools. It reads the whiteboard.

### 5.2 Actuator tool

```python
def _precheck_resume_print(whiteboard=None, **kwargs) -> dict:
    job_state = whiteboard.read("printer.job_state")
    if job_state != "PAUSED":
        return {"error": f"Cannot resume: job is {job_state}, not PAUSED"}
    return {"status": "ok"}

@tool(kind="actuator", requires_approval=False, max_proposal_age_ms=30000,
      precheck_fn=_precheck_resume_print)
def resume_print(whiteboard=None, **kwargs) -> dict:
    """Resume a paused print via G-code M24."""
    http = _get_http()
    result = http.post("/api/v1/gcode", json_body={"command": "M24"})
    if "error" in result:
        return result
    return {"status": "success", "action": "resume_print", "gcode": "M24"}
```

**Required fields:**
| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `kind` | `"actuator"` | — | Identifies this as on-demand |
| `requires_approval` | bool | `False` | If True, engine asks human before dispatch |
| `max_proposal_age_ms` | int | `30000` | Deadline. Stale proposals are REJECTED. |

**Optional fields:**
| Field | Type | Description |
|-------|------|-------------|
| `precheck_fn` | callable | Side-effect-free precondition check. Engine calls before dispatch. |

**Return value:** Always a dict. `{"status": "success", ...}` or `{"error": "reason"}`. Stored in ledger. Shown to LLM in episode context.

**Precheck vs execute:** Prechecks are separate functions passed via the `precheck_fn` decorator parameter. The engine calls `precheck()` as TOCTOU gate 4. If it returns `{"error": ...}`, the action is REJECTED. If it returns `{"status": "ok"}`, dispatch proceeds. The main function body also contains runtime precondition checks as defense-in-depth.

**RULE:** Preconditions check DISCRETE STATES (job.state == "PAUSED", faults is empty). NOT continuous sensor values. Temperature fluctuating ±0.2°C must NOT trigger TOCTOU rejection.

### 5.3 Built-in tools

Always available. Not tied to any device pack. Registered from `wallee.tools.builtins`.

```python
@tool(kind="actuator", requires_approval=False)
def discover_hardware(whiteboard=None, **kwargs) -> dict:
    """Run lightweight local discovery for camera and printer-related hardware.
    Checks configured device packs, serial port, nozzle camera port, buddy cameras."""

@tool(kind="actuator", requires_approval=False)
def call_human(message: str = "", severity: str = "info", whiteboard=None, **kwargs) -> dict:
    """Escalate to human operator. Fallback: Telegram -> CLI -> durable outbox."""

@tool(kind="actuator", requires_approval=False)
def trends(key: str = "", whiteboard=None, **kwargs) -> dict:
    """Trend analysis for a whiteboard key: rising/falling/stable + magnitude."""

@tool(kind="actuator", requires_approval=False)
def differential(key: str = "", whiteboard=None, **kwargs) -> dict:
    """Rate of change for a numerical whiteboard key (units per second).
    Uses actual timestamps from history_ts for accurate rate calculation."""

@tool(kind="actuator", requires_approval=False)
def get_sensor_history(key: str = "", depth: int = 30, whiteboard=None, **kwargs) -> dict:
    """Raw ring buffer values for deeper analysis."""

@tool(kind="actuator", requires_approval=False)
def remember(observation: str = "", whiteboard=None, **kwargs) -> dict:
    """Persist an observation to knowledge/OBSERVATIONS.md.
    Use to record patterns, operator instructions, or visual observations
    that should persist across restarts. Capped at 50 most recent entries."""
```

`trends()` returns direction + magnitude ("rising +0.4 over 10 readings").
`differential()` returns rate of change using real timestamps ("changing at +0.04/s").
`get_sensor_history()` returns raw values from the ring buffer.
`remember()` appends a timestamped observation to `knowledge/OBSERVATIONS.md` (newest first, max 50).

**Note:** `web_search` and `git_pull` are NOT implemented. Do not reference them.

---

## 6. Registered tools — complete inventory

### 6.1 Sensor tools (publishers)

| Tool | Pack | refresh_hz | history_depth | Whiteboard keys |
|------|------|-----------|---------------|-----------------|
| `read_cpu_temp` | host_pi | 1.0 | 10 | `host.cpu_temp` |
| `read_system_stats` | host_pi | 0.2 | 5 | `host.cpu_percent`, `host.memory_percent`, `host.disk_percent`, `host.uptime_s` |
| `read_usb_devices` | host_pi | 0.02 | 0 | `host.usb_devices` |
| `read_network_interfaces` | host_pi | 0.1 | 0 | `host.network_interfaces` |
| `read_printer_state` | prusa_link | 0.5 | 0 | `printer.state`, `printer.temp_nozzle`, `printer.target_nozzle`, `printer.temp_bed`, `printer.target_bed`, `printer.speed`, `printer.flow`, `printer.job_state`, `printer.job_progress`, `printer.job_time_remaining_s`, `printer.job_time_printing_s` |
| `read_printer_info` | prusa_link | 0.1 | 0 | `printer.firmware`, `printer.model`, `printer.serial`, `printer.nozzle_diameter` |
| `read_file_list` | prusa_link | 0.02 | 0 | `printer.files` |
| `read_temperatures` | prusa_metrics | 0.3 | 30 | `printer.temp_nozzle`, `printer.target_nozzle`, `printer.temp_bed`, `printer.target_bed`, `printer.temp_chamber`, `printer.temp_heatbreak`, `printer.temp_board`, `printer.temp_mcu` |
| `read_electrical` | prusa_metrics | 0.3 | 30 | `printer.volt_bed`, `printer.volt_nozzle`, `printer.curr_nozzle`, `printer.oc_nozzle`, `printer.oc_input` |
| `read_fans` | prusa_metrics | 0.5 | 10 | `printer.fan_heatbreak_*`, `printer.fan_print_*`, `printer.xbe_fan_{1,2,3}_*` |
| `read_position` | prusa_metrics | 5.0 | 10 | `printer.pos_{x,y,z}`, `printer.ipos_{x,y,z}` |
| `read_filament` | prusa_metrics | 1.0 | 10 | `printer.fsensor_state`, `printer.fsensor_flow`, `printer.fsensor_rotation`, `printer.fsensor_rotation_inc` |
| `read_enclosure` | prusa_metrics | 0.3 | 5 | `printer.door_sensor`, `printer.temp_chamber` |
| `read_firmware_health` | prusa_metrics | 0.2 | 5 | `printer.heap_free`, `printer.heap_total`, `printer.cpu_usage`, `printer.stepper_stall` |
| `read_print_state` | prusa_metrics | 0.3 | 0 | `printer.is_printing`, `printer.print_filename`, `printer.heater_enabled`, `printer.pwm_nozzle`, `printer.pwm_bed` |
| `read_nozzle_camera` | pi_cameras | 1.0 | 3 | `camera.nozzle_frame` (base64), `camera.nozzle_frame_size`, `camera.nozzle_status`, `camera.nozzle_port` |
| `read_buddy_cameras` | pi_cameras | 0.1 | 3 | `camera.buddy{N}_frame` (base64), `camera.buddy{N}_frame_size`, `camera.buddy{N}_status`, `camera.buddy{N}_ip`, `camera.buddy_count` |

### 6.2 Actuator tools

| Tool | Pack | requires_approval | has_precheck | max_proposal_age_ms | Description |
|------|------|:-----------------:|:------------:|:-------------------:|-------------|
| `pause_print` | prusa_link | No | Yes | 15000 | Pause via M25 G-code |
| `resume_print` | prusa_link | No | Yes | 30000 | Resume via M24 G-code |
| `cancel_print` | prusa_link | **Yes** | Yes | 30000 | Cancel via DELETE /api/v1/job |
| `start_print` | prusa_link | **Yes** | Yes | 60000 | Start via POST /api/v1/files/{path}/pprint |
| `set_temperature` | prusa_link | No | Yes | 15000 | Set temp via M104/M140/M141 G-code |
| `home_axes` | prusa_link | No | Yes | 30000 | Home all axes via G28 |
| `disable_motors` | prusa_link | No | Yes | 15000 | Disable steppers via M18 |
| `set_speed_factor` | prusa_link | No | Yes | 15000 | Print speed % via M220 |
| `set_flow_factor` | prusa_link | No | Yes | 15000 | Flow rate % via M221 |
| `set_position` | prusa_link | No | Yes | 30000 | Move toolhead via G1 |
| `extrude` | prusa_link | No | Yes | 30000 | Extrude filament via G1 E (needs >=170°C) |
| `retract` | prusa_link | No | Yes | 30000 | Retract filament via G1 E- (needs >=170°C) |
| `read_endstops` | prusa_serial | No | No | 10000 | Read endstop states via M119 |
| `send_gcode` | prusa_serial | No | No | 30000 | Send allowed diagnostic G-code (M105, M114, M115, M119, M503 only) |
| `discover_hardware` | builtin | No | No | 30000 | Scan for cameras and serial port |
| `call_human` | builtin | No | No | 30000 | Escalate to human operator |
| `trends` | builtin | No | No | 30000 | Trend analysis for a whiteboard key |
| `differential` | builtin | No | No | 30000 | Rate of change for a whiteboard key |
| `get_sensor_history` | builtin | No | No | 30000 | Raw ring buffer values |
| `remember` | builtin | No | No | 30000 | Persist observation to knowledge/OBSERVATIONS.md |

### 6.3 Precheck summary

All prusa_link actuators have prechecks. Key constraints enforced:

| Actuator | Precheck logic |
|----------|---------------|
| `pause_print` | job.phase != PREPARING (405 bug), job_state == PRINTING |
| `resume_print` | job_state == PAUSED, printer.state in (PAUSED, ATTENTION, READY) |
| `cancel_print` | job_state not IDLE |
| `start_print` | job_state == IDLE, printer.state != ERROR, file_path required |
| `set_temperature` | heater in (nozzle, bed, chamber), within safe limits (nozzle 0-300, bed 0-120, chamber 0-50), printer.state != ERROR |
| `home_axes` | printer.state in (IDLE, FINISHED) |
| `disable_motors` | printer.state in (IDLE, FINISHED) |
| `set_speed_factor` | 10-200%, printer.state == PRINTING |
| `set_flow_factor` | 10-150%, printer.state == PRINTING |
| `set_position` | X 0-252, Y 0-220, Z 0-220, printer.state in (IDLE, FINISHED) |
| `extrude` | 0-100mm, printer.state in (IDLE, FINISHED), nozzle >= 170°C |
| `retract` | 0-100mm, printer.state in (IDLE, FINISHED), nozzle >= 170°C |

---

## 7. Agent loop

```python
class AgentLoop:
    def run_once(self):
        # 1. Read entire whiteboard with trend annotations
        state = self.wb.read_all_with_trends()

        # 2. Read current episode from ledger
        episode = self.ledger.current_episode()

        # 3. Read human intent (skip if already responded)
        intent = self.wb.read("human.intent")

        # 4. Detect external changes (not caused by Wallee)
        external_changes = self._change_detector.detect(state, episode)

        # 5. Load knowledge (SOUL.md, HARDWARE.md, LEARNED.md)
        knowledge = self._load_knowledge()

        # 6. Build prompt + messages (including vision content)
        prompt = build_prompt(state, episode, intent, knowledge, tools, ...)
        messages = build_messages(prompt, state)  # adds camera frames + human photos

        # 7. Call LLM (stateless, with vision)
        raw_response = self.llm.call(prompt, messages=messages)

        # 8. Parse (with printer-state-aware interval clamping)
        decision = parse_llm_output(raw_response, printer_state=state.get("printer.state"))

        # 9. Route decision
        self._route_decision(decision)
```

**RULE:** Agent may only write to the ledger (proposals, waits) and to `agent.heartbeat`, `agent.last_decision`, and `agent.activity_log` on the whiteboard. Nothing else.

### 7.1 LLM output format

The LLM must return JSON (enforced by json_schema response_format):

```json
{"type": "ACTION", "tool": "resume_print", "params": {}, "reason": "temps nominal, operator requested resume"}
```
```json
{"type": "WAIT", "reason": "all nominal, no action needed", "check_after_s": 60}
```
```json
{"type": "CALL_HUMAN", "message": "humidity at 72%, unsure if filament is safe", "severity": "warning"}
```

**Output parser rules:**
- Invalid JSON → default to WAIT, log parse error
- Unknown type → default to WAIT, log error
- Tool doesn't exist → default to WAIT, log error
- Missing required fields → default to WAIT, log error
- Parser NEVER crashes. Unparseable output = do nothing.
- Markdown code fences (```` ```json ... ``` ````) are automatically stripped.

### 7.2 Agent cycle timing

Configurable via `.env`. Enforced by the parser (code, not LLM):

| State | check_after_s range | Default |
|-------|-------------------|---------|
| Printing / Paused / Attention | 30 – 120s | 60s |
| Idle / other | 30 – 300s | 60s |

The LLM requests a `check_after_s` value; the parser clamps it to the configured range. The agent sleeps in 0.5s chunks so `stop()` remains responsive.

### 7.3 External change detector

The `ExternalChangeDetector` compares whiteboard snapshots between cycles. It tracks:

| Key | Expected Wallee tool |
|-----|---------------------|
| `printer.state` | any change flagged |
| `printer.target_nozzle` | `set_temperature` |
| `printer.target_bed` | `set_temperature` |
| `printer.target_chamber` | `set_temperature` |
| `printer.speed` | `set_speed_factor` |
| `printer.flow` | `set_flow_factor` |
| `printer.job_progress` | any change flagged |

Changes not attributable to a DISPATCHED/DONE Wallee action are reported at the top of the LLM prompt as `!!! EXTERNAL CHANGES DETECTED !!!`.

### 7.4 Vision support

The `build_messages()` function includes camera frames and human photos as `image_url` content blocks in the user message:

- **Camera priority order:** nozzle → buddy1 → buddy2 (max 2 cameras per cycle)
- **Human photos:** Operator sends photo via Telegram → published as `human.image` on whiteboard → included in next LLM call
- Each frame is base64 JPEG (~200KB per frame)

### 7.5 LLM provider config (OpenRouter)

```python
class LLMClient:
    def __init__(self, api_key, model="google/gemini-3.1-pro-preview", enable_web_search=True):
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"

    def call(self, prompt, messages=None):
        payload = {
            "model": self.model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": DECISION_SCHEMA,  # strict structured output
            },
            "plugins": [
                {"id": "web", "max_results": 3},  # Exa web search
                {"id": "response-healing"},         # auto-fix malformed JSON
            ],
            "max_tokens": 2048,
        }
```

**Features enabled via OpenRouter:**
- Structured outputs (json_schema) — guarantees valid decision JSON
- Response healing plugin — fixes malformed JSON automatically
- Web search plugin (Exa) — real-time web access for troubleshooting
- Prompt caching — system message marked `cache_control: ephemeral` for 90% discount on repeated prompts
- Retry with exponential backoff (3 retries, 2s base)

### 7.6 Prompt structure

The system prompt is assembled in this order:

1. **External changes** (if any) — `!!! EXTERNAL CHANGES DETECTED !!!`
2. **SOUL.md** — mission briefing and decision heuristics
3. **HARDWARE.md** — auto-generated hardware inventory (if exists)
4. **LEARNED.md** — printing knowledge and troubleshooting playbook
5. **Whiteboard state** — all keys except skipped verbose/binary ones
6. **Human intent** — current operator intent (if active and not already handled)
7. **Recent actions** — episode context from ledger (max 12 actions, or 5 fallback)
8. **Available tools** — actuator tool names + descriptions + approval requirement
9. **Instructions** — JSON output format, rules, timing guidance
10. **Current time**

Skipped from whiteboard state: `host.usb_devices`, `host.network_interfaces`, `printer.files`, `printer.firmware`, `printer.serial`, `printer.model`, `printer.nozzle_diameter`, `agent.last_decision`, `agent.heartbeat`, `engine.heartbeat`, all `camera.*_frame` keys (sent as vision blocks), `camera.*_frame_size`, `human.image`, and any string values > 100 chars.

---

## 8. Engine

The engine is a separate thread (started in `main.py`). It polls the ledger for PROPOSED actions and runs them through gates.

### 8.1 Gate sequence (as implemented in dispatch.py)

```
Gate 0: ESTOP check     — if safety.estop is set on whiteboard, REJECT immediately
Gate 1: Queue guard     — if device_group has DISPATCHED action, SKIP (retry next poll)
Gate 2: Deadline        — if age_ms > max_proposal_age_ms, REJECT as expired
Gate 3: Approval        — if requires_approval, check for approval record
                          If none: set WAITING_APPROVAL, notify via Telegram, skip
                          If REJECT: REJECT
                          If APPROVE: continue to gate 4
Gate 4: TOCTOU          — if tool has_precheck, call precheck(whiteboard, **params)
                          If {"error": ...}: REJECT with TOCTOU reason
Gate 5: Dispatch        — write IN_FLIGHT to diary (fsync), set DISPATCHED in ledger
                          Execute tool. Record SUCCESS or FAILED in diary + ledger.
```

**Engine also:** Polls WAITING_APPROVAL actions. Expires them after `approval_timeout` (default 300s). Re-processes from gate 1 when approval arrives.

**Engine MUST NOT:** call LLM, perform network I/O (except local Redis/SQLite + tool execution), access cloud APIs.

### 8.2 Reconcile loop (crash recovery)

Runs on boot (from `main.py`) and checks for DISPATCHED actions left over from a crash:

```
Diary says SUCCESS → set ledger DONE
Diary says FAILED  → set ledger FAILED
Diary says IN_FLIGHT → set ledger UNKNOWN, CALL_HUMAN (critical)
Diary has no record → set ledger FAILED ("never dispatched")
```

---

## 9. Whiteboard (Redis)

### 9.1 Schema — all published keys

**Host Pi:**
```
host.cpu_temp              float   (history: 10)
host.cpu_percent           float   (history: 5)
host.memory_percent        float   (history: 5)
host.disk_percent          float   (history: 5)
host.uptime_s              float   (history: 5)
host.usb_devices           list
host.network_interfaces    list
```

**Printer (HTTP API — prusa_link):**
```
printer.state              str     IDLE|PRINTING|PAUSED|ATTENTION|ERROR|...
printer.job_state          str     IDLE|PRINTING|PAUSED|...
printer.job_progress       float   0-100
printer.job_time_remaining_s  int
printer.job_time_printing_s   int
printer.temp_nozzle        float   (also from metrics)
printer.target_nozzle      float   (also from metrics)
printer.temp_bed           float   (also from metrics)
printer.target_bed         float   (also from metrics)
printer.speed              int     speed factor %
printer.flow               int     flow factor %
printer.firmware           str
printer.model              str
printer.serial             str
printer.nozzle_diameter    str
printer.files              list
```

**Printer (UDP metrics — prusa_metrics):**
```
printer.temp_nozzle        float   (history: 30)
printer.target_nozzle      int     (history: 30)
printer.temp_bed           float   (history: 30)
printer.target_bed         int     (history: 30)
printer.temp_chamber       float   (history: 30 via read_temperatures, 5 via read_enclosure)
printer.temp_heatbreak     float   (history: 30)
printer.temp_board         float   (history: 30)
printer.temp_mcu           int     (history: 30)
printer.volt_bed           float   (history: 30)
printer.volt_nozzle        float   (history: 30)
printer.curr_nozzle        float   (history: 30)
printer.oc_nozzle          int     (history: 30) — safety-critical overcurrent
printer.oc_input           int     (history: 30) — safety-critical overcurrent
printer.fan_heatbreak_{state,pwm,rpm}  (history: 10)
printer.fan_print_{state,pwm,rpm}      (history: 10)
printer.xbe_fan_{1,2,3}_{pwm,rpm}      (history: 10)
printer.pos_{x,y,z}       float   (history: 10) — mm
printer.ipos_{x,y,z}      int     (history: 10) — steps
printer.fsensor_state      int     (history: 10) — 2=present
printer.fsensor_flow       int     (history: 10)
printer.fsensor_rotation   int     (history: 10)
printer.fsensor_rotation_inc  int  (history: 10)
printer.door_sensor        int     (history: 5)
printer.heap_free          int     (history: 5)
printer.heap_total         int     (history: 5)
printer.cpu_usage          int     (history: 5)
printer.stepper_stall      int     (history: 5)
printer.is_printing        bool
printer.print_filename     str
printer.heater_enabled     bool
printer.pwm_nozzle         int
printer.pwm_bed            int
```

**Cameras (pi_cameras):**
```
camera.nozzle_frame        str     base64 JPEG
camera.nozzle_frame_size   int
camera.nozzle_status       str     live|stale|offline
camera.nozzle_port         str
camera.buddy_count         int
camera.buddy{N}_frame      str     base64 JPEG
camera.buddy{N}_frame_size int
camera.buddy{N}_status     str     live|stale|offline
camera.buddy{N}_ip         str
```

**System:**
```
agent.heartbeat            float   monotonic timestamp, TTL 3s
engine.heartbeat           float   monotonic timestamp, TTL 3s
agent.last_decision        str     last decision summary, TTL 600s
agent.activity_log         list    (Redis LIST, max 20 entries, JSON objects)
human.intent               str     operator intent, TTL 600s
human.intent_log           list    (Redis LIST, max 10 entries, from Telegram)
human.urgent               bool    TTL 600s
human.image                str     base64 JPEG from Telegram, TTL 600s
safety.estop               bool    TTL 600s
```

### 9.2 Whiteboard client

Key features:
- `publish(key, value, ttl, history_depth)` — SET with optional EX + LPUSH/LTRIM for history
- `publish_many(values, ttl, history_depth)` — atomic multi-key publish via Redis pipeline
- `read(key)` — GET with JSON decode, returns None if expired/missing
- `read_history(key)` — LRANGE on `{key}:history`, newest first
- `read_history_timestamps(key)` — LRANGE on `{key}:history_ts`, newest first
- `read_all()` — scan all string-type keys (skips lists)
- `read_all_with_trends()` — read_all + compute trends for numeric histories

### 9.3 Trend computation

```python
def compute_trend(history: list[float], threshold=0.1) -> str:
    delta = history[0] - history[-1]  # newest minus oldest
    if abs(delta) < threshold: return "stable"
    direction = "rising" if delta > 0 else "falling"
    return f"{direction} {delta:+.1f} over {len(history)} readings"
```

Runs inside `read_all_with_trends()` — pure Python, no LLM call.

---

## 10. Ledger (SQLite WAL)

Path: `{data_dir}/ledger.db`

### 10.1 Schema

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    params_json TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    device_group TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'PROPOSED','WAITING_APPROVAL','DISPATCHED',
        'DONE','FAILED','REJECTED','UNKNOWN'
    )),
    reason TEXT,
    result_json TEXT,
    error_json TEXT,
    created_ts REAL NOT NULL,
    created_mono REAL NOT NULL,
    updated_ts REAL NOT NULL,
    requires_approval INTEGER NOT NULL DEFAULT 0,
    max_proposal_age_ms INTEGER NOT NULL DEFAULT 30000
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES actions(action_id),
    params_hash TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('APPROVE','REJECT')),
    approved_by TEXT NOT NULL,
    created_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    component TEXT NOT NULL,
    level TEXT NOT NULL CHECK(level IN ('DEBUG','INFO','WARN','ERROR','CRITICAL')),
    action_id TEXT,
    message TEXT NOT NULL,
    details_json TEXT
);
```

### 10.2 Diary schema

Path: `{data_dir}/diary_{device_group}.db`

```sql
CREATE TABLE IF NOT EXISTS idempotency_exec (
    idempotency_key TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('IN_FLIGHT','SUCCESS','FAILED')),
    started_ts REAL NOT NULL,
    completed_ts REAL,
    result_json TEXT
);
```

### 10.3 Episode computation

Returns actions since last WAIT or CALL_HUMAN event, newest first, max 12. Falls back to last 5 actions if no episode boundary found.

### 10.4 Idempotency

Proposals with the same `tool:params_hash` idempotency key that are still active (PROPOSED, WAITING_APPROVAL, DISPATCHED, UNKNOWN) are deduplicated. Retries append `:retry:N` to the key.

---

## 11. Safety kernel

Separate thread. Starts first, stops last. Checks every 0.5s.

### 11.1 Monitored signals

| Signal | Source | Threshold | Action |
|--------|--------|-----------|--------|
| `agent.heartbeat` | whiteboard | Missing or > 3s stale | call_human (critical) |
| `engine.heartbeat` | whiteboard | Missing or > 3s stale | call_human (critical) |
| `printer.oc_nozzle` | prusa_metrics | Non-zero | call_human (critical): "OVERCURRENT DETECTED: nozzle heater" |
| `printer.oc_input` | prusa_metrics | Non-zero | call_human (critical): "OVERCURRENT DETECTED: input power" |
| `safety.estop` | whiteboard | Truthy | call_human (critical): "ESTOP ACTIVE" |

### 11.2 Boot grace period

Heartbeat alerts are suppressed for 10 seconds after boot to allow components to start.

### 11.3 Alert deduplication

Each signal has a `_alerted` flag. Alerts fire once per event and clear when the signal returns to normal.

**Note:** No GPIO interlock in current version. Will be added per-device-pack when dangerous actuators are connected.

---

## 12. Human interface

### 12.1 Channels
- **CLI:** Direct terminal interface. For development and debugging.
- **Telegram:** Remote operations. Push notifications, approvals, intent, photos, camera snapshots.
- **Dashboard:** Read-only web page (port 8081). Live whiteboard state via websocket (port 8082). Temperature charts, camera feeds, print status, agent activity log.

### 12.2 Inbound
- **Intent:** Human types text → `whiteboard.publish("human.intent", text, ttl=600)`
- **Urgent:** `/urgent` → `whiteboard.publish("human.urgent", True, ttl=600)`
- **ESTOP:** `/estop` → `whiteboard.publish("safety.estop", True, ttl=600)`
- **Approval:** approve/reject → ledger approval row (via CLI command, Telegram command, or inline keyboard)
- **Photo:** Telegram photo → resized JPEG → `whiteboard.publish("human.image", base64, ttl=600)`. Caption published as intent.

### 12.3 Outbound
- Approval requests via Telegram inline keyboard + CLI
- Alerts and status updates via Telegram
- Camera snapshots on demand via `/snapshot`

### 12.4 Telegram commands

| Command | Description |
|---------|-------------|
| `/status` | Printer state, temps, safety, cameras, host |
| `/snapshot` | Send all live camera frames |
| `/urgent` | Set urgent flag |
| `/estop` | Emergency stop |
| `/approve <id>` | Approve pending action |
| `/reject <id>` | Reject pending action |
| `/help` | Show all commands |
| (text) | Set as human.intent |
| (photo) | Publish to human.image for LLM analysis |

Quick-reply keyboard with Status, Snapshot, Urgent, ESTOP buttons.

### 12.5 call_human fallback chain
```
Telegram ×3 (exponential backoff: 1s, 2s) → CLI/TTY → durable outbox ({data_dir}/outbox/)
```
System NEVER silently gives up.

### 12.6 Human response timing
The agent responds on the **next cycle** (30-300 seconds depending on state). Not synchronous.

---

## 13. Knowledge files

### 13.1 SOUL.md
LLM's mission briefing. Objectives, operating philosophy, decision heuristics, timing guidance.
Key rule: **observe-only mode by default** — do NOT propose actions unless there is an active `human.intent` or a genuine safety emergency.

### 13.2 HARDWARE.md
Auto-generated by `discover_hardware()`. Lists all connected devices, buses, available tools. Currently not auto-generated on boot.

### 13.3 LEARNED.md
Printing knowledge and troubleshooting playbook. Covers: printing fundamentals by material, autonomous fixes (stringing, over/underextrusion, temp issues, speed artifacts, chamber, first layer, door), when to escalate, consistency guidance, API credit management.

---

## 14. Device pack structure

```
device_packs/
    host_pi/                  # Raspberry Pi introspection
        __init__.py           # PACK_META: bus=sysfs, always available
        sensors.py            # read_cpu_temp, read_system_stats, read_usb_devices, read_network_interfaces
        tests/
    prusa_link/               # PrusaLink HTTP API
        __init__.py           # PACK_META: bus=network, discovery via /api/version
        sensors.py            # read_printer_state, read_printer_info, read_file_list
        actuators.py          # pause/resume/cancel/start_print, set_temperature, home_axes,
                              # disable_motors, set_speed_factor, set_flow_factor, set_position,
                              # extrude, retract
        tests/
    prusa_metrics/            # UDP metrics stream
        __init__.py           # PACK_META: bus=udp, port 8514
        sensors.py            # read_temperatures, read_electrical, read_fans, read_position,
                              # read_filament, read_enclosure, read_firmware_health, read_print_state
        tests/
    prusa_serial/             # USB serial for G-code + endstops
        __init__.py           # PACK_META: bus=serial, VID 2c99
        actuators.py          # read_endstops (M119), send_gcode (restricted allowlist)
        tests/
    pi_cameras/               # Camera feeds
        __init__.py           # PACK_META: bus=network, localhost snapshot
        sensors.py            # read_nozzle_camera, read_buddy_cameras
                              # Auto-discovers nozzle port + buddy IPs by MAC prefix
        tests/
```

Each pack declares `PACK_META` with name, description, bus, and discovery_match.

---

## 15. File layout

```
wallee/
    agent/
        loop.py               # AgentLoop class — main loop + heartbeat + routing
        prompt.py             # build_prompt() + build_messages() (vision support)
        parser.py             # Output parser — JSON → Decision, interval clamping
        llm_client.py         # OpenRouter client — structured output + plugins
        change_detector.py    # ExternalChangeDetector — tracks non-Wallee changes
    engine/
        dispatch.py           # Engine class — gate sequence (ESTOP→queue→deadline→approval→TOCTOU→dispatch)
        reconcile.py          # Crash recovery — reconcile in-flight actions on boot
    safety/
        kernel.py             # SafetyKernel — heartbeat + overcurrent + ESTOP monitoring
    ledger/
        db.py                 # Ledger SQLite — proposals, approvals, episodes, events
        diary.py              # Diary SQLite — per-device idempotency
        migrations/
            001_init.sql      # Schema: actions, approvals, events tables
    whiteboard/
        client.py             # Redis client — TTL, ring buffers, timestamps, trends
    bus/
        network.py            # HTTPClient for PrusaLink API
        serial.py             # SerialBus — open-send-read-close, blacklist, garble detection
        udp_listener.py       # UDPListener + MetricsBuffer + InfluxDB line protocol parser
    device_packs/
        host_pi/              # Pi introspection (4 sensors)
        prusa_link/           # PrusaLink HTTP API (3 sensors, 12 actuators)
        prusa_metrics/        # UDP metrics stream (8 sensors)
        prusa_serial/         # USB serial (2 actuators)
        pi_cameras/           # Camera feeds (2 sensors with auto-discovery)
    tools/
        registry.py           # ToolRegistry — discovers packs, registers tools, runs sensor threads
        decorator.py          # @tool decorator — attaches metadata to functions
        builtins/
            discover.py       # discover_hardware — local camera + serial port discovery
            call_human_tool.py # call_human — escalation via fallback chain
            trends.py         # trends — direction + magnitude analysis
            differential.py   # differential — rate of change using real timestamps
            sensor_history.py # get_sensor_history — raw ring buffer values
    knowledge/
        SOUL.md               # Mission briefing + decision heuristics
        LEARNED.md            # Printing knowledge + troubleshooting playbook
    human/
        cli.py                # Interactive REPL — intent, approve, reject, status, ESTOP
        telegram.py           # TelegramBot — full remote control + photos + inline keyboards
        call_human.py         # Fallback chain: Telegram → CLI → outbox
    ui/
        dashboard.py          # Read-only web dashboard — HTTP + WebSocket, inline HTML/CSS/JS
    config.py                 # Reads .env, provides typed Config dataclass
    main.py                   # Boot sequence + process management
    .env                      # NEVER committed to git
    .env.example              # Template
```

---

## 16. Configuration (.env)

All configuration is loaded via `config.py`. Missing values use defaults.

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENROUTER_API_KEY` | (required) | OpenRouter API key |
| `OPENROUTER_MODEL` | `google/gemini-3.1-pro-preview` | LLM model ID |
| `OPENROUTER_ENABLE_WEB_SEARCH` | `true` | Enable Exa web search plugin |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection URL |
| `DEVICE_PACKS` | `host_pi` | Comma-separated pack names to load |
| `PRUSALINK_HOST` | (empty) | Printer IP (e.g., 192.168.1.50) |
| `PRUSALINK_API_KEY` | (empty) | PrusaLink local API key |
| `TELEGRAM_BOT_TOKEN` | (empty) | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | (empty) | Telegram chat ID for notifications |
| `TELEGRAM_ALLOWED_USER_IDS` | (empty) | Comma-separated user IDs for auth |
| `WALLEE_DATA_DIR` | `/var/lib/wallee` | Ledger, diary, outbox storage |
| `WALLEE_LOG_DIR` | `/var/log/wallee` | Log directory |
| `AGENT_POLL_INTERVAL_S` | `5` | Base agent poll interval |
| `AGENT_HEARTBEAT_INTERVAL_S` | `1` | Heartbeat publish interval |
| `AGENT_HEARTBEAT_TTL_S` | `3` | Heartbeat Redis TTL |
| `AGENT_MIN_CHECK_INTERVAL_S` | `30` | Minimum LLM-requested check_after_s |
| `AGENT_MAX_CHECK_INTERVAL_S` | `120` | Maximum check_after_s (active print) |
| `AGENT_MAX_CHECK_INTERVAL_IDLE_S` | `300` | Maximum check_after_s (idle) |
| `AGENT_DEFAULT_CHECK_INTERVAL_S` | `60` | Default check_after_s |
| `AGENT_LAST_DECISION_TTL_S` | `600` | TTL for agent.last_decision on whiteboard |
| `ENGINE_POLL_INTERVAL_S` | `0.5` | Engine ledger poll interval |
| `ENGINE_APPROVAL_TIMEOUT_S` | `300` | Approval wait timeout before auto-reject |
| `HUMAN_INTENT_TTL_S` | `600` | TTL for human.intent on whiteboard |
| `HUMAN_URGENT_TTL_S` | `600` | TTL for human.urgent on whiteboard |
| `HUMAN_IMAGE_TTL_S` | `600` | TTL for human.image on whiteboard |
| `HUMAN_ESTOP_TTL_S` | `600` | TTL for safety.estop on whiteboard |
| `DEFAULT_MAX_PROPOSAL_AGE_MS` | `30000` | Default proposal expiry |
| `DASHBOARD_PORT` | `8081` | Dashboard HTTP port (WebSocket = port+1) |
| `NOZZLE_CAMERA_PORT` | (auto-discovered) | Override nozzle camera ustreamer port |

---

## 17. Boot sequence (as implemented in main.py)

```
 1. Load config from .env
 2. Ensure data directory exists (fallback: ~/.wallee/data)
 3. Connect to Redis (whiteboard) — exit if unavailable
 4. Safety kernel starts FIRST (heartbeat + overcurrent + ESTOP monitor)
 5. Ledger initialized (SQLite WAL, run migrations)
 6. Reconcile on boot (check for pre-crash in-flight actions)
 7. Load built-in tools + device packs from DEVICE_PACKS config
 8. Start sensor background threads
 9. Engine starts (polls ledger, runs gate sequence)
10. Agent starts LAST (loads knowledge, begins LLM loop)
11. Dashboard starts (HTTP + WebSocket)
12. Telegram bot starts (if configured)
13. CLI starts (if TTY available) — or headless mode
```

**RULE:** Safety kernel starts FIRST, agent starts LAST.

Graceful shutdown on SIGINT/SIGTERM: stops agent, engine, safety, sensors, closes ledger.

---

## 18. PrusaLink API notes

### 18.1 Endpoints used

| Method | Endpoint | Usage |
|--------|----------|-------|
| GET | `/api/v1/status` | Printer state, temps, job progress |
| GET | `/api/v1/info` | Printer identity |
| GET | `/api/version` | Firmware version, discovery check |
| GET | `/api/v1/files/usb` | File listing |
| POST | `/api/v1/gcode` | G-code injection (pause M25, resume M24, temp M104/M140/M141, move G1, home G28, etc.) |
| POST | `/api/v1/files/usb/{path}/pprint` | Start a print |
| DELETE | `/api/v1/job` | Cancel print (irreversible) |

### 18.2 Known quirks

- **Core One+ returns 405 on PUT /api/v1/job** during many states. Pause/resume uses M25/M24 G-code injection via POST /api/v1/gcode instead.
- **HTTP API reports state=PRINTING during purge/preparation phase.** The `job.phase` field distinguishes PREPARING from actual PRINTING. Pause precheck blocks during PREPARING.
- **Metrics stream stops sending some values during IDLE** (temp_bed, chamber_temp). HTTP API always reports them — serves as fallback.

### 18.3 Metrics stream

The printer pushes ~50 UDP packets/sec to port 8514 containing syslog-wrapped (RFC 5424) InfluxDB line protocol metrics. Parsed by `UDPListener` into a shared `MetricsBuffer`. Sensor tools in `prusa_metrics` read from this buffer — they do NOT poll the printer.

### 18.4 Serial interface

USB serial (VID 2c99) is used only for diagnostic commands that require reading the response (M119 endstops, M105 temps, M114 position, M115 version, M503 settings). All state-changing commands go through HTTP G-code injection.

**Blacklisted serial commands:** M997 (firmware update), M112 (emergency stop), M502 (factory reset), M500 (save settings).

**Serial disconnect:** The Core One+ USB serial disconnects every 1-3 seconds. The `SerialBus` uses an open-send-read-close pattern per command and auto-reconnects with configurable wait.

---

## 19. Known issues and decisions

1. **One action per cycle.** LLM proposes one action at a time. No multi-step plans.
2. **Entire whiteboard dump.** No filtering at current scale. Verbose/binary keys skipped in prompt.
3. **Episode = since last WAIT/CALL_HUMAN.** Bounded to 12 actions max.
4. **TTL = 2× refresh period by default.** Override available per sensor tool.
5. **No GPIO interlock in current version.** Added per-device-pack when dangerous actuators are connected.
6. **Prusa connection uses three interfaces:** PrusaLink HTTP + UDP metrics + USB serial.
7. **Dashboard is read-only.** Control via CLI or Telegram only.
8. **Trends computed by context builder (Python math), not LLM.** Built-in tools call same functions.
9. **differential() ≠ trends().** Trends = direction + magnitude. Differential = rate of change per second.
10. **Human messages processed on next agent cycle (30-300s).** Not synchronous.
11. **Camera frames sent as base64 JPEG vision blocks.** Max 2 cameras per LLM call.
12. **External change detector** flags whiteboard changes not caused by Wallee (e.g., manual operator actions on the printer).
13. **Serial disconnects every 1-3s** on Core One+. Open-send-read-close pattern handles this.
14. **HTTP API says PRINTING during purge.** Precheck uses job.phase to distinguish PREPARING.
15. **Observe-only by default.** Agent only proposes actions when human.intent is active or safety emergency exists.
