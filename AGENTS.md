# Wallee — Ground Truth Specification

**Status:** Normative. If implementation conflicts with this document, this document wins.
**Audience:** Coding agents (Codex) and developers.
**Hardware:** Raspberry Pi 5 (8GB), Raspberry Pi OS Bookworm.
**LLM Provider:** OpenRouter → openai/gpt-5.4.

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
│  │ (Python proc)│  │ (Python  │  │ (Python proc,     │   │
│  │              │  │  proc)   │  │  independent)     │   │
│  │ Calls LLM    │  │ Gates +  │  │ Watches heartbeat │   │
│  │ via OpenRouter│  │ dispatch │  │ call_human on fail│   │
│  └──────┬───────┘  └────┬─────┘  └────────┬──────────┘  │
│         │               │                  │             │
│         │ proposals      │ dispatch         │ monitors    │
│         ▼               ▼                  ▼             │
│  ┌──────────────────────────────────────────────────┐   │
│  │              Device Packs (Tools)                 │   │
│  │  Sensor tools (publishers) + Actuator tools       │   │
│  │  host_pi | prusa_link | (future packs)            │   │
│  └──────────────────────┬───────────────────────────┘   │
│                         │ bus frameworks                 │
│                         ▼                                │
│  ┌──────────────────────────────────────────────────┐   │
│  │     GPIO | I2C | SPI | UART | Camera | USB | Net  │   │
│  └──────────────────────┬───────────────────────────┘   │
│                         ▼                                │
│                  Physical Hardware                        │
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
   Cloud LLM (OpenRouter)        Prusa Printer (PrusaLink HTTP)
```

### Data flows
- **Down:** Agent proposes → Engine gates → Tool executes → Hardware
- **Sideways:** Everything publishes to Whiteboard → Agent reads it
- **Independent:** Safety kernel monitors heartbeats, calls human on failure

### Trust boundary
The Agent is UNTRUSTED. It may:
- Read whiteboard (all state)
- Read knowledge files
- Write proposals to ledger
- Publish agent.heartbeat to whiteboard

It may NOT:
- Call tool functions directly
- Write to whiteboard (except heartbeat)
- Approve actions
- Bypass the engine

---

## 4. Terminology

- **Tool** — the overarching name. A piece of Python code that interfaces with hardware or performs an action.
- **Sensor tool** — a tool that reads hardware on a schedule and publishes to the whiteboard. Runs in background. LLM reads output, never calls it. Also called a "publisher."
- **Actuator tool** — a tool that performs an action on demand. LLM proposes, engine dispatches through gates.
- **Built-in tool** — a tool always available regardless of hardware (web_search, discover_hardware, etc).
- **Device pack** — a folder containing sensor tools + actuator tools + tests for one specific hardware device.

Do NOT use the word "skill." Always say "tool."

---

## 5. Tool contracts

### 5.1 Sensor tool (publisher)

```python
@tool(kind="sensor", refresh_hz=1.0, history_depth=10)
def read_environment(self):
    """Read temperature and humidity from BME280."""
    # ttl_ms auto-calculated: int((1000 / refresh_hz) * 2) = 2000
    # Can override: @tool(kind="sensor", refresh_hz=1.0, ttl_ms=5000)

    raw = self.i2c.read_registers(self.address, 0xF7, 8)
    temp = self._compensate_temp(raw)
    humidity = self._compensate_humidity(raw)

    # Sanity check (tool-level safety)
    if temp < -40 or temp > 85:
        raise ValueError(f"BME280 temp {temp} outside operating range")

    return {
        "env.temperature": round(temp, 1),
        "env.humidity": round(humidity, 1),
    }
