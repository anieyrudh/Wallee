from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wallee.packs.prusa_core_one_plus import vision


def _jpeg_bytes(seed: bytes = b"x") -> bytes:
    return b"\xff\xd8" + (seed * 128) + b"\xff\xd9"


class _UrlopenResponse:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._body


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        nozzle_camera_host="127.0.0.1",
        nozzle_camera_port=None,
        nozzle_camera_discovery_ports=("8083",),
        nozzle_camera_device_path="/dev/v4l/by-id/usb-3DO_3DO_NOZZLE_CAMERA_V2_3DO-video-index0",
        nozzle_camera_max_frame_age_s=30.0,
        nozzle_camera_liveness_window_size=2,
        vision_api_key="token",
        vision_model="vision-model",
    )


def _health_settings(tmp_path: Path, *, device_present: bool = True) -> SimpleNamespace:
    device_path = tmp_path / "video0"
    if device_present:
        device_path.write_text("device", encoding="utf-8")
    return SimpleNamespace(
        nozzle_camera_host="127.0.0.1",
        nozzle_camera_port="8080",
        nozzle_camera_discovery_ports=("8080",),
        nozzle_camera_device_path=str(device_path),
        nozzle_camera_max_frame_age_s=30.0,
        nozzle_camera_liveness_window_size=2,
        vision_api_key="token",
        vision_model="vision-model",
    )


def _captured_frame(*, captured_at: str | None = None, seed: bytes = b"x") -> vision.CapturedNozzleFrame:
    return vision.CapturedNozzleFrame(
        camera_id="camera-nozzle",
        captured_at=captured_at or vision._utc_now_iso(),
        jpeg_bytes=_jpeg_bytes(seed),
    )


def test_capture_nozzle_frame_uses_snapshot_when_valid(monkeypatch):
    monkeypatch.setattr(vision, "discover_nozzle_camera_port", lambda settings: "8083")
    monkeypatch.setattr(
        vision,
        "_http_read",
        lambda url, *, timeout_s, accept="image/jpeg": (_jpeg_bytes(), "image/jpeg"),
    )

    frame = vision.capture_nozzle_frame(_settings())

    assert frame.camera_id == "camera-nozzle"
    assert frame.captured_at.endswith("Z")
    assert frame.jpeg_bytes == _jpeg_bytes()


def test_capture_nozzle_frame_falls_back_to_stream(monkeypatch):
    monkeypatch.setattr(vision, "discover_nozzle_camera_port", lambda settings: "8083")
    monkeypatch.setattr(
        vision,
        "_http_read",
        lambda url, *, timeout_s, accept="image/jpeg": (b"not-a-jpeg", "application/octet-stream"),
    )
    monkeypatch.setattr(vision, "_capture_stream_frame", lambda base_url: _jpeg_bytes(b"s"))

    frame = vision.capture_nozzle_frame(_settings())

    assert frame.jpeg_bytes == _jpeg_bytes(b"s")


def test_capture_nozzle_frame_uses_active_print_burst_and_picks_latest_acceptable(monkeypatch):
    frames = [_jpeg_bytes(b"a"), _jpeg_bytes(b"b"), _jpeg_bytes(b"c")]
    analyses = {
        _jpeg_bytes(b"a"): vision._FrameAnalysis(True, 1920, 1080, 0.2, 0, 0, bright_ratio=0.05, mean_luma=120.0, blur_score=18.0),
        _jpeg_bytes(b"b"): vision._FrameAnalysis(True, 1920, 1080, 0.82, 0, 0, bright_ratio=0.05, mean_luma=120.0, blur_score=18.0),
        _jpeg_bytes(b"c"): vision._FrameAnalysis(True, 1920, 1080, 0.2, 0, 0, bright_ratio=0.05, mean_luma=120.0, blur_score=14.0),
    }
    calls: list[bytes] = []
    iterator = iter(frames)

    def _capture(_base_url, timeout_s=5.0):
        payload = next(iterator)
        calls.append(payload)
        return payload

    monkeypatch.setattr(vision, "discover_nozzle_camera_port", lambda settings, force=False: "8083")
    monkeypatch.setattr(vision, "_capture_stream_frame", _capture)
    monkeypatch.setattr(vision, "_analyze_frame_bytes", lambda payload: analyses[payload])
    monkeypatch.setattr(vision.time, "sleep", lambda _seconds: None)

    frame = vision.capture_nozzle_frame(_settings(), active_printing=True)

    assert frame.jpeg_bytes == _jpeg_bytes(b"c")
    assert calls == frames


