"""Stable entry shim for the Wallee runtime.

`python -m wallee.main` and the `wallee` console script are permanent; the
implementation lives in `cli` (argument parsing + wiring), `composition`
(component graph), `loop` (the cycle loop), `archive`/`artifacts` (run
artifacts). Import from those modules directly in new code; this shim
re-exports the stable surface so existing callers and tests keep working.
"""

from __future__ import annotations

from .archive import _RuntimeArchive, _install_run_log, _restore_run_log
from .artifacts import (
    _artifact_planning_current,
    _utc_now_iso,
    _world_artifact_payload,
    _write_cycle_artifact,
    _write_cycle_artifact_with_worlds,
    _write_runtime_state,
)
from .cli import main
from .composition import (
    build_planner,
    build_runtime,
    reconcile_runtime_start_state,
    reset_runtime_start_state,
)
from .loop import (
    PLANNER_FAILURES_BEFORE_NO_ACTION,
    PLANNER_RETRY_ATTEMPTS,
    RUNTIME_CYCLE_FAILURES_BEFORE_PERSISTENT,
    _apply_remote_estop_requests,
    _handle_persistent_cycle_failure,
    _write_heartbeat_beacon,
    plan_with_retry,
)

__all__ = [
    "PLANNER_FAILURES_BEFORE_NO_ACTION",
    "PLANNER_RETRY_ATTEMPTS",
    "RUNTIME_CYCLE_FAILURES_BEFORE_PERSISTENT",
    "_RuntimeArchive",
    "_apply_remote_estop_requests",
    "_artifact_planning_current",
    "_handle_persistent_cycle_failure",
    "_install_run_log",
    "_restore_run_log",
    "_utc_now_iso",
    "_world_artifact_payload",
    "_write_cycle_artifact",
    "_write_cycle_artifact_with_worlds",
    "_write_heartbeat_beacon",
    "_write_runtime_state",
    "build_planner",
    "build_runtime",
    "main",
    "plan_with_retry",
    "reconcile_runtime_start_state",
    "reset_runtime_start_state",
]

if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