```

**Required fields:**
| Field | Type | Description |
|-------|------|-------------|
| `kind` | `"sensor"` | Identifies this as a publisher |
| `refresh_hz` | float | Read frequency. Publisher decides based on hardware. |
| `history_depth` | int | Ring buffer size. 0 = no history. |
| `keys` | list[str] | Derived from return dict keys. Whiteboard keys this sensor writes. |

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
The system runs `read()` in a background thread every `1/refresh_hz` seconds. Each returned key:value pair is published to the whiteboard with the computed TTL. If `history_depth > 0`, each value is also LPUSH'd to a Redis list `{key}:history` and LTRIM'd to `history_depth`.

The LLM never calls sensor tools. It reads the whiteboard.

### 5.2 Actuator tool

```python
@tool(kind="actuator", requires_approval=True, max_proposal_age_ms=30000)
def resume_print(self, whiteboard):
    """Resume a paused print job. Only call when temps are nominal and no faults."""

    # Precondition check (engine calls this for TOCTOU gate)
    state = whiteboard.read("printer.job_state")
    if state != "PAUSED":
        return {"error": f"Cannot resume: job is {state}, not PAUSED"}

    faults = whiteboard.read("printer.active_faults")
    if faults:
        return {"error": f"Cannot resume: active faults: {faults}"}

    # Execute via bus framework
    response = self.http.post(f"{self.base_url}/api/v1/job/resume")

    # Publish result to whiteboard
    whiteboard.publish("printer.last_action", "resume_print")

    return {"status": "success", "response": response.status_code}
```

**Required fields:**
| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `kind` | `"actuator"` | — | Identifies this as on-demand |
| `requires_approval` | bool | `False` | If True, engine asks human before dispatch |
| `max_proposal_age_ms` | int | `30000` | Deadline. Stale proposals are REJECTED. |

**Return value:** Always a dict. `{"status": "success", ...}` or `{"error": "reason"}`. Stored in ledger. Shown to LLM in episode context.

**Precondition vs execute:** The function body starts with precondition checks (reading whiteboard state). The engine runs the function once for TOCTOU (it returns early with an error dict if preconditions fail). If TOCTOU passes, the engine dispatches by calling the function again for real execution. Alternative implementation: separate `precondition()` and `execute()` methods on the tool class. Choose whichever is cleaner.

**RULE:** Preconditions check DISCRETE STATES (job.state == "PAUSED", faults is empty). NOT continuous sensor values. Temperature fluctuating ±0.2°C must NOT trigger TOCTOU rejection.

### 5.3 Built-in tools

Always available. Not tied to any device pack.

```python
@tool(kind="actuator", requires_approval=False)
def web_search(query: str) -> dict:
    """Search the web for technical information, datasheets, troubleshooting."""

@tool(kind="actuator", requires_approval=False)
def discover_hardware() -> dict:
    """Scan all buses, enumerate devices, match to packs, update HARDWARE.md."""

@tool(kind="actuator", requires_approval=False)
def call_human(message: str, severity: str = "info") -> dict:
    """Escalate to human operator. Fallback: Telegram → CLI → durable outbox."""

@tool(kind="actuator", requires_approval=True)
def git_pull(repo_url: str, branch: str = "main") -> dict:
    """Pull updates for device packs or knowledge files."""

@tool(kind="actuator", requires_approval=False)
def trends(key: str) -> dict:
    """Trend analysis for a whiteboard key: rising/falling/stable + magnitude."""

@tool(kind="actuator", requires_approval=False)
def differential(key: str) -> dict:
    """Rate of change for a numerical whiteboard key (units per second)."""

@tool(kind="actuator", requires_approval=False)
def get_sensor_history(key: str, depth: int = 30) -> dict:
    """Raw ring buffer values for deeper analysis."""
```

`trends()` returns direction + magnitude ("rising +0.4° over 10 readings").
`differential()` returns rate of change ("changing at +0.04°/second").
`get_sensor_history()` returns raw values from the ring buffer.

---

## 6. Agent loop

```python
class AgentLoop:
    def __init__(self, whiteboard, ledger, llm, tools, knowledge):
        self.wb = whiteboard
        self.ledger = ledger
        self.llm = llm
        self.tools = tools
        self.knowledge = knowledge
        # Start heartbeat background thread
        threading.Thread(target=self._heartbeat, daemon=True).start()

    def _heartbeat(self):
        while True:
            self.wb.publish("agent.heartbeat", time.monotonic(), ttl=3)
            time.sleep(1)

    def run(self):
        while True:
            # 1. Read entire whiteboard + trend annotations
            state = self.wb.read_all_with_trends()

            # 2. Read current episode from ledger
            #    (all actions since last WAIT or CALL_HUMAN)
            episode = self.ledger.current_episode()

            # 3. Read human intent
            intent = self.wb.read("human.intent")

            # 4. Check for urgent flag
            urgent = self.wb.read("human.urgent")

            # 5. Assemble prompt
            prompt = build_prompt(
                state=state,
                episode=episode,
                intent=intent,
                knowledge=self.knowledge,  # SOUL.md, HARDWARE.md, LEARNED.md
                tools=self.tools.list_for_llm(),
                current_time=time.time(),
            )

            # 6. Call LLM (stateless)
            raw_response = self.llm.call(prompt)

            # 7. Parse response
            decision = self.parse_output(raw_response)

            # 8. Route decision
            if decision.type == "ACTION":
                if decision.tool not in self.tools:
                    self.ledger.record_event("WARN", f"Unknown tool: {decision.tool}")
                    continue
                self.ledger.propose(
                    tool=decision.tool,
                    params=decision.params,
                    reason=decision.reason,
                )
            elif decision.type == "WAIT":
                self.ledger.record_wait(decision.reason)
                time.sleep(decision.check_after_s or 5)
            elif decision.type == "CALL_HUMAN":
                call_human(decision.message, decision.severity)
                # Pause loop until human acknowledges
                self.wait_for_human_ack()
