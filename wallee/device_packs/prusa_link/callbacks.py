"""Prusa-specific callbacks that plug hardware behavior into the generic core."""

from __future__ import annotations

import json
import os

from wallee.device_packs.callbacks import DevicePackCallbacks, SafetyProfile
from wallee.device_packs.prusa_link.sensors import publish_job_metadata


class PrusaCallbacks(DevicePackCallbacks):
    """Pack-specific hooks for job metadata, idle autostart, and safety config."""

    def on_job_start(self, state: dict, whiteboard, data_dir=None):
        publish_job_metadata(whiteboard=whiteboard)

    def on_idle_check(self, state: dict, whiteboard):
        queue_raw = whiteboard.read("print.queue")
        if not queue_raw:
            return None

        queue_items = json.loads(queue_raw) if isinstance(queue_raw, str) else queue_raw
        if not isinstance(queue_items, list) or not queue_items:
            return None

        ready_temp_c = float(os.environ.get("QUEUE_AUTOSTART_READY_TEMP_C", "35"))
        surface_temp = float(state.get("printer.temp_bed") or 100)
        if surface_temp >= ready_temp_c:
            return None

        next_file = queue_items[0]
        remaining = queue_items[1:]
        return {
            "tool": "start_print",
            "params": {"file_path": next_file},
            "observation": f"Queue has {len(queue_items)} prints and the chamber surface is ready.",
            "reasoning": f"Auto-starting queued file: {next_file}",
            "state_updates": {"print.queue": remaining},
            "state_ttl": 86400,
        }

    def get_job_context(self, state: dict, whiteboard) -> str:
        filename = state.get("job.filename") or whiteboard.read("job.filename")
        material = state.get("job.material") or whiteboard.read("job.material")
        parts = []
        if filename:
            parts.append(f"Filename: {filename}")
        if material:
            parts.append(f"Material: {material}")
        return "\n".join(parts)

    def get_safety_profile(self) -> SafetyProfile | None:
        host = os.environ.get("PRUSALINK_HOST", "").strip()
        if not host:
            return None
        return SafetyProfile(
            control_host=host,
            control_api_key=os.environ.get("PRUSALINK_API_KEY", "").strip(),
            primary_stop={"method": "POST", "path": "/api/v1/gcode", "json": {"command": "M25"}},
            fallback_stop={"method": "DELETE", "path": "/api/v1/job"},
            fault_monitors=[
                {"key": "printer.oc_nozzle", "label": "heater channel"},
                {"key": "printer.oc_input", "label": "input power"},
            ],
        )


CALLBACKS = PrusaCallbacks()
