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

# Previous frames for temporal comparison (camera_type → base64 string)
_prev_frames: dict[str, str] = {}

# Camera geometry descriptions — helps the vision LLM understand what it's looking at
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
You are inspecting a working 3D printer. Minor residue, small ooze, slight
discoloration, and thin wisps are NORMAL artifacts of the printing process.
Do not flag them.

Only flag something as a defect if it would:
- Affect print quality (visible stringing across the part, layer gaps, surface roughness)
- Risk hardware damage (blob growing toward heater block, filament wrapping around hotend)
- Indicate print failure (part detached from bed, spaghetti, severe warping lifting corners)

A small blob sitting on the nozzle tip is not a defect. A blob growing and
engulfing the heater block IS a defect. Calibrate accordingly.

Write a one-sentence description of ONLY what you physically see. No diagnosis, no cause analysis, no interpretation. Good: "White fuzzy residue on overhangs, rough bumpy texture on top surface." Bad: "Moisture in filament causing steam bubbles during extrusion."

CRITICAL: Your numerical scores and your text description MUST agree. If you describe something concerning in the description, the corresponding defect score MUST be above 0.3. If all scores are below 0.3, the description must say the print looks normal. Do not describe "detachment" or "failure" while scoring spaghetti at 0.0.

If two frames are provided, compare them. Look for:
- Filament accumulation growing between frames = spaghetti (print failing)
- Defect getting worse between frames = escalate urgency
- Defect stable or improving between frames = note but lower urgency

Respond with JSON only:
{"stringing": 0.0, "spaghetti": 0.0, "blob": 0.0, "warping": 0.0, "layer_shift": 0.0, "underextrusion": 0.0, "overextrusion": 0.0, "burn_marks": 0.0, "bed_adhesion_ok": 1.0, "normal": 1.0, "confidence": 0.8, "description": "Clean bead, smooth top surface, no threads or blobs visible"}"""

_BUDDY_PROMPT = """You are a 3D print quality inspector analyzing a WIDE-ANGLE BED CAMERA image (overview of the entire build plate).

Score each defect 0.0 (absent) to 1.0 (clearly present). Focus on: spaghetti (filament in air), warping (corners lifting), detachment (print shifted or fallen). Be conservative.
If the image is blurry or unclear, set all defect scores low and normal high.
You are inspecting a working 3D printer. Minor residue, small ooze, slight
discoloration, and thin wisps are NORMAL artifacts of the printing process.
Do not flag them.

Only flag something as a defect if it would:
- Affect print quality (visible stringing across the part, layer gaps, surface roughness)
- Risk hardware damage (blob growing toward heater block, filament wrapping around hotend)
- Indicate print failure (part detached from bed, spaghetti, severe warping lifting corners)

A small blob sitting on the nozzle tip is not a defect. A blob growing and
engulfing the heater block IS a defect. Calibrate accordingly.

Write a one-sentence description of ONLY what you physically see. No diagnosis, no cause analysis, no interpretation.

CRITICAL: Your numerical scores and your text description MUST agree. If you describe something concerning, the corresponding defect score MUST be above 0.3. If all scores are below 0.3, the description must say the print looks normal.

If two frames are provided, compare them. Look for:
- Filament accumulation growing between frames = spaghetti (print failing)
- Defect getting worse between frames = escalate urgency
- Defect stable or improving between frames = note but lower urgency