```

**RULE:** Agent may only write to the ledger (proposals, waits) and to agent.heartbeat on the whiteboard. Nothing else.

### 6.1 LLM output format

The LLM must return JSON:

```json
{"type": "ACTION", "tool": "resume_print", "params": {}, "reason": "temps nominal, operator requested resume"}
```
```json
{"type": "WAIT", "reason": "all nominal, no action needed", "check_after_s": 10}
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

### 6.2 LLM provider config (OpenRouter)

```python
import httpx

class LLMClient:
    def __init__(self, api_key: str, model: str = "openai/gpt-5.4"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"

    def call(self, prompt: str) -> str:
        response = httpx.post(
            self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": "Decide your next action."},
                ],
                "response_format": {"type": "json_object"},
            },
            timeout=60.0,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
```

### 6.3 Context builder — trend computation

The context builder computes trends directly (not via LLM tools):

```python
def compute_trend(history: list[float]) -> str:
    """Compute trend from ring buffer values. Newest first."""
    if len(history) < 2:
        return "insufficient data"
    delta = history[0] - history[-1]  # newest minus oldest
    threshold = 0.1  # configurable per sensor
    if abs(delta) < threshold:
        return "stable"
    direction = "rising" if delta > 0 else "falling"
    return f"{direction} {delta:+.1f} over {len(history)} readings"

def compute_differential(history: list[float], interval_s: float) -> str:
    """Rate of change per second."""
    if len(history) < 2:
        return "insufficient data"
    delta = history[0] - history[-1]
    time_span = interval_s * (len(history) - 1)
    rate = delta / time_span if time_span > 0 else 0
    return f"{rate:+.3f}/s"
```

This runs inside `read_all_with_trends()` — pure Python, no LLM call. The `trends()` and `differential()` built-in tools call these same functions but are available for the LLM to invoke explicitly when it wants analysis on a specific key.

---

## 7. Engine

The engine is a separate Python process. It polls the ledger for PROPOSED actions and runs them through gates.

```python
class Engine:
    def __init__(self, whiteboard, ledger, tools):
        self.wb = whiteboard
        self.ledger = ledger
        self.tools = tools
        # Start heartbeat
        threading.Thread(target=self._heartbeat, daemon=True).start()

    def _heartbeat(self):
        while True:
            self.wb.publish("engine.heartbeat", time.monotonic(), ttl=3)
            time.sleep(1)

    def run(self):
        while True:
            # Poll for proposals
            proposals = self.ledger.get_proposals()
            for proposal in proposals:
                self.process_proposal(proposal)
            # Reconcile loop
            self.reconcile()
            time.sleep(0.5)

    def process_proposal(self, proposal):
        tool = self.tools.get(proposal.tool)

        # Gate 1: TOCTOU
        result = tool.execute(self.wb, **proposal.params)
        if "error" in result:
            self.ledger.reject(proposal.id, result["error"])
            return

        # Gate 2: Queue guard
        if self.ledger.has_inflight(tool.device_group):
            return  # Skip, retry next poll

        # Gate 3: Deadline
        age_ms = (time.monotonic() - proposal.created_mono) * 1000
        if age_ms > tool.max_proposal_age_ms:
            self.ledger.reject(proposal.id, "expired")
            return

        # Gate 4: Approval
        if tool.requires_approval:
            self.ledger.set_status(proposal.id, "WAITING_APPROVAL")
            call_human(f"Approve: {proposal.tool}({proposal.params})?")
            # Wait for approval (blocking with timeout)
            approval = self.ledger.wait_for_approval(proposal.id, timeout=300)
            if not approval or approval.decision != "APPROVE":
                self.ledger.reject(proposal.id, "not approved")
                return

        # Gate 5: Dispatch
        diary = Diary(tool.device_group)
        diary.write_inflight(proposal.idempotency_key, proposal.id)  # fsync
        self.ledger.set_status(proposal.id, "DISPATCHED")

        try:
            result = tool.execute(self.wb, **proposal.params)
            if "error" in result:
                diary.write_failed(proposal.idempotency_key, result)
                self.ledger.set_status(proposal.id, "FAILED", error=result)
            else:
                diary.write_success(proposal.idempotency_key, result)
                self.ledger.set_status(proposal.id, "DONE", result=result)
        except Exception as e:
            diary.write_failed(proposal.idempotency_key, {"error": str(e)})
            self.ledger.set_status(proposal.id, "FAILED", error={"error": str(e)})
```

