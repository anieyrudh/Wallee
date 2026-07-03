"""Per-run artifact archive, live-view symlinks, and run-log tee."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys

from .artifacts import _utc_now_iso
from .config import Config


class _TeeTextStream:
    """Mirror runtime output to both the current console stream and a per-run log."""

    def __init__(self, *streams) -> None:
        self._streams = streams
        self.encoding = getattr(streams[0], "encoding", "utf-8") if streams else "utf-8"

    def write(self, data: str) -> int:
        for stream in self._streams:
            stream.write(data)
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()

    def isatty(self) -> bool:
        return False


class _RuntimeArchive:
    """Manage append-only per-run artifacts while preserving live inspection paths."""

    def __init__(self, config: Config, *, repo_root: Path) -> None:
        self.config = config
        self.repo_root = repo_root
        self.started_at = datetime.now(timezone.utc)
        self.run_id = self.started_at.strftime("%Y-%m-%dT%H%M%SZ") + f"-pid{os.getpid()}"
        self.runs_root = config.data_dir / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.run_dir = self.runs_root / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_cycles_dir = self.run_dir / "runtime_cycles"
        self.runtime_cycles_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_logs_dir = self.run_dir / "runtime_logs"
        self.runtime_logs_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_state_history_dir = self.run_dir / "runtime_state_history"
        self.runtime_state_history_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_state_current_path = self.run_dir / "runtime_state.json"
        self.vision_bundle_root = self.run_dir / "vision_bundle"
        self.vision_bundle_root.mkdir(parents=True, exist_ok=True)
        self._copied_refs: set[str] = set()
        self._job_scope_tokens: set[str] = set()
        self._write_run_meta()
        self._refresh_live_views()

    def _write_run_meta(self) -> None:
        payload = {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
            "pid": os.getpid(),
            "data_dir": str(self.config.data_dir),
            "repo_root": str(self.repo_root),
            "remote_code": str(self.repo_root),
            "run_dir": str(self.run_dir),
            "job_scope_tokens": sorted(self._job_scope_tokens),
        }
        (self.run_dir / "run_meta.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _archive_existing_live_path(self, path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        if path.is_symlink():
            path.unlink()
            return
        legacy_root = self.runs_root / f"legacy-{path.name}-{self.started_at.strftime('%Y-%m-%dT%H%M%SZ')}"
        legacy_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(legacy_root / path.name))

    def _replace_live_symlink(self, live_path: Path, target: Path) -> None:
        self._archive_existing_live_path(live_path)
        live_path.symlink_to(target, target_is_directory=target.is_dir())

    def _refresh_live_views(self) -> None:
        self._replace_live_symlink(self.config.data_dir / "runtime_cycles", self.runtime_cycles_dir)
        self._replace_live_symlink(self.config.data_dir / "runtime_logs", self.runtime_logs_dir)
        current_run = self.config.data_dir / "current_run"
        self._replace_live_symlink(current_run, self.run_dir)
        latest_run_path = self.config.data_dir / "latest_run.json"
        latest_run_path.write_text(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "run_dir": str(self.run_dir),
                    "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def observe_job_scope(self, value: object) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        token = value.strip()
        if token in self._job_scope_tokens:
            return
        self._job_scope_tokens.add(token)
        alias_root = self.runs_root / "by_job_scope" / _safe_slug(token)
        alias_root.mkdir(parents=True, exist_ok=True)
        alias_path = alias_root / self.run_id
        if alias_path.exists() or alias_path.is_symlink():
            alias_path.unlink()
        alias_path.symlink_to(self.run_dir, target_is_directory=True)
        self._write_run_meta()

    def runtime_log_path(self) -> Path:
        return self.runtime_logs_dir / f"continuous-runtime-{self.started_at.strftime('%Y-%m-%dT%H%M%SZ')}-pid{os.getpid()}.log"

    def write_runtime_state_history(self, *, cycle_index: int, phase: str, payload: dict[str, object]) -> Path:
        ts = payload.get("ts") if isinstance(payload.get("ts"), str) else _utc_now_iso()
        stem = str(ts).replace(":", "").replace("+00:00", "Z")
        out_path = self.runtime_state_history_dir / f"{stem}-cycle-{cycle_index + 1:04d}-{_safe_slug(phase)}.json"
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        self.runtime_state_current_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return out_path

    def _resolve_ref_path(self, ref: str) -> Path | None:
        raw = Path(ref)
        candidates = [raw] if raw.is_absolute() else [self.repo_root / raw, self.config.data_dir / raw]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _copy_ref(self, *, ref: str | None, kind: str) -> str | None:
        if not isinstance(ref, str) or not ref.strip():
            return None
        normalized = ref.strip()
        source_path = self._resolve_ref_path(normalized)
        if source_path is None:
            return None
        destination_dir = self.vision_bundle_root / kind
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / source_path.name
        if normalized not in self._copied_refs:
            shutil.copy2(source_path, destination)
            self._copied_refs.add(normalized)
        return str(destination)

    def capture_referenced_artifacts(self, *, decision_payload: dict[str, object], post_execution_payload: dict[str, object]) -> dict[str, list[str]]:
        refs: dict[str, list[str]] = {"observations": [], "replays": [], "frames": []}
        seen = {key: set() for key in refs}
        for payload in (decision_payload, post_execution_payload):
            facts = payload.get("facts")
            if not isinstance(facts, dict):
                continue
            observation_ref = facts.get("printer_1.vision_observation_ref")
            frame_ref = facts.get("printer_1.vision_frame_ref")
            for kind, value in (("observations", observation_ref), ("frames", frame_ref)):
                copied = self._copy_ref(ref=value if isinstance(value, str) else None, kind=kind)
                if copied and copied not in seen[kind]:
                    seen[kind].add(copied)
                    refs[kind].append(copied)
            if isinstance(observation_ref, str) and observation_ref:
                replay_ref = observation_ref.replace("/observations/", "/replays/")
                copied_replay = self._copy_ref(ref=replay_ref, kind="replays")
                if copied_replay and copied_replay not in seen["replays"]:
                    seen["replays"].add(copied_replay)
                    refs["replays"].append(copied_replay)
        manifest_path = self.run_dir / "vision_bundle" / "manifest.json"
        manifest_payload = {
            "run_id": self.run_id,
            "copied_at": _utc_now_iso(),
            "artifacts": refs,
        }
        manifest_path.write_text(json.dumps(manifest_payload, indent=2, sort_keys=True), encoding="utf-8")
        return refs


def _safe_slug(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value.strip())
    parts = [part for part in cleaned.split("-") if part]
    return "-".join(parts) or "unknown"


def _install_run_log(archive: _RuntimeArchive) -> tuple[Path, object, object, object]:
    """Create a fresh per-launch log file and tee stdout/stderr into it."""
    log_path = archive.runtime_log_path()
    log_handle = log_path.open("a", encoding="utf-8", buffering=1)
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = _TeeTextStream(original_stdout, log_handle)
    sys.stderr = _TeeTextStream(original_stderr, log_handle)
    print(f"runtime_log={log_path}")
    return log_path, log_handle, original_stdout, original_stderr


def _restore_run_log(log_handle, original_stdout, original_stderr) -> None:
    if log_handle is None:
        return
    sys.stdout = original_stdout
    sys.stderr = original_stderr
    log_handle.close()
