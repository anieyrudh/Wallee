from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from wallee.packs.prusa_core_one_plus import hardware_smoke


def _status(*, job_id: int = 7, progress: float = 12.0, speed: float = 100.0, flow: float = 100.0, nozzle_target: float = 225.0, bed_target: float = 60.0):
    return {
        "lifecycle": "PRINTING",
        "health": "OK",
        "job_active": True,
        "job_id": job_id,
        "job_progress_pct": progress,
        "current_file": None,
        "speed_pct": speed,
        "flow_pct": flow,
        "nozzle_temp_c": nozzle_target,
        "nozzle_target_c": nozzle_target,
        "bed_temp_c": bed_target,
        "bed_target_c": bed_target,
    }


def _managed_result(action_id: str, action_source: str, verify_field: str, verify_value: float):
    return {
        "action_id": action_id,
        "action_source": action_source,
        "frontier_ids": ["A_PRUSA_TRIM_SPEED_DOWN_SMALL"],
        "operator_action_ids": [
            "A_PRUSA_OPERATOR_RESTORE_FLOW_DEFAULT",
            "A_PRUSA_OPERATOR_RESTORE_SPEED_DEFAULT",
            "A_PRUSA_TRIM_BED_DOWN_SMALL",
            "A_PRUSA_TRIM_BED_UP_SMALL",
            "A_PRUSA_TRIM_FLOW_DOWN_SMALL",
            "A_PRUSA_TRIM_NOZZLE_DOWN_SMALL",
            "A_PRUSA_TRIM_NOZZLE_UP_SMALL",
            "A_PRUSA_TRIM_SPEED_DOWN_SMALL",
        ],
        "report": {
            "executed_action_ids": [action_id],
            "blocked_action_run_id": None,
            "replan_required": False,
            "failed_action_run_id": None,
            "notes": [],
            "human_request_ids": [],
        },
        "post_status": {
            "lifecycle": "PRINTING",
            "job_active": True,
            "job_id": 7,
            verify_field: verify_value,
        },
    }


def test_run_managed_family_proof_accepts_clean_speed_sequence():
    spec = hardware_smoke._MANAGED_FAMILY_ORDER[0]
    statuses = iter(
        [_status(speed=100.0)]
        + [_status(speed=95.0) for _ in range(5)]
        + [_status(speed=100.0) for _ in range(5)]
    )

    def status_reader(_settings):
        return next(statuses)

    def executor(action_id, goal, _settings):
        if action_id == spec.trim_action_id:
            return _managed_result(action_id, "frontier", spec.verify_field, 95.0)
        if action_id == spec.reset_action_id:
            return _managed_result(action_id, "operator_only", spec.verify_field, 100.0)
        raise AssertionError(f"unexpected action_id {action_id}")

    report = hardware_smoke._run_managed_family_proof(
        spec,
        SimpleNamespace(),
        status_reader=status_reader,
        executor=executor,
    )

    assert report["family"] == "speed"
    assert report["baseline_value"] == 100.0
    assert report["trimmed_value"] == 95.0


def test_run_managed_family_proof_rejects_clean_attribution_mismatch():
    spec = hardware_smoke._MANAGED_FAMILY_ORDER[2]
    statuses = iter(
        [_status(flow=100.0)]
        + [_status(flow=95.0) for _ in range(5)]
        + [_status(flow=100.0) for _ in range(5)]
    )

    def status_reader(_settings):
        return next(statuses)

    def executor(action_id, goal, _settings):
        if action_id == spec.trim_action_id:
            return _managed_result(action_id, "frontier", spec.verify_field, 95.0)
        if action_id == spec.reset_action_id:
            return _managed_result(action_id, "operator_only", spec.verify_field, 100.0)
        raise AssertionError(f"unexpected action_id {action_id}")

    with pytest.raises(RuntimeError, match="attribution mismatch"):
        hardware_smoke._run_managed_family_proof(
            spec,
            SimpleNamespace(),
            status_reader=status_reader,
            executor=executor,
        )


def test_managed_execute_prefers_operator_action_when_requested(monkeypatch):
    action = SimpleNamespace(action_id="A_PRUSA_TRIM_NOZZLE_DOWN_SMALL")
    world = SimpleNamespace(frontier=[action])

    class FakePack:
        def operator_actions(self, _world):
            return [action]

    class FakeRegistry:
        def publish_all_raw_state(self, _whiteboard):
            return None

        def get(self, _pack_id):
            return FakePack()

        def close_all(self):
            return None

    class FakeEngine:
        def execute_operator_action(self, goal, world, selected, why):
            return SimpleNamespace(
                executed_action_ids=[selected.action_id],
                blocked_action_run_id=None,
                replan_required=False,
                failed_action_run_id=None,
                notes=[],
                human_request_ids=[],
            )

    class FakeHumanGateway:
        def submit_goal(self, goal):
            return None

        def pending_messages(self):
            return []

    class FakeWorldCompiler:
        def compile(self, goal, pending_messages):
            return world

    monkeypatch.setattr(
        hardware_smoke,
        "build_runtime",
        lambda config: (
            SimpleNamespace(close=lambda: None),
            SimpleNamespace(),
            FakeRegistry(),
            FakeWorldCompiler(),
            FakeHumanGateway(),
            SimpleNamespace(),
            FakeEngine(),
            SimpleNamespace(
                prompt_stack_view=lambda: {"system_prompt": "sys", "developer_prompt": "dev", "rubric_text": "rubric"},
                last_request_payload={"messages": []},
                last_response_payload={"id": "resp-1"},
                last_provider_metadata={"model": "openai/gpt-5.4", "latency_ms": 123.4},
            ),
        ),
    )
    monkeypatch.setattr(hardware_smoke.Config, "from_env", staticmethod(lambda: SimpleNamespace()))
    monkeypatch.setattr(hardware_smoke, "_command_status", lambda settings: _status(nozzle_target=220.0))

    result = hardware_smoke._managed_execute(
        "A_PRUSA_TRIM_NOZZLE_DOWN_SMALL",
        "Lower nozzle target temperature by 5C while keeping the current print running.",
        SimpleNamespace(),
        prefer_operator_only=True,
    )

    assert result["action_source"] == "operator_only"


def test_managed_proof_session_stops_on_first_failed_family(monkeypatch):
    calls: list[str] = []

    def fake_run(spec, settings):
        calls.append(spec.family)
        if spec.family == "speed":
            return {"family": "speed"}
        raise RuntimeError("nozzle verify failed")

    monkeypatch.setattr(hardware_smoke, "_run_managed_family_proof", fake_run)

    payload, exit_code = hardware_smoke._managed_proof_session(SimpleNamespace())

    assert exit_code == 1
    assert payload["completed_families"] == ["speed"]
    assert payload["failed_family"] == "nozzle_temp"
    assert calls == ["speed", "nozzle_temp"]


def test_phase1_multi_family_shadow_frontier_selects_only_requested_families():
    world = SimpleNamespace(
        facts={
            "printer_1.speed_autonomy_eligible": True,
            "printer_1.speed_autonomy_blockers": None,
            "printer_1.flow_shadow_actions": "A_PRUSA_TRIM_FLOW_DOWN_SMALL",
            "printer_1.nozzle_shadow_actions": "A_PRUSA_TRIM_NOZZLE_DOWN_SMALL|A_PRUSA_TRIM_NOZZLE_UP_SMALL",
            "printer_1.bed_shadow_actions": "A_PRUSA_TRIM_BED_DOWN_SMALL|A_PRUSA_TRIM_BED_UP_SMALL",
        }
    )
    operator_actions = [
        SimpleNamespace(action_id="A_PRUSA_TRIM_SPEED_DOWN_SMALL"),
        SimpleNamespace(action_id="A_PRUSA_TRIM_FLOW_DOWN_SMALL"),
        SimpleNamespace(action_id="A_PRUSA_TRIM_NOZZLE_UP_SMALL"),
        SimpleNamespace(action_id="A_PRUSA_TRIM_BED_UP_SMALL"),
    ]

    frontier = hardware_smoke._phase1_multi_family_shadow_frontier(
        world,
        operator_actions,
        ("speed", "flow"),
    )

    assert [action.action_id for action in frontier] == [
        "A_PRUSA_TRIM_SPEED_DOWN_SMALL",
        "A_PRUSA_TRIM_FLOW_DOWN_SMALL",
    ]