**Engine MUST NOT:** call LLM, perform network I/O (except local Redis/SQLite), access cloud APIs.

### 7.1 Reconcile loop (crash recovery)

```python
def reconcile(self):
    """Run on boot and every 500ms. Check for in-flight actions from pre-crash."""
    dispatched = self.ledger.get_by_status("DISPATCHED")
    for action in dispatched:
        diary = Diary(action.device_group)
        diary_status = diary.lookup(action.idempotency_key)

        if diary_status == "SUCCESS":
            # Tool completed, ledger just wasn't updated
            self.ledger.set_status(action.id, "DONE", result=diary.get_result(action.idempotency_key))
        elif diary_status == "FAILED":
            self.ledger.set_status(action.id, "FAILED", error=diary.get_result(action.idempotency_key))
        elif diary_status == "IN_FLIGHT":
            # DANGEROUS: command sent, outcome unknown
            self.ledger.set_status(action.id, "UNKNOWN")
            call_human(f"UNKNOWN state: {action.tool} was in-flight when system crashed", severity="critical")
        elif diary_status is None:
            # Command never reached hardware
            self.ledger.set_status(action.id, "FAILED", error={"error": "never dispatched (crash before diary)"})
```

---

## 8. Whiteboard (Redis)

### 8.1 Schema

```
# Current values (SET with EX)
SET env.temperature 215.2 EX 2
SET env.humidity 58.4 EX 2
SET host.cpu_temp 52.1 EX 4
SET printer.job_state "PRINTING" EX 4
SET camera.frame <base64> EX 4
SET agent.heartbeat 1710234567.123 EX 3
SET engine.heartbeat 1710234567.456 EX 3
SET human.intent "resume the print" EX 60
SET human.urgent "true" EX 10

# Ring buffers (LIST with LTRIM)
LPUSH env.temperature:history 215.2
LTRIM env.temperature:history 0 9  # keep last 10

LPUSH env.humidity:history 58.4
LTRIM env.humidity:history 0 9
```

### 8.2 Whiteboard client

```python
class Whiteboard:
    def __init__(self, redis_url="redis://localhost:6379"):
        self.r = redis.Redis.from_url(redis_url, decode_responses=True)

    def publish(self, key: str, value, ttl: int = None, history_depth: int = 0):
        """Publish a value with optional TTL and history."""
        self.r.set(key, json.dumps(value), ex=ttl)
        if history_depth > 0:
            self.r.lpush(f"{key}:history", json.dumps(value))
            self.r.ltrim(f"{key}:history", 0, history_depth - 1)

    def read(self, key: str):
        """Read a single key. Returns None if expired."""
        val = self.r.get(key)
        return json.loads(val) if val else None

    def read_history(self, key: str) -> list:
        """Read ring buffer for a key."""
        vals = self.r.lrange(f"{key}:history", 0, -1)
        return [json.loads(v) for v in vals]

    def read_all(self) -> dict:
        """Read all non-system keys."""
        keys = [k for k in self.r.keys("*") if not k.endswith(":history")]
        result = {}
        for key in keys:
            result[key] = self.read(key)
        return result

    def read_all_with_trends(self) -> dict:
        """Read all keys with trend annotations."""
        state = self.read_all()
        for key in list(state.keys()):
            history = self.read_history(key)
            if history and len(history) >= 2 and isinstance(history[0], (int, float)):
                state[f"{key}:trend"] = compute_trend(history)
        return state
```

