"""Agent loop — reads state, calls LLM, routes decisions."""

import logging
import threading
import time
from pathlib import Path

from wallee.agent.llm_client import LLMClient
from wallee.agent.parser import parse_llm_output
from wallee.agent.prompt import build_prompt, build_messages
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
        ledger=None,  # Phase 2
    ):
        self.wb = whiteboard
        self.llm = llm
        self.tools = tools
        self.knowledge_dir = knowledge_dir
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_ttl = heartbeat_ttl
        self.ledger = ledger
        self.call_human_fn = None  # Set by main.py to wire Telegram
        self._running = False
        self._knowledge_cache: dict[str, str] = {}
        self._change_detector = ExternalChangeDetector()

    def _load_knowledge(self) -> dict[str, str]:
        """Load knowledge files from disk. Cached after first load."""
        if self._knowledge_cache:
            return self._knowledge_cache
        for name in ["SOUL.md", "HARDWARE.md", "LEARNED.md"]:
            path = self.knowledge_dir / name
            if path.exists():
                self._knowledge_cache[name] = path.read_text()
            else:
                self._knowledge_cache[name] = ""
        return self._knowledge_cache

    def _heartbeat(self):
        """Background thread: publish heartbeat independently of main loop."""
        while self._running:
            try:
                self.wb.publish("agent.heartbeat", time.monotonic(), ttl=self.heartbeat_ttl)
            except Exception as e:
                logger.error(f"Heartbeat publish failed: {e}")
            time.sleep(self.heartbeat_interval)

    def _get_episode(self) -> list[dict]:
        """Get current episode from ledger. Returns [] if no ledger (Phase 1)."""
        if self.ledger and hasattr(self.ledger, "current_episode"):
            return self.ledger.current_episode()
        return []

    def _route_decision(self, decision):
        """Route a parsed decision to the appropriate handler."""
        summary = ""

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
                )
            summary = f"ACTION: {decision.tool} — {decision.reason}"
            logger.info(f"Proposed: {decision.tool}({decision.params}) — {decision.reason}")

        elif decision.type == "WAIT":
            summary = f"WAIT: {decision.reason}"
            logger.info(f"WAIT: {decision.reason} (check after {decision.check_after_s}s)")
            if self.ledger and hasattr(self.ledger, "record_wait"):
                self.ledger.record_wait(decision.reason)

        elif decision.type == "CALL_HUMAN":
            summary = f"CALL_HUMAN [{decision.severity}]: {decision.message}"
            logger.warning(f"CALL_HUMAN [{decision.severity}]: {decision.message}")
            if self.call_human_fn:
                try:
                    self.call_human_fn(decision.message, decision.severity)
                except Exception as e:
                    logger.error(f"call_human delivery failed: {e}")

        # Publish to whiteboard so dashboard can show agent activity
        if summary:
            import json as _json
            self.wb.publish("agent.last_decision", summary, ttl=120)
            # Append to activity log (kept in Redis list, max 20 entries)
            entry = _json.dumps({
                "ts": time.strftime("%H:%M:%S"),
                "type": decision.type,
                "text": summary[:300],
            })
            self.wb.r.lpush("agent.activity_log", entry)
            self.wb.r.ltrim("agent.activity_log", 0, 19)

    def run_once(self) -> str:
        """Run a single agent cycle. Returns the raw LLM response. Useful for testing."""
        # 1. Read whiteboard with trends
        state = self.wb.read_all_with_trends()

        # 2. Read episode
        episode = self._get_episode()

        # 3. Read human intent
        intent = self.wb.read("human.intent")

        # 4. Detect external changes
        external_changes = self._change_detector.detect(state, episode)

        # 5. Load knowledge
        knowledge = self._load_knowledge()

        # 6. Build prompt
        prompt = build_prompt(
            state=state,
            episode=episode,
            intent=intent,
            knowledge=knowledge,
            tools=self.tools.list_for_llm(),
            current_time=time.time(),
            external_changes=external_changes,
        )

        # 6. Build messages with vision content (camera frames, human images)
        messages = build_messages(prompt, state)

        # 7. Call LLM with full messages (includes image blocks if cameras are live)
        raw_response = self.llm.call(prompt, messages=messages)

        # 8. Parse (pass printer state for interval clamping)
        printer_state = state.get("printer.state")
        decision = parse_llm_output(raw_response, printer_state=printer_state)

        # 9. Route
        self._route_decision(decision)

        return raw_response

    def run(self):
        """Main loop. Blocks until stop() is called."""
        self._running = True

        # Start heartbeat thread
        hb_thread = threading.Thread(target=self._heartbeat, daemon=True, name="agent-heartbeat")
        hb_thread.start()
        logger.info("Agent loop started")

        try:
            while self._running:
                try:
                    self.run_once()
                except Exception as e:
                    logger.error(f"Agent cycle error: {e}")
                time.sleep(self.poll_interval)
        finally:
            self._running = False
            logger.info("Agent loop stopped")

    def stop(self):
        """Signal the loop to stop."""
        self._running = False
