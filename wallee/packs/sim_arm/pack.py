"""Simulated arm pack."""

from __future__ import annotations

from typing import Any

from ...models import DeviceSummary, HazardClass, LegalAction, NormalizedPackState, ResourceState, WorldPacket
from ...predicates import all_of, atom
from ...whiteboard import BaseWhiteboard
from ..base import BasePack


class Pack(BasePack):
    """Simple arm pack that can unload a cooled part into a tray."""

    DEVICE_ID = "arm_1"
    TRAY_ID = "tray_A"

    def __init__(self, manifest) -> None:
        super().__init__(manifest)
        self.state = {
            f"{self.DEVICE_ID}.mode": "IDLE",
            f"{self.DEVICE_ID}.health": "OK",
            f"{self.DEVICE_ID}.holding": False,
            f"{self.TRAY_ID}.state": "EMPTY",
        }

    def publish_raw_state(self, whiteboard: BaseWhiteboard, *, mode: str = "full") -> None:
        whiteboard.batch_publish(dict(self.state))

    def normalize(self, snapshot: dict[str, Any]) -> NormalizedPackState:
        mode = str(snapshot.get(f"{self.DEVICE_ID}.mode", "UNKNOWN"))
        health = str(snapshot.get(f"{self.DEVICE_ID}.health", "UNKNOWN"))
        holding = bool(snapshot.get(f"{self.DEVICE_ID}.holding", False))
        tray_state = str(snapshot.get(f"{self.TRAY_ID}.state", self.state[f"{self.TRAY_ID}.state"]))
        available = mode == "IDLE" and health == "OK" and not holding

        blockers: list[str] = []
        if health != "OK":
            blockers.append("Arm health is not OK")

        summary = DeviceSummary(
            device_id=self.DEVICE_ID,
            display_name=self.manifest.display_name,
            category=self.manifest.category,
            mode=mode,
            health=health,
            summary=f"mode={mode} | holding={holding} | tray={tray_state}",
        )
        resources = [
            ResourceState(
                id=f"{self.DEVICE_ID}.gripper",
                kind="gripper",
                owner=self.DEVICE_ID,
                state="HOLDING" if holding else "OPEN",
                attributes={"available": available},
            ),
            ResourceState(
                id=self.TRAY_ID,
                kind="tray",
                owner=self.DEVICE_ID,
                state=tray_state,
                attributes={},
            ),
        ]
        facts = {
            f"{self.DEVICE_ID}.mode": mode,
            f"{self.DEVICE_ID}.health": health,
            f"{self.DEVICE_ID}.holding": holding,
            f"{self.DEVICE_ID}.available": available,
            f"{self.TRAY_ID}.state": tray_state,
        }
        return NormalizedPackState(summary=summary, facts=facts, resources=resources, blockers=blockers)

    def candidate_actions(self, world: WorldPacket) -> list[LegalAction]:
        available = bool(world.facts.get(f"{self.DEVICE_ID}.available", False))
        tray_empty = world.facts.get(f"{self.TRAY_ID}.state") == "EMPTY"
        part_present = bool(world.facts.get("printer_1.part_present", False))
        safe_to_unload = bool(world.facts.get("printer_1.safe_to_unload", False))

        actions: list[LegalAction] = []
        if available and tray_empty and part_present and safe_to_unload:
            actions.append(
                LegalAction(
                    action_id="A_ARM_UNLOAD",
                    verb="UNLOAD_PART",
                    description="Unload the cooled part from printer_1 into tray_A",
                    owner_pack=self.pack_id,
                    execute_ref="unload_part",
                    args={"source": "printer_1.bed", "destination": self.TRAY_ID},
                    required_locks=[f"{self.DEVICE_ID}.motion", "printer_1.bed", self.TRAY_ID],
                    hazard_class=HazardClass.LOW,
                    approval_required=False,
                    preconditions=all_of(
                        atom(f"{self.DEVICE_ID}.available", "==", True),
                        atom("printer_1.part_present", "==", True),
                        atom("printer_1.safe_to_unload", "==", True),
                        atom(f"{self.TRAY_ID}.state", "==", "EMPTY"),
                    ),
                    expected_delta=[
                        atom("printer_1.part_present", "==", False),
                        atom(f"{self.TRAY_ID}.state", "==", "OCCUPIED"),
                        atom(f"{self.DEVICE_ID}.holding", "==", False),
                    ],
                    target_device=self.DEVICE_ID,
                    rank_hint=5,
                )
            )

        if not actions and world.blockers:
            actions.append(
                LegalAction(
                    action_id="A_ARM_CALL_HUMAN",
                    verb="CALL_HUMAN",
                    description="Ask a human to inspect the handling cell",
                    owner_pack="builtin",
                    execute_ref="builtin.call_human",
                    args={"message": "Handling cell cannot progress automatically"},
                    required_locks=[],
                    rank_hint=200,
                )
            )

        return actions

    def _realize(self, action: LegalAction, whiteboard: BaseWhiteboard) -> dict[str, Any]:
        if action.verb != "UNLOAD_PART":
            raise ValueError(f"unsupported simulated arm verb {action.verb}")

        if self.state[f"{self.TRAY_ID}.state"] != "EMPTY":
            raise RuntimeError("destination tray is not empty")

        self.state[f"{self.DEVICE_ID}.mode"] = "BUSY"
        self.state[f"{self.DEVICE_ID}.holding"] = True
        whiteboard.batch_publish(
            {
                f"{self.DEVICE_ID}.mode": "BUSY",
                f"{self.DEVICE_ID}.holding": True,
            }
        )

        # The simulated action updates the shared world directly because the arm
        # is the pack that physically moves the part.  This is exactly the kind
        # of pack-local side effect the generic core should not know how to do.
        whiteboard.batch_publish(
            {
                "printer_1.part_present": False,
                f"{self.TRAY_ID}.state": "OCCUPIED",
            }
        )
        self.state[f"{self.TRAY_ID}.state"] = "OCCUPIED"

        self.state[f"{self.DEVICE_ID}.holding"] = False
        self.state[f"{self.DEVICE_ID}.mode"] = "IDLE"
        whiteboard.batch_publish(
            {
                f"{self.DEVICE_ID}.holding": False,
                f"{self.DEVICE_ID}.mode": "IDLE",
            }
        )
        return {
            "moved_from": action.args.get("source"),
            "moved_to": action.args.get("destination"),
        }
