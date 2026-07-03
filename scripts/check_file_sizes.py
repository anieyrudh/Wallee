#!/usr/bin/env python3
"""Enforce (and ratchet down) the production file-size cap.

No production module should exceed ``MAX_LINES`` (800). Files already over
the cap when the check landed are allowlisted in
``.file-size-allowlist.json`` with their line count at that moment; each
allowance may only SHRINK. A new file over the cap, growth in an
allowlisted file, or an allowance that fails to shrink when the file did,
all fail. When the allowlist empties, the cap is absolute.

Run from the repo root:  python scripts/check_file_sizes.py
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "wallee"
ALLOWLIST_PATH = REPO_ROOT / ".file-size-allowlist.json"
MAX_LINES = 800


def _production_modules() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def main() -> int:
    allowlist: dict[str, int] = (
        json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8")) if ALLOWLIST_PATH.exists() else {}
    )
    errors: list[str] = []
    observed: dict[str, int] = {}

    for module in _production_modules():
        rel = module.relative_to(REPO_ROOT).as_posix()
        lines = len(module.read_text(encoding="utf-8").splitlines())
        if lines > MAX_LINES:
            observed[rel] = lines
        allowed = allowlist.get(rel, MAX_LINES)
        if lines > allowed:
            if rel not in allowlist:
                errors.append(f"{rel}: {lines} lines exceeds the {MAX_LINES}-line cap for new/clean files")
            else:
                errors.append(
                    f"{rel}: {lines} lines exceeds its allowlisted {allowed} (the allowlist may only shrink)"
                )

    for rel, allowed in allowlist.items():
        actual = observed.get(rel, 0)
        if actual <= MAX_LINES:
            errors.append(
                f"{rel}: now within the cap ({actual} lines) — remove it from "
                f"{ALLOWLIST_PATH.relative_to(REPO_ROOT).as_posix()}"
            )
        elif actual < allowed:
            errors.append(
                f"{rel}: now {actual} lines but the allowlist still permits {allowed}; lower it"
            )
        if not (REPO_ROOT / rel).exists():
            errors.append(f"{rel}: allowlisted file no longer exists; remove it from the allowlist")

    if errors:
        print("File-size checks failed:")
        for err in errors:
            print(f"- {err}")
        return 1

    over = sum(observed.values())
    print(
        f"File sizes OK: {len(observed)} allowlisted files remain over the {MAX_LINES}-line cap "
        f"({over} total lines, ratcheting down)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
