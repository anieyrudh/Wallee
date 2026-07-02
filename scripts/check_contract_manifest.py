#!/usr/bin/env python3
"""Guard the guards: the safety-contract suite may not shrink silently.

Rules enforced:
  1. The collected test ids in wallee_v6/tests/contract/ must match
     tests/contract/MANIFEST exactly (parametrized variants collapse to their
     base id). Deleting, renaming, or adding an invariant without updating
     MANIFEST in the same commit is a red build — additions are deliberate,
     removals are impossible to sneak.
  2. No `pytest.mark.skip` in the contract suite: an invariant is green,
     or a strict xfail with a tracked reason — never silently skipped.
  3. Every xfail is `strict=True` and its reason references the execution
     plan (docs/REFACTOR_EXECUTION_PLAN.md §N), so a gap always points at
     the item that closes it.
  4. Collection must be non-empty (a conftest error or marker typo that
     collects zero tests must not green the safety check).

Run from the repo root inside the v6 environment:
  python scripts/check_contract_manifest.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
V6_ROOT = REPO_ROOT / "wallee_v6"
CONTRACT_DIR = V6_ROOT / "tests" / "contract"
MANIFEST = CONTRACT_DIR / "MANIFEST"

# Reasons are written either literally or via the conftest PLAN constant
# (f"... {PLAN} §5"); both source forms count as a tracked reference.
XFAIL_REASON = re.compile(r"(?:docs/REFACTOR_EXECUTION_PLAN\.md|\{PLAN\}) §\d")


def collected_ids() -> set[str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/contract", "--collect-only", "-q", "-o", "addopts="],
        cwd=V6_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 5):
        print(result.stdout)
        print(result.stderr)
        raise SystemExit(f"contract collection failed (exit {result.returncode})")
    ids = set()
    for line in result.stdout.splitlines():
        if "::" in line:
            ids.add(line.split("[", 1)[0].strip())
    return ids


def main() -> int:
    errors: list[str] = []

    manifest_ids = {
        line.strip()
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    live_ids = collected_ids()

    if not live_ids:
        errors.append("contract suite collected ZERO tests — the safety check would pass vacuously")

    missing = sorted(manifest_ids - live_ids)
    unlisted = sorted(live_ids - manifest_ids)
    for test_id in missing:
        errors.append(f"MANIFEST invariant no longer collected (deleted or renamed?): {test_id}")
    for test_id in unlisted:
        errors.append(f"collected contract test not in MANIFEST (add it deliberately): {test_id}")

    for source in sorted(CONTRACT_DIR.glob("test_*.py")):
        text = source.read_text(encoding="utf-8")
        rel = source.relative_to(REPO_ROOT).as_posix()
        if re.search(r"pytest\.mark\.skip|pytest\.skip\(", text):
            # pytest.skip inside a test body for environmental preconditions is
            # tolerated only in the genericity file (frontier-size guard).
            if source.name != "test_core_genericity.py":
                errors.append(f"{rel}: skip found — contract tests are green or strict-xfail, never skipped")
        for match in re.finditer(r"@pytest\.mark\.xfail\((.*?)\)\s*\ndef (\w+)", text, re.DOTALL):
            body, test_name = match.group(1), match.group(2)
            if "strict=True" not in body:
                errors.append(f"{rel}::{test_name}: xfail without strict=True")
            if not XFAIL_REASON.search(body):
                errors.append(f"{rel}::{test_name}: xfail reason must reference docs/REFACTOR_EXECUTION_PLAN.md §N")

    if errors:
        print("Contract-manifest checks failed:")
        for err in errors:
            print(f"- {err}")
        return 1

    print(f"Contract manifest OK: {len(live_ids)} invariants collected, all listed, xfail hygiene clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
