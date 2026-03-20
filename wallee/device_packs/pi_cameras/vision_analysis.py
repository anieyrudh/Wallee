"""Vision analysis sensor — classifies camera frames into structured defect scores
using Gemini Flash Lite. Scores only, no text descriptions. The main agent LLM
reads numerical scores from the whiteboard."""

import json
import logging
import time

import httpx
import redis

from wallee.tools.decorator import tool

logger = logging.getLogger(__name__)

# Module-level config — set by configure_vision() at boot
_api_key = ""
_vision_model = "google/gemini-3.1-flash-lite-preview"
_redis_url = "redis://localhost:6379"
_redis_client = None

# Previous frames for temporal comparison (camera_type → base64 string)
_prev_frames: dict[str, str] = {}

# Camera geometry — helps the vision LLM understand what it's looking at
CAMERA_DESCRIPTIONS = {
    "nozzle": (
        "This is from a camera mounted on the printhead, looking DOWN at roughly 45 degrees. "
        "You see the nozzle tip at the top of frame and the print surface below. "
        "Filament extrudes downward from the nozzle onto the part. "
        "If you see filament loops, curls, or accumulation BELOW the nozzle with no part surface "
        "underneath them, that is spaghetti — the print has failed and filament is extruding into air. "
        "This is NOT stringing. Stringing is thin whiskers between two solid parts of the print."
    ),
    "buddy": (
        "This is from a wide-angle camera mounted on the frame, looking at the full build plate. "
        "You see the bed surface, the print growing upward, and the gantry above. "
        "If the bed area where the print should be is empty, or you see loose filament piled up, "
        "the print has detached — that is spaghetti/detachment."
    ),
}

# Scores-only prompt — no description output
_SCORES_PROMPT = """Analyze this 3D printer camera image. Output ONLY a JSON object with defect scores from 0.0 to 1.0 for each category. No description, no explanation, no text.

You are inspecting a working 3D printer. Minor residue, small ooze, slight discoloration, and thin wisps are NORMAL artifacts. Do not flag them.

Only flag something as a defect if it would:
- Affect print quality (visible stringing across the part, layer gaps, surface roughness)
- Risk hardware damage (blob growing toward heater block, filament wrapping around hotend)
- Indicate print failure (part detached from bed, spaghetti, severe warping lifting corners)

A small blob sitting on the nozzle tip is not a defect. A blob growing and engulfing the heater block IS a defect.

Score meaning: 0.0=absent, 0.3=minor hint/noise, 0.5=possibly present, 0.7=likely present, 1.0=clearly obvious.

If two frames are provided, compare them. Growing accumulation = spaghetti. Worsening defect = escalate scores. Improving = lower scores.

Respond with ONLY this JSON structure, no other text:
{"normal": 1.0, "stringing": 0.0, "spaghetti": 0.0, "blob": 0.0, "warping": 0.0, "underextrusion": 0.0, "overextrusion": 0.0, "layer_shift": 0.0, "bed_adhesion_ok": 1.0, "burn_marks": 0.0, "confidence": 0.8}"""


def configure_vision(api_key: str, vision_model: str, redis_url: str):
    """Set vision config at boot from central config. Called by main.py."""
    global _api_key, _vision_model, _redis_url, _redis_client
    _api_key = api_key
    _vision_model = vision_model
    _redis_url = redis_url
    _redis_client = None
    logger.info(f"Vision sensor configured: model={vision_model}, api_key={'set' if api_key else 'MISSING'}")


def _get_redis():
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(_redis_url, decode_responses=True)
    return _redis_client


def _read_wb(r, key: str):
    """Read a whiteboard key, unwrapping JSON encoding."""
    raw = r.get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _build_prompt(camera_type: str, phase: str | None) -> str:
    """Build the full prompt with camera geometry + phase context."""
    camera_ctx = CAMERA_DESCRIPTIONS.get(camera_type, "")
    prompt = f"Camera: {camera_ctx}\n\n{_SCORES_PROMPT}" if camera_ctx else _SCORES_PROMPT
    if phase:
        prompt += f"\n\nCurrent print phase: {phase}. Calibrate your judgment to what is normal for this phase."
    return prompt


def _analyze_frame(frame_b64: str, prompt: str, prev_frame: str | None = None) -> dict | None:
    """Send frame(s) to Gemini Flash Lite. Returns scores dict or None."""
    content = []
    if prev_frame:
        content.append({"type": "text", "text": "PREVIOUS frame (~10 seconds ago):"})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{prev_frame}"}})
        content.append({"type": "text", "text": "CURRENT frame (now):"})
    else:
        content.append({"type": "text", "text": "First frame (no previous available):"})
    content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"}})
    content.append({"type": "text", "text": prompt})

    try:
        response = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": _vision_model,
                "messages": [{"role": "user", "content": content}],
                "max_tokens": 150,
                "temperature": 0.1,
                "stream": False,
            },
            timeout=15.0,
        )

        logger.info(f"Vision API response: status={response.status_code}, length={len(response.text)}")
        if response.status_code != 200:
            logger.warning(f"Vision API error: {response.status_code} — {response.text[:500]}")
            return None

        data = response.json()
        raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end < 0:
            logger.warning(f"Vision: no JSON in response: {raw[:100]}")
            return None

        return json.loads(raw[start:end + 1])

    except Exception as e:
        logger.warning(f"Vision analysis failed: {type(e).__name__}: {e}")
        return None