def test_frame_is_acceptable_for_active_print_allows_dark_but_usable_frame():
    analysis = vision._FrameAnalysis(
        True,
        1920,
        1080,
        0.82,
        0,
        0,
        bright_ratio=0.01,
        mean_luma=25.0,
        blur_score=2.4,
    )

    assert vision._frame_is_acceptable_for_active_print(analysis) is True


def test_capture_nozzle_frame_retries_forced_port_discovery_before_failing(monkeypatch):
    calls: list[bool] = []

    def _discover(settings, force=False):
        calls.append(force)
        return "8083" if force else None

    monkeypatch.setattr(vision, "discover_nozzle_camera_port", _discover)
    monkeypatch.setattr(
        vision,
        "_http_read",
        lambda url, *, timeout_s, accept="image/jpeg": (_jpeg_bytes(b"f"), "image/jpeg"),
    )

    frame = vision.capture_nozzle_frame(_settings())

    assert frame.jpeg_bytes == _jpeg_bytes(b"f")
    assert calls == [False, True]


def test_capture_nozzle_frame_uses_stream_when_snapshot_is_stale(monkeypatch):
    snapshots = [_jpeg_bytes(b"a"), _jpeg_bytes(b"a")]

    monkeypatch.setattr(vision, "discover_nozzle_camera_port", lambda settings: "8083")
    monkeypatch.setattr(
        vision,
        "_http_read",
        lambda url, *, timeout_s, accept="image/jpeg": (snapshots.pop(0), "image/jpeg"),
    )
    monkeypatch.setattr(vision.time, "sleep", lambda _: None)
    monkeypatch.setattr(vision, "_capture_stream_frame", lambda base_url, timeout_s=5.0: _jpeg_bytes(b"l"))

    frame = vision.capture_nozzle_frame(_settings())

    assert frame.jpeg_bytes == _jpeg_bytes(b"l")


def test_capture_nozzle_frame_uses_stream_when_snapshot_probe_times_out(monkeypatch):
    calls = {"count": 0}

    def _read(url, *, timeout_s, accept="image/jpeg"):
        calls["count"] += 1
        if calls["count"] == 1:
            return _jpeg_bytes(b"a"), "image/jpeg"
        raise TimeoutError("timed out")

    monkeypatch.setattr(vision, "discover_nozzle_camera_port", lambda settings: "8083")
    monkeypatch.setattr(vision, "_http_read", _read)
    monkeypatch.setattr(vision.time, "sleep", lambda _: None)
    monkeypatch.setattr(vision, "_capture_stream_frame", lambda base_url, timeout_s=5.0: _jpeg_bytes(b"l"))

    frame = vision.capture_nozzle_frame(_settings())

    assert frame.jpeg_bytes == _jpeg_bytes(b"l")


def test_capture_nozzle_frame_fails_loudly_when_snapshot_and_stream_fail(monkeypatch):
    monkeypatch.setattr(vision, "discover_nozzle_camera_port", lambda settings: "8083")
    monkeypatch.setattr(
        vision,
        "_http_read",
        lambda url, *, timeout_s, accept="image/jpeg": (b"not-a-jpeg", "application/octet-stream"),
    )
    monkeypatch.setattr(vision, "_capture_stream_frame", lambda base_url: None)

    with pytest.raises(vision.VisionError, match="Nozzle camera capture failed after /snapshot and /stream attempts"):
        vision.capture_nozzle_frame(_settings())


def test_capture_nozzle_frame_active_print_fails_when_burst_has_no_acceptable_frame(monkeypatch):
    monkeypatch.setattr(vision, "discover_nozzle_camera_port", lambda settings, force=False: "8083")
    monkeypatch.setattr(vision, "_capture_stream_frame", lambda _base_url, timeout_s=5.0: _jpeg_bytes(b"a"))
    monkeypatch.setattr(
        vision,
        "_analyze_frame_bytes",
        lambda _payload: vision._FrameAnalysis(True, 1920, 1080, 0.9, 0, 0, bright_ratio=0.02, mean_luma=10.0, blur_score=2.0),
    )
    monkeypatch.setattr(vision.time, "sleep", lambda _seconds: None)

    with pytest.raises(vision.VisionError, match="no acceptable stable frame"):
        vision.capture_nozzle_frame(_settings(), active_printing=True)