def test_phase1_multi_family_suppression_reasons_report_missing_remaining_family():
    world = SimpleNamespace(
        facts={
            "printer_1.active_printing": True,
            "printer_1.speed_autonomy_blockers": None,
            "printer_1.speed_pct": 100.0,
            "printer_1.flow_shadow_eligible": False,
            "printer_1.flow_shadow_blockers": "flow_progress_not_ready",
            "printer_1.flow_shadow_actions": None,
        },
        frontier=[],
    )

    reasons = hardware_smoke._phase1_multi_family_suppression_reasons(
        world,
        families=("speed", "flow"),
        allowed_action_ids=["A_PRUSA_TRIM_SPEED_DOWN_SMALL"],
    )

    assert "flow_unavailable" in reasons
    assert "flow:flow_shadow_not_eligible" in reasons
    assert "flow:flow_progress_not_ready" in reasons


def test_phase1_multi_family_suppression_reasons_reject_plan_outside_allowed_set():
    world = SimpleNamespace(
        facts={
            "printer_1.active_printing": True,
            "printer_1.speed_autonomy_blockers": None,
            "printer_1.speed_pct": 100.0,
            "printer_1.flow_shadow_eligible": True,
            "printer_1.flow_shadow_blockers": None,
            "printer_1.flow_shadow_actions": "A_PRUSA_TRIM_FLOW_DOWN_SMALL",
        },
        frontier=[],
    )
    plan = SimpleNamespace(decision="EXECUTE", sequence=["A_PRUSA_TRIM_NOZZLE_UP_SMALL"])

    reasons = hardware_smoke._phase1_multi_family_suppression_reasons(
        world,
        families=("speed", "flow"),
        allowed_action_ids=["A_PRUSA_TRIM_SPEED_DOWN_SMALL", "A_PRUSA_TRIM_FLOW_DOWN_SMALL"],
        plan=plan,
    )

    assert reasons == ["planner_selected_outside_allowed_set"]


def test_phase1_multi_family_session_retries_startup_before_suppressing(monkeypatch, tmp_path):
    class FakeWorld:
        def __init__(self, facts, frontier=None):
            self.facts = facts
            self.frontier = frontier or []

        def model_copy(self, update):
            return FakeWorld(self.facts, frontier=update.get("frontier", self.frontier))

        def prompt_view(self):
            return {"facts": dict(self.facts), "frontier": [action.action_id for action in self.frontier]}

    worlds = iter(
        [
            FakeWorld(
                {
                    "printer_1.nozzle_shadow_eligible": False,
                    "printer_1.nozzle_shadow_blockers": "startup_printing|live_tuning_unavailable",
                    "printer_1.nozzle_shadow_actions": None,
                    "printer_1.printing_phase": "startup_printing",
                    "printer_1.lifecycle": "PRINTING",
                    "printer_1.job_active": True,
                    "printer_1.job_id": 7,
                }
            ),
            FakeWorld(
                {
                    "printer_1.nozzle_shadow_eligible": True,
                    "printer_1.nozzle_shadow_blockers": None,
                    "printer_1.nozzle_shadow_actions": "A_PRUSA_TRIM_NOZZLE_UP_SMALL",
                    "printer_1.printing_phase": "active_printing",
                    "printer_1.lifecycle": "PRINTING",
                    "printer_1.job_active": True,
                    "printer_1.job_id": 7,
                }
            ),
            FakeWorld(
                {
                    "printer_1.nozzle_shadow_eligible": True,
                    "printer_1.nozzle_shadow_blockers": None,
                    "printer_1.nozzle_shadow_actions": "A_PRUSA_TRIM_NOZZLE_UP_SMALL",
                    "printer_1.printing_phase": "active_printing",
                    "printer_1.lifecycle": "PRINTING",
                    "printer_1.job_active": True,
                    "printer_1.job_id": 7,
                }
            ),
        ]
    )

    action = SimpleNamespace(action_id="A_PRUSA_TRIM_NOZZLE_UP_SMALL")
    compiled: list[str] = []

    class FakePack:
        def operator_actions(self, _world):
            return [action]

    class FakeRegistry:
        def publish_all_raw_state(self, _whiteboard):
            return None

        def get(self, _pack_id):
            return FakePack()

        def close_all(self):
            return None

    class FakeWorldCompiler:
        def compile(self, _goal, _pending_messages):
            world = next(worlds)
            compiled.append(str(world.facts.get("printer_1.printing_phase")))
            return world

    class FakeHumanGateway:
        def submit_goal(self, _goal):
            return None

        def pending_messages(self):
            return []

    class FakeEngine:
        def execute_operator_action(self, _goal, _world, selected, why):
            return SimpleNamespace(
                plan_id="plan-1",
                executed_action_ids=[selected.action_id],
                blocked_action_run_id=None,
                replan_required=False,
                failed_action_run_id=None,
                notes=[],
                human_request_ids=[],
            )

    monkeypatch.setattr(
        hardware_smoke,
        "build_runtime",
        lambda config: (
            SimpleNamespace(close=lambda: None),
            SimpleNamespace(),
            FakeRegistry(),
            FakeWorldCompiler(),
            FakeHumanGateway(),
            SimpleNamespace(),
            FakeEngine(),
            SimpleNamespace(
                prompt_stack_view=lambda: {"system_prompt": "sys", "developer_prompt": "dev", "rubric_text": "rubric"},
                last_request_payload={"messages": []},
                last_response_payload={"id": "resp-1"},
                last_provider_metadata={"model": "openai/gpt-5.4", "latency_ms": 123.4},
            ),
        ),
    )
    monkeypatch.setattr(hardware_smoke.Config, "from_env", staticmethod(lambda: SimpleNamespace(planner_backend="stub")))
    monkeypatch.setattr(
        hardware_smoke,
        "plan_with_retry",
        lambda planner, engine, world, goal: SimpleNamespace(
            decision="EXECUTE",
            sequence=["A_PRUSA_TRIM_NOZZLE_UP_SMALL"],
            model_dump=lambda: {"decision": "EXECUTE", "sequence": ["A_PRUSA_TRIM_NOZZLE_UP_SMALL"]},
        ),
    )
    status_values = iter(
        [
            {"lifecycle": "PRINTING", "job_active": True, "job_id": 7, "nozzle_target_c": 225.0},
            {"lifecycle": "PRINTING", "job_active": True, "job_id": 7, "nozzle_target_c": 225.0},
            {"lifecycle": "PRINTING", "job_active": True, "job_id": 7, "nozzle_target_c": 230.0},
            {"lifecycle": "PRINTING", "job_active": True, "job_id": 7, "nozzle_target_c": 230.0},
            {"lifecycle": "PRINTING", "job_active": True, "job_id": 7, "nozzle_target_c": 230.0},
            {"lifecycle": "PRINTING", "job_active": True, "job_id": 7, "nozzle_target_c": 230.0},
            {"lifecycle": "PRINTING", "job_active": True, "job_id": 7, "nozzle_target_c": 230.0},
            {"lifecycle": "PRINTING", "job_active": True, "job_id": 7, "nozzle_target_c": 230.0},
            {"lifecycle": "PRINTING", "job_active": True, "job_id": 7, "nozzle_target_c": 225.0},
            {"lifecycle": "PRINTING", "job_active": True, "job_id": 7, "nozzle_target_c": 225.0},
            {"lifecycle": "PRINTING", "job_active": True, "job_id": 7, "nozzle_target_c": 225.0},
            {"lifecycle": "PRINTING", "job_active": True, "job_id": 7, "nozzle_target_c": 225.0},
            {"lifecycle": "PRINTING", "job_active": True, "job_id": 7, "nozzle_target_c": 225.0},
            {"lifecycle": "PRINTING", "job_active": True, "job_id": 7, "nozzle_target_c": 225.0},
        ]
    )
    monkeypatch.setattr(hardware_smoke, "_command_status", lambda _settings: next(status_values))
    monkeypatch.setattr(hardware_smoke, "_collect_supported_startup_trace", lambda _settings, samples, interval_s: {"case": "phase1_startup_trace"})
    monkeypatch.setattr(
        hardware_smoke,
        "_write_phase1_artifact",
        lambda **kwargs: tmp_path / "phase1-startup-trace.json",
    )

    result = hardware_smoke._run_phase1_multi_family_session(
        SimpleNamespace(),
        goal="Trim nozzle a little while keeping the print running.",
        families=("nozzle_temp",),
        trace_samples=2,
        trace_interval_s=0.01,
        repeat_reads=5,
    )

    assert result["status"] == "executed"
    assert result["completed_families"] == ["nozzle_temp"]
    assert compiled[:2] == ["startup_printing", "active_printing"]


