"""Configuration loading for the lean Wallee v6 runtime.

The configuration intentionally avoids a sprawling settings framework.  This file
only exposes the small number of switches that materially affect runtime
behavior.  Keeping configuration shallow makes the codebase easier to reason
about and reduces the chance that an operator changes a risky knob without
understanding the consequences.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


def _absolute_path(path: str | Path) -> Path:
    """Return an absolute path without dereferencing symlinks."""
    value = Path(path).expanduser()
    return value if value.is_absolute() else value.absolute()


def _parse_enabled_packs(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _prusa_runtime_requested(enabled_packs: tuple[str, ...] = ()) -> bool:
    if "prusa_core_one_plus" in enabled_packs:
        return True
    for name in (
        "PRUSA_CORE_ONE_HOST",
        "PRUSALINK_HOST",
        "PRUSA_CORE_ONE_API_KEY",
        "PRUSALINK_API_KEY",
        "PRUSA_CORE_ONE_SERIAL_PORT",
    ):
        value = os.environ.get(name)
        if value is not None and value.strip():
            return True
    return False


@dataclass(slots=True)
class Config:
    """Runtime configuration derived from environment variables.

    The defaults are chosen to make local simulation easy while keeping the
    safety-relevant decisions explicit.  Anything that changes the trust model
    should be a named field here, not a hidden constant elsewhere.
    """

    data_dir: Path
    db_path: Path
    knowledge_dir: Path
    pack_root: Path
    outbox_dir: Path
    planner_backend: str
    openrouter_api_key: str | None
    openrouter_model: str
    openrouter_response_healing: bool
    reasoning_effort: str
    planner_request_timeout_seconds: float
    planner_retry_attempts: int
    planner_retry_backoff_seconds: float
    planner_failures_before_no_action: int
    planner_min_interval_seconds: float
    planner_vision_lead_seconds: float
    frontier_limit: int
    delta_limit_per_device: int
    approval_ttl_seconds: int
    heartbeat_timeout_seconds: int
    runtime_poll_interval_seconds: float
    flush_state_on_start: bool
    monitor_when_inactive: bool
    simulation_mode: bool
    enabled_packs: tuple[str, ...]
    auto_approve_low_hazard: bool
    safe_to_unload_temp_c: float
    control_lock_path: Path

    @classmethod
    def from_env(cls, repo_root: str | Path | None = None) -> "Config":
        """Build a :class:`Config` from environment variables.

        Parameters
        ----------
        repo_root:
            Optional repository root.  When omitted, it is resolved from this
            file's location.

        Returns
        -------
        Config
            Fully-populated configuration with directories created as needed.
        """
        resolved_root = _absolute_path(repo_root) if repo_root else _absolute_path(Path(__file__).absolute().parents[1])
        data_dir = _absolute_path(os.environ.get("WALLEE_DATA_DIR", resolved_root / ".runtime"))
        data_dir.mkdir(parents=True, exist_ok=True)
        outbox_dir = data_dir / "outbox"
        outbox_dir.mkdir(parents=True, exist_ok=True)

        enabled_env = os.environ.get("WALLEE_ENABLED_PACKS")
        if enabled_env is None:
            enabled = ("prusa_core_one_plus",) if _prusa_runtime_requested() else ("sim_printer", "sim_arm")
        else:
            enabled = _parse_enabled_packs(enabled_env)
        live_prusa_requested = _prusa_runtime_requested(enabled)
        simulation_env = os.environ.get("WALLEE_SIMULATION")
        if simulation_env is None:
            simulation_mode = not live_prusa_requested
        else:
            simulation_mode = simulation_env.strip() not in {"0", "false", "False"}

        return cls(
            data_dir=data_dir,
            db_path=_absolute_path(data_dir / "runtime.db"),
            knowledge_dir=_absolute_path(resolved_root / "knowledge"),
            pack_root=_absolute_path(resolved_root / "wallee_v6" / "packs"),
            outbox_dir=_absolute_path(outbox_dir),
            planner_backend=os.environ.get("WALLEE_PLANNER_BACKEND", "heuristic").strip().lower(),
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY"),
            openrouter_model=os.environ.get("OPENROUTER_MODEL", "openai/gpt-5-mini").strip(),
            openrouter_response_healing=os.environ.get("WALLEE_OPENROUTER_RESPONSE_HEALING", "1").strip()
            not in {"0", "false", "False"},
            reasoning_effort=os.environ.get("WALLEE_REASONING_EFFORT", "low").strip().lower(),
            planner_request_timeout_seconds=float(os.environ.get("WALLEE_PLANNER_REQUEST_TIMEOUT_S", "20.0")),
            planner_retry_attempts=max(1, int(os.environ.get("WALLEE_PLANNER_RETRY_ATTEMPTS", "3"))),
            planner_retry_backoff_seconds=max(0.0, float(os.environ.get("WALLEE_PLANNER_RETRY_BACKOFF_S", "1.0"))),
            planner_failures_before_no_action=max(
                1,
                int(os.environ.get("WALLEE_PLANNER_FAILURES_BEFORE_NO_ACTION", "3")),
            ),
            planner_min_interval_seconds=max(0.0, float(os.environ.get("WALLEE_PLANNER_MIN_INTERVAL_S", "0.0"))),
            planner_vision_lead_seconds=max(0.0, float(os.environ.get("WALLEE_PLANNER_VISION_LEAD_S", "15.0"))),
            frontier_limit=int(os.environ.get("WALLEE_FRONTIER_LIMIT", "8")),
            delta_limit_per_device=int(os.environ.get("WALLEE_DELTA_LIMIT_PER_DEVICE", "3")),
            approval_ttl_seconds=int(os.environ.get("WALLEE_APPROVAL_TTL_SECONDS", "120")),
            heartbeat_timeout_seconds=int(os.environ.get("WALLEE_HEARTBEAT_TIMEOUT_SECONDS", "3")),
            runtime_poll_interval_seconds=float(os.environ.get("WALLEE_RUNTIME_POLL_INTERVAL_S", "5.0")),
            # Default OFF: the exec journal exists to answer "did a side effect
            # start before the crash?" — wiping it on boot erases exactly the
            # evidence crash recovery needs. Boot residue is swept by
            # main.reconcile_runtime_start_state instead.
            flush_state_on_start=os.environ.get("WALLEE_FLUSH_STATE_ON_START", "0").strip()
            not in {"0", "false", "False"},
            monitor_when_inactive=os.environ.get("WALLEE_MONITOR_WHEN_INACTIVE", "0").strip()
            not in {"0", "false", "False"},
            simulation_mode=simulation_mode,
            enabled_packs=enabled,
            auto_approve_low_hazard=os.environ.get("WALLEE_AUTO_APPROVE_LOW", "1").strip()
            not in {"0", "false", "False"},
            safe_to_unload_temp_c=float(os.environ.get("WALLEE_SAFE_TO_UNLOAD_TEMP_C", "35.0")),
            control_lock_path=_absolute_path(data_dir / "control.lock"),
        )
