"""Human gateway and durable outbox support."""

from __future__ import annotations

import json
import time
from typing import Any

from .config import Config
from .ids import new_id
from .models import ApprovalRecord, HazardClass
from .runtime_db import RuntimeDB


class HumanGateway:
    """Minimal operator interface abstraction.

    The reference implementation keeps the interface intentionally small:
    approvals, escalation messages, and a pending-outbox view.  Real transport
    integrations such as Telegram can sit behind this abstraction later.
    """

    def __init__(self, *, config: Config, runtime_db: RuntimeDB) -> None:
        self.config = config
        self.runtime_db = runtime_db
        self.outbox_dir = config.outbox_dir
        self.outbox_dir.mkdir(parents=True, exist_ok=True)

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def submit_goal(self, goal: str) -> None:
        """Persist a user goal as an event."""
        self.runtime_db.record_event("human_gateway", "INFO", "goal_submitted", context={"goal": goal})

    def call_human(
        self,
        *,
        title: str,
        body: str,
        severity: str = "warn",
        require_ack: bool = False,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Append a durable human notification request.

        The durable outbox is intentionally dumb.  If delivery channels fail, an
        operator can still inspect the queued JSON files and reconstruct what the
        system wanted a human to see.
        """
        request_id = new_id("human")
        payload = {
            "request_id": request_id,
            "severity": severity,
            "require_ack": require_ack,
            "title": title,
            "body": body,
            "context": context or {},
            "created_ts_ms": self.now_ms(),
        }
        path = self.outbox_dir / f"{request_id}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        self.runtime_db.record_event("human_gateway", "WARN", title, context=payload)
        return request_id

    def request_approval(
        self,
        *,
        action_run_id: str,
        args_hash: str,
        hazard_class: HazardClass,
        reason: str,
    ) -> bool:
        """Request approval and return ``True`` if already approved.

        For low-hazard simulation work, auto-approval can be enabled by config so
        the developer loop stays fast.  Medium and high hazard actions always
        require an explicit approval path.
        """
        if hazard_class == HazardClass.LOW and self.config.auto_approve_low_hazard:
            self.approve(action_run_id=action_run_id, args_hash=args_hash, approved_by="auto.low")
            return True

        if self.runtime_db.has_valid_approval(action_run_id, args_hash):
            return True

        self.call_human(
            title="Approval required",
            body=reason,
            severity="warn",
            require_ack=True,
            context={
                "action_run_id": action_run_id,
                "args_hash": args_hash,
                "hazard_class": hazard_class.value,
            },
        )
        return False

    def approve(
        self,
        *,
        action_run_id: str,
        args_hash: str,
        approved_by: str,
        ttl_seconds: int | None = None,
    ) -> ApprovalRecord:
        """Record a bound approval."""
        ttl = ttl_seconds or self.config.approval_ttl_seconds
        approval = ApprovalRecord(
            approval_id=new_id("approval"),
            action_run_id=action_run_id,
            args_hash=args_hash,
            approved_by=approved_by,
            decision="APPROVE",
            created_ts_ms=self.now_ms(),
            expires_ts_ms=self.now_ms() + ttl * 1000,
        )
        self.runtime_db.record_approval(approval)
        return approval

    def reject(
        self,
        *,
        action_run_id: str,
        args_hash: str,
        approved_by: str,
        ttl_seconds: int | None = None,
    ) -> ApprovalRecord:
        """Record a bound rejection."""
        ttl = ttl_seconds or self.config.approval_ttl_seconds
        approval = ApprovalRecord(
            approval_id=new_id("approval"),
            action_run_id=action_run_id,
            args_hash=args_hash,
            approved_by=approved_by,
            decision="REJECT",
            created_ts_ms=self.now_ms(),
            expires_ts_ms=self.now_ms() + ttl * 1000,
        )
        self.runtime_db.record_approval(approval)
        return approval

    def pending_messages(self) -> list[str]:
        """Return compact descriptions of queued operator messages."""
        messages: list[str] = []
        for path in sorted(self.outbox_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            messages.append(f"{payload.get('severity', 'info').upper()}: {payload.get('title', path.name)}")
        return messages
