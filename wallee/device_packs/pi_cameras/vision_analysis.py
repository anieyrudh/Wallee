"""Vision analysis sensor — classifies camera frames into structured defect scores
using Gemini Flash Lite. The main agent LLM reads these scores as structured data
instead of interpreting raw images directly."""

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


def configure_vision(api_key: str, vision_model: str, redis_url: str):
    """Set vision config at boot from central config. Called by main.py."""
    global _api_key, _vision_model, _redis_url, _redis_client
    _api_key = api_key
    _vision_model = vision_model
    _redis_url = redis_url
    _redis_client = None  # Reset so next call picks up new URL
    logger.info(f"Vision sensor configured: model={vision_model}, api_key={'set' if api_key else 'MISSING'}")


def _get_redis():
    """Get or create module-level Redis client for reading whiteboard keys."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(_redis_url, decode_responses=True)
    return _redis_client


def _read_wb(r, key: str):
    """Read a whiteboard key, unwrapping JSON encoding.

    The whiteboard publishes via json.dumps(), so strings are stored with
    JSON quotes. We must json.loads() to get the actual value.
    """
    raw = r.get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw  # Already plain string (shouldn't happen, but safe)


_NOZZLE_PROMPT = """You are a 3D print quality inspector analyzing a NOZZLE CAMERA image (close-up of the print head and surface).

Score each defect 0.0 (absent) to 1.0 (clearly present). Be conservative — only score above 0.5 if confident.
If the image is blurry or unclear, set all defect scores low and normal high.

Also write a one-sentence description of what you see in plain language.

Respond with JSON only:
{"stringing": 0.0, "spaghetti": 0.0, "blob": 0.0, "warping": 0.0, "layer_shift": 0.0, "underextrusion": 0.0, "overextrusion": 0.0, "burn_marks": 0.0, "bed_adhesion_ok": 1.0, "normal": 1.0, "confidence": 0.8, "description": "Clean extrusion bead with good layer adhesion, no visible defects"}"""

_BUDDY_PROMPT = """You are a 3D print quality inspector analyzing a WIDE-ANGLE BED CAMERA image (overview of the entire build plate).

Score each defect 0.0 (absent) to 1.0 (clearly present). Focus on: spaghetti (filament in air), warping (corners lifting), detachment (print shifted or fallen). Be conservative.
If the image is blurry or unclear, set all defect scores low and normal high.

