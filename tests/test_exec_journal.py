from wallee.models import PlanIR


def test_exec_journal_inflight_is_committed_before_effect(runtime):
    compiler = runtime["compiler"]
    engine = runtime["engine"]
    registry = runtime["registry"]
    whiteboard = runtime["whiteboard"]
    db = runtime["db"]

    printer = registry.get("sim_printer")
    printer.state["printer_1.part_temp_c"] = 30.0
    registry.publish_all_raw_state(whiteboard)
    arm = registry.get("sim_arm")

    observed = {}

    def on_effect_started(idempotency_key: str) -> None:
        entry = db.get_exec_journal(idempotency_key)
        observed["state"] = entry.exec_state.value if entry else None

    arm.effect_started_hook = on_effect_started

    world = compiler.compile("Unload cooled part from printer_1 into tray_A")
    plan = PlanIR(decision="EXECUTE", sequence=["A_ARM_UNLOAD"], why="execute unload")
    report = engine.execute_plan("Unload cooled part from printer_1 into tray_A", world, plan)

    assert observed["state"] == "IN_FLIGHT"
    assert report.failed_action_run_id is None
