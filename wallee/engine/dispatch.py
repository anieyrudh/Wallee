"""Engine — polls ledger for proposals and runs them through safety gates."""

import logging
import threading
import time

from wallee.ledger.db import Ledger
from wallee.ledger.diary import Diary
from wallee.tools.registry import ToolRegistry
from wallee.whiteboard.client import Whiteboard

logger = logging.getLogger(__name__)


class Engine:
    def __init__(
        self,
        whiteboard: Whiteboard,
        ledger: Ledger,
        tools: ToolRegistry,
        data_dir: str,
        poll_interval: float = 0.5,
        heartbeat_interval: float = 1.0,
        heartbeat_ttl: int = 3,
        approval_timeout: float = 120.0,
        approval_notifier=None,
    ):
        self.wb = whiteboard
        self.ledger = ledger
        self.tools = tools
        self.data_dir = data_dir
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_ttl = heartbeat_ttl
        self.approval_timeout = approval_timeout
        self.approval_notifier = approval_notifier
        self._running = False
        self._diaries: dict[str, Diary] = {}

    def _get_chain_predecessors(self, chain_id: str, chain_seq: int) -> list[dict]:
        """Get all actions in the same chain with lower sequence numbers."""
        with self.ledger._lock:
            rows = self.ledger.conn.execute(
                """SELECT * FROM actions WHERE chain_id = ? AND chain_seq < ?
                   ORDER BY chain_seq ASC""",
                (chain_id, chain_seq),
            ).fetchall()
            return [dict(r) for r in rows]

    def _get_diary(self, device_group: str) -> Diary:
        """Get or create diary for a device group."""
        if device_group not in self._diaries:
            self._diaries[device_group] = Diary(device_group, self.data_dir)
        return self._diaries[device_group]

    def _heartbeat(self):
        """Background thread: publish engine heartbeat."""
        while self._running:
            try:
                self.wb.publish("engine.heartbeat", time.monotonic(), ttl=self.heartbeat_ttl)
            except Exception as e:
                logger.error(f"Engine heartbeat failed: {e}")
            time.sleep(self.heartbeat_interval)

    def _notify_approval_request(self, action_id: str, tool_name: str, params: dict,
                                    reason: str = "", observation: str = ""):
        if self.approval_notifier is None:
            return
        try:
            self.approval_notifier(action_id, tool_name, params, reason=reason, observation=observation)
        except Exception as e:
            logger.error(f"Approval notification failed for {action_id}: {e}")

    def process_proposal(self, proposal: dict) -> str:
        """Run a proposal through all gates. Returns final status string."""
        action_id = proposal["action_id"]
        tool_name = proposal["tool"]
        tool = self.tools.get(tool_name)

        if tool is None:
            logger.warning(f"REJECTED {tool_name}: unknown tool")
            self.ledger.reject(action_id, f"unknown tool: {tool_name}")
            return "REJECTED"

        params = __import__("json").loads(proposal["params_json"])

        # Chain predecessor check — if this action is part of a chain,
        # ensure all predecessors completed successfully
        chain_id = proposal.get("chain_id")
        chain_seq = proposal.get("chain_seq")
        if chain_id is not None and chain_seq is not None and chain_seq > 0:
            predecessors = self._get_chain_predecessors(chain_id, chain_seq)
            for pred in predecessors:
                if pred["status"] in ("FAILED", "REJECTED"):
                    self.ledger.reject(
                        action_id,
                        f"chain_skipped: predecessor {pred['action_id'][:8]} was {pred['status']}",
                    )
                    return "REJECTED"
                if pred["status"] in ("PROPOSED", "WAITING_APPROVAL", "DISPATCHED", "UNKNOWN"):
                    # Predecessor still pending — skip, retry later
                    return "SKIPPED"

        if self.wb.read("safety.estop"):
            logger.critical(f"REJECTED {tool_name}: safety.estop active")
            self.ledger.reject(action_id, "blocked by safety.estop")
            return "REJECTED"

        # Gate 1: Queue guard — no double-dispatch per device group
        device_group = tool.device_group
        if self.ledger.has_inflight(device_group):
            # Don't reject, just skip — retry next poll
            logger.debug(f"Queue guard: {device_group} has inflight, skipping {action_id}")
            return "SKIPPED"

        # Gate 2: Deadline — reject stale proposals
        age_ms = (time.monotonic() - proposal["created_mono"]) * 1000
        max_age = proposal["max_proposal_age_ms"]
        if age_ms > max_age:
            self.ledger.reject(action_id, f"expired ({age_ms:.0f}ms > {max_age}ms)")
            return "REJECTED"

        # Gate 3: Approval — if required, check for approval record
        if proposal["requires_approval"]:
            approval = self.ledger.get_approval(action_id)
            if approval is None:
                if proposal.get("status") != "WAITING_APPROVAL":
                    self.ledger.set_status(action_id, "WAITING_APPROVAL")
                    self._notify_approval_request(
                        action_id, tool_name, params,
                        reason=proposal.get("reason", ""),
                        observation=proposal.get("observation", ""),
                    )
                logger.info(f"Waiting for approval: {action_id} ({tool_name})")
                return "WAITING_APPROVAL"
            if approval["decision"] != "APPROVE":
                self.ledger.reject(action_id, "not approved by operator")
                return "REJECTED"

        # Gate 4: TOCTOU — run the tool's side-effect-free precheck if defined
        if tool.has_precheck:
            try:
                result = tool.precheck(whiteboard=self.wb, **params)
                if isinstance(result, dict) and "error" in result:
                    logger.warning(f"REJECTED {tool_name}: TOCTOU — {result['error']}")
                    self.ledger.reject(action_id, f"TOCTOU: {result['error']}")
                    return "REJECTED"
            except Exception as e:
                logger.warning(f"REJECTED {tool_name}: TOCTOU exception — {e}")
                self.ledger.reject(action_id, f"TOCTOU exception: {e}")
                return "REJECTED"

        # Gate 5: Dispatch — diary write BEFORE execution
        diary = self._get_diary(device_group)
        idempotency_key = proposal["idempotency_key"]
        diary.write_inflight(idempotency_key, action_id, tool_name)
        self.ledger.set_status(action_id, "DISPATCHED")

        try:
            result = tool.execute(whiteboard=self.wb, **params)
            if isinstance(result, dict) and "error" in result:
                diary.write_failed(idempotency_key, result)
                self.ledger.set_status(action_id, "FAILED", error=result)
                return "FAILED"
            else:
                diary.write_success(idempotency_key, result)
                self.ledger.set_status(action_id, "DONE", result=result)
                return "DONE"
        except Exception as e:
            error = {"error": str(e)}
            diary.write_failed(idempotency_key, error)
            self.ledger.set_status(action_id, "FAILED", error=error)
            return "FAILED"

    def poll_once(self):
        """Single poll cycle: process all pending proposals."""
        proposals = self.ledger.get_proposals()
        for proposal in proposals:
            self.process_proposal(proposal)

        # Also check WAITING_APPROVAL actions that may have been approved or expired
        waiting = self.ledger.get_by_status("WAITING_APPROVAL")
        for action in waiting:
            # Expire stale approval requests
            age_ms = (time.monotonic() - action["created_mono"]) * 1000
            if age_ms > self.approval_timeout * 1000:
                logger.warning(f"EXPIRED approval wait: {action['tool']} ({action['action_id'][:8]})")
                self.ledger.reject(action["action_id"], f"approval timeout ({self.approval_timeout}s)")
                continue

            approval = self.ledger.get_approval(action["action_id"])
            if approval is not None:
                if approval["decision"] == "APPROVE":
                    # Re-process from gate 1 (TOCTOU may have changed)
                    self.process_proposal(action)
                else:
                    self.ledger.reject(action["action_id"], "not approved by operator")

    def run(self):
        """Main engine loop. Blocks until stop() is called."""
        self._running = True

        hb_thread = threading.Thread(target=self._heartbeat, daemon=True, name="engine-heartbeat")
        hb_thread.start()
        logger.info("Engine started")

        try:
            while self._running:
                try:
                    self.poll_once()
                except Exception as e:
                    logger.error(f"Engine poll error: {e}")
                time.sleep(self.poll_interval)
        finally:
            self._running = False
            for d in self._diaries.values():
                d.close()
            logger.info("Engine stopped")

    def stop(self):
        self._running = False