Respond with JSON only:
{"stringing": 0.0, "spaghetti": 0.0, "blob": 0.0, "warping": 0.0, "layer_shift": 0.0, "underextrusion": 0.0, "overextrusion": 0.0, "burn_marks": 0.0, "bed_adhesion_ok": 1.0, "normal": 1.0, "confidence": 0.8, "description": "Print attached to bed, no visible defects from wide angle"}"""


def _analyze_frame(frame_b64: str, prompt: str) -> dict | None:
    """Send a single frame to Gemini Flash Lite for analysis. Returns scores dict or None."""
    try:
        response = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": _vision_model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"}},
                        {"type": "text", "text": prompt},
                    ],
                }],
                "max_tokens": 200,
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
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end < 0:
            logger.warning(f"Vision: no JSON in response: {content[:100]}")
            return None

        return json.loads(content[start:end + 1])

    except Exception as e:
        logger.warning(f"Vision analysis failed: {type(e).__name__}: {e}")
        return None


def _scores_to_result(scores: dict, prefix: str) -> dict:
    """Convert raw scores dict to whiteboard keys with the given prefix."""
    result = {}
    for key in ("stringing", "spaghetti", "blob", "warping", "layer_shift",
                 "underextrusion", "overextrusion", "burn_marks", "bed_adhesion_ok",
                 "normal", "confidence"):
        val = scores.get(key)
        if val is not None:
            result[f"{prefix}.{key}"] = round(float(val), 2)

    if scores.get("description"):
        result[f"{prefix}.description"] = str(scores["description"])[:200]

    # Derive status from defect scores
    defect_scores = {k: v for k, v in scores.items()
                     if k not in ("normal", "confidence", "description", "bed_adhesion_ok")}
    max_defect = max(defect_scores.values()) if defect_scores else 0
    max_defect_name = max(defect_scores, key=defect_scores.get) if defect_scores else "none"

    if max_defect > 0.7:
        result[f"{prefix}.status"] = f"DEFECT:{max_defect_name}"
    elif max_defect > 0.4:
        result[f"{prefix}.status"] = f"POSSIBLE:{max_defect_name}"
    else:
        result[f"{prefix}.status"] = "NORMAL"

    return result


@tool(kind="sensor", refresh_hz=0.1, history_depth=5)
def read_vision_analysis() -> dict:
    """Analyze camera frames for print defects using Gemini Flash Lite.

    Checks nozzle camera and buddy camera. Publishes per-camera scores
    (vision.nozzle.*, vision.buddy.*) and a combined vision.status.
    Runs every ~10s. Only active during PRINTING/PREPARING/PAUSED phases.
    """
    r = _get_redis()

    # Heartbeat — always publish so dashboard can confirm sensor is alive
    r.set("vision.last_analysis_ts", str(time.time()), ex=30)

    # Only analyze during active print phases
    phase = _read_wb(r, "job.phase")
    if phase not in ("PRINTING", "PREPARING", "PAUSED"):
        logger.debug(f"Vision: skipping, phase is {phase}")
        return {}

    if not _api_key:
        logger.warning("Vision: API key not configured (configure_vision not called or key empty)")
        return {}

    # Read camera frames (whiteboard stores as json.dumps(b64_string), so _read_wb unwraps)
    nozzle_frame = _read_wb(r, "camera.nozzle_frame")
    buddy_frame = _read_wb(r, "camera.buddy1_frame")

    if nozzle_frame:
        logger.info(f"Vision: got nozzle frame, length={len(str(nozzle_frame))}, type={type(nozzle_frame).__name__}")
    if buddy_frame:
        logger.info(f"Vision: got buddy frame, length={len(str(buddy_frame))}, type={type(buddy_frame).__name__}")

    if not nozzle_frame and not buddy_frame:
        # Log available camera keys for debugging
        all_keys = [k for k in r.keys("camera.*") if not k.endswith(":history")]
        logger.warning(f"Vision: no camera frames available. Camera keys in Redis: {all_keys}")
        return {}

    result = {}
    all_statuses = []

    # Analyze nozzle camera (close-up: stringing, extrusion quality, blob)
    if nozzle_frame and isinstance(nozzle_frame, str):
        scores = _analyze_frame(nozzle_frame, _NOZZLE_PROMPT)
        if scores:
            nozzle_result = _scores_to_result(scores, "vision.nozzle")
            result.update(nozzle_result)
            all_statuses.append(nozzle_result.get("vision.nozzle.status", "NORMAL"))
            logger.info(f"Vision nozzle: {nozzle_result.get('vision.nozzle.status', '?')}")

    # Analyze buddy camera (wide-angle: spaghetti, detachment, warping)
    if buddy_frame and isinstance(buddy_frame, str):
        scores = _analyze_frame(buddy_frame, _BUDDY_PROMPT)
        if scores:
            buddy_result = _scores_to_result(scores, "vision.buddy")
            result.update(buddy_result)
            all_statuses.append(buddy_result.get("vision.buddy.status", "NORMAL"))
            logger.info(f"Vision buddy: {buddy_result.get('vision.buddy.status', '?')}")

    # Combined status: worst across all cameras
    if any(s.startswith("DEFECT:") for s in all_statuses):
        defects = [s for s in all_statuses if s.startswith("DEFECT:")]
        result["vision.status"] = defects[0]
    elif any(s.startswith("POSSIBLE:") for s in all_statuses):
        possibles = [s for s in all_statuses if s.startswith("POSSIBLE:")]
        result["vision.status"] = possibles[0]
    elif all_statuses:
        result["vision.status"] = "NORMAL"

    # Copy nozzle confidence as the primary confidence (backward compat)
    if "vision.nozzle.confidence" in result:
        result["vision.confidence"] = result["vision.nozzle.confidence"]
    if "vision.nozzle.description" in result:
        result["vision.description"] = result["vision.nozzle.description"]

    return result