---

## 9. Ledger (SQLite WAL)

Path: `/var/lib/wallee/ledger.db`

### 9.1 Schema

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
    created_ts REAL NOT NULL,        -- time.time()
    created_mono REAL NOT NULL,      -- time.monotonic()
    updated_ts REAL NOT NULL,
    requires_approval INTEGER NOT NULL DEFAULT 0,
    max_proposal_age_ms INTEGER NOT NULL DEFAULT 30000
);

CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
CREATE INDEX IF NOT EXISTS idx_actions_device ON actions(device_group, status);

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

### 9.2 Diary schema

Path: `/var/lib/wallee/diary_{device_group}.db`

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA busy_timeout=5000;

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

### 9.3 Episode computation

```python
def current_episode(self) -> list[dict]:
    """Return all actions since the last WAIT or CALL_HUMAN."""
    rows = self.db.execute("""
        SELECT * FROM actions
        WHERE created_ts > COALESCE(
            (SELECT MAX(created_ts) FROM events
             WHERE message IN ('WAIT', 'CALL_HUMAN')),
            0
        )
        ORDER BY created_ts ASC
    """).fetchall()
    return [dict(row) for row in rows]
```

---

## 10. Safety kernel

Separate OS process. Starts first, stops last.

```python
class SafetyKernel:
    def __init__(self, whiteboard, device_packs):
        self.wb = whiteboard
        self.safety_limits = {}
        for pack in device_packs:
            if hasattr(pack, 'safety_limits'):
                self.safety_limits.update(pack.safety_limits)

    def run(self):
        while True:
            # Check agent heartbeat
            hb = self.wb.read("agent.heartbeat")
            if hb is None or (time.monotonic() - hb) > 3.0:
                call_human("Agent heartbeat lost", severity="critical")

            # Check engine heartbeat
            ehb = self.wb.read("engine.heartbeat")
            if ehb is None or (time.monotonic() - ehb) > 3.0:
                call_human("Engine heartbeat lost", severity="critical")

            time.sleep(0.5)
```

**v1 scope:** Heartbeat monitoring only. No GPIO interlock (no dangerous hardware yet).
**Future:** When actuators with thermal/mechanical risk are connected, add GPIO interlock relay.

---

## 11. Human interface

### 11.1 Channels
- **CLI:** Direct terminal interface. For development and debugging.
- **Telegram:** Remote operations. Push notifications, approvals, intent.
- **Dashboard:** Read-only web page. Live whiteboard state via websocket.

### 11.2 Inbound
- **Intent:** Human types "resume the print" → `whiteboard.publish("human.intent", "resume the print", ttl=60)`
- **Urgent:** Human sends /urgent → `whiteboard.publish("human.urgent", True, ttl=10)`
- **Approval:** approve/reject → ledger approval row
- **ESTOP:** → directly to safety kernel (bypasses everything)

### 11.3 Outbound
- Approval requests via Telegram/CLI
- Alerts and status updates via Telegram/CLI/Dashboard

### 11.4 call_human fallback chain
```
Telegram ×3 (exponential backoff) → CLI/TTY → durable outbox (/var/lib/wallee/outbox/)
```
If outbox reached: pause all requires_approval actions, retry every 30s.
System NEVER silently gives up.

### 11.5 Human response timing
The agent responds on the **next cycle** (3-10 seconds). Not synchronous. Human sends intent → whiteboard → agent reads on next loop → LLM decides → response via Telegram. Fast enough for human interaction.

---

## 12. Knowledge files

### 12.1 SOUL.md
LLM's mission briefing. Objectives, operating philosophy, decision heuristics.
NOT safety enforcement. If LLM ignores SOUL.md, deterministic safety layers still catch problems.

### 12.2 HARDWARE.md
Auto-generated by `discover_hardware()`. Lists all connected devices, buses, available tools.
Rebuilt on each discovery run.

### 12.3 LEARNED.md
Accumulated wisdom. Initially manual. Updated via `git_pull` or operator edits.
Example entries: "Filament sensor false positives common at >60% humidity."

---

## 13. Device pack structure

```
device_packs/
    host_pi/
        __init__.py
        sensors.py        # Sensor tools (publishers)
        actuators.py      # Actuator tools (if any)
        tests/
            test_sensors.py
    prusa_link/           # SEPARATE folder — different device
        __init__.py
        sensors.py        # Printer state publishers
        actuators.py      # resume, pause, set_temp, etc.
        tests/
            test_sensors.py
            test_actuators.py
```

