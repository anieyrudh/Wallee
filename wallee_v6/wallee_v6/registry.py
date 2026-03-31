"""Pack discovery and registry."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
import socket
from typing import Iterable

import yaml

from .config import Config
from .models import PackManifest
from .packs.base import BasePack


@dataclass(slots=True)
class LoadedPack:
    """Bundle of a manifest and its instantiated pack class."""

    manifest: PackManifest
    instance: BasePack


class PackRegistry:
    """Load and expose device packs.

    The registry intentionally implements only a few discovery mechanisms:
    simulation, explicit enablement, environment-variable toggles, simple serial
    globbing, and basic TCP probes.  Going wider than that on day one would add
    a lot of complexity before the project has enough real hardware to justify
    it.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._packs: dict[str, LoadedPack] = {}

    def load(self) -> None:
        """Discover and instantiate packs."""
        for manifest_path in sorted(self.config.pack_root.glob("*/manifest.yaml")):
            manifest = self._load_manifest(manifest_path)
            if not self._should_enable(manifest):
                continue
            pack = self._instantiate(manifest)
            self._packs[manifest.pack_id] = LoadedPack(manifest=manifest, instance=pack)

    def _load_manifest(self, manifest_path: Path) -> PackManifest:
        with manifest_path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        return PackManifest.model_validate(data)

    def _instantiate(self, manifest: PackManifest) -> BasePack:
        module_name, symbol = manifest.python_entrypoint.split(":")
        module = import_module(module_name)
        pack_cls = getattr(module, symbol)
        pack = pack_cls(manifest)
        if not isinstance(pack, BasePack):
            raise TypeError(f"{manifest.python_entrypoint} did not create a BasePack")
        return pack

    def _should_enable(self, manifest: PackManifest) -> bool:
        detection = manifest.detection or {}

        if self.config.simulation_mode and detection.get("simulate", False):
            return True

        if manifest.pack_id in self.config.enabled_packs:
            return True

        env_var = detection.get("env_var")
        if isinstance(env_var, str):
            env_names = [env_var]
        elif isinstance(env_var, list):
            env_names = [name for name in env_var if isinstance(name, str)]
        else:
            env_names = []
        if any(bool(__import__("os").environ.get(name)) for name in env_names):
            return True

        serial_glob = detection.get("serial_glob")
        if serial_glob:
            if list(Path("/").glob(serial_glob.lstrip("/"))):
                return True

        tcp_probe = detection.get("tcp_probe")
        if isinstance(tcp_probe, dict):
            host = tcp_probe.get("host")
            port = tcp_probe.get("port")
            timeout = float(tcp_probe.get("timeout_s", 0.3))
            if host and port and self._tcp_probe(str(host), int(port), timeout):
                return True

        return False

    def _tcp_probe(self, host: str, port: int, timeout_s: float) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout_s):
                return True
        except OSError:
            return False

    def all_packs(self) -> list[BasePack]:
        """Return all instantiated packs."""
        return [loaded.instance for loaded in self._packs.values()]

    def get(self, pack_id: str) -> BasePack:
        """Return one pack by ID."""
        return self._packs[pack_id].instance

    def manifests(self) -> list[PackManifest]:
        """Return manifests for all loaded packs."""
        return [loaded.manifest for loaded in self._packs.values()]

    def publish_all_raw_state(self, whiteboard) -> None:
        """Ask every pack to publish its current raw state."""
        for pack in self.all_packs():
            pack.publish_raw_state(whiteboard)

    def close_all(self) -> None:
        """Release resources held by loaded packs.

        The registry centralises teardown so callers do not need pack-specific
        cleanup logic.  This keeps the runtime shallow at the top level while
        letting packs own any sockets or device handles they create.
        """
        for pack in self.all_packs():
            pack.close()
