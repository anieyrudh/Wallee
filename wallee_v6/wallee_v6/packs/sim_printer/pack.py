"""Simulated additive pack used by tests and local development."""

from __future__ import annotations

from typing import Any

from ...models import DeviceSummary, HazardClass, LegalAction, NormalizedPackState, ResourceState, WorldPacket
from ...predicates import atom
from ...whiteboard import BaseWhiteboard
from ..base import BasePack


class Pack(BasePack):
    """Simple simulated printer.

    The pack intentionally publishes only a handful of raw values.  The world
    compiler and normalizer do the work of turning those raw numbers into
    semantic planning facts such as `printer_1.safe_to_unload`.
    """

    DEVICE_ID = "printer_1"
    AMBIENT_C = 24.0

    def __init__(self, manifest) -> None:
        super().__init__(manifest)
        self.state = {
            f"{self.DEVICE_ID}.mode": "PAUSED",
            f"{self.DEVICE_ID}.health": "OK",
            f"{self.DEVICE_ID}.part_present": True,
            f"{self.DEVICE_ID}.part_temp_c": 42.0,
        }

    def publish_raw_state(self, whiteboard: BaseWhiteboard, *, mode: str = "full") -> None:
        """Publish raw printer state and simulate passive cooling.

        The printer pack synchronizes against the whiteboard first so that
        cross-device actions, such as the arm removing a part from the bed, do
        not get overwritten by the next sensor tick.  In a real deployment the
        device itself would be the authority; the simulation needs this extra
        bookkeeping because multiple packs can change the same modeled cell.
        """
        external_part_present = whiteboard.get(f"{self.DEVICE_ID}.part_present")
        if external_part_present is not None:
            self.state[f"{self.DEVICE_ID}.part_present"] = bool(external_part_present)

        if self.state[f"{self.DEVICE_ID}.mode"] == "PAUSED" and self.state[f"{self.DEVICE_ID}.part_present"]:
            current = float(self.state[f"{self.DEVICE_ID}.part_temp_c"])
            if current > self.AMBIENT_C:
                self.state[f"{self.DEVICE_ID}.part_temp_c"] = max(self.AMBIENT_C, round(current - 2.5, 1))
        whiteboard.batch_publish(dict(self.state))

    def normalize(self, snapshot: dict[str, Any]) -> NormalizedPackState:
        mode = str(snapshot.get(f"{self.DEVICE_ID}.mode", "UNKNOWN"))
        health = str(snapshot.get(f"{self.DEVICE_ID}.health", "UNKNOWN"))
        part_present = bool(snapshot.get(f"{self.DEVICE_ID}.part_present", False))
        part_temp_c = float(snapshot.get(f"{self.DEVICE_ID}.part_temp_c", self.AMBIENT_C))
        safe_to_unload = part_present and part_temp_c <= 35.0

        blockers: list[str] = []
        if part_present and not safe_to_unload:
            blockers.append("Part is still too hot to unload safely")

        summary = DeviceSummary(
            device_id=self.DEVICE_ID,
            display_name=self.manifest.display_name,
            category=self.manifest.category,
            mode=mode,
            health=health,
            summary=f"mode={mode} | part_present={part_present} | part_temp_c={part_temp_c:.1f}C",
        )
        resources = [
            ResourceState(
                id=f"{self.DEVICE_ID}.bed",
                kind="bed",
                owner=self.DEVICE_ID,
                state="OCCUPIED" if part_present else "EMPTY",
                attributes={
                    "part_temp_c": round(part_temp_c, 1),
                    "safe_to_unload": safe_to_unload,
                },
            )
        ]
        facts = {
            f"{self.DEVICE_ID}.mode": mode,
            f"{self.DEVICE_ID}.health": health,
            f"{self.DEVICE_ID}.part_present": part_present,
            f"{self.DEVICE_ID}.part_temp_c": round(part_temp_c, 1),
            f"{self.DEVICE_ID}.safe_to_unload": safe_to_unload,
        }
        return NormalizedPackState(summary=summary, facts=facts, resources=resources, blockers=blockers)

    def candidate_actions(self, world: WorldPacket) -> list[LegalAction]:
        mode = world.facts.get(f"{self.DEVICE_ID}.mode")
        part_present = bool(world.facts.get(f"{self.DEVICE_ID}.part_present", False))
        safe_to_unload = bool(world.facts.get(f"{self.DEVICE_ID}.safe_to_unload", False))

        actions: list[LegalAction] = []
        if mode == "RUNNING":
            actions.append(
                LegalAction(
                    action_id="A_PRN_PAUSE",
                    verb="PAUSE_PROCESS",
                    description="Pause the running printer job",
                    owner_pack=self.pack_id,
                    execute_ref="pause_process",
                    args={},
                    required_locks=[f"{self.DEVICE_ID}.motion"],
                    target_device=self.DEVICE_ID,
                    rank_hint=20,
                )
            )

        if part_present and not safe_to_unload:
            actions.append(
                LegalAction(
                    action_id="A_PRN_WAIT_COOL",
                    verb="WAIT_UNTIL",
                    description="Wait until the printed part is cool enough to unload",
                    owner_pack="builtin",
                    execute_ref="builtin.wait_until",
                    args={"timeout_s": 300, "predicate": f"{self.DEVICE_ID}.safe_to_unload == true"},
                    required_locks=[],
                    preconditions=atom(f"{self.DEVICE_ID}.part_present", "==", True),
                    verify=atom(f"{self.DEVICE_ID}.safe_to_unload", "==", True),
                    target_device=self.DEVICE_ID,
                    rank_hint=10,
                )
            )

        return actions

    def _realize(self, action: LegalAction, whiteboard: BaseWhiteboard) -> dict[str, Any]:
        if action.verb == "PAUSE_PROCESS":
            self.state[f"{self.DEVICE_ID}.mode"] = "PAUSED"
            whiteboard.publish(f"{self.DEVICE_ID}.mode", "PAUSED")
            return {"mode": "PAUSED"}

        if action.verb == "START_PROCESS":
            self.state[f"{self.DEVICE_ID}.mode"] = "RUNNING"
            whiteboard.publish(f"{self.DEVICE_ID}.mode", "RUNNING")
            return {"mode": "RUNNING"}

        if action.verb == "STOP_PROCESS":
            self.state[f"{self.DEVICE_ID}.mode"] = "IDLE"
            self.state[f"{self.DEVICE_ID}.part_present"] = False
            whiteboard.batch_publish(
                {
                    f"{self.DEVICE_ID}.mode": "IDLE",
                    f"{self.DEVICE_ID}.part_present": False,
                }
            )
            return {"mode": "IDLE"}

        raise ValueError(f"unsupported simulated printer verb {action.verb}")