def test_observation_artifact_matches_schema_and_links_frame(tmp_path, monkeypatch):
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "Thin strands connect two nearby features.",
                            "findings": [
                                {
                                    "finding_type": "stringing",
                                    "descriptors": ["minor", "between isolated features"],
                                    "evidence_strength": "moderate",
                                    "note": "Thin strands span two nearby features.",
                                }
                            ],
                        }
                    )
                }
            }
        ]
    }
    monkeypatch.setattr(vision.request, "urlopen", lambda req, timeout=45: _UrlopenResponse(json.dumps(payload)))

    captured = vision.CapturedNozzleFrame(
        camera_id="camera-nozzle",
        captured_at="2026-04-14T15:30:15Z",
        jpeg_bytes=_jpeg_bytes(),
    )
    persisted = vision.persist_frame(captured, output_root=tmp_path, job_identity="job-17-hash-abc123")
    observation = vision.analyze_persisted_frame(
        persisted,
        settings=_settings(),
        capture_mode="active-print",
        lifecycle="PRINTING",
    )
    observation_path = vision.persist_observation(observation, output_root=tmp_path)
    stored = json.loads(observation_path.read_text(encoding="utf-8"))

    assert stored == {
        "schema_version": "1.0",
        "frame_id": persisted.frame_id,
        "camera_id": "camera-nozzle",
        "captured_at": "2026-04-14T15:30:15Z",
        "image_ref": persisted.image_ref,
        "findings": [
            {
                "finding_id": f"{persisted.frame_id}-f01",
                "finding_type": "stringing",
                "descriptors": ["minor", "between_isolated_features"],
                "evidence_strength": "moderate",
                "note": "Thin strands span two nearby features",
            }
        ],
        "summary": "Thin strands connect two nearby features",
    }
    assert stored["image_ref"] == persisted.image_ref
    assert Path(persisted.frame_path).exists()
    assert "job-17-hash-abc123" in persisted.frame_path.name
    assert persisted.frame_path.name.endswith("camera-nozzle.jpg")


def test_analyze_persisted_frame_with_trace_captures_provider_metadata(tmp_path, monkeypatch):
    payload = {
        "id": "gen-123",
        "model": "vision-model",
        "provider": {"name": "openrouter"},
        "usage": {
            "prompt_tokens": 101,
            "completion_tokens": 19,
            "native_tokens_reasoning": 7,
        },
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "A dark blob is visible on the nozzle.",
                            "findings": [
                                {
                                    "finding_type": "blob",
                                    "descriptors": ["minor"],
                                    "evidence_strength": "strong",
                                    "note": "A dark blob is visible on the nozzle.",
                                }
                            ],
                        }
                    )
                },
            }
        ],
    }

    class _Response(_UrlopenResponse):
        def __init__(self, body: str):
            super().__init__(body)
            self.headers = {"x-request-id": "req-123"}

    monkeypatch.setattr(vision.request, "urlopen", lambda req, timeout=45: _Response(json.dumps(payload)))
    persisted = vision.persist_frame(
        vision.CapturedNozzleFrame(
            camera_id="camera-nozzle",
            captured_at="2026-04-14T15:30:15Z",
            jpeg_bytes=_jpeg_bytes(),
        ),
        output_root=tmp_path,
        job_identity="job-17",
    )

    observation, trace = vision.analyze_persisted_frame_with_trace(
        persisted,
        settings=_settings(),
        capture_mode="active-print",
        lifecycle="PRINTING",
    )

    assert observation.findings[0].finding_type == "blob"
    assert trace.provider_metadata["request_id"] == "gen-123"
    assert trace.provider_metadata["generation_id"] == "gen-123"
    assert trace.provider_metadata["provider_name"] == "openrouter"
    assert trace.provider_metadata["model"] == "vision-model"
    assert trace.provider_metadata["tokens_prompt"] == 101
    assert trace.provider_metadata["tokens_completion"] == 19
    assert trace.provider_metadata["native_tokens_reasoning"] == 7
    assert trace.provider_metadata["finish_reason"] == "stop"
    assert trace.provider_metadata["schema_valid"] is True
    assert trace.request_payload["messages"][0]["content"][0]["text"] == trace.prompt_text
    assert trace.request_payload["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert trace.response_payload["id"] == "gen-123"


def test_analyze_persisted_frame_with_trace_uses_previous_frame_for_comparison(tmp_path, monkeypatch):
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "Thin strands remain visible near the nozzle.",
                            "findings": [
                                {
                                    "finding_type": "stringing",
                                    "descriptors": ["thin_strands"],
                                    "evidence_strength": "strong",
                                    "note": "Thin strands remain visible near the nozzle.",
                                }
                            ],
                            "comparison_delta": "same",
                            "comparison_confidence": "strong",
                            "comparison_summary": "The visible stringing looks about the same.",
                        }
                    )
                }
            }
        ]
    }
    captured: dict[str, Any] = {}

    def _urlopen(req, timeout=45):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _UrlopenResponse(json.dumps(payload))

    monkeypatch.setattr(vision.request, "urlopen", _urlopen)

    previous = vision.persist_frame(
        vision.CapturedNozzleFrame(
            camera_id="camera-nozzle",
            captured_at="2026-04-14T15:30:05Z",
            jpeg_bytes=_jpeg_bytes(),
        ),
        output_root=tmp_path,
        job_identity="job-17",
    )
    current = vision.persist_frame(
        vision.CapturedNozzleFrame(
            camera_id="camera-nozzle",
            captured_at="2026-04-14T15:30:15Z",
            jpeg_bytes=_jpeg_bytes(),
        ),
        output_root=tmp_path,
        job_identity="job-17",
    )

    observation, trace = vision.analyze_persisted_frame_with_trace(
        current,
        settings=_settings(),
        capture_mode="active-print",
        lifecycle="PRINTING",
        previous_frame=previous,
    )

    content = captured["payload"]["messages"][0]["content"]
    assert content[1]["text"].startswith("PREVIOUS frame")
    assert content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert observation.reference_image_ref == previous.image_ref
    assert observation.comparison_delta == "same"
    assert observation.comparison_confidence == "strong"
    assert observation.comparison_summary == "The visible stringing looks about the same"
    assert trace.request_payload["messages"][0]["content"][3]["text"].startswith("CURRENT frame")


