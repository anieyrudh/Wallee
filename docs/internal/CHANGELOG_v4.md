# CHANGELOG v4

## v4.0 — 2026-03-18

### New: `remember` built-in tool
- **Files:** `wallee/tools/builtins/remember.py` (new), `wallee/tools/registry.py`
- **What:** New actuator tool that persists agent observations to `knowledge/OBSERVATIONS.md`. Timestamped, newest-first, capped at 50 entries. Registered alongside discover_hardware, call_human, trends, differential, get_sensor_history.
- **Why:** The agent had no way to persist learned patterns across restarts. Now it can record things like "stringing observed at 215C with PLA — lowered to 210C and it resolved" and have that knowledge available in future sessions.

### New: OBSERVATIONS.md in knowledge loading path
- **Files:** `wallee/agent/loop.py`, `wallee/agent/prompt.py`
- **What:** `_load_knowledge()` now includes `OBSERVATIONS.md` alongside SOUL.md, HARDWARE.md, and LEARNED.md. Unlike static knowledge files, OBSERVATIONS.md is re-read every cycle (not cached) since the `remember` tool writes to it at runtime.
- **Why:** Without this, observations would be written to disk but never shown to the LLM.

### Rewrite: CLAUDE.md aligned to actual codebase
- **Files:** `CLAUDE.md`
- **What:** Complete rewrite of the ground truth spec to match the implemented code. Key corrections:
  - LLM is `google/gemini-3.1-pro-preview` via OpenRouter (was `openai/gpt-5.4`)
  - 5 device packs (host_pi, prusa_link, prusa_metrics, prusa_serial, pi_cameras), not 2-3
  - Engine gate order: ESTOP → Queue guard → Deadline → Approval → TOCTOU → Dispatch (was TOCTOU-first)
  - Pause/resume uses M25/M24 via `POST /api/v1/gcode` (not `PUT /api/v1/job` — Core One+ returns 405)
  - Vision support documented (camera frames + human photos as image_url blocks)
  - Full tool inventory: 17 sensors + 20 actuators with actual parameters
  - Complete whiteboard key schema (~80 keys)
  - Removed references to `web_search` and `git_pull` (deleted in v3.1)
  - Added all .env variables with defaults from config.py
  - Documented PrusaLink quirks, serial disconnect behavior, camera auto-discovery

### New: ARCHITECTURE.md
- **Files:** `ARCHITECTURE.md` (new)
- **What:** Concise architecture reference for developers and AI agents. Includes:
  - System diagram (all components and data flow)
  - Annotated file tree with one-line descriptions
  - Step-by-step agent cycle walkthrough
  - Engine gate flow diagram
  - How to add a new device pack / sensor / actuator
  - Known limitations and quirks (Core One+ 405, serial disconnects, HTTP PRINTING during purge, single-process threading)
- **Why:** CLAUDE.md is the normative spec. ARCHITECTURE.md is the quick-start guide.

---

## v4.1 — 2026-03-18 (8-change overhaul)

### Change 1: Structured output schema with observation + reasoning
- **Files:** `wallee/agent/llm_client.py`, `wallee/agent/parser.py`
- **What:** JSON schema now requires `observation` and `reasoning` fields on every decision (replacing the old `reason` field). Web search plugin removed. `stream: false` set explicitly. Response-healing plugin only.
- **Impact:** Every LLM response now contains a one-sentence observation (what it sees) and one-sentence reasoning (why it chose this action). Both are included in ledger events, Telegram approval messages, and dashboard activity log.

### Change 2: Prompt restructure with enforced rules
- **Files:** `wallee/agent/prompt.py`, `wallee/knowledge/SOUL.md`
- **What:** System prompt restructured into cached prefix (system message) + dynamic suffix (user message). SOUL.md now has a RULES section enforcing: check job.phase before deciding, never CALL_HUMAN twice for same issue, never escalate about FINISHED prints, one adjustment per cycle, terse output.
- **Impact:** Better LLM adherence to operational constraints. Prompt caching saves ~90% on system message tokens.