Each pack declares:
```python
# device_packs/host_pi/__init__.py
PACK_META = {
    "name": "host_pi",
    "description": "Raspberry Pi host introspection",
    "bus": "sysfs",
    "discovery_match": {"type": "always"},  # Always available on a Pi
}
```

```python
# device_packs/prusa_link/__init__.py
PACK_META = {
    "name": "prusa_link",
    "description": "Prusa printer via PrusaLink local HTTP API",
    "bus": "network",
    "discovery_match": {"type": "http", "path": "/api/version", "expect_key": "api"},
}
```

---

## 14. File layout

```
wallee/
    agent/
        loop.py               # AgentLoop class
        prompt.py             # build_prompt function
        parser.py             # Output parser (JSON → decision)
        llm_client.py         # OpenRouter client
    engine/
        dispatch.py           # Engine class with gate sequence
        reconcile.py          # Crash recovery reconcile loop
    safety/
        kernel.py             # SafetyKernel class
    ledger/
        db.py                 # Ledger SQLite operations
        diary.py              # Diary SQLite operations
        migrations/
            001_init.sql
    whiteboard/
        client.py             # Redis client with TTL + ring buffers + trends
    bus/
        gpio.py
        i2c.py
        spi.py
        uart.py
        camera.py
        usb.py
        network.py            # HTTP client for PrusaLink etc
    device_packs/
        host_pi/
            __init__.py
            sensors.py
            tests/
        prusa_link/           # SEPARATE device pack
            __init__.py
            sensors.py
            actuators.py
            tests/
    tools/
        registry.py           # Tool registry (discovers packs, registers tools)
        decorator.py          # @tool decorator implementation
        builtins/
            web_search.py
            discover.py
            call_human.py
            git_pull.py
            trends.py
            differential.py
            sensor_history.py
    knowledge/
        SOUL.md
        HARDWARE.md
        LEARNED.md
    human/
        cli.py
        telegram.py
        call_human.py         # Fallback chain implementation
    ui/
        dashboard.py          # Read-only web dashboard
    config.py                 # Reads .env, provides config to all components
    main.py                   # Boot sequence + process management
    .env                      # NEVER committed to git
    .env.example              # Template
```

---

## 15. Configuration (.env)

```bash
# === SSH (for Codex on laptop → Pi) ===
PI_HOST=                          # Pi IP address
PI_USER=                          # Pi username
PI_SSH_KEY_PATH=~/.ssh/id_rsa     # Or use PI_PASSWORD

# === LLM (agent on Pi calls this) ===
OPENROUTER_API_KEY=               # OpenRouter API key
OPENROUTER_MODEL=openai/gpt-5.4

# === Redis (whiteboard) ===
REDIS_URL=redis://localhost:6379

# === Prusa (Phase 3) ===
PRUSALINK_HOST=                   # Printer IP (ethernet)
PRUSALINK_API_KEY=                # PrusaLink API key

# === Telegram (Phase 4) ===
TELEGRAM_BOT_TOKEN=               # From @BotFather

# === Paths ===
WALLEE_DATA_DIR=/var/lib/wallee   # Ledger, diary, outbox
WALLEE_LOG_DIR=/var/log/wallee    # Logs
```

---

## 16. Boot sequence

```
1. Safety kernel starts FIRST
   - Begins heartbeat monitoring loop
   - No dependencies except Redis

2. Redis starts (if not already running)
   - systemd service, auto-start

3. Ledger service initializes
   - Opens/creates SQLite WAL databases
   - Runs migrations if needed
   - Runs reconcile loop (check for pre-crash in-flight actions)

4. Bus frameworks initialize
   - Scan buses, enumerate connected devices

5. Device packs load
   - Match discovered devices to registered packs
   - Start sensor tool background threads
   - Register actuator tools in tool registry
   - Push safety_limits to kernel (if any)
   - Auto-generate HARDWARE.md

6. Engine starts
   - Begins polling ledger for proposals
   - Begins reconcile loop (500ms)
   - Begins heartbeat

7. Agent starts LAST
   - Loads knowledge files (SOUL.md, HARDWARE.md, LEARNED.md)
   - Begins heartbeat thread
   - Begins agent loop

8. Human interfaces start
   - CLI listener
   - Telegram bot (Phase 4)
   - Dashboard web server
```