def test_wait_for_active_benchy_waits_for_requested_family_eligibility(monkeypatch):
    class FakeWorld:
        def __init__(self, facts):
            self.facts = facts
            self.frontier = []

        def model_copy(self, update):
            return FakeWorld(self.facts)

        def prompt_view(self):
            return {"facts": dict(self.facts), "frontier": []}

    worlds = iter(
        [
            FakeWorld(
                {
                    "printer_1.lifecycle": "PRINTING",
                    "printer_1.health": "OK",
                    "printer_1.job_active": True,
                    "printer_1.job_id": 7,
                    "printer_1.job_progress_pct": 0.0,
                    "printer_1.job_time_printing_s": 25.0,
                    "printer_1.current_file": "benchy.bgcode",
                    "printer_1.speed_pct": 100.0,
                    "printer_1.flow_pct": 100.0,
                    "printer_1.nozzle_temp_c": 210.0,
                    "printer_1.nozzle_target_c": 235.0,
                    "printer_1.bed_temp_c": 55.0,
                    "printer_1.bed_target_c": 57.0,
                    "printer_1.active_printing": True,
                    "printer_1.printing_phase": "active_printing",
                    "printer_1.nozzle_cam_usable": True,
                    "printer_1.vision_advisory_summary": "vision:blob; test",
                    "printer_1.speed_autonomy_eligible": False,
                    "printer_1.speed_autonomy_blockers": "speed_progress_not_ready",
                    "printer_1.nozzle_shadow_eligible": False,
                    "printer_1.nozzle_shadow_blockers": "nozzle_progress_not_ready",
                }
            ),
            FakeWorld(
                {
                    "printer_1.lifecycle": "PRINTING",
                    "printer_1.health": "OK",
                    "printer_1.job_active": True,
                    "printer_1.job_id": 7,
                    "printer_1.job_progress_pct": 12.0,
                    "printer_1.job_time_printing_s": 35.0,
                    "printer_1.current_file": "benchy.bgcode",
                    "printer_1.speed_pct": 100.0,
                    "printer_1.flow_pct": 100.0,
                    "printer_1.nozzle_temp_c": 220.0,
                    "printer_1.nozzle_target_c": 235.0,
                    "printer_1.bed_temp_c": 56.0,
                    "printer_1.bed_target_c": 57.0,
                    "printer_1.active_printing": True,
                    "printer_1.printing_phase": "active_printing",
                    "printer_1.nozzle_cam_usable": True,
                    "printer_1.vision_advisory_summary": "vision:blob; test",
                    "printer_1.speed_autonomy_eligible": True,
                    "printer_1.speed_autonomy_blockers": None,
                    "printer_1.nozzle_shadow_eligible": True,
                    "printer_1.nozzle_shadow_blockers": None,
                }
            ),
        ]
    )

    class FakeRegistry:
        def publish_all_raw_state(self, _whiteboard):
            return None

        def close_all(self):
            return None

    class FakeWorldCompiler:
        def compile(self, _goal, _pending_messages):
            return next(worlds)

    class FakeHumanGateway:
        def submit_goal(self, _goal):
            return None

        def pending_messages(self):
            return []

    monkeypatch.setattr(
        hardware_smoke,
        "build_runtime",
        lambda config: (
            SimpleNamespace(close=lambda: None),
            SimpleNamespace(),
            FakeRegistry(),
            FakeWorldCompiler(),
            FakeHumanGateway(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
        ),
    )
    clock = {"now": 0.0}
    monkeypatch.setattr(hardware_smoke.time, "time", lambda: clock["now"])
    monkeypatch.setattr(hardware_smoke.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))

    result = hardware_smoke._wait_for_active_benchy(
        SimpleNamespace(),
        config=SimpleNamespace(),
        benchy_file="benchy.bgcode",
        goal="Keep print running.",
        families=("speed", "nozzle_temp"),
        timeout_s=1.0,
        poll_s=1.0,
    )

    assert len(result["samples"]) == 2
    assert result["samples"][0]["warmup_progress_signs"] == []
    assert result["samples"][0]["requested_family_eligibility"] == {
        "speed": False,
        "nozzle_temp": False,
    }
    assert result["samples"][1]["warmup_progress_signs"] == [
        "job_time_printing_advancing",
        "nozzle_temp_changing",
        "bed_temp_changing",
    ]
    assert result["samples"][1]["requested_family_eligibility"] == {
        "speed": True,
        "nozzle_temp": True,
    }
    assert result["status"]["job_progress_pct"] == 12.0


def test_observe_only_runtime_inspection_records_plan_without_execution(monkeypatch):
    action = SimpleNamespace(action_id="A_PRUSA_TRIM_SPEED_DOWN_SMALL")

    class FakeWorld:
        def __init__(self):
            self.facts = {
                "printer_1.lifecycle": "PRINTING",
                "printer_1.health": "OK",
                "printer_1.job_active": True,
                "printer_1.job_progress_pct": 12.0,
                "printer_1.job_time_printing_s": 120.0,
                "printer_1.current_file": "benchy.bgcode",
                "printer_1.speed_pct": 100.0,
                "printer_1.speed_autonomy_eligible": True,
                "printer_1.speed_autonomy_blockers": None,
                "printer_1.nozzle_shadow_eligible": True,
                "printer_1.nozzle_shadow_blockers": None,
                "printer_1.nozzle_shadow_actions": "A_PRUSA_TRIM_NOZZLE_UP_SMALL",
                "printer_1.vision_advisory_summary": "vision:blob; test",
                "printer_1.vision_advisory_finding_types": "blob",
                "printer_1.nozzle_cam_usable": True,
            }
            self.frontier = [action]

        def model_copy(self, update):
            clone = FakeWorld()
            clone.facts = dict(self.facts)
            clone.frontier = update.get("frontier", self.frontier)
            return clone

        def prompt_view(self):
            return {
                "decision_signals": {
                    "vision_signal": {
                        "usable": True,
                        "summary": "vision:blob; test",
                    }
                },
                "frontier": [item.action_id for item in self.frontier],
            }

    class FakePack:
        def operator_actions(self, _world):
            return [action, SimpleNamespace(action_id="A_PRUSA_TRIM_NOZZLE_UP_SMALL")]

    class FakeRegistry:
        def publish_all_raw_state(self, _whiteboard):
            return None

        def get(self, _pack_id):
            return FakePack()

        def close_all(self):
            return None

    class FakeWorldCompiler:
        def compile(self, _goal, _pending_messages):
            return FakeWorld()

    class FakeHumanGateway:
        def submit_goal(self, _goal):
            return None

        def pending_messages(self):
            return []

    class FakeEngine:
        def execute_operator_action(self, *args, **kwargs):
            raise AssertionError("observe-only inspection must not execute actions")

    monkeypatch.setattr(
        hardware_smoke,
        "build_runtime",
        lambda config: (
            SimpleNamespace(close=lambda: None),
            SimpleNamespace(),
            FakeRegistry(),
            FakeWorldCompiler(),
            FakeHumanGateway(),
            SimpleNamespace(),
            FakeEngine(),
            SimpleNamespace(
                prompt_stack_view=lambda: {"system_prompt": "sys", "developer_prompt": "dev", "rubric_text": "rubric"},
                last_request_payload={"messages": []},
                last_response_payload={"id": "resp-1"},
                last_provider_metadata={"model": "openai/gpt-5.4", "latency_ms": 123.4},
            ),
        ),
    )
    monkeypatch.setattr(
        hardware_smoke,
        "plan_with_retry",
        lambda planner, engine, world, goal: SimpleNamespace(
            decision="EXECUTE",
            sequence=["A_PRUSA_TRIM_SPEED_DOWN_SMALL"],
            model_dump=lambda: {"decision": "EXECUTE", "sequence": ["A_PRUSA_TRIM_SPEED_DOWN_SMALL"]},
        ),
    )

    result = hardware_smoke._observe_only_runtime_inspection(
        config=SimpleNamespace(planner_backend="stub"),
        goal="Keep print running.",
        families=("speed", "nozzle_temp"),
    )

    assert result["status"] == "observe_only"
    assert result["planner_invoked"] is True
    assert result["chosen_action_id"] == "A_PRUSA_TRIM_SPEED_DOWN_SMALL"
    assert result["planner_output"] == {
        "decision": "EXECUTE",
        "sequence": ["A_PRUSA_TRIM_SPEED_DOWN_SMALL"],
    }
    assert result["planner_request_payload"] == {"messages": []}
    assert result["planner_response_payload"] == {"id": "resp-1"}
    assert result["planner_provider_metadata"]["model"] == "openai/gpt-5.4"
    assert result["prompt_stack"]["system_prompt"] == "sys"