def test_analysis_rejects_forbidden_top_level_fields(tmp_path, monkeypatch):
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "No visible issue.",
                            "findings": [],
                            "confidence": 0.8,
                        }
                    )
                }
            }
        ]
    }
    monkeypatch.setattr(vision.request, "urlopen", lambda req, timeout=45: _UrlopenResponse(json.dumps(payload)))
    persisted = vision.persist_frame(
        vision.CapturedNozzleFrame(
            camera_id="camera-nozzle",
            captured_at="2026-04-14T15:30:15Z",
            jpeg_bytes=_jpeg_bytes(),
        ),
        output_root=tmp_path,
        job_identity="idle",
    )

    with pytest.raises(vision.VisionError, match="schema validation"):
        vision.analyze_persisted_frame(persisted, settings=_settings(), capture_mode="idle", lifecycle="IDLE")


def test_analysis_rejects_forbidden_nested_fields(tmp_path, monkeypatch):
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "Edge looks rough.",
                            "findings": [
                                {
                                    "finding_type": "stringing",
                                    "descriptors": ["minor"],
                                    "evidence_strength": "weak",
                                    "note": "One edge looks rough.",
                                    "location": "front_left",
                                }
                            ],
                        }
                    )
                }
            }
        ]
    }
    monkeypatch.setattr(vision.request, "urlopen", lambda req, timeout=45: _UrlopenResponse(json.dumps(payload)))
    persisted = vision.persist_frame(
        vision.CapturedNozzleFrame(
            camera_id="camera-nozzle",
            captured_at="2026-04-14T15:30:15Z",
            jpeg_bytes=_jpeg_bytes(),
        ),
        output_root=tmp_path,
        job_identity="job-17",
    )

    with pytest.raises(vision.VisionError, match="schema validation"):
        vision.analyze_persisted_frame(persisted, settings=_settings(), capture_mode="active-print", lifecycle="PRINTING")


def test_analysis_accepts_residue_as_nozzle_cam_specific_finding(tmp_path, monkeypatch):
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "Light residue is visible on the nozzle exterior.",
                            "findings": [
                                {
                                    "finding_type": "residue",
                                    "descriptors": ["minor", "darkened_tip"],
                                    "evidence_strength": "weak",
                                    "note": "Light residue is visible on the outer nozzle surface.",
                                }
                            ],
                        }
                    )
                }
            }
        ]
    }
    monkeypatch.setattr(vision.request, "urlopen", lambda req, timeout=45: _UrlopenResponse(json.dumps(payload)))
    persisted = vision.persist_frame(
        vision.CapturedNozzleFrame(
            camera_id="camera-nozzle",
            captured_at="2026-04-14T15:30:15Z",
            jpeg_bytes=_jpeg_bytes(),
        ),
        output_root=tmp_path,
        job_identity="idle",
    )

    observation = vision.analyze_persisted_frame(persisted, settings=_settings(), capture_mode="idle", lifecycle="IDLE")

    assert observation.findings[0].finding_type == "residue"
    assert observation.findings[0].note == "Light residue is visible on the outer nozzle surface"


