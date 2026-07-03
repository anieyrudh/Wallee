#!/usr/bin/env python3
"""Enforce (and ratchet down) device-knowledge in the v6 core.

Device-specific knowledge — concrete device ids (`printer_1`), a vendor name
(`PRUSA`), or the Prusa tuning-id grammar (`_TRIM_`) — belongs in packs, not in
the generic core. The end state is zero such references outside `packs/`.

Getting there is a large, prompt-affecting refactor (the decision-signal
compilation is the planner's prompt), so this check ratchets: each core module
has an allowed hit count in `.core-purity-allowlist.json` that may only
SHRINK. New leakage into an already-clean area fails immediately; a module not
in the allowlist must have zero hits. When every count reaches zero the file
is emptied and the check becomes an absolute "no device knowledge in core".

Run from the repo root:  python scripts/check_core_purity.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = REPO_ROOT / "wallee_v6" / "wallee_v6"
ALLOWLIST_PATH = REPO_ROOT / "wallee_v6" / ".core-purity-allowlist.json"

DEVICE_KNOWLEDGE = re.compile(r"printer_1|PRUSA|_TRIM_")


def _core_modules() -> list[Path]:
    return [
        p
        for p in CORE_ROOT.rglob("*.py")
        if "packs" not in p.relative_to(CORE_ROOT).parts and p.name != "__init__.py"
    ]


def _hit_count(path: Path) -> int:
    return len(DEVICE_KNOWLEDGE.findall(path.read_text(encoding="utf-8")))


def main() -> int:
    allowlist: dict[str, int] = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8")) if ALLOWLIST_PATH.exists() else {}
    errors: list[str] = []
    observed: dict[str, int] = {}

    for module in sorted(_core_modules()):
        rel = module.relative_to(REPO_ROOT).as_posix()
        hits = _hit_count(module)
        if hits:
            observed[rel] = hits
        allowed = allowlist.get(rel, 0)
        if hits > allowed:
            if allowed == 0:
                errors.append(f"{rel}: {hits} device-knowledge reference(s) in a core module that must have none")
            else:
                errors.append(
                    f"{rel}: {hits} device-knowledge references exceeds the allowlisted {allowed} "
                    "(the allowlist may only shrink)"
                )

    # A module that dropped below its allowance must lower the allowlist so the
    # ratchet cannot silently loosen again.
    for rel, allowed in allowlist.items():
        actual = observed.get(rel, 0)
        if actual < allowed:
            errors.append(
                f"{rel}: now has {actual} references but the allowlist still permits {allowed}; "
                f"lower it to {actual} in {ALLOWLIST_PATH.relative_to(REPO_ROOT).as_posix()}"
            )
        if rel not in {m.relative_to(REPO_ROOT).as_posix() for m in _core_modules()}:
            errors.append(f"{rel}: allowlisted module no longer exists; remove it from the allowlist")

    if errors:
        print("Core-purity checks failed:")
        for err in errors:
            print(f"- {err}")
        return 1

    remaining = sum(observed.values())
    print(f"Core purity OK: {remaining} allowlisted device-knowledge references remain (ratcheting to zero).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
