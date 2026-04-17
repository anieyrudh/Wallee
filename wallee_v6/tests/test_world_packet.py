from wallee_v6.models import DeviceSummary, LegalAction, NormalizedPackState, PackManifest, ResourceState
from wallee_v6.packs.base import BasePack
from wallee_v6.registry import LoadedPack


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


def test_world_packet_truncates_deltas_and_frontier(runtime):
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
    assert len(world.frontier) == 3