def test_analysis_accepts_residue_and_stringing_together(tmp_path, monkeypatch):
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "Residue is visible on the nozzle and a thin strand trails away from it.",
                            "findings": [
                                {
                                    "finding_type": "residue",
                                    "descriptors": ["minor"],
                                    "evidence_strength": "weak",
                                    "note": "Minor residue is adhered to the nozzle exterior.",
                                },
                                {
                                    "finding_type": "stringing",
                                    "descriptors": ["thin", "trailing"],
                                    "evidence_strength": "moderate",
                                    "note": "A thin filament strand trails away from the nozzle tip.",
                                },
                            ],
                        }
                    )
                }
            }
        ]
    }
    monkeypatch.setattr(vision.request, "urlopen", lambda req, timeout=45: _UrlopenResponse(json.dumps(payload)))
    persisted = vision.persist_frame(
        vision.CapturedNozzleFrame(
            camera_id="camera-nozzle",
            captured_at="2026-04-14T15:30:15Z",
            jpeg_bytes=_jpeg_bytes(),
        ),
        output_root=tmp_path,
        job_identity="job-17",
    )

    observation = vision.analyze_persisted_frame(
        persisted,
        settings=_settings(),
        capture_mode="active-print",
        lifecycle="PRINTING",
    )

    assert [item.finding_type for item in observation.findings] == ["residue", "stringing"]


def test_analysis_accepts_spaghetti_as_nozzle_cam_specific_finding(tmp_path, monkeypatch):
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "A loose nest of filament is visible near the nozzle.",
                            "findings": [
                                {
                                    "finding_type": "spaghetti",
                                    "descriptors": ["loose", "tangled"],
                                    "evidence_strength": "strong",
                                    "note": "A tangled mass of loose filament is visible near the nozzle.",
                                }
                            ],
                        }
                    )
                }
            }
        ]
    }
    monkeypatch.setattr(vision.request, "urlopen", lambda req, timeout=45: _UrlopenResponse(json.dumps(payload)))
    persisted = vision.persist_frame(
        vision.CapturedNozzleFrame(
            camera_id="camera-nozzle",
            captured_at="2026-04-14T15:30:15Z",
            jpeg_bytes=_jpeg_bytes(),
        ),
        output_root=tmp_path,
        job_identity="job-17",
    )

    observation = vision.analyze_persisted_frame(
        persisted,
        settings=_settings(),
        capture_mode="active-print",
        lifecycle="PRINTING",
    )

    assert observation.findings[0].finding_type == "spaghetti"
    assert observation.findings[0].note == "A tangled mass of loose filament is visible near the nozzle"


def test_analysis_accepts_blob_for_thick_buildup(tmp_path, monkeypatch):
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "A thick hanging buildup is visible on the nozzle.",
                            "findings": [
                                {
                                    "finding_type": "blob",
                                    "descriptors": ["thick", "hanging"],
                                    "evidence_strength": "strong",
                                    "note": "A thick hanging mass of material is attached near the nozzle tip.",
                                }
                            ],
                        }
                    )
                }
            }
        ]
    }
    monkeypatch.setattr(vision.request, "urlopen", lambda req, timeout=45: _UrlopenResponse(json.dumps(payload)))
    persisted = vision.persist_frame(
        vision.CapturedNozzleFrame(
            camera_id="camera-nozzle",
            captured_at="2026-04-14T15:30:15Z",
            jpeg_bytes=_jpeg_bytes(),
        ),
        output_root=tmp_path,
        job_identity="job-17",
    )

    observation = vision.analyze_persisted_frame(
        persisted,
        settings=_settings(),
        capture_mode="active-print",
        lifecycle="PRINTING",
    )

    assert observation.findings[0].finding_type == "blob"


def test_analysis_accepts_unknown_for_ambiguous_frame(tmp_path, monkeypatch):
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "The visible feature is too unclear to classify confidently.",
                            "findings": [
                                {
                                    "finding_type": "unknown",
                                    "descriptors": ["blurry"],
                                    "evidence_strength": "weak",
                                    "note": "The image is too blurry to determine whether the visible feature is a thin strand.",
                                }
                            ],
                        }
                    )
                }
            }
        ]
    }
    monkeypatch.setattr(vision.request, "urlopen", lambda req, timeout=45: _UrlopenResponse(json.dumps(payload)))
    persisted = vision.persist_frame(
        vision.CapturedNozzleFrame(
            camera_id="camera-nozzle",
            captured_at="2026-04-14T15:30:15Z",
            jpeg_bytes=_jpeg_bytes(),
        ),
        output_root=tmp_path,
        job_identity="job-17",
    )

    observation = vision.analyze_persisted_frame(
        persisted,
        settings=_settings(),
        capture_mode="active-print",
        lifecycle="PRINTING",
    )

    assert observation.findings[0].finding_type == "unknown"


