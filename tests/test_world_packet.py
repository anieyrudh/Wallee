from wallee.models import DeviceSummary, LegalAction, NormalizedPackState, PackManifest, ResourceState
from wallee.packs.base import BasePack
from wallee.registry import LoadedPack


class DummyPack(BasePack):
    def publish_raw_state(self, whiteboard):
        whiteboard.batch_publish(
            {
                "dummy_1.mode": "IDLE",
                "dummy_1.health": "OK",
                "dummy_1.v0": 0,
                "dummy_1.v1": 1,
                "dummy_1.v2": 2,
                "dummy_1.v3": 3,
            }
        )

    def normalize(self, snapshot):
        return NormalizedPackState(
            summary=DeviceSummary(
                device_id="dummy_1",
                display_name="Dummy",
                category="test",
                mode="IDLE",
                health="OK",
                summary="dummy",
            ),
            facts={f"dummy_1.v{i}": snapshot[f"dummy_1.v{i}"] for i in range(4)},
            resources=[ResourceState(id="dummy_1.slot", kind="slot", owner="dummy_1", state="FREE")],
            blockers=[],
        )

    def candidate_actions(self, world):
        return [
            LegalAction(
                action_id=f"A{i}",
                verb="WAIT",
                description=f"Action {i}",
                owner_pack="builtin",
                execute_ref="builtin.wait",
                args={"seconds": 0},
                rank_hint=i,
            )
            for i in range(10)
        ]

    def _realize(self, action, whiteboard):
        return {}


def test_world_packet_keeps_full_frontier_but_limits_planner_prompt(runtime):
    config = runtime["config"]
    config.delta_limit_per_device = 2
    config.frontier_limit = 3

    registry = runtime["registry"]
    whiteboard = runtime["whiteboard"]
    compiler = runtime["compiler"]

    manifest = PackManifest(
        pack_id="dummy",
        display_name="Dummy Pack",
        category="test",
        python_entrypoint="tests.test_world_packet:DummyPack",
    )
    dummy = DummyPack(manifest)
    registry._packs["dummy"] = LoadedPack(manifest=manifest, instance=dummy)
    dummy.publish_raw_state(whiteboard)

    world = compiler.compile("Test truncation")
    assert len([delta for delta in world.deltas if delta.device_id == "dummy_1"]) <= 2
    assert len(world.frontier) >= 10
    assert {f"A{i}" for i in range(10)}.issubset({action.action_id for action in world.frontier})
    prompt_view = world.prompt_view()
    assert len(prompt_view["frontier"]) == 3
    assert prompt_view["decision_signals"]["allowed_frontier_ids"] == ["A0", "A1", "A2"]