def test_vision_runtime_benchy_validation_leaves_started_print_running_in_observe_only_mode(monkeypatch, tmp_path):
    class FakeDriver:
        def __init__(self):
            self.cancel_called = False

        def start_print(self, file_path):
            return {"started": True, "file_path": file_path}

        def cancel(self):
            self.cancel_called = True
            raise AssertionError("observe-only benchy validation must not cancel the print it started")

    driver = FakeDriver()
    status_values = iter(
        [
            {
                "lifecycle": "STOPPED",
                "health": "OK",
                "job_active": False,
                "current_file": None,
            },
            {
                "lifecycle": "PRINTING",
                "health": "OK",
                "job_active": True,
                "job_id": 7,
                "current_file": "benchy.bgcode",
            },
            {
                "lifecycle": "PRINTING",
                "health": "OK",
                "job_active": True,
                "job_id": 7,
                "current_file": "benchy.bgcode",
            },
        ]
    )

    monkeypatch.setattr(hardware_smoke.Config, "from_env", staticmethod(lambda: SimpleNamespace(planner_backend="openrouter", openrouter_api_key="key", openrouter_model="model")))
    monkeypatch.setattr(hardware_smoke, "_vision_runtime_root", lambda: tmp_path)
    monkeypatch.setattr(hardware_smoke, "_artifact_timestamp", lambda: "2026-04-14T193819")
    monkeypatch.setattr(hardware_smoke, "_build_driver", lambda settings: driver)
    monkeypatch.setattr(hardware_smoke, "_command_status", lambda settings: next(status_values))
    monkeypatch.setattr(
        hardware_smoke,
        "_wait_for_active_benchy",
        lambda settings, config, benchy_file, goal, families, timeout_s, poll_s: {
            "status": {
                "lifecycle": "PRINTING",
                "job_active": True,
                "job_id": 7,
                "current_file": benchy_file,
            },
            "samples": [],
        },
    )
    monkeypatch.setattr(
        hardware_smoke,
        "_command_nozzle_camera_health",
        lambda settings, output_root: ({"usable": True, "artifact_path": str(tmp_path / "health.json")}, 0),
    )
    monkeypatch.setattr(
        hardware_smoke,
        "_command_nozzle_vision_once",
        lambda settings, mode, output_root, planner_debug_preview: {
            "summary": "vision:blob; test",
            "observation_path": str(tmp_path / "vision.json"),
            "frame_path": str(tmp_path / "frame.jpg"),
        },
    )
    monkeypatch.setattr(
        hardware_smoke,
        "_observe_only_runtime_inspection",
        lambda config, goal, families: {
            "status": "observe_only",
            "planner_invoked": True,
            "planner_input": {
                "decision_signals": {
                    "vision_signal": {
                        "usable": True,
                        "summary": "vision:blob; test",
                    }
                }
            },
            "planner_output": {"decision": "EXECUTE", "sequence": ["A_PRUSA_TRIM_SPEED_DOWN_SMALL"]},
            "chosen_action_id": "A_PRUSA_TRIM_SPEED_DOWN_SMALL",
            "suppression_reasons": [],
            "world": {},
        },
    )
    monkeypatch.setattr(hardware_smoke, "_copy_if_present", lambda src, dst: None)
    monkeypatch.setattr(hardware_smoke, "_runtime_db_summary", lambda config: {"events": []})
    monkeypatch.setattr(hardware_smoke, "_outbox_summary", lambda config: {"messages": []})
    monkeypatch.setattr(
        hardware_smoke,
        "_write_benchy_validation_run",
        lambda run_dir, payload: run_dir / "run.json",
    )
    monkeypatch.setattr(
        hardware_smoke,
        "_write_benchy_validation_report",
        lambda run_dir, payload: run_dir / "report.md",
    )

    payload, exit_code = hardware_smoke._run_vision_runtime_benchy_validation(
        SimpleNamespace(),
        goal="Observe runtime only.",
        benchy_file="benchy.bgcode",
        wait_timeout_s=1.0,
        poll_s=0.1,
    )

    assert exit_code == 0
    assert payload["status"] == "passed"
    assert driver.cancel_called is False
    assert payload["cleanup"] == {
        "started_print": True,
        "canceled_validation_print": False,
        "observe_only": True,
        "left_validation_print_running": True,
        "cancel_skipped_reason": "observe_only_validation",
        "post_cleanup_status": {
            "lifecycle": "PRINTING",
            "health": "OK",
            "job_active": True,
            "job_id": 7,
            "current_file": "benchy.bgcode",
        },
    }
    assert "hardware_smoke cancel" not in "\n".join(payload["cli_commands"])


def test_exact_restore_action_uses_session_baseline_for_bed():
    spec = hardware_smoke._MANAGED_FAMILY_SPECS["bed_temp"]

    action = hardware_smoke._exact_restore_action(spec, baseline_value=57.0)

    assert action.action_id == "A_PRUSA_TRIM_BED_DOWN_SMALL"
    assert action.execute_ref == "trim_bed_down_small"
    assert action.args == {"target_bed_c": 57.0}
    assert action.verify == {"fact": "printer_1.bed_target_c", "op": "==", "value": 57.0}


def test_exact_restore_action_uses_session_baseline_for_nozzle():
    spec = hardware_smoke._MANAGED_FAMILY_SPECS["nozzle_temp"]

    action = hardware_smoke._exact_restore_action(spec, baseline_value=235.0)

    assert action.action_id == "A_PRUSA_TRIM_NOZZLE_DOWN_SMALL"
    assert action.execute_ref == "trim_nozzle_down_small"
    assert action.args == {"target_nozzle_c": 235.0}
    assert action.verify == {"fact": "printer_1.nozzle_target_c", "op": "==", "value": 235.0}


def test_phase1_speed_suppression_reasons_block_startup_boundary():
    world = SimpleNamespace(
        facts={
            "printer_1.active_printing": False,
            "printer_1.speed_autonomy_blockers": "startup_printing|live_tuning_unavailable",
        },
        frontier=[SimpleNamespace(action_id="A_PRUSA_PAUSE")],
    )

    reasons = hardware_smoke._phase1_speed_suppression_reasons(world)

    assert reasons == [
        "active_printing_boundary_not_met",
        "startup_printing",
        "live_tuning_unavailable",
    ]


