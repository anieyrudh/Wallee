"""Base classes for device packs."""

from __future__ import annotations

from abc import ABC, abstractmethod
import time
from typing import Any, Callable

from ..models import (
    ExecJournalStatus,
    ExecutionResult,
    LegalAction,
    NormalizedPackState,
    PackManifest,
    WorldPacket,
)
from ..runtime_db import RuntimeDB
from ..whiteboard import BaseWhiteboard


class BasePack(ABC):
    """Base class for a hardware pack.

    A pack owns three responsibilities:

    1. publish raw state
    2. normalize raw state into planner-friendly facts and resources
    3. realize legal actions against hardware

    The pack is intentionally the only place where vendor-specific details are
    allowed to leak in.
    """

    manifest: PackManifest

    def __init__(self, manifest: PackManifest) -> None:
        self.manifest = manifest
        self.effect_started_hook: Callable[[str], None] | None = None

    @property
    def pack_id(self) -> str:
        return self.manifest.pack_id

    @abstractmethod
    def publish_raw_state(self, whiteboard: BaseWhiteboard, *, mode: str = "full") -> None:
        """Publish the current raw device state to the whiteboard."""

    @abstractmethod
    def normalize(self, snapshot: dict[str, Any]) -> NormalizedPackState:
        """Return the semantic state this pack contributes to the world packet."""

    @abstractmethod
    def candidate_actions(self, world: WorldPacket) -> list[LegalAction]:
        """Return action candidates for the current world.

        Packs may inspect the full compiled world because cross-device action
        opportunities often depend on resources exposed by other packs.
        """

    def operator_actions(self, world: WorldPacket) -> list[LegalAction]:
        """Return bounded operator-only actions for managed proofs or debugging.

        These actions are not part of the planner-visible frontier.  They exist
        so an operator can exercise the deterministic dispatch and verification
        path without broadening planner language.
        """
        return []

    def execute(
        self,
        *,
        action: LegalAction,
        action_run_id: str,
        idempotency_key: str,
        args_hash: str,
        db: RuntimeDB,
        whiteboard: BaseWhiteboard,
    ) -> ExecutionResult:
        """Execute an action with idempotency and journal guarantees.

        The method is intentionally implemented here so every pack gets the same
        "commit `IN_FLIGHT` before touching hardware" behavior.  Re-implementing
        that barrier in every pack would almost guarantee drift over time.
        """
        now_mono = time.monotonic_ns() // 1_000_000
        existing = db.get_exec_journal(idempotency_key)
        if existing:
            if existing.exec_state == ExecJournalStatus.IN_FLIGHT:
                return ExecutionResult(status="in_flight", error={"reason": "already_in_flight"})
            if existing.exec_state == ExecJournalStatus.EXPIRED:
                return ExecutionResult(status="expired", error={"reason": "expired"})
            if existing.exec_state == ExecJournalStatus.SUCCESS:
                return ExecutionResult(status="success", result=existing.result or {})
            return ExecutionResult(status="failed", error=existing.error or {"reason": "previous_failure"})

        # This commit is the critical durability barrier.  If power dies after
        # this line returns, the official record still says the hardware may
        # already have started moving.
        db.start_exec_journal(
            idempotency_key=idempotency_key,
            action_run_id=action_run_id,
            verb=action.verb,
            args_hash=args_hash,
            started_mono_ms=now_mono,
        )

        if self.effect_started_hook is not None:
            self.effect_started_hook(idempotency_key)

        try:
            result = self._realize(action, whiteboard)
        except Exception as exc:
            db.finish_exec_journal(
                idempotency_key,
                ExecJournalStatus.FAILED,
                ended_mono_ms=time.monotonic_ns() // 1_000_000,
                error={"type": exc.__class__.__name__, "message": str(exc)},
            )
            return ExecutionResult(status="failed", error={"type": exc.__class__.__name__, "message": str(exc)})

        db.finish_exec_journal(
            idempotency_key,
            ExecJournalStatus.SUCCESS,
            ended_mono_ms=time.monotonic_ns() // 1_000_000,
            result=result,
        )
        return ExecutionResult(status="success", result=result)

    @abstractmethod
    def _realize(self, action: LegalAction, whiteboard: BaseWhiteboard) -> dict[str, Any]:
        """Perform the actual hardware-side realization for *action*."""

    def close(self) -> None:
        """Release pack-owned resources.

        Most packs do not need explicit teardown, so the base implementation is
        intentionally a no-op.  Real packs with sockets or files can override
        it without forcing every simulation pack to care.
        """
        return None
