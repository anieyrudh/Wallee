"""Tool registry — discovers device packs, registers sensor and actuator tools."""

import importlib
import inspect
import logging
import threading
import time
from pathlib import Path
from typing import Any

from wallee.whiteboard.client import Whiteboard

logger = logging.getLogger(__name__)


class RegisteredTool:
    """A tool registered in the system with its metadata and callable."""

    def __init__(self, name: str, fn, meta: dict, pack_name: str):
        self.name = name
        self.fn = fn
        self.meta = meta
        self.pack_name = pack_name

    @property
    def kind(self) -> str:
        return self.meta["kind"]

    @property
    def requires_approval(self) -> bool:
        return self.meta.get("requires_approval", False)

    @property
    def max_proposal_age_ms(self) -> int:
        return self.meta.get("max_proposal_age_ms", 30000)

    @property
    def device_group(self) -> str:
        return self.pack_name

    @property
    def has_precheck(self) -> bool:
        return callable(self.meta.get("precheck_fn"))

    def precheck(self, whiteboard: Whiteboard | None = None, **kwargs):
        """Run the tool's side-effect-free precheck if one is defined."""
        precheck_fn = self.meta.get("precheck_fn")
        if not callable(precheck_fn):
            return None
        return precheck_fn(whiteboard=whiteboard, **kwargs) if whiteboard else precheck_fn(**kwargs)

    def execute(self, whiteboard: Whiteboard | None = None, **kwargs):
        """Execute the tool function."""
        return self.fn(whiteboard=whiteboard, **kwargs) if whiteboard else self.fn(**kwargs)


class ToolRegistry:
    """Discovers and manages all tools from device packs and builtins."""

    def __init__(self):
        self._tools: dict[str, RegisteredTool] = {}
        self._sensor_threads: list[threading.Thread] = []
        self._running = False

    def register(self, name: str, fn, meta: dict, pack_name: str):
        """Register a single tool."""
        if name in self._tools:
            logger.warning(f"Tool '{name}' already registered, overwriting")
        self._tools[name] = RegisteredTool(name, fn, meta, pack_name)
        logger.info(f"Registered {meta['kind']} tool: {name} (pack: {pack_name})")

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def list_all(self) -> list[RegisteredTool]:
        return list(self._tools.values())

    def list_sensors(self) -> list[RegisteredTool]:
        return [t for t in self._tools.values() if t.kind == "sensor"]

    def list_actuators(self) -> list[RegisteredTool]:
        return [t for t in self._tools.values() if t.kind == "actuator"]

    def list_for_llm(self) -> list[dict]:
        """Return tool descriptions formatted for the LLM prompt."""
        result = []
        for t in self._tools.values():
            if t.kind == "actuator":
                result.append({
                    "name": t.name,
                    "description": t.meta["doc"],
                    "requires_approval": t.requires_approval,
                })
        return result

    def load_pack(self, pack_module_path: str):
        """Load tools from a device pack module path (e.g. 'wallee.device_packs.host_pi')."""
        try:
            pack_mod = importlib.import_module(pack_module_path)
        except ImportError as e:
            logger.error(f"Failed to import pack {pack_module_path}: {e}")
            return

        pack_meta = getattr(pack_mod, "PACK_META", {})
        pack_name = pack_meta.get("name", pack_module_path.split(".")[-1])

        # Look for sensors.py and actuators.py in the pack
        for sub in ("sensors", "actuators"):
            try:
                sub_mod = importlib.import_module(f"{pack_module_path}.{sub}")
            except ImportError:
                continue

            # Find all decorated functions/methods in the module
            for attr_name in dir(sub_mod):
                obj = getattr(sub_mod, attr_name)
                if callable(obj) and hasattr(obj, "_tool_meta"):
                    self.register(obj._tool_meta["name"], obj, obj._tool_meta, pack_name)

    def load_builtins(self):
        """Load all built-in tools from wallee.tools.builtins."""
        builtin_modules = [
            "wallee.tools.builtins.trends",
            "wallee.tools.builtins.differential",
            "wallee.tools.builtins.sensor_history",
            "wallee.tools.builtins.call_human_tool",
            "wallee.tools.builtins.discover",
            "wallee.tools.builtins.remember",
            "wallee.tools.builtins.web_search",
            "wallee.tools.builtins.lookup_issue",
        ]
        for mod_path in builtin_modules:
            try:
                mod = importlib.import_module(mod_path)
                for attr_name in dir(mod):
                    obj = getattr(mod, attr_name)
                    if callable(obj) and hasattr(obj, "_tool_meta"):
                        self.register(obj._tool_meta["name"], obj, obj._tool_meta, "builtin")
            except ImportError as e:
                logger.error(f"Failed to load builtin {mod_path}: {e}")

    def start_sensors(self, whiteboard: Whiteboard, wake_fn=None):
        """Start background threads for all sensor tools."""
        self._running = True
        self._wake_fn = wake_fn
        for tool in self.list_sensors():
            t = threading.Thread(
                target=self._sensor_loop,
                args=(tool, whiteboard),
                daemon=True,
                name=f"sensor-{tool.name}",
            )
            t.start()
            self._sensor_threads.append(t)
            logger.info(f"Started sensor thread: {tool.name} at {tool.meta['refresh_hz']}Hz")

    def _sensor_loop(self, tool: RegisteredTool, whiteboard: Whiteboard):
        """Background loop for a sensor tool."""
        refresh_hz = tool.meta["refresh_hz"]
        interval = 1.0 / refresh_hz
        ttl_s = tool.meta["ttl_ms"] / 1000 if tool.meta["ttl_ms"] else None
        # Convert TTL to int seconds for Redis (minimum 1)
        ttl_redis = max(1, int(ttl_s)) if ttl_s else None
        history_depth = tool.meta["history_depth"]
        prev_printer_state = None
        prev_fsensor_state = None

        while self._running:
            try:
                result = tool.fn()
                if isinstance(result, dict):
                    payload = {key: value for key, value in result.items() if not key.startswith("error")}
                    if payload:
                        whiteboard.publish_many(
                            payload,
                            ttl=ttl_redis,
                            history_depth=history_depth,
                        )
                    # Wake agent on printer.state change
                    new_state = payload.get("printer.state")
                    if new_state is not None and self._wake_fn:
                        if prev_printer_state is not None and new_state != prev_printer_state:
                            logger.info(f"Printer state changed: {prev_printer_state} → {new_state}, waking agent")
                            self._wake_fn()
                        prev_printer_state = new_state
                    # Wake agent on filament sensor state change
                    new_fsensor = payload.get("printer.fsensor_state")
                    if new_fsensor is not None and self._wake_fn:
                        if prev_fsensor_state is not None and new_fsensor != prev_fsensor_state:
                            logger.info(f"Filament sensor changed: {prev_fsensor_state} → {new_fsensor}, waking agent")
                            self._wake_fn()
                        prev_fsensor_state = new_fsensor
            except Exception as e:
                logger.error(f"Sensor {tool.name} error: {e}")
            time.sleep(interval)

    def stop_sensors(self):
        """Signal sensor threads to stop."""
        self._running = False