def test_phase1_speed_suppression_reasons_reject_non_speed_sequence():
    world = SimpleNamespace(
        facts={
            "printer_1.active_printing": True,
            "printer_1.speed_autonomy_blockers": None,
        },
        frontier=[SimpleNamespace(action_id="A_PRUSA_TRIM_SPEED_DOWN_SMALL")],
    )
    plan = SimpleNamespace(decision="EXECUTE", sequence=["A_PRUSA_PAUSE"])

    reasons = hardware_smoke._phase1_speed_suppression_reasons(world, plan=plan)

    assert reasons == ["planner_selected_non_speed_sequence"]


def test_phase1_nozzle_suppression_reasons_block_shadow_ineligible():
    world = SimpleNamespace(
        facts={
            "printer_1.nozzle_shadow_eligible": False,
            "printer_1.nozzle_shadow_blockers": "startup_printing|live_tuning_unavailable",
        },
        frontier=[],
    )

    reasons = hardware_smoke._phase1_nozzle_suppression_reasons(
        world,
        allowed_action_ids=[],
    )

    assert reasons == [
        "nozzle_shadow_not_eligible",
        "startup_printing",
        "live_tuning_unavailable",
        "nozzle_shadow_actions_unavailable",
    ]


def test_phase1_nozzle_suppression_reasons_reject_non_nozzle_sequence():
    world = SimpleNamespace(
        facts={
            "printer_1.nozzle_shadow_eligible": True,
            "printer_1.nozzle_shadow_blockers": None,
        },
        frontier=[],
    )
    plan = SimpleNamespace(decision="EXECUTE", sequence=["A_PRUSA_PAUSE"])

    reasons = hardware_smoke._phase1_nozzle_suppression_reasons(
        world,
        allowed_action_ids=["A_PRUSA_TRIM_NOZZLE_UP_SMALL", "A_PRUSA_TRIM_NOZZLE_DOWN_SMALL"],
        plan=plan,
    )

    assert reasons == ["planner_selected_non_nozzle_sequence"]


def test_phase1_nozzle_shadow_frontier_filters_operator_actions():
    world = SimpleNamespace(
        facts={
            "printer_1.nozzle_shadow_actions": "A_PRUSA_TRIM_NOZZLE_DOWN_SMALL|A_PRUSA_TRIM_NOZZLE_UP_SMALL",
        }
    )
    operator_actions = [
        SimpleNamespace(action_id="A_PRUSA_TRIM_FLOW_DOWN_SMALL"),
        SimpleNamespace(action_id="A_PRUSA_TRIM_NOZZLE_UP_SMALL"),
        SimpleNamespace(action_id="A_PRUSA_TRIM_NOZZLE_DOWN_SMALL"),
    ]

    filtered = hardware_smoke._phase1_nozzle_shadow_frontier(world, operator_actions)

    assert [action.action_id for action in filtered] == [
        "A_PRUSA_TRIM_NOZZLE_UP_SMALL",
        "A_PRUSA_TRIM_NOZZLE_DOWN_SMALL",
    ]


def test_phase1_flow_suppression_reasons_block_shadow_ineligible():
    world = SimpleNamespace(
        facts={
            "printer_1.flow_shadow_eligible": False,
            "printer_1.flow_shadow_blockers": "startup_printing|flow_progress_not_ready",
        },
        frontier=[],
    )

    reasons = hardware_smoke._phase1_flow_suppression_reasons(
        world,
        allowed_action_ids=[],
    )

    assert reasons == [
        "flow_shadow_not_eligible",
        "startup_printing",
        "flow_progress_not_ready",
        "flow_shadow_actions_unavailable",
    ]


def test_phase1_flow_suppression_reasons_reject_non_flow_sequence():
    world = SimpleNamespace(
        facts={
            "printer_1.flow_shadow_eligible": True,
            "printer_1.flow_shadow_blockers": None,
        },
        frontier=[],
    )
    plan = SimpleNamespace(decision="EXECUTE", sequence=["A_PRUSA_PAUSE"])

    reasons = hardware_smoke._phase1_flow_suppression_reasons(
        world,
        allowed_action_ids=["A_PRUSA_TRIM_FLOW_DOWN_SMALL"],
        plan=plan,
    )

    assert reasons == ["planner_selected_non_flow_sequence"]


def test_phase1_flow_shadow_frontier_filters_operator_actions():
    world = SimpleNamespace(
        facts={
            "printer_1.flow_shadow_actions": "A_PRUSA_TRIM_FLOW_DOWN_SMALL",
        }
    )
    operator_actions = [
        SimpleNamespace(action_id="A_PRUSA_TRIM_FLOW_DOWN_SMALL"),
        SimpleNamespace(action_id="A_PRUSA_TRIM_NOZZLE_UP_SMALL"),
    ]

    filtered = hardware_smoke._phase1_flow_shadow_frontier(world, operator_actions)

    assert [action.action_id for action in filtered] == ["A_PRUSA_TRIM_FLOW_DOWN_SMALL"]


def test_phase1_bed_suppression_reasons_block_shadow_ineligible():
    world = SimpleNamespace(
        facts={
            "printer_1.bed_shadow_eligible": False,
            "printer_1.bed_shadow_blockers": "startup_printing|bed_progress_not_ready",
        },
        frontier=[],
    )

    reasons = hardware_smoke._phase1_bed_suppression_reasons(
        world,
        allowed_action_ids=[],
    )

    assert reasons == [
        "bed_shadow_not_eligible",
        "startup_printing",
        "bed_progress_not_ready",
        "bed_shadow_actions_unavailable",
    ]


def test_phase1_bed_suppression_reasons_reject_non_bed_sequence():
    world = SimpleNamespace(
        facts={
            "printer_1.bed_shadow_eligible": True,
            "printer_1.bed_shadow_blockers": None,
        },
        frontier=[],
    )
    plan = SimpleNamespace(decision="EXECUTE", sequence=["A_PRUSA_PAUSE"])

    reasons = hardware_smoke._phase1_bed_suppression_reasons(
        world,
        allowed_action_ids=["A_PRUSA_TRIM_BED_UP_SMALL"],
        plan=plan,
    )

    assert reasons == ["planner_selected_non_bed_sequence"]


def test_phase1_bed_shadow_frontier_filters_operator_actions():
    world = SimpleNamespace(
        facts={
            "printer_1.bed_shadow_actions": "A_PRUSA_TRIM_BED_DOWN_SMALL|A_PRUSA_TRIM_BED_UP_SMALL",
        }
    )
    operator_actions = [
        SimpleNamespace(action_id="A_PRUSA_TRIM_BED_DOWN_SMALL"),
        SimpleNamespace(action_id="A_PRUSA_TRIM_BED_UP_SMALL"),
        SimpleNamespace(action_id="A_PRUSA_TRIM_FLOW_DOWN_SMALL"),
    ]

    filtered = hardware_smoke._phase1_bed_shadow_frontier(world, operator_actions)

    assert [action.action_id for action in filtered] == [
        "A_PRUSA_TRIM_BED_DOWN_SMALL",
        "A_PRUSA_TRIM_BED_UP_SMALL",
    ]


def test_phase1_nozzle_reset_spec_uses_down_after_up_trim():
    action_id, goal = hardware_smoke._phase1_nozzle_reset_spec(
        baseline_value=220.0,
        trimmed_value=225.0,
    )

    assert action_id == "A_PRUSA_TRIM_NOZZLE_DOWN_SMALL"
    assert "Lower nozzle target temperature" in goal


def test_phase1_nozzle_reset_spec_uses_up_after_down_trim():
    action_id, goal = hardware_smoke._phase1_nozzle_reset_spec(
        baseline_value=225.0,
        trimmed_value=220.0,
    )

    assert action_id == "A_PRUSA_TRIM_NOZZLE_UP_SMALL"
    assert "Raise nozzle target temperature" in goal