### Change 3: Clean SOUL.md / LEARNED.md split
- **Files:** `wallee/knowledge/SOUL.md`, `wallee/knowledge/LEARNED.md`
- **What:** Agent behavior rules moved from LEARNED.md to SOUL.md (autonomous operator mindset, learning from outcomes, API credit management). Printing knowledge stays in LEARNED.md. Added Prusa Core One+ quirks section to LEARNED.md.
- **Rule:** If you swap the printer for a CNC machine, SOUL.md stays unchanged, LEARNED.md gets replaced.

### Change 4: JOB_CONTEXT.md — per-print memory
- **Files:** `wallee/agent/loop.py`
- **What:** Job context lifecycle managed in agent loop. On PREPARING: creates `knowledge/JOB_CONTEXT.md` with filename/material/timestamp, fetches thumbnail from PrusaLink. During print: appends adjustments and issues. On FINISHED: archives summary to OBSERVATIONS.md, deletes JOB_CONTEXT.md and thumbnail.
- **Impact:** LLM has persistent per-job context. Print history accumulates automatically in OBSERVATIONS.md.

### Change 5: read_job_phase sensor
- **Files:** `wallee/device_packs/prusa_link/sensors.py`, `wallee/ui/dashboard.py`
- **What:** New 1Hz sensor deriving `job.phase` (IDLE/PREPARING/PRINTING/PAUSED/FINISHED/ERROR) from PrusaLink status. PREPARING detected when temps not at target or progress==0. Dashboard hero card now shows Phase instead of Printer State.
- **Keys published:** `job.phase`, `job.phase_detail`, `job.time_in_phase_s`

### Change 6: Prompt caching architecture
- **Files:** `wallee/agent/prompt.py`, `wallee/agent/loop.py`
- **What:** Prompt split into `build_system_prompt()` (static knowledge + tools + JOB_CONTEXT.md — cached by Gemini) and `build_user_message()` (phase banner, pending callout, whiteboard, episode, cameras — changes every cycle). Job thumbnail included as vision block in system message.
- **Impact:** ~90% discount on repeated system prompt tokens. Cache invalidates only when JOB_CONTEXT.md changes (new job starts).

### Change 7: CALL_HUMAN dedup
- **Files:** `wallee/agent/loop.py`, `wallee/human/telegram.py`
- **What:** Before sending CALL_HUMAN, checks `human.pending_callout` on whiteboard. If same message hash is already PENDING, suppresses the duplicate and logs a WAIT instead. When human responds via Telegram intent, pending callout status is set to ACKNOWLEDGED. Prompt shows pending callout status so LLM sees "PENDING CALLOUT: PENDING — 'message' (sent Ns ago)".
- **Impact:** Eliminates the 13+ message spam loop observed in v4.0 debug session.

### Change 8: Event-driven wake
- **Files:** `wallee/agent/loop.py`, `wallee/human/telegram.py`, `wallee/human/cli.py`, `wallee/main.py`
- **What:** Replaced fixed sleep with `threading.Event` wake mechanism. Telegram intent/urgent/estop/photo handlers and CLI intent/urgent handler call `agent.wake()` to interrupt the sleep immediately. Telegram outbound timeout increased from 10s to 30s.
- **Impact:** Agent responds to human messages within 1 cycle (seconds) instead of waiting up to 300s. Stop() is also instant.

### Test updates
- **Files:** `tests/test_agent_loop.py`, `tests/test_prompt.py`, `tests/test_llm_client.py`, `tests/test_dashboard.py`, `tests/test_change_detector.py`, `tests/test_engine.py`
- **What:** All tests updated for new schema (observation/reasoning fields), prompt split (system vs user message), dashboard label change, engine approval notifier signature, web search removal. Added new tests for CALL_HUMAN dedup and wake mechanism.
- **Result:** 298 tests pass.

### Agent started on Pi — 2026-03-18 20:09 SGT
- All 5 device packs loaded, 17 sensors + 19 actuators registered
- Buddy camera discovered at 192.168.0.194
- PrusaLink connected to 192.168.0.195
- Telegram bot connected
- First cycle: detected print FINISHED with nozzle blob, called human
- Second cycle: WAIT 300s — temps nominal, awaiting operator
