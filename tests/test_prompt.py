"""Tests for prompt builder and vision message construction."""

import json
import time
import pytest
from wallee.agent.prompt import build_prompt, build_messages


@pytest.fixture
def sample_state():
    return {
        "env.temperature": 21.5,
        "env.temperature:trend": "rising +1.5 over 10 readings",
        "host.cpu_temp": 45.2,
        "host.cpu_percent": 12.3,
    }


@pytest.fixture
def sample_tools():
    return [
        {"name": "resume_print", "description": "Resume paused print", "requires_approval": True},
        {"name": "web_search", "description": "Search the web", "requires_approval": False},
    ]


@pytest.fixture
def sample_knowledge():
    return {
        "SOUL.md": "You are Wallee, an autonomous hardware controller.",
        "HARDWARE.md": "Connected: BME280 (I2C), Pi Camera v3",
        "LEARNED.md": "Filament sticks above 65% humidity.",
    }


class TestBuildPrompt:
    def test_contains_all_sections(self, sample_state, sample_tools, sample_knowledge):
        prompt = build_prompt(
            state=sample_state,
            episode=[],
            intent=None,
            knowledge=sample_knowledge,
            tools=sample_tools,
            current_time=time.time(),
        )
        assert "SOUL.md" in prompt
        assert "HARDWARE.md" in prompt
        assert "LEARNED.md" in prompt
        assert "WHITEBOARD STATE" in prompt
        assert "env.temperature: 21.5" in prompt
        assert "AVAILABLE TOOLS" in prompt
        assert "resume_print" in prompt
        assert "INSTRUCTIONS" in prompt

    def test_includes_human_intent(self, sample_state, sample_tools, sample_knowledge):
        prompt = build_prompt(
            state=sample_state,
            episode=[],
            intent="resume the print please",
            knowledge=sample_knowledge,
            tools=sample_tools,
            current_time=time.time(),
        )
        assert "HUMAN INTENT" in prompt
        assert "resume the print please" in prompt

    def test_no_intent_section_when_none(self, sample_state, sample_tools, sample_knowledge):
        prompt = build_prompt(
            state=sample_state,
            episode=[],
            intent=None,
            knowledge=sample_knowledge,
            tools=sample_tools,
            current_time=time.time(),
        )
        assert "HUMAN INTENT" not in prompt

    def test_episode_context(self, sample_state, sample_tools, sample_knowledge):
        episode = [
            {"status": "DONE", "tool": "web_search", "reason": "checking docs", "result_json": '{"url": "..."}', "error_json": ""},
            {"status": "FAILED", "tool": "resume_print", "reason": "temps ok", "error_json": "not paused", "result_json": ""},
        ]
        prompt = build_prompt(
            state=sample_state,
            episode=episode,
            intent=None,
            knowledge=sample_knowledge,
            tools=sample_tools,
            current_time=time.time(),
        )
        assert "[DONE] web_search" in prompt
        assert "[FAILED] resume_print" in prompt
        assert "not paused" in prompt

    def test_empty_state(self, sample_tools, sample_knowledge):
        prompt = build_prompt(
            state={},
            episode=[],
            intent=None,
            knowledge=sample_knowledge,
            tools=sample_tools,
            current_time=time.time(),
        )
        assert "(no data)" in prompt

    def test_no_tools(self, sample_state, sample_knowledge):
        prompt = build_prompt(
            state=sample_state,
            episode=[],
            intent=None,
            knowledge=sample_knowledge,
            tools=[],
            current_time=time.time(),
        )
        assert "(no actuator tools available)" in prompt

    def test_approval_annotation(self, sample_state, sample_tools, sample_knowledge):
        prompt = build_prompt(
            state=sample_state,
            episode=[],
            intent=None,
            knowledge=sample_knowledge,
            tools=sample_tools,
            current_time=time.time(),
        )
        assert "[REQUIRES APPROVAL]" in prompt

    def test_trend_in_state(self, sample_state, sample_tools, sample_knowledge):
        prompt = build_prompt(
            state=sample_state,
            episode=[],
            intent=None,
            knowledge=sample_knowledge,
            tools=sample_tools,
            current_time=time.time(),
        )
        assert "rising +1.5 over 10 readings" in prompt

    def test_knowledge_order(self, sample_state, sample_tools, sample_knowledge):
        prompt = build_prompt(
            state=sample_state,
            episode=[],
            intent=None,
            knowledge=sample_knowledge,
            tools=sample_tools,
            current_time=time.time(),
        )
        soul_pos = prompt.index("SOUL.md")
        hw_pos = prompt.index("HARDWARE.md")
        assert soul_pos < hw_pos

    def test_camera_frames_excluded_from_text(self):
        """Camera base64 data should not appear in text prompt — sent as vision blocks."""
        state = {
            "camera.nozzle_frame": "AAAA" * 1000,
            "printer.state": "IDLE",
        }
        prompt = build_prompt(state=state, episode=[], intent=None,
                              knowledge={}, tools=[], current_time=time.time())
        assert "A" * 180 not in prompt

    def test_episode_payloads_are_summarized(self, sample_state, sample_tools, sample_knowledge):
        long_reason = "check " * 40
        episode = [
            {
                "status": "DONE",
                "tool": "web_search",
                "reason": long_reason,
                "result_json": json.dumps({"items": ["x" * 50, "y" * 50, "z" * 50]}),
                "error_json": "",
            },
            {
                "status": "FAILED",
                "tool": "resume_print",
                "reason": "temps ok",
                "error_json": json.dumps({"error": "not paused", "details": "A" * 200}),
                "result_json": "",
            },
        ]
        prompt = build_prompt(
            state=sample_state,
            episode=episode,
            intent=None,
            knowledge=sample_knowledge,
            tools=sample_tools,
            current_time=time.time(),
        )
        assert '"items"' in prompt
        assert '"details"' in prompt
        assert "A" * 180 not in prompt
        assert "..." in prompt
        assert long_reason not in prompt

    def test_long_state_json_is_truncated(self, sample_tools, sample_knowledge):
        state = {"printer.snapshot": {"blob": "B" * 300}}
        prompt = build_prompt(
            state=state,
            episode=[],
            intent=None,
            knowledge=sample_knowledge,
            tools=sample_tools,
            current_time=time.time(),
        )
        assert '"blob"' in prompt
        assert "..." in prompt
        assert "B" * 120 not in prompt


