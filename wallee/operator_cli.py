"""Small operator CLI helpers."""

from __future__ import annotations

import argparse

from .config import Config
from .human import HumanGateway
from .runtime_db import RuntimeDB
from .safety import remove_estop_latch, write_estop_signal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect, approve, or stop Wallee runtime items")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("pending", help="List pending human messages")

    approve = subparsers.add_parser("approve", help="Approve an action run")
    approve.add_argument("action_run_id")
    approve.add_argument("args_hash")
    approve.add_argument("--approved-by", default="operator-cli")

    estop = subparsers.add_parser("estop", help="Request a remote ESTOP (soft stop at the next cycle)")
    estop.add_argument("--reason", default="operator remote ESTOP")
    estop.add_argument("--by", default="operator-cli", dest="requested_by")

    clear = subparsers.add_parser("clear-estop", help="Clear the ESTOP latch (attended operation only)")
    clear.add_argument("--by", default="operator-cli", dest="requested_by")

    args = parser.parse_args(argv)

    config = Config.from_env()

    if args.command == "estop":
        write_estop_signal(config.estop_request_path, reason=args.reason, requested_by=args.requested_by)
        print(
            "remote ESTOP requested: the running control loop will stop the machine and latch at the "
            "next cycle boundary. This does not interrupt an in-flight action mid-call — use the physical "
            "ESTOP for that. The latch blocks all further dispatch until 'clear-estop'."
        )
        return 0

    if args.command == "clear-estop":
        # Attended-only: releasing the latch must follow a human physically
        # confirming the machine is safe. Remove the durable latch (so a stopped
        # runtime boots clean) and signal a live loop to sync its in-memory state.
        removed = remove_estop_latch(config.estop_latch_path)
        write_estop_signal(config.estop_clear_request_path, reason="operator clear", requested_by=args.requested_by)
        print(
            "ESTOP clear requested. ATTENDED OPERATION ONLY: only run this while physically present and "
            "after confirming the machine is in a safe state. "
            f"Durable latch {'removed' if removed else 'was not present'}; a running loop will resume dispatch next cycle."
        )
        return 0

    runtime_db = RuntimeDB(config.db_path)
    human = HumanGateway(config=config, runtime_db=runtime_db)

    if args.command == "pending":
        for line in human.pending_messages():
            print(line)
        return 0

    if args.command == "approve":
        human.approve(
            action_run_id=args.action_run_id,
            args_hash=args.args_hash,
            approved_by=args.approved_by,
        )
        print("approved")
        return 0

    return 1


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
