from wallee.models import LegalAction, PlanIR
from wallee.predicates import atom


def test_verification_mismatch_requests_replan(runtime):
    compiler = runtime["compiler"]
    engine = runtime["engine"]

    world = compiler.compile("Synthetic mismatch test")
    action = LegalAction(
        action_id="A_SYNTH",
        verb="WAIT",
        description="Synthetic action that will verify against a false fact",
        owner_pack="builtin",
        execute_ref="builtin.wait",
        args={"seconds": 0},
        verify=atom("nonexistent.fact", "==", True),
    )
    world.frontier = [action]
    plan = PlanIR(decision="EXECUTE", sequence=["A_SYNTH"], why="test mismatch")

    report = engine.execute_plan("Synthetic mismatch test", world, plan)
    assert report.replan_required is True


def test_safety_kernel_trips_on_stale_heartbeat(runtime):
    safety = runtime["safety"]
    safety._last_heartbeat_mono -= (safety.heartbeat_timeout_s + 0.1)
    assert safety.poll() is True
    assert safety.interlock.engaged is True