**RULE:** Safety kernel starts FIRST, agent starts LAST.

---

## 17. Build phases

### Phase 1: Host Pi + Whiteboard + Agent Loop
**Goal:** Agent observes the Pi and reasons about it. Zero hardware risk.

**Tasks:**
1. Set up project structure on Pi (SSH from laptop)
2. Install Redis if not present: `sudo apt install redis-server`
3. Implement `whiteboard/client.py` (Redis wrapper with TTL, ring buffers, trends)
4. Implement `tools/decorator.py` (@tool decorator)
5. Implement `tools/registry.py` (discovers packs, registers tools)
6. Implement `device_packs/host_pi/sensors.py`:
   - `read_cpu_temp()` — from `/sys/class/thermal/thermal_zone0/temp`
   - `read_system_stats()` — CPU load, memory %, disk %, uptime
   - `read_usb_devices()` — from `lsusb` or `/sys/bus/usb/devices/`
   - `read_network_interfaces()` — from `psutil` or `/sys/class/net/`
7. Implement `agent/llm_client.py` (OpenRouter client)
8. Implement `agent/parser.py` (JSON output parser with fail-safe)
9. Implement `agent/prompt.py` (build_prompt function)
10. Implement `agent/loop.py` (AgentLoop with heartbeat)
11. Implement `knowledge/SOUL.md` (initial version)
12. Implement `config.py` (reads .env)
13. Implement `main.py` (boots agent only, no engine yet)
14. Write tests for whiteboard, parser, host_pi sensors

**Success criteria:**
- Agent loop runs on Pi
- Whiteboard has host_pi sensor data with trends
- LLM responds with observations about Pi state
- Heartbeat publishing every 1s
- Output parser handles bad JSON gracefully
- Ring buffers working (verify with `redis-cli LRANGE host.cpu_temp:history 0 -1`)

### Phase 2: Full Harness
**Goal:** Engine gates, ledger, diary, safety kernel, CLI.

**Tasks:**
1. Implement `ledger/db.py` (SQLite WAL, action state machine)
2. Implement `ledger/diary.py` (per-device idempotency DB)
3. Implement `ledger/migrations/001_init.sql`
4. Implement `engine/dispatch.py` (gate sequence)
5. Implement `engine/reconcile.py` (crash recovery)
6. Implement `safety/kernel.py` (heartbeat monitor for agent + engine)
7. Implement `human/cli.py` (basic CLI: send intent, approve, ESTOP)
8. Implement `human/call_human.py` (fallback chain — CLI only for now)
9. Implement built-in tools: `web_search`, `discover_hardware`, `call_human`, `trends`, `differential`, `get_sensor_history`
10. Update `main.py` (boot all processes in correct order)
11. Write tests for engine gates, ledger state machine, diary, reconcile

**Success criteria:**
- Full proposal → gate → dispatch → record cycle works
- TOCTOU rejects when preconditions change
- Queue guard prevents double-dispatch
- Deadline rejects stale proposals
- Diary prevents double-actuation (idempotency)
- Safety kernel detects agent/engine death and calls human
- Crash recovery works (kill agent, verify kernel detects, verify reconcile on restart)
- CLI can send intent and approve actions

### Phase 3: Prusa Printer (parallel build)
**Goal:** PrusaLink device pack. Can be built simultaneously on laptop.

**Tasks (second Codex instance or subagent):**
1. Implement `device_packs/prusa_link/__init__.py` (pack metadata)
2. Implement `device_packs/prusa_link/sensors.py`:
   - `read_printer_state()` — job state, progress, time remaining
   - `read_temperatures()` — hotend temp, bed temp, target temps
   - `read_printer_info()` — firmware version, printer model, serial number
3. Implement `device_packs/prusa_link/actuators.py`:
   - `resume_print()` [requires_approval=True]
   - `pause_print()` [requires_approval=False]
   - `set_temperature(target, heater)` [requires_approval=True]
   - `start_print(file_path)` [requires_approval=True]
   - `cancel_print()` [requires_approval=True]
4. Implement `bus/network.py` (HTTP client for PrusaLink API)
5. Write tests against PrusaLink API (can use mock server locally, then test on real printer)

