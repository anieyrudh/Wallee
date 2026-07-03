def test_hot_part_frontier_prefers_wait_over_unload(runtime):
    compiler = runtime["compiler"]
    world = compiler.compile("Unload cooled part from printer_1 into tray_A")

    action_ids = {action.action_id for action in world.frontier}
    assert "A_PRN_WAIT_COOL" in action_ids
    assert "A_ARM_UNLOAD" not in action_ids


def test_cooled_part_frontier_contains_unload(runtime):
    registry = runtime["registry"]
    whiteboard = runtime["whiteboard"]
    compiler = runtime["compiler"]

    printer = registry.get("sim_printer")
    printer.state["printer_1.part_temp_c"] = 30.0
    registry.publish_all_raw_state(whiteboard)

    world = compiler.compile("Unload cooled part from printer_1 into tray_A")
    action_ids = {action.action_id for action in world.frontier}
    assert "A_ARM_UNLOAD" in action_ids
