"""Tests for prompt builder and vision message construction."""

import json
import time

import pytest

from wallee.agent.prompt import build_prompt, build_system_prompt, build_user_message, build_messages


@pytest.fixture
def sample_state():
    return {
        "env.temperature": 21.5,
        "env.temperature:trend": "rising +1.5 over 10 readings",
        "host.cpu_temp": 45.2,
        "host.cpu_percent": 12.3,
        "job.phase": "IDLE",
        "job.phase_detail": "No active job",
        "job.time_in_phase_s": 100,
    }


@pytest.fixture
def sample_tools():
    return [
        {"name": "resume_print", "description": "Resume paused print", "requires_approval": True},
        {"name": "set_temperature", "description": "Set temp", "requires_approval": False},
    ]


@pytest.fixture
def sample_knowledge():
    return {
        "SOUL.md": "You are Wallee, an autonomous hardware controller.",
        "HARDWARE.md": "Connected: BME280 (I2C), Pi Camera v3",
        "LEARNED.md": "Filament sticks above 65% humidity.",
    }


class TestBuildPrompt:
    """Tests for the legacy build_prompt (combines system + user)."""

    def test_contains_all_sections(self, sample_state, sample_tools, sample_knowledge):
        prompt = build_prompt(
            state=sample_state, episode=[], intent=None,
            knowledge=sample_knowledge, tools=sample_tools, current_time=time.time(),
        )
        assert "SOUL.md" in prompt
        assert "LEARNED.md" in prompt
        assert "WHITEBOARD STATE" in prompt
        assert "env.temperature: 21.5" in prompt
        assert "AVAILABLE TOOLS" in prompt
        assert "resume_print" in prompt
        assert "PHASE:" in prompt

    def test_includes_human_intent(self, sample_state, sample_tools, sample_knowledge):
        prompt = build_prompt(
            state=sample_state, episode=[], intent="resume the print please",
            knowledge=sample_knowledge, tools=sample_tools, current_time=time.time(),
        )
        assert "HUMAN INTENT" in prompt
        assert "resume the print please" in prompt

    def test_no_intent_section_when_none(self, sample_state, sample_tools, sample_knowledge):
        prompt = build_prompt(
            state=sample_state, episode=[], intent=None,
            knowledge=sample_knowledge, tools=sample_tools, current_time=time.time(),
        )
        assert "HUMAN INTENT" not in prompt

    def test_episode_context(self, sample_state, sample_tools, sample_knowledge):
        episode = [
            {"status": "DONE", "tool": "set_temperature", "reason": "checking docs", "result_json": '{"url": "..."}', "error_json": ""},
            {"status": "FAILED", "tool": "resume_print", "reason": "temps ok", "error_json": "not paused", "result_json": ""},
        ]
        prompt = build_prompt(
            state=sample_state, episode=episode, intent=None,
            knowledge=sample_knowledge, tools=sample_tools, current_time=time.time(),
        )
        assert "[DONE] set_temperature" in prompt
        assert "[FAILED] resume_print" in prompt
        assert "not paused" in prompt

    def test_empty_state(self, sample_tools, sample_knowledge):
        prompt = build_prompt(
            state={}, episode=[], intent=None,
            knowledge=sample_knowledge, tools=sample_tools, current_time=time.time(),
        )
        assert "(no data)" in prompt

    def test_no_tools(self, sample_state, sample_knowledge):
        prompt = build_prompt(
            state=sample_state, episode=[], intent=None,
            knowledge=sample_knowledge, tools=[], current_time=time.time(),
        )
        assert "(no actuator tools available)" in prompt

    def test_approval_annotation(self, sample_state, sample_tools, sample_knowledge):
        prompt = build_prompt(
            state=sample_state, episode=[], intent=None,
            knowledge=sample_knowledge, tools=sample_tools, current_time=time.time(),
        )
        assert "[REQUIRES APPROVAL]" in prompt

    def test_trend_in_state(self, sample_state, sample_tools, sample_knowledge):
        prompt = build_prompt(
            state=sample_state, episode=[], intent=None,
            knowledge=sample_knowledge, tools=sample_tools, current_time=time.time(),
        )
        assert "rising +1.5 over 10 readings" in prompt

    def test_knowledge_order(self, sample_state, sample_tools, sample_knowledge):
        prompt = build_prompt(
            state=sample_state, episode=[], intent=None,
            knowledge=sample_knowledge, tools=sample_tools, current_time=time.time(),
        )
        soul_pos = prompt.index("SOUL.md")
        learned_pos = prompt.index("LEARNED.md")
        assert soul_pos < learned_pos

    def test_camera_frames_excluded_from_text(self):
        state = {"camera.nozzle_frame": "AAAA" * 1000, "printer.state": "IDLE"}
        prompt = build_prompt(state=state, episode=[], intent=None, knowledge={}, tools=[], current_time=time.time())
        assert "A" * 180 not in prompt

    def test_agent_last_decision_is_excluded_from_text(self):
        state = {"agent.last_decision": "CALL_HUMAN: ask about weather", "printer.state": "IDLE"}
        prompt = build_prompt(state=state, episode=[], intent=None, knowledge={}, tools=[], current_time=time.time())
        assert "ask about weather" not in prompt
        assert "printer.state: IDLE" in prompt