Respond with JSON only:
{"stringing": 0.0, "spaghetti": 0.0, "blob": 0.0, "warping": 0.0, "layer_shift": 0.0, "underextrusion": 0.0, "overextrusion": 0.0, "burn_marks": 0.0, "bed_adhesion_ok": 1.0, "normal": 1.0, "confidence": 0.8, "description": "Object centered on bed, no loose filament, corners flat"}"""


def _prompt_with_phase(prompt: str, phase: str | None) -> str:
    """Append current print phase context to the vision prompt."""
    if not phase:
        return prompt
    return f"{prompt}\n\nCurrent print phase: {phase}. Calibrate your judgment to what is normal for this phase."


def _analyze_frame(frame_b64: str, prompt: str, prev_frame: str | None = None) -> dict | None:
    """Send frame(s) to Gemini Flash Lite for analysis. Returns scores dict or None.

    If prev_frame is provided, sends both frames for temporal comparison
    so the LLM can detect changes (growing spaghetti, worsening defects).
    """
    # Build content blocks with optional temporal comparison
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
    buddy1_frame = _read_wb(r, "camera.buddy1_frame")
    buddy2_frame = _read_wb(r, "camera.buddy2_frame")

    if nozzle_frame:
        logger.info(f"Vision: got nozzle frame, length={len(str(nozzle_frame))}, type={type(nozzle_frame).__name__}")
    if buddy1_frame:
        logger.info(f"Vision: got buddy1 frame, length={len(str(buddy1_frame))}, type={type(buddy1_frame).__name__}")
    if buddy2_frame:
        logger.info(f"Vision: got buddy2 frame, length={len(str(buddy2_frame))}, type={type(buddy2_frame).__name__}")

    if not nozzle_frame and not buddy1_frame and not buddy2_frame:
        # Log available camera keys for debugging
        all_keys = [k for k in r.keys("camera.*") if not k.endswith(":history")]
        logger.warning(f"Vision: no camera frames available. Camera keys in Redis: {all_keys}")
        return {}

    result = {}
    all_statuses = []

    # Analyze nozzle camera (close-up: stringing, extrusion quality, blob)
    if nozzle_frame and isinstance(nozzle_frame, str):
        nozzle_prompt = f"Camera: {CAMERA_DESCRIPTIONS['nozzle']}\n\n{_prompt_with_phase(_NOZZLE_PROMPT, phase)}"
        prev_nozzle = _prev_frames.get("nozzle")
        _prev_frames["nozzle"] = nozzle_frame
        scores = _analyze_frame(nozzle_frame, nozzle_prompt, prev_frame=prev_nozzle)
        if scores:
            nozzle_result = _scores_to_result(scores, "vision.nozzle")
            result.update(nozzle_result)
            all_statuses.append(nozzle_result.get("vision.nozzle.status", "NORMAL"))
            logger.info(f"Vision nozzle: {nozzle_result.get('vision.nozzle.status', '?')}")

    # Analyze buddy cameras (wide-angle: spaghetti, detachment, warping)
    for buddy_key, buddy_frame, buddy_prefix in [
        ("buddy1", buddy1_frame, "vision.buddy"),
        ("buddy2", buddy2_frame, "vision.buddy2"),
    ]:
        if buddy_frame and isinstance(buddy_frame, str):
            buddy_prompt = f"Camera: {CAMERA_DESCRIPTIONS['buddy']}\n\n{_prompt_with_phase(_BUDDY_PROMPT, phase)}"
            prev_buddy = _prev_frames.get(buddy_key)
            _prev_frames[buddy_key] = buddy_frame
            scores = _analyze_frame(buddy_frame, buddy_prompt, prev_frame=prev_buddy)
            if scores:
                buddy_result = _scores_to_result(scores, buddy_prefix)
                result.update(buddy_result)
                all_statuses.append(buddy_result.get(f"{buddy_prefix}.status", "NORMAL"))
                logger.info(f"Vision {buddy_key}: {buddy_result.get(f'{buddy_prefix}.status', '?')}")

    # Combined status: worst across all cameras
    if any(s.startswith("DEFECT:") for s in all_statuses):
        defects = [s for s in all_statuses if s.startswith("DEFECT:")]
        result["vision.status"] = defects[0]
    elif any(s.startswith("POSSIBLE:") for s in all_statuses):
        possibles = [s for s in all_statuses if s.startswith("POSSIBLE:")]
        result["vision.status"] = possibles[0]
    elif all_statuses:
        result["vision.status"] = "NORMAL"

    # Combined description from all cameras
    descriptions = []
    nozzle_desc = result.get("vision.nozzle.description", "")
    buddy_desc = result.get("vision.buddy.description", "")
    buddy2_desc = result.get("vision.buddy2.description", "")
    if nozzle_desc:
        descriptions.append(f"Nozzle: {nozzle_desc}")
    if buddy_desc:
        descriptions.append(f"Bed: {buddy_desc}")
    if buddy2_desc:
        descriptions.append(f"Bed2: {buddy2_desc}")
    result["vision.description"] = " | ".join(descriptions) if descriptions else "No visual data"

    # Primary confidence from nozzle (backward compat)
    if "vision.nozzle.confidence" in result:
        result["vision.confidence"] = result["vision.nozzle.confidence"]

    # Cross-check: if description contains concerning keywords but scores are low, flag inconsistency
    combined_desc = result.get("vision.description", "").lower()
    concerning_words = ["detach", "spaghetti", "blob", "fail", "loose", "empty bed", "no adhesion"]
    max_defect_val = 0.0
    for k, v in result.items():
        if k.startswith("vision.") and k.endswith((".stringing", ".spaghetti", ".blob", ".warping",
                                                     ".layer_shift", ".underextrusion", ".overextrusion",
                                                     ".burn_marks")):
            max_defect_val = max(max_defect_val, float(v))

    matched_words = [w for w in concerning_words if w in combined_desc]
    if matched_words and max_defect_val < 0.5:
        logger.warning(f"Vision inconsistency: description mentions {matched_words} but max defect score is {max_defect_val:.2f}. Re-deriving from description.")

        # Trust the description over the scores — the LLM saw something but scored it wrong
        word_to_defect = [
            ("detach", "spaghetti"), ("spaghetti", "spaghetti"),
            ("blob", "blob"), ("string", "stringing"),
            ("warp", "warping"), ("shift", "layer_shift"),
            ("loose", "spaghetti"), ("empty bed", "spaghetti"),
            ("no adhesion", "spaghetti"), ("fail", "spaghetti"),
        ]
        re_derived = False
        for word, defect in word_to_defect:
            if word in combined_desc:
                # Bump the score to at least 0.5 across all camera prefixes that have results
                for prefix in ("vision.nozzle", "vision.buddy", "vision.buddy2"):
                    key = f"{prefix}.{defect}"
                    if key in result:
                        result[key] = max(result[key], 0.5)
                        re_derived = True
                # Also set the top-level defect key if nozzle has it
                nk = f"vision.nozzle.{defect}"
                if nk in result:
                    result[nk] = max(result[nk], 0.5)

        # Re-derive combined status from updated scores
        worst_status = result.get("vision.status", "NORMAL")
        result["vision.status"] = f"INCONSISTENT:{worst_status}"
        result["vision.confidence"] = 0.5  # Medium confidence — signals disagree

    # Hysteresis: if we detected a defect last cycle and current reading is ambiguous,
    # maintain the previous assessment with reduced confidence instead of flipping to NORMAL.
    # This prevents flip-flopping on borderline readings (e.g., blob 0.65 → 0.3 → 0.65).
    current_status = result.get("vision.status", "NORMAL")
    if current_status == "NORMAL":
        prev_status = _read_wb(r, "vision.status") or "NORMAL"
        if "DEFECT" in str(prev_status) or "POSSIBLE" in str(prev_status):
            current_normal = result.get("vision.nozzle.normal", result.get("vision.normal", 0))
            if current_normal < 0.8:  # Not clearly normal
                prev_defect = str(prev_status).split(":")[-1] if ":" in str(prev_status) else "unknown"
                result["vision.status"] = f"FADING:{prev_defect}"
                result["vision.confidence"] = max(result.get("vision.confidence", 0), 0.4)
                logger.info(f"Vision hysteresis: maintaining {prev_defect} detection with reduced confidence (normal={current_normal})")

    return result