def test_advisory_summary_stays_compact():
    observation = vision.VisionObservation(
        schema_version="1.0",
        frame_id="frame-1",
        camera_id="camera-nozzle",
        captured_at="2026-04-14T15:30:15Z",
        image_ref="docs/evidence/prusa_core_one_plus/vision/frames/frame-1.jpg",
        findings=[
            vision.VisionFinding(
                finding_id="frame-1-f01",
                finding_type="blob",
                descriptors=["minor"],
                evidence_strength="moderate",
                note="Small buildup is visible.",
            )
        ],
        summary="Small buildup is visible on the nozzle.",
    )

    text = vision.advisory_summary(observation)

    assert text.startswith("vision:blob;")
    assert len(text) <= 160
    assert "image/jpeg" not in text
    assert "data:" not in text


def test_build_prompt_requires_stringing_when_thin_strand_is_visible():
    prompt = vision._build_prompt(camera_id="camera-nozzle", capture_mode="active-print", lifecycle="PRINTING")

    assert "Multiple findings may coexist" in prompt
    assert "include stringing even if residue is also visible" in prompt
    assert "Use residue only" in prompt


def test_nozzle_camera_health_device_missing_marks_unusable(tmp_path, monkeypatch):
    settings = _health_settings(tmp_path, device_present=False)
    monkeypatch.setattr(vision, "discover_nozzle_camera_port", lambda settings, force=False: "8080")
    monkeypatch.setattr(vision, "_service_ok", lambda base_url: True)
    monkeypatch.setattr(
        vision,
        "capture_nozzle_frame",
        lambda settings, timeout_s=5.0, active_printing=False: _captured_frame(),
    )
    monkeypatch.setattr(vision, "_analyze_frame_bytes", lambda jpeg_bytes: vision._FrameAnalysis(True, 1920, 1080, 0.4, 0, 0))

    result = vision.evaluate_nozzle_camera_health(settings, active_printing=False).report

    assert result.nozzle_cam_device_present is False
    assert result.nozzle_cam_usable is False
    assert result.nozzle_cam_health_state == "unusable"
    assert vision.NOZZLE_CAM_BLOCKER_DEVICE_MISSING in result.nozzle_cam_blockers


def test_nozzle_camera_health_service_down_marks_unusable(tmp_path, monkeypatch):
    settings = _health_settings(tmp_path)
    monkeypatch.setattr(vision, "discover_nozzle_camera_port", lambda settings, force=False: "8080")
    monkeypatch.setattr(vision, "_service_ok", lambda base_url: False)
    monkeypatch.setattr(
        vision,
        "capture_nozzle_frame",
        lambda settings, timeout_s=5.0, active_printing=False: (_ for _ in ()).throw(vision.VisionError("down")),
    )

    result = vision.evaluate_nozzle_camera_health(settings, active_printing=False).report

    assert result.nozzle_cam_service_ok is False
    assert result.nozzle_cam_usable is False
    assert vision.NOZZLE_CAM_BLOCKER_SERVICE_DOWN in result.nozzle_cam_blockers


def test_nozzle_camera_health_capture_fail_marks_unusable(tmp_path, monkeypatch):
    settings = _health_settings(tmp_path)
    monkeypatch.setattr(vision, "discover_nozzle_camera_port", lambda settings, force=False: "8080")
    monkeypatch.setattr(vision, "_service_ok", lambda base_url: True)
    monkeypatch.setattr(
        vision,
        "capture_nozzle_frame",
        lambda settings, timeout_s=5.0, active_printing=False: (_ for _ in ()).throw(vision.VisionError("capture failed")),
    )

    result = vision.evaluate_nozzle_camera_health(settings, active_printing=False).report

    assert result.nozzle_cam_capture_ok is False
    assert result.nozzle_cam_usable is False
    assert vision.NOZZLE_CAM_BLOCKER_CAPTURE_FAILED in result.nozzle_cam_blockers


