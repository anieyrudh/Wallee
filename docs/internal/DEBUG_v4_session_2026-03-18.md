# V4 Debug Report — 2026-03-18 20:09-20:33 SGT

> Historical debug log for a past session on the example deployment. It is preserved as an incident record, not as the current architectural reference.

## Session summary

Agent ran for ~24 minutes on Pi (<PI_WIFI_IP>) with v4 code. Printer (Core One+ at <PRINTER_IP>) had a previously paused print with nozzle blob. Operator resumed and started a new print externally. Print detached from bed mid-way. Agent correctly identified the failure via camera vision but entered a CALL_HUMAN spam loop after the print finished.

---

## Ledger: 3 actions

| Time | Status | Tool | Reason |
|------|--------|------|--------|
| 14:23:24 | **DONE** | `pause_print` | Nozzle blob visible at 28% — paused for cleanup (M25) |
| 20:18:08 | **REJECTED** | `cancel_print` | Operator rejected — determined print was fine |
| 20:23:23 | **REJECTED** | `cancel_print` | Approval timeout (300s) — no response from operator |

## Ledger: 27 events (v4 session)

```
20:04:07 [WAIT ] Printer FINISHED and idle, temps cooled. Waiting for intent.
20:06:23 [WAIT ] Nozzle camera shows cold filament blob. Manual cleaning needed.
20:09:41 [CALL ] PLA print paused at 28% due to nozzle blob. Clean nozzle and resume.
20:09:59 [WAIT ] Print FINISHED, temps cooled. Awaiting operator cleanup.
20:15:33 [WAIT ] New print started externally. Nozzle 171C, bed 60C. Observing.
20:16:57 [WAIT ] Operator raised nozzle to 235C (cleaning blob). Awaiting completion.
20:18:52 [WAIT ] Cancel proposed — bed detachment + nozzle blob. Awaiting approval.
20:20:22 [REJCT] cancel_print rejected by operator
20:20:24 [CALL ] Blob visible on nozzle camera. If left running, will encase heater block.
20:21:03 [CALL ] Blob still present. Since operator resumed, continuing to monitor.
20:21:40 [WAIT ] Operator says print is fine. Observing silently.
20:24:46 [WAIT ] ACTION missing tool (LLM parser error)
20:26:49 [CALL ] Buddy cam: bed empty. Nozzle cam: filament on nozzle tip.
20:27:15 [CALL ] Grey build plate completely empty at 72%. Part detached.
20:27:39 [CALL ] Both cameras confirm: empty bed + nozzle blob.
20:28:03 [CALL ] Nozzle cam: blob stuck to nozzle. Buddy cam: bed empty.
20:28:23 [REJCT] cancel_print approval timeout (300s)
20:28:28 [CALL ] Buddy cam: empty plate. Nozzle cam: blob on nozzle tip.
20:28:54 [CALL ] Buddy cam: empty plate. Nozzle cam: brown blob on hotend.
20:29:13 [CALL ] Same observation repeated.
20:29:39 [CALL ] Same observation repeated.
20:30:01 [CALL ] Same observation repeated.
20:30:22 [CALL ] Buddy cam: bare steel sheet visible. No printed model.
20:30:55 [CALL ] Same observation repeated.
20:31:19 [CALL ] Buddy cam: stringy filament debris. Nozzle cam: blob on nozzle.
20:31:38 [CALL ] Same observation repeated.
20:32:11 [CALL ] Same observation repeated.
```

## Whiteboard state at shutdown

### Printer
- **State:** FINISHED / Job: IDLE
- **File:** Benchy_Bonkers_0.4n_0.28mm_PLA_COREONE_8m.bgcod
- **Nozzle:** 47.0°C / target 0°C
- **Bed:** 35.2°C / target 0°C
- **Speed/Flow:** 100% / 100%
- **Overcurrent:** nozzle=0, input=0 (safe)
- **Voltage:** bed=24.1V, nozzle=0.04V

### Cameras
- **Nozzle:** live (19KB frame, port 8080)
- **Buddy1:** live (49KB frame, IP <CAM1_IP>)
- **Buddy2/3:** offline

### Host Pi
- CPU: 47.4°C, 6.9% load, 6.9% memory, 17.1% disk, 24.3h uptime

### Human state
- **Intent:** "where does it show that the model is no longer attached?"
- **Urgent/ESTOP:** not set

---

## Specific checks

### 1. `read_job_phase` sensor
**NOT FOUND in codebase.** No file contains `read_job_phase`. The `job.phase` whiteboard key has history lists in Redis (published by a previous v3 session) but the string keys are expired. The `pause_print` precheck reads `job.phase` but it always returns `None` since no sensor publishes it.

### 2. Dashboard hero card
Shows `printer.state` (FINISHED/IDLE/PRINTING/etc). Does NOT show `job.phase`. Reference: `dashboard.py:239`.

### 3. Event-driven wake mechanism
**None.** Agent uses polling only. After routing a decision, it sleeps for `check_after_s` seconds (in 0.5s chunks). No mechanism to wake the agent early on whiteboard state changes.

---

## Issues found

### P0: CALL_HUMAN spam loop
After the print finished (20:25:46 PRINTING→FINISHED), the agent sent 13+ consecutive CALL_HUMAN messages about the same issue (empty bed + nozzle blob) at ~20-25s intervals. No deduplication. The print was already FINISHED/IDLE so there was nothing actionable.

**Root cause:** The agent has no memory of having already escalated. Each cycle it sees the camera showing the same failed state and escalates again. The `_last_responded_intent` dedup only covers human intents, not CALL_HUMAN messages.

**Fix needed:** Track last CALL_HUMAN message hash. If the same or substantially similar message was sent within N minutes, suppress or downgrade to WAIT.

### P1: Telegram outbound failing
Every `call_human` fell to durable outbox. One explicit `Telegram send failed: Timed out` at 20:27:20. The Telegram bot receives inbound messages fine (operator was interacting via Telegram) but outbound `send()` times out.

**Possible cause:** The synchronous `send()` method calls `asyncio.run_coroutine_threadsafe()` with a 10s timeout. Under load (rapid CALL_HUMAN cycles), the async event loop may be saturated.

### P2: LLM parser error
At 20:24:46, the LLM returned an ACTION with no tool name. Parser correctly defaulted to WAIT, but this represents a wasted LLM cycle.

### P3: `read_job_phase` missing
The `pause_print` precheck reads `job.phase` to block during PREPARING, but no sensor publishes this key. The precheck always gets `None` and skips the check, meaning a pause during PREPARING would hit the Core One+ 405 error.

### P4: No event-driven wake
State changes (PRINTING→FINISHED, manual temperature changes, external pause/resume) are only detected on the next polling cycle. With 60-300s intervals, the agent can be minutes behind reality.