class TestBuildSystemPrompt:
    def test_contains_knowledge(self, sample_knowledge, sample_tools):
        prompt = build_system_prompt(sample_knowledge, sample_tools)
        assert "SOUL.md" in prompt
        assert "LEARNED.md" in prompt
        assert "Wallee" in prompt

    def test_contains_tools(self, sample_knowledge, sample_tools):
        prompt = build_system_prompt(sample_knowledge, sample_tools)
        assert "resume_print" in prompt
        assert "[REQUIRES APPROVAL]" in prompt

    def test_includes_job_context(self, sample_tools):
        knowledge = {"SOUL.md": "agent", "JOB_CONTEXT.md": "File: benchy.gcode\nMaterial: PLA"}
        prompt = build_system_prompt(knowledge, sample_tools)
        assert "JOB_CONTEXT.md" in prompt
        assert "benchy.gcode" in prompt


class TestBuildUserMessage:
    def test_contains_phase_banner(self, sample_state):
        text = build_user_message(sample_state, [], None, time.time())
        assert "PHASE: IDLE" in text

    def test_contains_whiteboard(self, sample_state):
        text = build_user_message(sample_state, [], None, time.time())
        assert "env.temperature: 21.5" in text

    def test_pending_callout_shown(self, sample_state):
        pending = {"hash": "abc", "message": "nozzle blob", "time": time.time() - 60, "status": "PENDING"}
        text = build_user_message(sample_state, [], None, time.time(), pending_callout=pending)
        assert "STILL PENDING" in text
        assert "HAS NOT RESPONDED" in text
        assert "nozzle blob" in text

    def test_no_pending_callout(self, sample_state):
        text = build_user_message(sample_state, [], None, time.time())
        assert "No pending escalation" in text


class TestBuildMessages:
    def test_no_images_simple_message(self):
        msgs = build_messages("system prompt", "user text", {})
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert msgs[1]["content"] == "user text"

    def test_nozzle_camera_included(self):
        state = {"camera.nozzle_frame": "abc123base64"}
        msgs = build_messages("sys", "decide", state)
        user_msg = msgs[1]
        assert isinstance(user_msg["content"], list)
        image_blocks = [b for b in user_msg["content"] if b.get("type") == "image_url"]
        assert len(image_blocks) == 1
        assert "abc123base64" in image_blocks[0]["image_url"]["url"]

    def test_cameras_and_human_image(self):
        state = {
            "camera.nozzle_frame": "nozzle_b64",
            "camera.buddy1_frame": "buddy1_b64",
            "human.image": "operator_b64",
        }
        msgs = build_messages("sys", "decide", state)
        user_msg = msgs[1]
        assert isinstance(user_msg["content"], list)
        image_blocks = [b for b in user_msg["content"] if b.get("type") == "image_url"]
        assert len(image_blocks) == 3