def test_nozzle_camera_health_invalid_frame_marks_unusable(tmp_path, monkeypatch):
    settings = _health_settings(tmp_path)
    monkeypatch.setattr(vision, "discover_nozzle_camera_port", lambda settings, force=False: "8080")
    monkeypatch.setattr(vision, "_service_ok", lambda base_url: True)
    monkeypatch.setattr(
        vision,
        "capture_nozzle_frame",
        lambda settings, timeout_s=5.0, active_printing=False: _captured_frame(),
    )
    monkeypatch.setattr(vision, "_analyze_frame_bytes", lambda jpeg_bytes: vision._FrameAnalysis(False, 0, 0, 1.0, 0, 0))

    result = vision.evaluate_nozzle_camera_health(settings, active_printing=False).report

    assert result.nozzle_cam_frame_valid is False
    assert result.nozzle_cam_usable is False
    assert vision.NOZZLE_CAM_BLOCKER_FRAME_INVALID in result.nozzle_cam_blockers


def test_nozzle_camera_health_no_signal_frame_marks_unusable(tmp_path, monkeypatch):
    settings = _health_settings(tmp_path)
    monkeypatch.setattr(vision, "discover_nozzle_camera_port", lambda settings, force=False: "8080")
    monkeypatch.setattr(vision, "_service_ok", lambda base_url: True)
    monkeypatch.setattr(
        vision,
        "capture_nozzle_frame",
        lambda settings, timeout_s=5.0, active_printing=False: _captured_frame(),
    )
    monkeypatch.setattr(
        vision,
        "_analyze_frame_bytes",
        lambda jpeg_bytes: vision._FrameAnalysis(True, 1920, 1080, 0.98, vision._NO_SIGNAL_AHASH, vision._NO_SIGNAL_DHASH),
    )

    result = vision.evaluate_nozzle_camera_health(settings, active_printing=False).report

    assert result.nozzle_cam_frame_not_signal_slate is False
    assert result.nozzle_cam_usable is False
    assert vision.NOZZLE_CAM_BLOCKER_NO_SIGNAL in result.nozzle_cam_blockers


def test_nozzle_camera_health_repeated_identical_frames_while_printing_fail_liveness(tmp_path, monkeypatch):
    settings = _health_settings(tmp_path)
    monkeypatch.setattr(vision, "discover_nozzle_camera_port", lambda settings, force=False: "8080")
    monkeypatch.setattr(vision, "_service_ok", lambda base_url: True)
    monkeypatch.setattr(
        vision,
        "capture_nozzle_frame",
        lambda settings, timeout_s=5.0, active_printing=False: _captured_frame(seed=b"a"),
    )
    monkeypatch.setattr(vision, "_analyze_frame_bytes", lambda jpeg_bytes: vision._FrameAnalysis(True, 1920, 1080, 0.4, 0, 0))

    result = vision.evaluate_nozzle_camera_health(settings, active_printing=True).report

    assert result.nozzle_cam_frame_live is False
    assert result.nozzle_cam_repeated_identical_count == 2
    assert result.nozzle_cam_health_state == "degraded"
    assert vision.NOZZLE_CAM_BLOCKER_FRAME_FROZEN in result.nozzle_cam_blockers


def test_nozzle_camera_health_uses_active_print_capture_path_when_printing(tmp_path, monkeypatch):
    settings = _health_settings(tmp_path)
    calls: list[bool] = []

    monkeypatch.setattr(vision, "discover_nozzle_camera_port", lambda settings, force=False: "8080")
    monkeypatch.setattr(vision, "_service_ok", lambda base_url: True)

    def _capture(settings, timeout_s=5.0, active_printing=False):
        calls.append(active_printing)
        return _captured_frame(seed=b"a")

    monkeypatch.setattr(vision, "capture_nozzle_frame", _capture)
    monkeypatch.setattr(vision, "_analyze_frame_bytes", lambda jpeg_bytes: vision._FrameAnalysis(True, 1920, 1080, 0.4, 0, 0))

    result = vision.evaluate_nozzle_camera_health(settings, active_printing=True).report

    assert result.nozzle_cam_capture_ok is True
    assert calls == [True, True]


def test_nozzle_camera_health_identical_frames_while_idle_do_not_fail_liveness(tmp_path, monkeypatch):
    settings = _health_settings(tmp_path)
    monkeypatch.setattr(vision, "discover_nozzle_camera_port", lambda settings, force=False: "8080")
    monkeypatch.setattr(vision, "_service_ok", lambda base_url: True)
    monkeypatch.setattr(
        vision,
        "capture_nozzle_frame",
        lambda settings, timeout_s=5.0, active_printing=False: _captured_frame(seed=b"a"),
    )
    monkeypatch.setattr(vision, "_analyze_frame_bytes", lambda jpeg_bytes: vision._FrameAnalysis(True, 1920, 1080, 0.4, 0, 0))

    result = vision.evaluate_nozzle_camera_health(settings, active_printing=False).report

    assert result.nozzle_cam_frame_live is True
    assert result.nozzle_cam_usable is True