def test_phase1_flow_reset_spec_restores_default_after_down_trim():
    action_id, goal = hardware_smoke._phase1_flow_reset_spec(
        baseline_value=100.0,
        trimmed_value=95.0,
    )

    assert action_id == "A_PRUSA_OPERATOR_RESTORE_FLOW_DEFAULT"
    assert "Restore flow to the default" in goal


def test_phase1_bed_reset_spec_uses_down_after_up_trim():
    action_id, goal = hardware_smoke._phase1_bed_reset_spec(
        baseline_value=60.0,
        trimmed_value=65.0,
    )

    assert action_id == "A_PRUSA_TRIM_BED_DOWN_SMALL"
    assert "Lower bed target temperature" in goal


def test_phase1_bed_reset_spec_uses_up_after_down_trim():
    action_id, goal = hardware_smoke._phase1_bed_reset_spec(
        baseline_value=65.0,
        trimmed_value=60.0,
    )

    assert action_id == "A_PRUSA_TRIM_BED_UP_SMALL"
    assert "Raise bed target temperature" in goal


def test_command_serial_preflight_uses_writer_and_closes(monkeypatch):
    closed = {"count": 0}

    class _FakeWriter:
        def __init__(self, settings):
            self.settings = settings

        def preflight(self):
            return {"ok": True, "command": "M400", "latency_ms": 12.3}

        def close(self):
            closed["count"] += 1

    monkeypatch.setattr(hardware_smoke, "PrusaSerialWriter", _FakeWriter)
    monkeypatch.setattr(
        hardware_smoke,
        "_command_status",
        lambda _settings: {
            **_status(),
            "job_active": False,
            "job_id": None,
            "job_progress_pct": None,
            "current_file": None,
            "lifecycle": "IDLE",
        },
    )

    payload = hardware_smoke._command_serial_preflight(
        SimpleNamespace(serial_enabled=True, serial_port="/dev/ttyACM0")
    )

    assert payload["ok"] is True
    assert payload["serial_enabled"] is True
    assert payload["command"] == "M400"
    assert closed["count"] == 1


def test_command_status_includes_pressure_advance_and_accel_readback(monkeypatch):
    class FakeHttp:
        def __init__(self, _settings):
            pass

        def get_info(self):
            return {"min_extrusion_temp": 170.0}

        def get_status(self):
            return {}

        def get_job(self):
            return {}

    class FakeDriver:
        def read_pressure_advance(self):
            return 0.03

        def read_print_accel_mm_s2(self):
            return 1750.0

        def close(self):
            return None

    monkeypatch.setattr(hardware_smoke, "PrusaLinkHttpClient", FakeHttp)
    monkeypatch.setattr(
        hardware_smoke,
        "status_to_snapshot",
        lambda _status, job, info: SimpleNamespace(
            lifecycle=SimpleNamespace(value="PRINTING"),
            health="OK",
            job_active=True,
            job_id=11,
            job_progress_pct=42.0,
            job_time_printing_s=120.0,
            current_file="demo.bgcode",
            speed_pct=95.0,
            flow_pct=90.0,
            nozzle_temp_c=220.0,
            nozzle_target_c=225.0,
            bed_temp_c=58.0,
            bed_target_c=60.0,
            model="CORE One",
            serial_number="SN123",
            min_extrusion_temp_c=170.0,
        ),
    )
    monkeypatch.setattr(hardware_smoke, "_build_driver", lambda _settings: FakeDriver())

    payload = hardware_smoke._command_status(
        SimpleNamespace(
            serial_enabled=True,
            pressure_advance_tuning_verification_enabled=True,
            accel_tuning_verification_enabled=True,
        )
    )

    assert payload["pressure_advance"] == 0.03
    assert payload["print_accel_mm_s2"] == 1750.0


def test_main_trim_speed_big_uses_big_step_target(monkeypatch):
    captured = {}

    class FakeDriver:
        speed_default_pct = 100.0
        speed_big_step_pct = 10.0
        speed_min_pct = 65.0
        speed_max_pct = 135.0

        def trim_speed_down_big(self, *, target_speed_pct):
            captured["target_speed_pct"] = target_speed_pct
            return {"speed_pct": target_speed_pct}

        def close(self):
            return None

    monkeypatch.setattr(hardware_smoke.PrusaCoreOneSettings, "from_env", staticmethod(lambda: SimpleNamespace()))
    monkeypatch.setattr(hardware_smoke, "_build_driver", lambda _settings: FakeDriver())
    monkeypatch.setattr(hardware_smoke, "_command_status", lambda _settings: _status(speed=100.0))
    monkeypatch.setattr(hardware_smoke, "_emit", lambda payload: payload)

    result = hardware_smoke.main(["trim-speed-big"])

    assert captured["target_speed_pct"] == 90.0
    assert result == {"speed_pct": 90.0}


def test_command_status_reads_pa_and_accel_when_experimental_tuning_enabled(monkeypatch):
    class FakeHttp:
        def __init__(self, _settings):
            pass

        def get_info(self):
            return {}

        def get_status(self):
            return {}

        def get_job(self):
            return {}

    class FakeDriver:
        def read_pressure_advance(self):
            return 0.02

        def read_print_accel_mm_s2(self):
            return 1500.0

        def close(self):
            return None

    monkeypatch.setattr(hardware_smoke, "PrusaLinkHttpClient", FakeHttp)
    monkeypatch.setattr(
        hardware_smoke,
        "status_to_snapshot",
        lambda _status, job, info: SimpleNamespace(
            lifecycle=SimpleNamespace(value="PRINTING"),
            health="OK",
            job_active=True,
            job_id=99,
            job_progress_pct=12.0,
            job_time_printing_s=10.0,
            current_file="demo.bgcode",
            speed_pct=100.0,
            flow_pct=100.0,
            nozzle_temp_c=220.0,
            nozzle_target_c=220.0,
            bed_temp_c=60.0,
            bed_target_c=60.0,
            model="CORE One",
            serial_number="SN",
            min_extrusion_temp_c=170.0,
        ),
    )
    monkeypatch.setattr(hardware_smoke, "_build_driver", lambda _settings: FakeDriver())

    payload = hardware_smoke._command_status(
        SimpleNamespace(
            serial_enabled=True,
            enable_experimental_tuning=True,
            pressure_advance_tuning_verification_enabled=False,
            accel_tuning_verification_enabled=False,
        )
    )

    assert payload["pressure_advance"] == 0.02
    assert payload["print_accel_mm_s2"] == 1500.0


def test_command_status_skips_serial_readback_when_runtime_active(monkeypatch):
    class FakeHttp:
        def __init__(self, _settings):
            pass

        def get_info(self):
            return {}

        def get_status(self):
            return {}

        def get_job(self):
            return {}

    class FakeDriver:
        def read_pressure_advance(self):
            raise AssertionError("serial readback should be skipped when runtime owns the port")

        def read_print_accel_mm_s2(self):
            raise AssertionError("serial readback should be skipped when runtime owns the port")

        def close(self):
            return None

    monkeypatch.setattr(hardware_smoke, "PrusaLinkHttpClient", FakeHttp)
    monkeypatch.setattr(
        hardware_smoke,
        "status_to_snapshot",
        lambda _status, job, info: SimpleNamespace(
            lifecycle=SimpleNamespace(value="PRINTING"),
            health="OK",
            job_active=True,
            job_id=42,
            job_progress_pct=10.0,
            job_time_printing_s=30.0,
            current_file="demo.bgcode",
            speed_pct=100.0,
            flow_pct=100.0,
            nozzle_temp_c=220.0,
            nozzle_target_c=220.0,
            bed_temp_c=60.0,
            bed_target_c=60.0,
            model="CORE One",
            serial_number="SN",
            min_extrusion_temp_c=170.0,
        ),
    )
    monkeypatch.setattr(hardware_smoke, "_active_runtime_holds_serial", lambda: True)
    monkeypatch.setattr(hardware_smoke, "_build_driver", lambda _settings: FakeDriver())

    payload = hardware_smoke._command_status(
        SimpleNamespace(
            serial_enabled=True,
            serial_timeout_s=0.01,
            enable_experimental_tuning=True,
            pressure_advance_tuning_verification_enabled=True,
            accel_tuning_verification_enabled=True,
        )
    )

    assert payload["pressure_advance"] is None
    assert payload["print_accel_mm_s2"] is None
    assert payload["serial_tuning_readback_skipped_reason"] == "runtime_serial_owner_active"
    assert payload["pressure_advance_read_warning"] is None
    assert payload["print_accel_mm_s2_read_warning"] is None


