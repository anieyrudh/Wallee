"""Small operator CLI helpers."""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import Config
from .human import HumanGateway
from .runtime_db import RuntimeDB


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or approve Wallee runtime items")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("pending", help="List pending human messages")

    approve = subparsers.add_parser("approve", help="Approve an action run")
    approve.add_argument("action_run_id")
    approve.add_argument("args_hash")
    approve.add_argument("--approved-by", default="operator-cli")

    args = parser.parse_args(argv)

    config = Config.from_env()
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
