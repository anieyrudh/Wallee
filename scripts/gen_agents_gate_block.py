#!/usr/bin/env python3
"""Keep the AGENTS.md gate table in sync with .github/workflows/ci.yml.

The table between ``<!--gate-block:start-->`` and ``<!--gate-block:end-->``
must list exactly the CI job names. Descriptions are prose (kept by hand);
the *check names* are the contract — a job added, removed, or renamed in
ci.yml without updating AGENTS.md is a red build.

Run from the repo root:
    python scripts/gen_agents_gate_block.py --check   # verify (CI)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
AGENTS_PATH = REPO_ROOT / "AGENTS.md"
START = "<!--gate-block:start-->"
END = "<!--gate-block:end-->"


def ci_job_names() -> list[str]:
    text = CI_PATH.read_text(encoding="utf-8")
    jobs_block = text.split("\njobs:\n", 1)[1]
    return re.findall(r"^  ([a-z0-9-]+):\n", jobs_block, re.MULTILINE)


def agents_table_names() -> list[str]:
    text = AGENTS_PATH.read_text(encoding="utf-8")
    try:
        block = text.split(START, 1)[1].split(END, 1)[0]
    except IndexError:
        raise SystemExit(f"AGENTS.md is missing the {START} / {END} markers")
    return re.findall(r"^\| `([a-z0-9-]+)` \|", block, re.MULTILINE)


def main() -> int:
    ci_names = ci_job_names()
    doc_names = agents_table_names()
    missing = [name for name in ci_names if name not in doc_names]
    stale = [name for name in doc_names if name not in ci_names]
    if missing or stale:
        print("AGENTS.md gate-block is out of sync with ci.yml:")
        for name in missing:
            print(f"- missing from AGENTS.md: {name}")
        for name in stale:
            print(f"- listed in AGENTS.md but not a CI job: {name}")
        return 1
    print(f"AGENTS.md gate-block OK: {len(ci_names)} checks in sync with ci.yml.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
