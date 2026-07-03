#!/usr/bin/env python3
"""The seeded-violation drill: prove every named guard actually fires.

A guard that has never fired is a claim, not a control. For each seeded
violation, this script creates a disposable git worktree at HEAD, applies the
violation, runs the NAMED check, and requires it to go red. A violation that
sails through green fails the drill.

Run from the repo root (takes a few minutes — it runs real checks):
    python scripts/seeded_violation_drill.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Violation:
    name: str
    named_check: str
    command: list[str]
    mutate: object  # Callable[[Path], None]
    notes: str = ""
    results: dict = field(default_factory=dict)


def _write(worktree: Path, rel: str, content: str) -> None:
    path = worktree / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _append(worktree: Path, rel: str, content: str) -> None:
    path = worktree / rel
    path.write_text(path.read_text(encoding="utf-8") + content, encoding="utf-8")


def v_oversized_core_file(worktree: Path) -> None:
    body = '"""Seeded drill violation: an 801-line core file."""\n' + ("x = 1\n" * 800)
    _write(worktree, "wallee/drill_oversized.py", body)


def v_device_knowledge_in_clean_core(worktree: Path) -> None:
    _append(worktree, "wallee/safety.py", '\n_DRILL = "printer_1.seeded_violation"\n')


def v_undefined_name(worktree: Path) -> None:
    _append(worktree, "wallee/coerce.py", "\n\ndef _drill() -> None:\n    return undefined_drill_name\n")


def v_laptop_path_in_test(worktree: Path) -> None:
    _append(
        worktree,
        "tests/test_predicates.py",
        '\nDRILL_PATH = "/Users/someone/wallee/secret-notes.txt"\n',
    )


def v_private_ip_in_docs(worktree: Path) -> None:
    _append(worktree, "docs/DEPLOYMENT.md", "\nDrill note: printer at 192.168.1.42.\n")


def v_delete_safety_invariants(worktree: Path) -> None:
    (worktree / "tests/contract/test_safety_invariants.py").unlink()


def v_hand_edited_golden(worktree: Path) -> None:
    golden = worktree / "tests/goldens/noop.json"
    # plan_ir_json is an escaped JSON string inside the trace, so edit the
    # bare token — the drill itself caught an earlier version of this seeding
    # that matched nothing and proved nothing.
    golden.write_text(
        golden.read_text(encoding="utf-8").replace("NO_ACTION", "EXECUTE"),
        encoding="utf-8",
    )


VIOLATIONS = [
    Violation(
        name="801-line core file",
        named_check="repo-contract (check_file_sizes)",
        command=[sys.executable, "scripts/check_file_sizes.py"],
        mutate=v_oversized_core_file,
    ),
    Violation(
        name="printer_1 in a clean core module (safety.py)",
        named_check="repo-contract (check_core_purity)",
        command=[sys.executable, "scripts/check_core_purity.py"],
        mutate=v_device_knowledge_in_clean_core,
    ),
    Violation(
        name="undefined name (F821)",
        named_check="gates (ruff)",
        command=["ruff", "check", "wallee/coerce.py"],
        mutate=v_undefined_name,
    ),
    Violation(
        name="laptop path in a test",
        named_check="gates (check_forbidden_patterns)",
        command=[sys.executable, "scripts/check_forbidden_patterns.py"],
        mutate=v_laptop_path_in_test,
    ),
    Violation(
        name="private LAN IP in docs",
        named_check="gates (check_forbidden_patterns)",
        command=[sys.executable, "scripts/check_forbidden_patterns.py"],
        mutate=v_private_ip_in_docs,
    ),
    Violation(
        name="deleted test_safety_invariants.py",
        named_check="safety-invariants (check_contract_manifest)",
        command=[sys.executable, "scripts/check_contract_manifest.py"],
        mutate=v_delete_safety_invariants,
        notes="proves the zero-collection guard: deleting the promises is loud",
    ),
    Violation(
        name="hand-edited golden trace",
        named_check="tests (test_golden_traces)",
        command=[sys.executable, "-m", "pytest", "tests/test_golden_traces.py", "-q", "-o", "addopts="],
        mutate=v_hand_edited_golden,
    ),
]


def run_drill() -> int:
    failures = []
    print(f"Seeded-violation drill over {len(VIOLATIONS)} violations\n")
    for index, violation in enumerate(VIOLATIONS, start=1):
        with tempfile.TemporaryDirectory(prefix="wallee-drill-") as tmp:
            worktree = Path(tmp) / "wt"
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
            )
            try:
                violation.mutate(worktree)
                result = subprocess.run(
                    violation.command, cwd=worktree, capture_output=True, text=True
                )
                fired = result.returncode != 0
                status = "FIRED" if fired else "DID NOT FIRE"
                print(f"[{index}/{len(VIOLATIONS)}] {violation.name}")
                print(f"    named check: {violation.named_check} -> {status}")
                if not fired:
                    failures.append(violation.name)
                    print("    stdout tail:", (result.stdout or "").strip().splitlines()[-3:])
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree)],
                    cwd=REPO_ROOT,
                    capture_output=True,
                )
    print()
    if failures:
        print(f"DRILL FAILED — guards that did not fire: {failures}")
        return 1
    print(f"DRILL PASSED — all {len(VIOLATIONS)} seeded violations failed on their named check.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_drill())
