"""CLI entry point wiring: parse args, compose the runtime, run the loop."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import traceback

from .archive import _RuntimeArchive, _install_run_log, _restore_run_log
from .composition import build_runtime, reconcile_runtime_start_state, reset_runtime_start_state
from .config import Config
from .loop import run_control_loop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the lean Wallee v6.5 reference runtime")
    parser.add_argument("--simulate", action="store_true", help="Force simulation-mode pack loading")
    parser.add_argument("--once", action="store_true", help="Run exactly one cycle")
    parser.add_argument("--cycles", type=int, default=None, help="Number of cycles to run when not using --once")
    parser.add_argument("--forever", action="store_true", help="Run continuously until interrupted")
    parser.add_argument(
        "--poll-interval-s",
        type=float,
        default=None,
        help="Seconds between runtime cycles; defaults to WALLEE_RUNTIME_POLL_INTERVAL_S",
    )
    parser.add_argument(
        "--goal",
        required=True,
        help="Goal string given to the planner",
    )
    args = parser.parse_args(argv)
    if args.once and args.forever:
        parser.error("choose only one of --once or --forever")
    if args.once and args.cycles is not None:
        parser.error("--once cannot be combined with --cycles")
    if args.forever and args.cycles is not None:
        parser.error("--forever cannot be combined with --cycles")
    if not args.once and not args.forever and args.cycles is None:
        parser.error("choose one of --once, --forever, or explicit --cycles N")

    config = Config.from_env()
    if args.simulate:
        config.simulation_mode = True
    log_handle = None
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    runtime_db = None
    registry = None
    engine = None
    repo_root = Path(__file__).resolve().parents[1]
    archive = _RuntimeArchive(config, repo_root=repo_root)
    try:
        reset_runtime_start_state(config)
        reconcile_runtime_start_state(config)
        log_path, log_handle, original_stdout, original_stderr = _install_run_log(archive)
        runtime_db, whiteboard, registry, world_compiler, human_gateway, safety, engine, planner = build_runtime(config)
        goal = args.goal
        human_gateway.submit_goal(goal)
        runtime_db.record_event(
            "runtime",
            "INFO",
            "runtime_started",
            context={
                "goal": goal,
                "log_path": str(log_path),
                "poll_interval_override_s": args.poll_interval_s,
                "planner_request_timeout_s": config.planner_request_timeout_seconds,
                "planner_retry_attempts": config.planner_retry_attempts,
                "planner_retry_backoff_s": config.planner_retry_backoff_seconds,
                "planner_failures_before_no_action": config.planner_failures_before_no_action,
                "planner_min_interval_s": config.planner_min_interval_seconds,
                "planner_vision_lead_s": config.planner_vision_lead_seconds,
                "monitor_when_inactive": config.monitor_when_inactive,
                "forever": bool(args.forever),
                "once": bool(args.once),
                "cycles": args.cycles,
            },
        )
        poll_interval_s = config.runtime_poll_interval_seconds if args.poll_interval_s is None else float(args.poll_interval_s)
        run_control_loop(
            args=args,
            config=config,
            archive=archive,
            runtime_db=runtime_db,
            whiteboard=whiteboard,
            registry=registry,
            world_compiler=world_compiler,
            human_gateway=human_gateway,
            safety=safety,
            engine=engine,
            planner=planner,
            goal=goal,
            poll_interval_s=poll_interval_s,
        )
    finally:
        for component_name, component in (("engine", engine), ("registry", registry), ("runtime_db", runtime_db)):
            if component is None:
                continue
            try:
                if component_name == "engine":
                    component.close()
                elif component_name == "registry":
                    component.close_all()
                else:
                    component.close()
            except Exception:
                print(f"runtime teardown error in {component_name}")
                print(traceback.format_exc())
        _restore_run_log(log_handle, original_stdout, original_stderr)
    return 0
