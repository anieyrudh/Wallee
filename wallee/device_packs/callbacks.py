"""Generic device-pack callbacks for agent and safety integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SafetyProfile:
    """Device-specific safety wiring for the generic safety kernel."""

    control_host: str = ""
    control_api_key: str = ""
    primary_stop: dict = field(default_factory=dict)
    fallback_stop: dict = field(default_factory=dict)
    fault_monitors: list[dict] = field(default_factory=list)


class DevicePackCallbacks:
    """Hooks that device packs can register for agent lifecycle events."""

    def on_job_start(self, state: dict, whiteboard, data_dir: Path | None = None):
        """Called when a new job starts. May publish metadata to the whiteboard."""

    def on_idle_check(self, state: dict, whiteboard):
        """Called during IDLE. Return an auto-proposal dict or None."""
        return None

    def get_job_context(self, state: dict, whiteboard) -> str:
        """Return extra job-context text for the prompt-visible job notes."""
        return ""

    def get_safety_profile(self) -> SafetyProfile | None:
        """Return device-specific safety configuration for the generic kernel."""
        return None


class CompositeDeviceCallbacks(DevicePackCallbacks):
    """Aggregate callbacks from all loaded device packs."""

    def __init__(self):
        self._callbacks: list[DevicePackCallbacks] = []

    def add(self, callbacks: DevicePackCallbacks):
        self._callbacks.append(callbacks)

    def on_job_start(self, state: dict, whiteboard, data_dir: Path | None = None):
        for callbacks in self._callbacks:
            callbacks.on_job_start(state, whiteboard, data_dir=data_dir)

    def on_idle_check(self, state: dict, whiteboard):
        for callbacks in self._callbacks:
            proposal = callbacks.on_idle_check(state, whiteboard)
            if proposal:
                return proposal
        return None

    def get_job_context(self, state: dict, whiteboard) -> str:
        parts = []
        for callbacks in self._callbacks:
            text = callbacks.get_job_context(state, whiteboard).strip()
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    def get_safety_profile(self) -> SafetyProfile | None:
        merged = SafetyProfile()
        found = False
        for callbacks in self._callbacks:
            profile = callbacks.get_safety_profile()
            if profile is None:
                continue
            found = True
            if profile.control_host and not merged.control_host:
                merged.control_host = profile.control_host
            if profile.control_api_key and not merged.control_api_key:
                merged.control_api_key = profile.control_api_key
            if profile.primary_stop and not merged.primary_stop:
                merged.primary_stop = dict(profile.primary_stop)
            if profile.fallback_stop and not merged.fallback_stop:
                merged.fallback_stop = dict(profile.fallback_stop)
            if profile.fault_monitors:
                merged.fault_monitors.extend(profile.fault_monitors)
        return merged if found else None