def test_nozzle_camera_health_usable_case_is_nominal(tmp_path, monkeypatch):
    settings = _health_settings(tmp_path)
    monkeypatch.setattr(vision, "discover_nozzle_camera_port", lambda settings, force=False: "8080")
    monkeypatch.setattr(vision, "_service_ok", lambda base_url: True)
    monkeypatch.setattr(
        vision,
        "capture_nozzle_frame",
        lambda settings, timeout_s=5.0, active_printing=False: _captured_frame(),
    )
    monkeypatch.setattr(vision, "_analyze_frame_bytes", lambda jpeg_bytes: vision._FrameAnalysis(True, 1920, 1080, 0.4, 0, 0))
    monkeypatch.setattr(vision, "_frame_age_s", lambda captured_at: 0.5)

    result = vision.evaluate_nozzle_camera_health(settings, active_printing=False).report

    assert result.nozzle_cam_usable is True
    assert result.nozzle_cam_health_state == "nominal"


def test_persist_nozzle_camera_health_artifact_writes_compact_artifact(tmp_path):
    report = vision.NozzleCameraHealthReport(
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
    result = vision.NozzleCameraHealthResult(
        report=report,
        frames=(vision.HealthProbeFrame(captured_at="2026-04-14T06:00:30Z", jpeg_bytes=_jpeg_bytes(), sha256="abc123"),),
    )

    stored_report, artifact_path = vision.persist_nozzle_camera_health_artifact(
        result,
        output_root=tmp_path,
        prefix="printer_1",
        job_identity={"artifact_token": "idle"},
        status={"lifecycle": "FINISHED", "job_active": False},
    )

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))

    assert stored_report.nozzle_cam_last_frame_ref.endswith(".jpg")
    assert payload["usable"] is True
    assert payload["facts"]["printer_1.nozzle_cam_usable"] is True
    assert payload["frame_refs_used"]


def test_persist_vision_replay_bundle_and_replay_request(tmp_path, monkeypatch):
    payload = {
        "id": "gen-456",
        "model": "vision-model",
        "provider": {"name": "openrouter"},
        "usage": {"prompt_tokens": 11, "completion_tokens": 5},
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "A thin strand is visible near the nozzle.",
                            "findings": [
                                {
                                    "finding_type": "stringing",
                                    "descriptors": ["thin"],
                                    "evidence_strength": "moderate",
                                    "note": "A thin strand trails from the nozzle.",
                                }
                            ],
                        }
                    )
                },
            }
        ],
    }
    captured_request: dict[str, Any] = {}

    class _Response(_UrlopenResponse):
        def __init__(self, body: str):
            super().__init__(body)
            self.headers = {"x-request-id": "req-456"}

    def _urlopen(req, timeout=45):
        captured_request["payload"] = json.loads(req.data.decode("utf-8"))
        return _Response(json.dumps(payload))

    monkeypatch.setattr(vision.request, "urlopen", _urlopen)
    persisted = vision.persist_frame(
        vision.CapturedNozzleFrame(
            camera_id="camera-nozzle",
            captured_at="2026-04-14T15:30:15Z",
            jpeg_bytes=_jpeg_bytes(b"z"),
        ),
        output_root=tmp_path,
        job_identity="job-17",
    )
    _observation, trace = vision.analyze_persisted_frame_with_trace(
        persisted,
        settings=_settings(),
        capture_mode="active-print",
        lifecycle="PRINTING",
    )
    replay_path = vision.persist_vision_replay_bundle(frame=persisted, trace=trace, output_root=tmp_path)
    bundle = json.loads(replay_path.read_text(encoding="utf-8"))

    assert bundle["frame_id"] == persisted.frame_id
    assert bundle["image_ref"] == persisted.image_ref
    assert bundle["image_bytes_base64"]
    assert bundle["prompt_text"] == trace.prompt_text
    assert bundle["request_payload"]["model"] == "vision-model"
    assert bundle["response_payload"]["id"] == "gen-456"

    replay_trace = vision.replay_saved_vision_bundle(replay_path, vision_api_key="token")

    assert captured_request["payload"] == bundle["request_payload"]
    assert replay_trace.prompt_text == bundle["prompt_text"]
    assert replay_trace.provider_metadata["request_id"] == "gen-456"
