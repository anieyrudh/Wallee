"""Vision analysis sensor — classifies camera frames into structured defect scores
using Gemini Flash Lite. The main agent LLM reads these scores as structured data
instead of interpreting raw images directly."""

import json
import logging
import os

import httpx
import redis

from wallee.tools.decorator import tool

logger = logging.getLogger(__name__)

VISION_MODEL = os.environ.get("VISION_MODEL", "google/gemini-3.1-flash-lite-preview")

_redis_client = None


def _get_redis():
    """Get or create module-level Redis client for reading whiteboard keys."""
    global _redis_client
    if _redis_client is None:
        url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        _redis_client = redis.from_url(url, decode_responses=True)
    return _redis_client


_ANALYSIS_PROMPT = """You are a 3D print quality inspector analyzing a nozzle camera image.

Score each defect 0.0 (absent) to 1.0 (clearly present). Be conservative — only score above 0.5 if confident.
If the image is blurry or unclear, set all defect scores low and normal high.

Also write a one-sentence description of what you see in plain language.

Respond with JSON only:
{"stringing": 0.0, "spaghetti": 0.0, "blob": 0.0, "warping": 0.0, "layer_shift": 0.0, "underextrusion": 0.0, "overextrusion": 0.0, "burn_marks": 0.0, "bed_adhesion_ok": 1.0, "normal": 1.0, "confidence": 0.8, "description": "Clean extrusion bead with good layer adhesion, no visible defects"}"""


@tool(kind="sensor", refresh_hz=0.1, history_depth=5)
def read_vision_analysis() -> dict:
    """Analyze nozzle camera frame for print defects using Gemini Flash Lite.

    Publishes structured defect scores and a text description to whiteboard.
    Runs every ~10s. Only active during PRINTING/PREPARING/PAUSED phases.
    """
    r = _get_redis()

    # Only analyze during active print phases
    phase = r.get("job.phase")
    if phase not in ("PRINTING", "PREPARING", "PAUSED"):
        return {}

    # Read the latest nozzle camera frame (base64 JPEG)
    frame_b64 = r.get("camera.nozzle_frame")
    if not frame_b64:
        return {}

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return {}

    try:
        response = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": VISION_MODEL,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"}},
                        {"type": "text", "text": _ANALYSIS_PROMPT},
                    ],
                }],
                "max_tokens": 200,
                "temperature": 0.1,
                "stream": False,
            },
            timeout=15.0,
        )

        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end < 0:
            logger.warning("Vision analysis returned no JSON")
            return {}

        scores = json.loads(content[start:end + 1])

        result = {}
        for key in ("stringing", "spaghetti", "blob", "warping", "layer_shift",
                     "underextrusion", "overextrusion", "burn_marks", "bed_adhesion_ok",
                     "normal", "confidence"):
            val = scores.get(key)
            if val is not None:
                result[f"vision.{key}"] = round(float(val), 2)

        if scores.get("description"):
            result["vision.description"] = str(scores["description"])[:200]

        # Derive overall status from defect scores
        defect_scores = {k: v for k, v in scores.items()
                         if k not in ("normal", "confidence", "description", "bed_adhesion_ok")}
        max_defect = max(defect_scores.values()) if defect_scores else 0
        max_defect_name = max(defect_scores, key=defect_scores.get) if defect_scores else "none"

        if max_defect > 0.7:
            result["vision.status"] = f"DEFECT:{max_defect_name}"
        elif max_defect > 0.4:
            result["vision.status"] = f"POSSIBLE:{max_defect_name}"
        else:
            result["vision.status"] = "NORMAL"

        return result

    except Exception as e:
        logger.warning(f"Vision analysis failed: {e}")
        return {}
