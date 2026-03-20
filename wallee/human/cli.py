"""Basic CLI for human interaction: send intent, approve actions, ESTOP."""

import json
import logging
import os
import threading

from wallee.config import (
    DEFAULT_HUMAN_ESTOP_TTL_S,
    DEFAULT_HUMAN_INTENT_TTL_S,
    DEFAULT_HUMAN_URGENT_TTL_S,
)
from wallee.safety.estop import estop_printer
from wallee.whiteboard.client import Whiteboard
from wallee.ledger.db import Ledger

logger = logging.getLogger(__name__)


class CLI:
    """Interactive CLI for Wallee. Runs in a background thread."""

    def __init__(
        self,
        whiteboard: Whiteboard,
        ledger: Ledger,
        intent_ttl: int = DEFAULT_HUMAN_INTENT_TTL_S,
        urgent_ttl: int = DEFAULT_HUMAN_URGENT_TTL_S,
        estop_ttl: int = DEFAULT_HUMAN_ESTOP_TTL_S,
        wake_agent_fn=None,
    ):
        self.wb = whiteboard
        self.ledger = ledger
        self.intent_ttl = intent_ttl
        self.urgent_ttl = urgent_ttl
        self.estop_ttl = estop_ttl
        self._wake_agent = wake_agent_fn
        self._running = False

    def _print_help(self):
        print("""
Wallee CLI Commands:
  intent <message>     — Set human intent (tells agent what you want)
  urgent               — Set urgent flag (agent prioritizes next cycle)
  approve <action_id>  — Approve a pending action
  reject <action_id>   — Reject a pending action
  status               — Show whiteboard state
  pending              — Show actions waiting for approval
  history              — Show recent actions
  estop                — Emergency stop (future: GPIO relay)
  help                 — Show this help
  quit                 — Exit CLI
""")

    def _acknowledge_pending_callout(self):
        """Clear pending callout from whiteboard when human responds."""
        pending = self.wb.read("human.pending_callout")
        if pending:
            self.wb.r.delete("human.pending_callout")
            logger.info("Pending callout cleared after human response")

    def _handle_intent(self, args: str):
        if not args:
            print("Usage: intent <message>")
            return
        self.wb.publish("human.intent", args, ttl=self.intent_ttl)
        self._acknowledge_pending_callout()
        if self._wake_agent:
            self._wake_agent()
        print(f"Intent set: {args}")

    def _handle_urgent(self):
        self.wb.publish("human.urgent", True, ttl=self.urgent_ttl)
        if self._wake_agent:
            self._wake_agent()
        print(f"Urgent flag set (expires in {self.urgent_ttl}s)")

    def _handle_approve(self, args: str):
        if not args:
            print("Usage: approve <action_id>")
            return
        action_id = args.strip()
        action = self.ledger.get_action(action_id)
        if not action:
            print(f"Action {action_id} not found")
            return
        if action["status"] != "WAITING_APPROVAL":
            print(f"Action is {action['status']}, not WAITING_APPROVAL")
            return
        self.ledger.record_approval(action_id, "APPROVE", "cli_operator")
        self._acknowledge_pending_callout()
        print(f"Approved: {action_id}")

    def _handle_reject(self, args: str):
        if not args:
            print("Usage: reject <action_id>")
            return
        action_id = args.strip()
        action = self.ledger.get_action(action_id)
        if not action:
            print(f"Action {action_id} not found")
            return
        self.ledger.record_approval(action_id, "REJECT", "cli_operator")
        self._acknowledge_pending_callout()
        print(f"Rejected: {action_id}")

    def _handle_status(self):
        state = self.wb.read_all()
        if not state:
            print("Whiteboard is empty")
            return
        print("--- Whiteboard ---")
        for key in sorted(state.keys()):
            val = state[key]
            if isinstance(val, (dict, list)):
                print(f"  {key}: {json.dumps(val, default=str)[:120]}")
            else:
                print(f"  {key}: {val}")

    def _handle_pending(self):
        waiting = self.ledger.get_by_status("WAITING_APPROVAL")
        if not waiting:
            print("No actions waiting for approval")
            return
        print("--- Pending Approval ---")
        for action in waiting:
            print(f"  [{action['action_id'][:8]}...] {action['tool']} — {action['reason']}")
            print(f"    Params: {action['params_json']}")

    def _handle_history(self):
        episode = self.ledger.current_episode()
        if not episode:
            print("No actions in current episode")
            return
        print("--- Current Episode ---")
        for action in episode:
            line = f"  [{action['status']}] {action['tool']} — {action.get('reason', '')}"
            print(line)
            if action.get("error_json"):
                print(f"    ERROR: {action['error_json']}")
            if action.get("result_json") and action["status"] == "DONE":
                print(f"    RESULT: {action['result_json']}")

    def _handle_estop(self):
        self.wb.publish("safety.estop", True, ttl=self.estop_ttl)
        estop_printer(os.environ.get("PRUSALINK_HOST", ""), os.environ.get("PRUSALINK_API_KEY", ""))
        if self._wake_agent:
            self._wake_agent()
        print("ESTOP ACTIVATED — printer paused. Manual intervention required.")

    def process_command(self, line: str) -> bool:
        """Process a single command. Returns False to quit."""
        line = line.strip()
        if not line:
            return True

        parts = line.split(None, 1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if cmd == "quit" or cmd == "exit":
            return False
        elif cmd == "help":
            self._print_help()
        elif cmd == "intent":
            self._handle_intent(args)
        elif cmd == "urgent":
            self._handle_urgent()
        elif cmd == "approve":
            self._handle_approve(args)
        elif cmd == "reject":
            self._handle_reject(args)
        elif cmd == "status":
            self._handle_status()
        elif cmd == "pending":
            self._handle_pending()
        elif cmd == "history":
            self._handle_history()
        elif cmd == "estop":
            self._handle_estop()
        else:
            print(f"Unknown command: {cmd}. Type 'help' for options.")
        return True

    def run(self):
        """Interactive REPL. Blocks until quit."""
        self._running = True
        self._print_help()
        while self._running:
            try:
                line = input("wallee> ")
                if not self.process_command(line):
                    break
            except (EOFError, KeyboardInterrupt):
                break
        self._running = False

    def stop(self):
        self._running = False