class TestBuildMessages:
    def test_no_images_simple_message(self):
        msgs = build_messages("system prompt", {})
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert msgs[1]["content"] == "Decide your next action."

    def test_nozzle_camera_included(self):
        state = {"camera.nozzle_frame": "abc123base64"}
        msgs = build_messages("sys", state)
        user_msg = msgs[1]
        assert isinstance(user_msg["content"], list)
        image_blocks = [b for b in user_msg["content"] if b.get("type") == "image_url"]
        assert len(image_blocks) == 1
        assert "abc123base64" in image_blocks[0]["image_url"]["url"]
        text_blocks = [b for b in user_msg["content"] if b.get("type") == "text"]
        assert any("Nozzle camera" in t["text"] for t in text_blocks)

    def test_cameras_and_human_image(self):
        state = {
            "camera.nozzle_frame": "nozzle_b64",
            "camera.buddy1_frame": "buddy1_b64",
            "human.image": "operator_b64",
        }
        msgs = build_messages("sys", state)
        user_msg = msgs[1]
        assert isinstance(user_msg["content"], list)
        image_blocks = [b for b in user_msg["content"] if b.get("type") == "image_url"]
        assert len(image_blocks) == 3  # 2 cameras (max) + human image
        text_blocks = [b for b in user_msg["content"] if b.get("type") == "text"]
        assert any("Nozzle" in t["text"] for t in text_blocks)
        assert any("Buddy" in t["text"] for t in text_blocks)
        assert any("operator" in t["text"] for t in text_blocks)
        assert any("Decide" in t["text"] for t in text_blocks)

    def test_human_image_only(self):
        state = {"human.image": "photo_b64"}
        msgs = build_messages("sys", state)
        user_msg = msgs[1]
        assert isinstance(user_msg["content"], list)
        image_blocks = [b for b in user_msg["content"] if b.get("type") == "image_url"]
        assert len(image_blocks) == 1
        assert "photo_b64" in image_blocks[0]["image_url"]["url"]
