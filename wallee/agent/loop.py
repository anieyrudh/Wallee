"""Agent loop — reads state, calls LLM, routes decisions.

v4 features: job context lifecycle, CALL_HUMAN dedup, event-driven wake.
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from pathlib import Path

import httpx

from wallee.agent.llm_client import LLMClient
from wallee.agent.parser import Decision, parse_llm_output
from wallee.agent.prompt import build_system_prompt, build_user_message, build_messages
from wallee.agent.change_detector import ExternalChangeDetector
from wallee.whiteboard.client import Whiteboard
from wallee.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class AgentLoop:
    def __init__(
        self,
        whiteboard: Whiteboard,
        llm: LLMClient,
        tools: ToolRegistry,
        knowledge_dir: Path,
        poll_interval: float = 5.0,
        heartbeat_interval: float = 1.0,
        heartbeat_ttl: int = 3,
        last_decision_ttl: int = 600,
        ledger=None,
        data_dir: Path | None = None,
    ):
        self.wb = whiteboard
        self.llm = llm
        self.tools = tools
        self.knowledge_dir = knowledge_dir
        self.data_dir = data_dir or knowledge_dir
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_ttl = heartbeat_ttl
        self.last_decision_ttl = last_decision_ttl
        self.ledger = ledger
        self.call_human_fn = None  # Set by main.py to wire Telegram
        self._running = False
        self._knowledge_cache: dict[str, str] = {}
        self._change_detector = ExternalChangeDetector()
        self._last_responded_intent: str | None = None
        self._next_cycle_delay_s = poll_interval
        # Change 8: event-driven wake
        self._wake_event = threading.Event()
        # Change 4: job context tracking
        self._last_job_phase: str | None = None
        # Stabilization: oscillation detector (tracks last 2 decisions for A-B-A check)
        self._recent_decisions: deque[str] = deque(maxlen=2)
        # Stale decision retry counter (max 3 discards before proceeding anyway)
        self._stale_retries: int = 0
        # Track which rejection action_ids have already triggered cooldown
        self._processed_rejections: set[str] = set()

    def wake(self):
        """Wake the agent from sleep immediately. Called by Telegram/CLI/sensors."""
        self._wake_event.set()

    # ── Knowledge loading ───────────────────────────────────────────

    def _load_knowledge(self) -> dict[str, str]:
        """Load knowledge files from disk.

        Static files (SOUL.md, LEARNED.md) are cached after first load.
        OBSERVATIONS.md and JOB_CONTEXT.md are re-read every cycle.
        """
        if not self._knowledge_cache:
            for name in ["SOUL.md", "HARDWARE.md", "LEARNED.md"]:
                path = self.knowledge_dir / name
                if path.exists():
                    self._knowledge_cache[name] = path.read_text()
                else:
                    self._knowledge_cache[name] = ""

        # Re-read every cycle — written at runtime to data_dir
        for name in ["OBSERVATIONS.md", "JOB_CONTEXT.md"]:
            path = self.data_dir / name
            if path.exists():
                self._knowledge_cache[name] = path.read_text()
            else:
                self._knowledge_cache.pop(name, None)

        return self._knowledge_cache

    # ── Job context lifecycle (Change 4) ────────────────────────────

    def _handle_job_phase_transition(self, state: dict):
        """Track job.phase transitions and manage JOB_CONTEXT.md lifecycle."""
        phase = state.get("job.phase")
        if phase is None or phase == self._last_job_phase:
            # No transition — but patch filename if still "unknown"
            if phase in ("PREPARING", "PRINTING", "PAUSED"):
                self._patch_job_context_filename(state)
            return

        prev = self._last_job_phase
        self._last_job_phase = phase

        if prev is None:
            # First cycle — recover job context if we restarted mid-print
            if phase in ("PRINTING", "PAUSED", "PREPARING"):
                ctx_path = self.data_dir / "JOB_CONTEXT.md"
                if not ctx_path.exists():
                    self._create_job_context(state)
                    logger.info("Recovered job context after restart")
            return

        # PREPARING from any other state → create JOB_CONTEXT.md
        if phase == "PREPARING" and prev != "PREPARING":
            ctx_path = self.data_dir / "JOB_CONTEXT.md"
            if not ctx_path.exists():
                self._create_job_context(state)
            else:
                logger.debug("JOB_CONTEXT.md already exists, skipping recreation")

        # FINISHED or IDLE from PRINTING/PAUSED → archive and clean up
        if phase in ("FINISHED", "IDLE") and prev in ("PRINTING", "PAUSED"):
            self._archive_job_context()

    _MATERIAL_RE = re.compile(
        r'(?:^|[_\-.\s/])(PLA|PETG|ASA|ABS|TPU|PC|PA|PP|PVB|HIPS)(?:$|[_\-.\s/\d])',
        re.IGNORECASE,
    )

    def _detect_material(self, filename: str, state: dict) -> str:
        """Detect material from filename regex, PrusaLink API fallback, or 'unknown'."""
        # Primary: regex match in filename
        m = self._MATERIAL_RE.search(filename)
        if m:
            return m.group(1).upper()

        # Fallback: PrusaLink GET /api/v1/job
        host = os.environ.get("PRUSALINK_HOST", "").strip()
        api_key = os.environ.get("PRUSALINK_API_KEY", "").strip()
        if host:
            base = host if host.startswith("http") else f"http://{host}"
            try:
                resp = httpx.get(f"{base}/api/v1/job",
                                 headers={"X-Api-Key": api_key}, timeout=5.0)
                if resp.status_code == 200:
                    data = resp.json()
                    mat = data.get("file", {}).get("material", "")
                    if mat:
                        return mat.upper()
            except Exception as e:
                logger.debug(f"PrusaLink /api/v1/job material lookup failed: {e}")

        return "unknown"

    def _fetch_job_filename(self, state: dict) -> str:
        """Fetch print filename — HTTP API first, whiteboard fallback, then 'unknown'."""
        # Primary: fetch from PrusaLink HTTP API
        host = os.environ.get("PRUSALINK_HOST", "").strip()
        api_key = os.environ.get("PRUSALINK_API_KEY", "").strip()
        if host:
            base = host if host.startswith("http") else f"http://{host}"
            try:
                resp = httpx.get(f"{base}/api/v1/job",
                                 headers={"X-Api-Key": api_key}, timeout=5.0)
                if resp.status_code == 200:
                    data = resp.json()
                    name = (data.get("file", {}).get("display_name")
                            or data.get("file", {}).get("name"))
                    if name:
                        return name
            except Exception as e:
                logger.debug(f"PrusaLink /api/v1/job filename fetch failed: {e}")

        # Fallback: whiteboard (UDP stream)
        wb_name = state.get("printer.print_filename")
        if wb_name:
            return wb_name

        return "unknown"

    def _create_job_context(self, state: dict):
        """Create JOB_CONTEXT.md when a new print starts."""
        filename = self._fetch_job_filename(state)
        material = self._detect_material(filename, state)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")

        ctx = (
            f"# Current Job\n"
            f"File: {filename}\n"
            f"Material: {material}\n"
            f"Started: {ts}\n\n"
            f"## Adjustments made\n(none yet)\n\n"
            f"## Issues observed\n(none yet)\n"
        )

        ctx_path = self.data_dir / "JOB_CONTEXT.md"
        try:
            ctx_path.write_text(ctx)
            logger.info(f"JOB_CONTEXT.md created for {filename}")
        except Exception as e:
            logger.error(f"Failed to create JOB_CONTEXT.md: {e}")

        # Fetch thumbnail from PrusaLink
        self._fetch_thumbnail(filename)

    def _patch_job_context_filename(self, state: dict):
        """If JOB_CONTEXT.md exists with 'unknown' filename, patch it with a real one."""
        ctx_path = self.data_dir / "JOB_CONTEXT.md"
        if not ctx_path.exists():
            return
        try:
            text = ctx_path.read_text()
        except Exception:
            return
        if "File: unknown" not in text and "File: \n" not in text:
            return
        # Try to get a real filename
        real_name = state.get("printer.print_filename")
        if not real_name:
            real_name = self._fetch_job_filename(state)
        if not real_name or real_name == "unknown":
            return
        # Patch the filename line
        text = text.replace("File: unknown", f"File: {real_name}", 1)
        text = text.replace("File: \n", f"File: {real_name}\n", 1)
        try:
            ctx_path.write_text(text)
            logger.info(f"JOB_CONTEXT.md filename patched to {real_name}")
            # Also update material if it was unknown
            if "Material: unknown" in text:
                material = self._detect_material(real_name, state)
                if material != "unknown":
                    text = text.replace("Material: unknown", f"Material: {material}", 1)
                    ctx_path.write_text(text)
        except Exception as e:
            logger.error(f"Failed to patch JOB_CONTEXT.md filename: {e}")

    def _fetch_thumbnail(self, filename: str):
        """Fetch print thumbnail from PrusaLink API."""
        host = os.environ.get("PRUSALINK_HOST", "").strip()
        api_key = os.environ.get("PRUSALINK_API_KEY", "").strip()
        if not host:
            return

        base = host if host.startswith("http") else f"http://{host}"
        thumb_path = self.data_dir / "job_thumbnail.png"

        for size in ("l", "s"):
            url = f"{base}/thumb/{size}/usb/{filename}"
            try:
                resp = httpx.get(url, headers={"X-Api-Key": api_key}, timeout=5.0)
                if resp.status_code == 200 and len(resp.content) > 100:
                    thumb_path.write_bytes(resp.content)
                    logger.info(f"Thumbnail saved ({len(resp.content)} bytes)")
                    return
            except Exception as e:
                logger.debug(f"Thumbnail fetch {size} failed: {e}")

        logger.info("No thumbnail available for this print")

    def _archive_job_context(self):
        """Archive JOB_CONTEXT.md to OBSERVATIONS.md and clean up."""
        ctx_path = self.data_dir / "JOB_CONTEXT.md"
        thumb_path = self.data_dir / "job_thumbnail.png"

        if not ctx_path.exists():
            return

        try:
            ctx = ctx_path.read_text()
            # Extract key fields
            lines = ctx.splitlines()
            filename = "unknown"
            material = "unknown"
            adjustments = []
            issues = []
            section = None
            for line in lines:
                if line.startswith("File: "):
                    filename = line[6:].strip()
                elif line.startswith("Material: "):
                    material = line[10:].strip()
                elif line.startswith("## Adjustments"):
                    section = "adj"
                elif line.startswith("## Issues"):
                    section = "iss"
                elif line.startswith("- ") and section == "adj":
                    adjustments.append(line[2:].strip())
                elif line.startswith("- ") and section == "iss":
                    issues.append(line[2:].strip())

            adj_str = "; ".join(adjustments) if adjustments else "none"
            iss_str = "; ".join(issues) if issues else "none"
            summary = f"Job complete: {filename} ({material}). Adjustments: {adj_str}. Issues: {iss_str}."

            # Direct call to remember() for internal bookkeeping — not LLM-proposed.
            # Archives the completed job's summary to OBSERVATIONS.md so future
            # cycles can reference past print outcomes.
            from wallee.tools.builtins.remember import remember
            remember(observation=summary)
            logger.info(f"Archived job context: {filename}")
        except Exception as e:
            logger.error(f"Failed to archive job context: {e}")

        # Request feedback via Telegram
        if self.call_human_fn:
            try:
                self.call_human_fn(
                    f"Print complete: {filename}. How did it turn out? Reply: great, ok, or failed",
                    "info",
                )
                self.wb.publish("agent.awaiting_feedback", json.dumps({
                    "filename": filename,
                    "completed_at": time.time(),
                }), ttl=3600)  # Wait up to 1 hour for feedback
                logger.info(f"Requested print feedback for {filename}")
            except Exception as e:
                logger.error(f"Failed to request print feedback: {e}")

        # Clean up
        try:
            ctx_path.unlink(missing_ok=True)
            thumb_path.unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"Failed to clean up job context files: {e}")

    def _append_to_job_context(self, section: str, entry: str):
        """Append an entry to a section in JOB_CONTEXT.md."""
        ctx_path = self.data_dir / "JOB_CONTEXT.md"
        if not ctx_path.exists():
            return

        try:
            text = ctx_path.read_text()
            ts = time.strftime("%H:%M:%S")
            new_entry = f"- {ts}: {entry}"

            marker = f"## {section}"
            if marker in text:
                # Replace "(none yet)" if present
                text = text.replace(f"{marker}\n(none yet)", f"{marker}\n{new_entry}")
                if new_entry not in text:
                    # Append after last entry in section
                    parts = text.split(marker, 1)
                    if len(parts) == 2:
                        after = parts[1]
                        # Find next section or end
                        next_section = after.find("\n## ")
                        if next_section > 0:
                            insert_at = next_section
                        else:
                            insert_at = len(after)
                        after = after[:insert_at].rstrip() + "\n" + new_entry + "\n" + after[insert_at:]
                        text = parts[0] + marker + after

                ctx_path.write_text(text)
        except Exception as e:
            logger.error(f"Failed to append to JOB_CONTEXT.md: {e}")

    # ── Helpers ─────────────────────────────────────────────────────

    def _heartbeat(self):
        """Background thread: publish heartbeat independently of main loop."""
        while self._running:
            try:
                self.wb.publish("agent.heartbeat", time.monotonic(), ttl=self.heartbeat_ttl)
            except Exception as e:
                logger.error(f"Heartbeat publish failed: {e}")
            time.sleep(self.heartbeat_interval)

    def _get_episode(self) -> list[dict]:
        """Get current episode from ledger."""
        if self.ledger and hasattr(self.ledger, "current_episode"):
            return self.ledger.current_episode()
        return []

    def _set_next_cycle_delay(self, decision):
        """Select the next loop delay based on the routed decision."""
        if getattr(decision, "type", "") == "WAIT":
            self._next_cycle_delay_s = max(0.0, float(decision.check_after_s))
        else:
            self._next_cycle_delay_s = self.poll_interval

    def _sleep_until_next_cycle(self, delay_s: float):
        """Sleep until delay expires or wake() is called."""
        self._wake_event.wait(timeout=max(0.0, float(delay_s)))
        self._wake_event.clear()

    def _get_pending_callout(self) -> dict | None:
        """Read the pending callout from whiteboard."""
        raw = self.wb.read("human.pending_callout")
        if raw is None:
            return None
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return None
        return raw if isinstance(raw, dict) else None

    # ── Stabilization guards ───────────────────────────────────────

    # Engine rejection reasons — these are NOT human rejections
    _ENGINE_REJECTION_PATTERNS = [
        "toctou", "expired", "chain_skipped", "precheck", "is required",
        "empty parameters", "outside bounds", "unknown tool", "outside safe range",
    ]

    def _is_human_rejection(self, reason: str) -> bool:
        """Return True only if this rejection came from a human, not the engine."""
        if not reason:
            return False
        reason_lower = reason.lower()
        for pattern in self._ENGINE_REJECTION_PATTERNS:
            if pattern in reason_lower:
                return False
        return True  # No engine pattern matched — assume human rejection

    def _scan_episode_for_rejections(self, episode: list[dict]):
        """Scan episode for human-rejected actions and publish cooldown.

        Only triggers cooldown for actual human rejections (via Telegram approve/reject),
        NOT for engine rejections (TOCTOU, expired, precheck failures).
        Each rejection is processed at most once (tracked by action_id).
        """
        for entry in reversed(episode):
            status = str(entry.get("status", "")).upper()
            if "REJECT" not in status:
                continue
            action_id = entry.get("action_id", "")
            if not action_id or action_id in self._processed_rejections:
                continue
            self._processed_rejections.add(action_id)

            tool = entry.get("tool", "")
            reason = entry.get("reason", "") or entry.get("error_json", "")
            if not tool:
                continue

            if not self._is_human_rejection(reason):
                logger.debug(f"Engine rejection (not cooldown): {tool} — {reason}")
                continue

            self.wb.publish("agent.cooldown", json.dumps({
                "tool": tool,
                "until": time.time() + 180,
                "reason": "Human rejected this action",
            }), ttl=180)
            logger.info(f"Cooldown published: {tool} rejected by human, suppressing for 180s")
            return  # Only publish for the most recent human rejection

    def _check_cooldown(self, decision) -> "Decision":
        """If the proposed tool was recently rejected by a human, convert to WAIT."""
        if decision.type not in ("ACTION", "ACTION_CHAIN"):
            return decision
        cooldown = self.wb.read("agent.cooldown")
        if not cooldown:
            return decision
        try:
            data = json.loads(cooldown) if isinstance(cooldown, str) else cooldown
        except (json.JSONDecodeError, TypeError):
            return decision

        proposed_tools = (
            [decision.tool] if decision.type == "ACTION"
            else [a.get("tool", "") for a in decision.actions]
        )
        if data.get("tool") in proposed_tools and time.time() < data.get("until", 0):
            remaining = int(data["until"] - time.time())
            logger.info(f"Cooldown active: {data['tool']} rejected by human, suppressing for {remaining}s more")
            return Decision(
                type="WAIT",
                observation=decision.observation,
                reasoning=f"Cooldown: {data['tool']} was rejected by human. Observing.",
                check_after_s=30,
            )
        return decision

    def _check_oscillation(self, decision) -> "Decision":
        """Detect ACTION flip-flop: ACTION:X → ACTION:Y → ACTION:X.

        WAIT in the middle is NOT oscillation — it's the healthy observe-act-observe
        pattern. Only flag when three consecutive ACTION decisions flip-flop between
        two different tools (e.g., pause → resume → pause).
        """
        current_key = decision.type + ":" + getattr(decision, "tool", "")

        if decision.type in ("ACTION", "ACTION_CHAIN"):
            if len(self._recent_decisions) >= 2:
                prev = list(self._recent_decisions)
                # A-B-A: current == prev[0] and current != prev[1]
                # But only if the middle entry is also an ACTION (WAIT never triggers oscillation)
                if prev[0] == current_key and prev[0] != prev[1] and not prev[1].startswith("WAIT:"):
                    logger.warning(f"Oscillation detected: {prev[0]} -> {prev[1]} -> {current_key}. Forcing extended WAIT.")
                    self._recent_decisions.clear()
                    return Decision(
                        type="WAIT",
                        observation=decision.observation,
                        reasoning="Oscillation detected — conflicting actions in last 3 cycles. Stepping back to observe.",
                        check_after_s=60,
                    )
            self._recent_decisions.append(current_key)
        else:
            # Track WAITs but they never trigger oscillation — WAIT is the healthy default
            self._recent_decisions.append("WAIT:")

        return decision

    def _extract_changes(self, decision, state: dict) -> list[dict]:
        """Build structured old -> new parameter deltas for dashboard display."""
        tool_specs = {
            "set_speed_factor": ("Speed", "printer.speed", "percent", "%"),
            "set_flow_factor": ("Flow", "printer.flow", "percent", "%"),
            "set_temperature": {
                "nozzle": ("Nozzle Temp", "printer.target_nozzle", "target", "C"),
                "bed": ("Bed Temp", "printer.target_bed", "target", "C"),
                "chamber": ("Chamber Temp", "printer.target_chamber", "target", "C"),
            },
        }

        if decision.type == "ACTION":
            actions = [{"tool": decision.tool, "params": decision.params or {}}]
        elif decision.type == "ACTION_CHAIN":
            actions = decision.actions or []
        else:
            return []

        changes = []
        for act in actions:
            tool_name = act.get("tool", "")
            params = act.get("params", {}) or {}
            spec = tool_specs.get(tool_name)
            if spec is None:
                continue

            if tool_name == "set_temperature":
                heater = str(params.get("heater", "")).lower()
                spec = spec.get(heater)
                if spec is None:
                    continue

            label, state_key, param_key, unit = spec
            new_value = params.get(param_key)
            if new_value is None:
                continue
            changes.append({
                "tool": tool_name,
                "label": label,
                "from": state.get(state_key),
                "to": new_value,
                "unit": unit,
            })
        return changes

    # ── Decision routing ────────────────────────────────────────────

    def _route_decision(self, decision, state: dict):
        """Route a parsed decision to the appropriate handler."""
        summary = ""
        observation = getattr(decision, "observation", "")
        reasoning = getattr(decision, "reasoning", "")

        if decision.type == "ACTION":
            if decision.tool not in self.tools:
                logger.warning(f"Unknown tool: {decision.tool}")
                return
            tool = self.tools.get(decision.tool)
            if self.ledger and hasattr(self.ledger, "propose"):
                self.ledger.propose(
                    tool=decision.tool,
                    params=decision.params,
                    reason=decision.reason,
                    device_group=tool.device_group,
                    requires_approval=tool.requires_approval,
                    max_proposal_age_ms=tool.max_proposal_age_ms,
                    observation=observation,
                )
            summary = f"ACTION: {decision.tool} — {reasoning}"
            logger.info(f"Proposed: {decision.tool}({decision.params}) — {reasoning}")
            # Job context: record adjustment
            self._append_to_job_context(
                "Adjustments made",
                f"{decision.tool}({json.dumps(decision.params, default=str)}) — {reasoning}"
            )

        elif decision.type == "ACTION_CHAIN":
            import uuid as _uuid
            chain_id = str(_uuid.uuid4())
            tools_in_chain = []
            for seq, act in enumerate(decision.actions):
                tool_name = act.get("tool", "")
                if tool_name not in self.tools:
                    logger.warning(f"ACTION_CHAIN: unknown tool '{tool_name}' at seq {seq}, skipping chain")
                    return
                tools_in_chain.append(tool_name)
            for seq, act in enumerate(decision.actions):
                tool_name = act["tool"]
                tool = self.tools.get(tool_name)
                params = act.get("params", {})
                if self.ledger and hasattr(self.ledger, "propose"):
                    self.ledger.propose(
                        tool=tool_name,
                        params=params,
                        reason=decision.reason,
                        device_group=tool.device_group,
                        requires_approval=tool.requires_approval,
                        max_proposal_age_ms=tool.max_proposal_age_ms,
                        observation=observation,
                        chain_id=chain_id,
                        chain_seq=seq,
                    )
            summary = f"ACTION_CHAIN ({len(decision.actions)} steps): {' → '.join(tools_in_chain)} — {reasoning}"
            logger.info(summary)

        elif decision.type == "WAIT":
            summary = f"WAIT: {reasoning}"
            logger.info(f"WAIT: {reasoning} (check after {decision.check_after_s}s)")
            if self.ledger and hasattr(self.ledger, "record_wait"):
                self.ledger.record_wait(reasoning, observation=observation, reasoning=reasoning)
            # Job context: record notable observations
            if observation and state.get("job.phase") in ("PRINTING", "PAUSED", "PREPARING"):
                obs_lower = observation.lower()
                if any(w in obs_lower for w in ("anomal", "issue", "error", "fail", "detach",
                                                  "blob", "string", "warp", "gap")):
                    self._append_to_job_context("Issues observed", observation)

        elif decision.type == "CALL_HUMAN":
            # Change 7: CALL_HUMAN dedup
            msg_hash = hashlib.md5(decision.message[:100].encode()).hexdigest()[:8]
            pending = self._get_pending_callout()

            if pending and pending.get("hash") == msg_hash and pending.get("status") == "PENDING":
                logger.info(f"CALL_HUMAN suppressed (duplicate of pending callout)")
                summary = f"WAIT: suppressed duplicate CALL_HUMAN"
                if self.ledger and hasattr(self.ledger, "record_wait"):
                    self.ledger.record_wait(f"suppressed duplicate: {decision.message[:80]}",
                                            observation=observation, reasoning=reasoning)
            else:
                summary = f"CALL_HUMAN [{decision.severity}]: {decision.message}"
                logger.warning(f"CALL_HUMAN [{decision.severity}]: {decision.message}")
                if self.ledger and hasattr(self.ledger, "record_call_human"):
                    self.ledger.record_call_human(decision.message,
                                                  observation=observation, reasoning=reasoning)
                if self.call_human_fn:
                    try:
                        self.call_human_fn(decision.message, decision.severity)
                    except Exception as e:
                        logger.error(f"call_human delivery failed: {e}")

                # Publish pending callout
                self.wb.publish("human.pending_callout", json.dumps({
                    "hash": msg_hash,
                    "message": decision.message[:200],
                    "time": time.time(),
                    "status": "PENDING",
                }), ttl=self.last_decision_ttl)

                # Job context: record issue
                self._append_to_job_context("Issues observed", decision.message[:200])

        # Publish to whiteboard for dashboard
        if summary:
            self.wb.publish("agent.last_decision", summary, ttl=self.last_decision_ttl)
            status = decision.type
            if decision.type == "ACTION_CHAIN":
                status = "ACTION"
            entry = json.dumps({
                "ts": time.strftime("%H:%M:%S"),
                "type": decision.type,
                "status": status,
                "text": summary[:300],
                "observation": observation[:200] if observation else "",
                "reasoning": reasoning[:200] if reasoning else "",
                "changes": self._extract_changes(decision, state),
            })
            self.wb.r.lpush("agent.activity_log", entry)
            self.wb.r.ltrim("agent.activity_log", 0, 19)

    # ── Main cycle ──────────────────────────────────────────────────

    def run_once(self) -> str:
        """Run a single agent cycle. Returns the raw LLM response."""
        # 1. Read whiteboard with trends
        state = self.wb.read_all_with_trends()

        # 2. Handle job phase transitions (create/archive JOB_CONTEXT.md)
        self._handle_job_phase_transition(state)

        # 3. Read episode
        episode = self._get_episode()

        # 3b. Scan episode for human rejections → publish cooldown
        self._scan_episode_for_rejections(episode)

        # 4. Read human intent (skip if already responded)
        raw_intent = self.wb.read("human.intent")
        if raw_intent and raw_intent == self._last_responded_intent:
            intent = None
        else:
            intent = raw_intent

        # 4b. Process print feedback (great/ok/failed) if awaiting
        intent_text = (raw_intent or "").lower().strip()
        awaiting = self.wb.read("agent.awaiting_feedback")
        if awaiting and intent_text in ("great", "ok", "failed"):
            try:
                data = json.loads(awaiting) if isinstance(awaiting, str) else awaiting
                feedback_filename = data.get("filename", "unknown")
                from wallee.tools.builtins.remember import remember
                remember(observation=f"Print feedback: {feedback_filename} rated '{intent_text}' by human")
                self.wb.r.delete("agent.awaiting_feedback")
                self._last_responded_intent = raw_intent
                logger.info(f"Recorded print feedback: {feedback_filename} = {intent_text}")
            except Exception as e:
                logger.error(f"Failed to process print feedback: {e}")

        # 4c. Auto-start next queued print if IDLE + queue non-empty + bed cooled
        current_phase = state.get("job.phase", "IDLE")
        if current_phase == "IDLE":
            queue_raw = self.wb.read("print.queue")
            bed_temp = float(state.get("printer.temp_bed") or 100)
            if queue_raw and bed_temp < 35:
                try:
                    items = json.loads(queue_raw) if isinstance(queue_raw, str) else queue_raw
                    if items and isinstance(items, list):
                        next_file = items.pop(0)
                        self.wb.publish("print.queue", json.dumps(items), ttl=86400)
                        logger.info(f"Auto-starting next queued print: {next_file}")
                        decision = Decision(
                            type="ACTION",
                            tool="start_print",
                            params={"file_path": next_file},
                            observation=f"Queue has {len(items) + 1} prints, bed cooled to {bed_temp:.0f}C",
                            reasoning=f"Auto-starting next queued print: {next_file}",
                        )
                        self._route_decision(decision, state)
                        self._set_next_cycle_delay(decision)
                        return '{"type": "ACTION", "observation": "auto-start from queue"}'
                except (json.JSONDecodeError, TypeError, ValueError) as e:
                    logger.error(f"Failed to process print queue: {e}")

        # 5. Detect external changes
        external_changes = self._change_detector.detect(state, episode)

        # 6. Load knowledge
        knowledge = self._load_knowledge()

        # 7. Read pending callout
        pending_callout = self._get_pending_callout()

        # 8. Build cached system prompt + dynamic user message
        system_prompt = build_system_prompt(
            knowledge=knowledge,
            tools=self.tools.list_for_llm(),
        )

        user_text = build_user_message(
            state=state,
            episode=episode,
            intent=intent,
            current_time=time.time(),
            external_changes=external_changes,
            pending_callout=pending_callout,
        )

        # 9. Build messages with vision content
        messages = build_messages(system_prompt, user_text, state,
                                  knowledge_dir=self.knowledge_dir,
                                  data_dir=self.data_dir)

        # 10. Capture pre-call state for stale decision check
        pre_call_intent = self.wb.read("human.intent")
        pre_call_pending = self.wb.read("human.pending_callout")
        pre_call_state = self.wb.read("printer.state")

        # 11. Call LLM (pass available tool names for output validation)
        tool_names = [t["name"] for t in self.tools.list_for_llm()]
        raw_response = self.llm.call(system_prompt, messages=messages,
                                     available_tools=tool_names)

        # 12. Stale decision check — discard if world changed during LLM call
        stale = False
        post_call_intent = self.wb.read("human.intent")
        if post_call_intent != pre_call_intent:
            logger.info("Human input arrived during LLM call.")
            stale = True
        post_call_pending = self.wb.read("human.pending_callout")
        if post_call_pending != pre_call_pending:
            logger.info("Pending callout changed during LLM call.")
            stale = True
        post_call_state = self.wb.read("printer.state")
        if post_call_state != pre_call_state:
            logger.info(f"Printer state changed during LLM call ({pre_call_state} → {post_call_state}).")
            stale = True

        if stale and self._stale_retries < 3:
            self._stale_retries += 1
            logger.info(f"Discarding stale decision (retry {self._stale_retries}/3)")
            return raw_response
        elif stale:
            logger.warning("Max stale retries reached, proceeding with potentially stale decision")
        self._stale_retries = 0

        # 13. Parse (use job.phase for interval clamping, fallback to printer.state)
        phase = state.get("job.phase", state.get("printer.state"))
        decision = parse_llm_output(raw_response, printer_state=phase)

        # 14. Stabilization: cooldown guard (human rejected this tool recently)
        decision = self._check_cooldown(decision)

        # 15. Stabilization: oscillation guard (A-B-A flip-flop detection)
        decision = self._check_oscillation(decision)

        # 16. Route
        self._route_decision(decision, state)
        self._set_next_cycle_delay(decision)

        # 18. Consume human intent — delete from whiteboard after the agent has seen it
        # The intent is already logged in human.intent_log for the dashboard, but the
        # raw key must be cleared so it doesn't anchor future cycles' reasoning.
        if raw_intent and raw_intent != self._last_responded_intent:
            self._last_responded_intent = raw_intent
            self.wb.r.delete("human.intent")
            logger.info(f"Human intent consumed and cleared: {raw_intent[:50]}")

        return raw_response

    def run(self):
        """Main loop. Blocks until stop() is called."""
        self._running = True

        hb_thread = threading.Thread(target=self._heartbeat, daemon=True, name="agent-heartbeat")
        hb_thread.start()
        logger.info("Agent loop started")

        try:
            while self._running:
                delay_s = self.poll_interval
                try:
                    self.run_once()
                    delay_s = self._next_cycle_delay_s
                except Exception as e:
                    logger.error(f"Agent cycle error: {e}")
                    self._next_cycle_delay_s = self.poll_interval
                self._sleep_until_next_cycle(delay_s)
        finally:
            self._running = False
            logger.info("Agent loop stopped")

    def stop(self):
        """Signal the loop to stop."""
        self._running = False
        self._wake_event.set()  # unblock sleep immediately
