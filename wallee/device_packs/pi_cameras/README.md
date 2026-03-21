# Pi Cameras Device Pack

Provides the reference deployment's camera capture and vision-analysis pipeline. The capture publishers run locally, while the vision-analysis publisher sends frames to a separate multimodal model and republishes structured scores.

## Interface

- Primary camera: local HTTP snapshot endpoint from `ustreamer`
- Auxiliary cameras: RTSP streams discovered on the LAN by MAC prefix
- Vision analysis: OpenRouter multimodal call using the configured `VISION_MODEL`

## Sensor publishers

| Sensor publisher | Keys | Rate | Description |
|---|---|---:|---|
| `read_nozzle_camera` | `camera.nozzle_frame`, `camera.nozzle_frame_size`, `camera.nozzle_status`, `camera.nozzle_port` | `1.0 Hz` | Primary close-up camera snapshot |
| `read_buddy_cameras` | `camera.buddy_count`, `camera.buddy1_frame`, `camera.buddy1_frame_size`, `camera.buddy1_status`, `camera.buddy1_ip`, `camera.buddy2_frame`, `camera.buddy2_frame_size`, `camera.buddy2_status`, `camera.buddy2_ip`, `camera.buddy3_status` | `0.1 Hz` | Auxiliary camera snapshots discovered over the LAN |
| `read_vision_analysis` | `vision.nozzle.*`, `vision.buddy.*`, `vision.buddy2.*`, `vision.status`, `vision.confidence`, `vision.last_analysis_ts` | `0.1 Hz` | Structured defect scoring over current and previous frames |

## Vision analysis behavior

The vision-analysis publisher:

- uses camera-geometry context for the primary and auxiliary views
- compares previous and current frames to detect worsening or improving defects
- is phase-aware and only runs during `PRINTING`, `PREPARING`, or `PAUSED`
- applies hysteresis by emitting `FADING:*` when a previous defect is not yet clearly resolved
- publishes scores for `normal`, `stringing`, `spaghetti`, `blob`, `warping`, `underextrusion`, `overextrusion`, `layer_shift`, `bed_adhesion_ok`, `burn_marks`, and `confidence`

## Setup

- Run a local snapshot service such as `ustreamer` for the primary camera
- Ensure the Wallee host can reach auxiliary RTSP cameras on the LAN
- Set `VISION_MODEL` and `OPENROUTER_API_KEY` so `read_vision_analysis` can call OpenRouter

## Notes

- Camera capture publishers are background sensors; the LLM never requests frames directly.
- The vision-analysis publisher writes only structured scores and statuses, not free-form descriptions.