def test_command_status_bounds_nonfatal_serial_readback(monkeypatch):
    class FakeHttp:
        def __init__(self, _settings):
            pass

        def get_info(self):
            return {}

        def get_status(self):
            return {}

        def get_job(self):
            return {}

    class FakeDriver:
        def __init__(self):
            self.closed = 0

        def read_pressure_advance(self):
            time.sleep(1.0)
            return 0.03

        def read_print_accel_mm_s2(self):
            return 1750.0

        def close(self):
            self.closed += 1

    driver = FakeDriver()
    monkeypatch.setattr(hardware_smoke, "PrusaLinkHttpClient", FakeHttp)
    monkeypatch.setattr(
        hardware_smoke,
        "status_to_snapshot",
        lambda _status, job, info: SimpleNamespace(
            lifecycle=SimpleNamespace(value="PRINTING"),
            health="OK",
            job_active=True,
            job_id=11,
            job_progress_pct=42.0,
            job_time_printing_s=120.0,
            current_file="demo.bgcode",
            speed_pct=95.0,
            flow_pct=90.0,
            nozzle_temp_c=220.0,
            nozzle_target_c=225.0,
            bed_temp_c=58.0,
            bed_target_c=60.0,
            model="CORE One",
            serial_number="SN123",
            min_extrusion_temp_c=170.0,
        ),
    )
    monkeypatch.setattr(hardware_smoke, "_active_runtime_holds_serial", lambda: False)
    monkeypatch.setattr(hardware_smoke, "_build_driver", lambda _settings: driver)

    started = time.monotonic()
    payload = hardware_smoke._command_status(
        SimpleNamespace(
            serial_enabled=True,
            serial_timeout_s=0.01,
            pressure_advance_tuning_verification_enabled=True,
            accel_tuning_verification_enabled=True,
            enable_experimental_tuning=False,
        )
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert payload["pressure_advance"] is None
    assert payload["print_accel_mm_s2"] == 1750.0
    assert payload["pressure_advance_read_warning"] == "pressure_advance_read_timeout"
    assert payload["print_accel_mm_s2_read_warning"] is None
    assert payload["serial_tuning_readback_skipped_reason"] is None
    assert driver.closed >= 1


def test_main_trim_pressure_advance_up_uses_scalar_readback(monkeypatch):
    captured = {}

    class FakeDriver:
        pressure_advance_min = 0.0
        pressure_advance_max = 0.12

        def pressure_advance_target_from_baseline(self, baseline, *, direction, magnitude):
            pct = 0.2 if magnitude == "big" else 0.1
            factor = 1.0 + pct if direction == "up" else 1.0 - pct
            return min(self.pressure_advance_max, max(self.pressure_advance_min, baseline * factor))

        def trim_pressure_advance_up_small(self, *, target_pressure_advance):
            captured["target_pressure_advance"] = target_pressure_advance
            return {"pressure_advance": target_pressure_advance}

        def close(self):
            return None

    monkeypatch.setattr(hardware_smoke.PrusaCoreOneSettings, "from_env", staticmethod(lambda: SimpleNamespace()))
    monkeypatch.setattr(hardware_smoke, "_build_driver", lambda _settings: FakeDriver())
    monkeypatch.setattr(
        hardware_smoke,
        "_command_status",
        lambda _settings: {**_status(), "pressure_advance": 0.03, "print_accel_mm_s2": 1500.0},
    )
    monkeypatch.setattr(hardware_smoke, "_emit", lambda payload: payload)

    result = hardware_smoke.main(["trim-pressure-advance-up"])

    assert captured["target_pressure_advance"] == pytest.approx(0.033)
    assert result == {"pressure_advance": pytest.approx(0.033)}


def test_main_trim_accel_down_big_uses_scalar_readback(monkeypatch):
    captured = {}

    class FakeDriver:
        accel_min_mm_s2 = 500.0
        accel_max_mm_s2 = 6000.0

        def print_accel_target_from_baseline(self, baseline, *, direction, magnitude):
            pct = 0.2 if magnitude == "big" else 0.1
            factor = 1.0 + pct if direction == "up" else 1.0 - pct
            return min(self.accel_max_mm_s2, max(self.accel_min_mm_s2, baseline * factor))

        def trim_print_accel_down_big(self, *, target_print_accel_mm_s2):
            captured["target_print_accel_mm_s2"] = target_print_accel_mm_s2
            return {"print_accel_mm_s2": target_print_accel_mm_s2}

        def close(self):
            return None

    monkeypatch.setattr(hardware_smoke.PrusaCoreOneSettings, "from_env", staticmethod(lambda: SimpleNamespace()))
    monkeypatch.setattr(hardware_smoke, "_build_driver", lambda _settings: FakeDriver())
    monkeypatch.setattr(
        hardware_smoke,
        "_command_status",
        lambda _settings: {**_status(), "pressure_advance": 0.03, "print_accel_mm_s2": 2000.0},
    )
    monkeypatch.setattr(hardware_smoke, "_emit", lambda payload: payload)

    result = hardware_smoke.main(["trim-accel-down-big"])

    assert captured["target_print_accel_mm_s2"] == 1600.0
    assert result == {"print_accel_mm_s2": 1600.0}


def test_command_serial_preflight_requires_serial_enabled():
    with pytest.raises(RuntimeError, match="Serial preflight requires"):
        hardware_smoke._command_serial_preflight(
            SimpleNamespace(serial_enabled=False, serial_port=None)
        )


def test_command_serial_preflight_requires_idle_printer(monkeypatch):
    monkeypatch.setattr(hardware_smoke, "_command_status", lambda _settings: _status())

    with pytest.raises(RuntimeError, match="requires an idle printer"):
        hardware_smoke._command_serial_preflight(
            SimpleNamespace(serial_enabled=True, serial_port="/dev/ttyACM0")
        )


def test_nozzle_vision_once_rejects_idle_mode_when_job_is_active():
    settings = SimpleNamespace(enable_vision_debug_context=False)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(hardware_smoke, "_command_status", lambda _settings: _status())
    try:
        with pytest.raises(RuntimeError, match="Idle nozzle vision capture requires no active job"):
            hardware_smoke._command_nozzle_vision_once(
                settings,
                mode="idle",
                output_root=None,
                planner_debug_preview=False,
            )
    finally:
        monkeypatch.undo()


def test_nozzle_vision_once_rejects_active_print_mode_when_printer_is_idle():
    settings = SimpleNamespace(enable_vision_debug_context=False)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        hardware_smoke,
        "_command_status",
        lambda _settings: {
            "lifecycle": "IDLE",
            "health": "OK",
            "job_active": False,
            "job_id": None,
            "job_progress_pct": None,
            "current_file": None,
            "speed_pct": 100.0,
            "flow_pct": 100.0,
            "nozzle_temp_c": 30.0,
            "nozzle_target_c": 0.0,
            "bed_temp_c": 30.0,
            "bed_target_c": 0.0,
        },
    )
    try:
        with pytest.raises(RuntimeError, match="Active-print nozzle vision capture requires an active job"):
            hardware_smoke._command_nozzle_vision_once(
                settings,
                mode="active-print",
                output_root=None,
                planner_debug_preview=False,
            )
    finally:
        monkeypatch.undo()