**PrusaLink API reference:**
- Base URL: `http://{PRUSALINK_HOST}`
- Auth: `X-Api-Key: {PRUSALINK_API_KEY}` header
- Endpoints:
  - `GET /api/v1/status` — printer status, temps, job info
  - `GET /api/v1/job` — current job details
  - `PUT /api/v1/job` — pause/resume (body: `{"command": "PAUSE"}` or `{"command": "RESUME"}`)
  - `DELETE /api/v1/job` — cancel job
  - `GET /api/v1/info` — printer info
  - `GET /api/v1/cameras` — camera list (if connected)

**Success criteria (manual testing by operator):**
- Sensor tools correctly read printer state, temps, job progress
- Actuator tools correctly pause/resume/set temp
- Each actuator has working preconditions
- All tools tested on real printer before enabling autonomous use

### Phase 4: Telegram
**Goal:** Remote human interface.

**Tasks:**
1. Implement `human/telegram.py` (Telegram bot)
2. Update `human/call_human.py` (add Telegram to fallback chain)
3. Wire approval flow through Telegram
4. Wire intent through Telegram

**Success criteria:**
- Can send intent from Telegram
- Receive approval requests on Telegram
- Approve/reject from Telegram
- ESTOP from Telegram
- call_human fallback: Telegram → CLI → outbox

---

## 18. Development setup for Codex

### 18.1 On your laptop

```bash
# Create project directory
mkdir wallee && cd wallee

# Create .env from template
cp .env.example .env
# Fill in: PI_HOST, PI_USER, OPENROUTER_API_KEY, etc.

# Codex will SSH into Pi to develop and test
# Ensure SSH key is set up: ssh-copy-id PI_USER@PI_HOST
```

### 18.2 On the Pi (Codex does this via SSH)

```bash
# System dependencies
sudo apt update
sudo apt install -y python3-pip python3-venv redis-server git

# Verify Redis
redis-cli ping  # Should return PONG

# Create project
mkdir -p ~/wallee
cd ~/wallee
python3 -m venv .venv
source .venv/bin/activate

# Python dependencies
pip install redis httpx psutil

# Create data directories
sudo mkdir -p /var/lib/wallee /var/log/wallee
sudo chown $USER:$USER /var/lib/wallee /var/log/wallee
```

### 18.3 Two-agent strategy

**Agent 1 (primary):** SSHes into Pi. Builds Phase 1 → Phase 2 → Phase 4 sequentially. Tests on real hardware.

**Agent 2 (parallel):** Works locally on laptop. Builds Phase 3 (prusa_link device pack) against mock PrusaLink API. When ready, copies to Pi for integration testing.

Merge point: After Phase 2 is complete and Phase 3 prusa_link pack is built, copy `device_packs/prusa_link/` to Pi and run integration tests with real printer.

---

## 19. Testing strategy

### Unit tests
Each component has its own test file. Tests use mock Redis and mock SQLite.

### Integration tests
- Agent loop + whiteboard + host_pi: verify sensor data flows to LLM prompt
- Engine + ledger + diary: verify gate sequence, crash recovery, idempotency
- Prusa pack + PrusaLink API: verify sensor reads and actuator commands

### End-to-end test
Full cycle on Pi: agent reads host_pi data → proposes action → engine gates → dispatches → records → agent sees result. Verify by checking ledger and whiteboard state.

### Crash test
Kill agent process mid-cycle. Verify:
1. Safety kernel detects heartbeat loss within 3 seconds
2. call_human fires
3. On restart, reconcile loop handles any in-flight actions
4. System recovers to known state

---

## 20. Known issues and decisions

1. **One action per cycle.** LLM proposes one action at a time. No multi-step plans. Option A from design discussions.
2. **Entire whiteboard dump.** No filtering at current scale. Revisit if whiteboard exceeds 2000 tokens.
3. **Episode = since last WAIT/CALL_HUMAN.** Not a fixed count. Adjust if episodes get too long.
4. **TTL = 2× refresh period by default.** Override available per sensor tool.
5. **No GPIO interlock in v1.** Added per-device-pack when dangerous actuators are connected.
6. **Prusa connection via PrusaLink HTTP API over ethernet.** Not serial.
7. **Dashboard is read-only.** Control via CLI or Telegram only.
8. **Trends computed by context builder (Python math), not LLM.** Built-in tools call same functions.
9. **differential() ≠ trends().** Trends = direction + magnitude. Differential = rate of change per second.
10. **Human messages processed on next agent cycle (3-10s).** Not synchronous.