_SCORE_KEYS = ("normal", "stringing", "spaghetti", "blob", "warping",
               "underextrusion", "overextrusion", "layer_shift",
               "bed_adhesion_ok", "burn_marks", "confidence")


def _scores_to_result(scores: dict, prefix: str) -> dict:
    """Convert raw scores dict to whiteboard keys with the given prefix."""
    result = {}
    for key in _SCORE_KEYS:
        val = scores.get(key)
        if val is not None:
            result[f"{prefix}.{key}"] = round(float(val), 2)

    # Derive status from defect scores
    defect_scores = {k: float(v) for k, v in scores.items()
                     if k in _SCORE_KEYS and k not in ("normal", "confidence", "bed_adhesion_ok")}
    if not defect_scores:
        result[f"{prefix}.status"] = "NORMAL"
        return result

    max_defect_name = max(defect_scores, key=defect_scores.get)
    max_defect_val = defect_scores[max_defect_name]

    if max_defect_val > 0.7:
        result[f"{prefix}.status"] = f"DEFECT:{max_defect_name}"
    elif max_defect_val > 0.4:
        result[f"{prefix}.status"] = f"POSSIBLE:{max_defect_name}"
    else:
        result[f"{prefix}.status"] = "NORMAL"

    return result


@tool(kind="sensor", refresh_hz=0.1, history_depth=5)
def read_vision_analysis() -> dict:
    """Analyze camera frames for print defects using Gemini Flash Lite.

    Publishes per-camera scores (vision.nozzle.*, vision.buddy.*) and
    a combined vision.status. Scores only — no text descriptions.
    Runs every ~10s. Only active during PRINTING/PREPARING/PAUSED phases.
    """
    r = _get_redis()

    # Heartbeat
    r.set("vision.last_analysis_ts", str(time.time()), ex=30)

    phase = _read_wb(r, "job.phase")
    if phase not in ("PRINTING", "PREPARING", "PAUSED"):
        logger.debug(f"Vision: skipping, phase is {phase}")
        return {}

    if not _api_key:
        logger.warning("Vision: API key not configured")
        return {}

    nozzle_frame = _read_wb(r, "camera.nozzle_frame")
    buddy1_frame = _read_wb(r, "camera.buddy1_frame")
    buddy2_frame = _read_wb(r, "camera.buddy2_frame")

    if not nozzle_frame and not buddy1_frame and not buddy2_frame:
        all_keys = [k for k in r.keys("camera.*") if not k.endswith(":history")]
        logger.warning(f"Vision: no camera frames available. Camera keys: {all_keys}")
        return {}

    result = {}
    all_statuses = []

    # Nozzle camera
    if nozzle_frame and isinstance(nozzle_frame, str):
        prev = _prev_frames.get("nozzle")
        _prev_frames["nozzle"] = nozzle_frame
        scores = _analyze_frame(nozzle_frame, _build_prompt("nozzle", phase), prev_frame=prev)
        if scores:
            nozzle_result = _scores_to_result(scores, "vision.nozzle")
            result.update(nozzle_result)
            all_statuses.append(nozzle_result.get("vision.nozzle.status", "NORMAL"))
            logger.info(f"Vision nozzle: {nozzle_result.get('vision.nozzle.status', '?')}")

    # Buddy cameras
    for cam_key, frame, prefix in [("buddy1", buddy1_frame, "vision.buddy"), ("buddy2", buddy2_frame, "vision.buddy2")]:
        if frame and isinstance(frame, str):
            prev = _prev_frames.get(cam_key)
            _prev_frames[cam_key] = frame
            scores = _analyze_frame(frame, _build_prompt("buddy", phase), prev_frame=prev)
            if scores:
                cam_result = _scores_to_result(scores, prefix)
                result.update(cam_result)
                all_statuses.append(cam_result.get(f"{prefix}.status", "NORMAL"))
                logger.info(f"Vision {cam_key}: {cam_result.get(f'{prefix}.status', '?')}")

    # Combined status: worst across all cameras
    if any(s.startswith("DEFECT:") for s in all_statuses):
        result["vision.status"] = next(s for s in all_statuses if s.startswith("DEFECT:"))
    elif any(s.startswith("POSSIBLE:") for s in all_statuses):
        result["vision.status"] = next(s for s in all_statuses if s.startswith("POSSIBLE:"))
    elif all_statuses:
        result["vision.status"] = "NORMAL"

    # Primary confidence from nozzle
    if "vision.nozzle.confidence" in result:
        result["vision.confidence"] = result["vision.nozzle.confidence"]

    # Hysteresis: maintain previous defect detection if current reading is ambiguous
    current_status = result.get("vision.status", "NORMAL")
    if current_status == "NORMAL":
        prev_status = _read_wb(r, "vision.status") or "NORMAL"
        if "DEFECT" in str(prev_status) or "POSSIBLE" in str(prev_status):
            current_normal = result.get("vision.nozzle.normal", 0)
            if current_normal < 0.8:
                prev_defect = str(prev_status).split(":")[-1] if ":" in str(prev_status) else "unknown"
                result["vision.status"] = f"FADING:{prev_defect}"
                result["vision.confidence"] = max(result.get("vision.confidence", 0), 0.4)
                logger.info(f"Vision hysteresis: maintaining {prev_defect} (normal={current_normal})")

    return result