def test_nozzle_vision_once_returns_compact_preview_when_requested(tmp_path, monkeypatch):
    frame_path = tmp_path / "frames" / "frame.jpg"
    frame_path.parent.mkdir(parents=True, exist_ok=True)
    frame_path.write_bytes(b"jpeg-bytes")
    observation_path = tmp_path / "observations" / "frame.json"
    observation_path.parent.mkdir(parents=True, exist_ok=True)
    observation_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(hardware_smoke, "_command_status", lambda _settings: _status())
    monkeypatch.setattr(
        hardware_smoke,
        "_resolve_vision_job_identity",
        lambda _settings, status: {
            "source": "job_hash",
            "artifact_token": "job-7-hash-abc123",
            "job_hash": "abc123",
            "job_id": 7,
            "current_file": "benchy.bgcode",
            "grounding_error": None,
        },
    )
    monkeypatch.setattr(
        hardware_smoke.vision,
        "capture_nozzle_frame",
        lambda _settings: SimpleNamespace(camera_id="camera-nozzle", captured_at="2026-04-14T15:30:15Z", jpeg_bytes=b"jpeg"),
    )
    monkeypatch.setattr(
        hardware_smoke.vision,
        "persist_frame",
        lambda captured, *, output_root, job_identity: SimpleNamespace(
            frame_id="frame-1",
            camera_id="camera-nozzle",
            captured_at=captured.captured_at,
            frame_path=frame_path,
            image_ref=str(frame_path),
            artifact_stem="frame-1",
            jpeg_bytes=b"jpeg",
        ),
    )
    observation = hardware_smoke.vision.VisionObservation(
        schema_version="1.0",
        frame_id="frame-1",
        camera_id="camera-nozzle",
        captured_at="2026-04-14T15:30:15Z",
        image_ref=str(frame_path),
        findings=[],
        summary="No clear visible defect",
    )
    trace = hardware_smoke.vision.VisionAnalysisTrace(
        provider_metadata={
            "provider_name": "openrouter",
            "model": "vision-model",
            "latency_ms": 123.4,
            "tokens_prompt": 11,
            "tokens_completion": 7,
            "schema_valid": True,
        },
        request_payload={"model": "vision-model"},
        response_payload={"id": "resp-1"},
        response_text='{"summary":"No clear visible defect","findings":[]}',
    )
    monkeypatch.setattr(
        hardware_smoke.vision,
        "analyze_persisted_frame_with_trace",
        lambda persisted, *, settings, capture_mode, lifecycle: (observation, trace),
    )
    monkeypatch.setattr(hardware_smoke.vision, "persist_observation", lambda observation, *, output_root: observation_path)
    monkeypatch.setattr(hardware_smoke.vision, "advisory_summary", lambda observation: "vision:normal; No clear visible defect")

    payload = hardware_smoke._command_nozzle_vision_once(
        SimpleNamespace(enable_vision_debug_context=False),
        mode="auto",
        output_root=str(tmp_path),
        planner_debug_preview=True,
    )

    assert payload["mode_effective"] == "active-print"
    assert payload["finding_types"] == []
    assert payload["frame_path"] == str(frame_path)
    assert payload["observation_path"] == str(observation_path)
    assert payload["provider_metadata"]["model"] == "vision-model"
    assert payload["provider_request_payload"] == {"model": "vision-model"}
    assert payload["provider_response_payload"] == {"id": "resp-1"}
    assert payload["planner_debug_preview"] == "vision:normal; No clear visible defect"


def test_nozzle_camera_health_command_persists_artifact_and_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hardware_smoke,
        "_command_status",
        lambda _settings: {
            "lifecycle": "FINISHED",
            "health": "OK",
            "job_active": False,
            "job_id": None,
            "current_file": None,
        },
    )
    monkeypatch.setattr(
        hardware_smoke,
        "_resolve_vision_job_identity",
        lambda _settings, status: {
            "source": "idle",
            "artifact_token": "idle",
            "job_hash": None,
            "job_id": None,
            "current_file": None,
            "grounding_error": None,
        },
    )
    monkeypatch.setattr(
        hardware_smoke.vision,
        "evaluate_nozzle_camera_health",
        lambda settings, active_printing: hardware_smoke.vision.NozzleCameraHealthResult(
            report=hardware_smoke.vision.NozzleCameraHealthReport(
                nozzle_cam_device_present=True,
                nozzle_cam_service_ok=True,
                nozzle_cam_capture_ok=True,
                nozzle_cam_frame_fresh=True,
                nozzle_cam_frame_valid=True,
                nozzle_cam_frame_not_signal_slate=True,
                nozzle_cam_frame_live=True,
                nozzle_cam_usable=True,
                nozzle_cam_health_state="nominal",
                nozzle_cam_blockers=[],
                nozzle_cam_last_capture_at="2026-04-14T06:00:30Z",
                nozzle_cam_frame_age_s=0.1,
                nozzle_cam_last_frame_ref="sha256:abc123",
                nozzle_cam_repeated_identical_count=0,
                frame_refs_used=["sha256:abc123"],
            )
        ),
    )

    payload, exit_code = hardware_smoke._command_nozzle_camera_health(
        SimpleNamespace(),
        output_root=str(tmp_path),
    )

    assert exit_code == 0
    assert payload["usable"] is True
    assert Path(payload["health_artifact_path"]).exists()


def test_nozzle_camera_health_command_returns_nonzero_when_unusable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hardware_smoke,
        "_command_status",
        lambda _settings: {
            "lifecycle": "PRINTING",
            "health": "OK",
            "job_active": True,
            "job_id": 7,
            "current_file": "benchy.bgcode",
        },
    )
    monkeypatch.setattr(
        hardware_smoke,
        "_resolve_vision_job_identity",
        lambda _settings, status: {
            "source": "job_id",
            "artifact_token": "job-7",
            "job_hash": None,
            "job_id": 7,
            "current_file": "benchy.bgcode",
            "grounding_error": None,
        },
    )
    monkeypatch.setattr(
        hardware_smoke.vision,
        "evaluate_nozzle_camera_health",
        lambda settings, active_printing: hardware_smoke.vision.NozzleCameraHealthResult(
            report=hardware_smoke.vision.NozzleCameraHealthReport(
                nozzle_cam_device_present=True,
                nozzle_cam_service_ok=True,
                nozzle_cam_capture_ok=True,
                nozzle_cam_frame_fresh=True,
                nozzle_cam_frame_valid=True,
                nozzle_cam_frame_not_signal_slate=True,
                nozzle_cam_frame_live=False,
                nozzle_cam_usable=False,
                nozzle_cam_health_state="degraded",
                nozzle_cam_blockers=[
                    hardware_smoke.vision.NOZZLE_CAM_BLOCKER_FRAME_FROZEN,
                    hardware_smoke.vision.NOZZLE_CAM_BLOCKER_UNUSABLE,
                ],
                nozzle_cam_last_capture_at="2026-04-14T06:00:30Z",
                nozzle_cam_frame_age_s=0.1,
                nozzle_cam_last_frame_ref="sha256:abc123",
                nozzle_cam_repeated_identical_count=2,
                frame_refs_used=["sha256:abc123"],
            )
        ),
    )

    payload, exit_code = hardware_smoke._command_nozzle_camera_health(
        SimpleNamespace(),
        output_root=str(tmp_path),
    )

    assert exit_code == 1
    assert payload["usable"] is False
    assert "nozzle_cam_frame_frozen" in payload["blockers"]


def test_ab_isolation_verdict_points_to_physical_path_when_benchy_only_fails():
    verdict = hardware_smoke._ab_isolation_verdict(
        benchy={"status": "failed"},
        idle={"status": "passed"},
    )

    assert verdict["isolation_verdict"] == "benchy_load_or_physical_path"


def test_ab_isolation_verdict_points_to_wallee_path_when_idle_only_fails():
    verdict = hardware_smoke._ab_isolation_verdict(
        benchy={"status": "passed"},
        idle={"status": "failed"},
    )

    assert verdict["isolation_verdict"] == "wallee_vision_path"
