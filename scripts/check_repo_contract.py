#!/usr/bin/env python3
"""Validate repository contracts for v6 device packs.

Pack conformance (P4: packs own hardware, the core stays generic):

- every pack directory ships a ``manifest.yaml`` that parses and validates
  against ``schemas/manifest.schema.json`` (a typo'd key must fail loudly,
  never silently disable a pack),
- ``python_entrypoint`` points at a module file that exists in-tree,
- every pack declares exactly one ``DEVICE_ID``,
- non-simulated packs document themselves (``README.md``),
- destructive actions carry the approval floor: any ``LegalAction`` whose
  action id contains ``CANCEL`` must set ``approval_required=True`` in
  source. (Runtime hazard behavior is separately pinned by the contract
  suite — ``test_auto_approve_never_applies_above_low_hazard``.)

Run from the repo root:  python scripts/check_repo_contract.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKS_ROOT = REPO_ROOT / "wallee" / "packs"
SCHEMA_PATH = REPO_ROOT / "schemas" / "manifest.schema.json"


def pack_dirs() -> list[Path]:
    return sorted(
        path
        for path in PACKS_ROOT.iterdir()
        if path.is_dir() and (path / "__init__.py").exists()
    )


def _load_manifest(path: Path):
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _entrypoint_module_file(entrypoint: str) -> Path | None:
    module_path = entrypoint.split(":", 1)[0]
    candidate = REPO_ROOT / Path(*module_path.split(".")).with_suffix(".py")
    return candidate if candidate.exists() else None


def _cancel_actions_missing_approval(source: str) -> list[str]:
    """Return CANCEL action ids whose LegalAction block lacks approval_required=True."""
    offenders: list[str] = []
    for match in re.finditer(r"LegalAction\(", source):
        depth, index = 1, match.end()
        while index < len(source) and depth:
            if source[index] == "(":
                depth += 1
            elif source[index] == ")":
                depth -= 1
            index += 1
        block = source[match.start() : index]
        id_match = re.search(r"action_id=\"([^\"]+)\"", block)
        if id_match and "CANCEL" in id_match.group(1) and "approval_required=True" not in block:
            offenders.append(id_match.group(1))
    return offenders


def main() -> int:
    import jsonschema

    errors: list[str] = []
    if not PACKS_ROOT.is_dir():
        print(f"Pack root not found: {PACKS_ROOT}")
        return 1

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    packs = pack_dirs()
    if not packs:
        errors.append(f"no pack directories under {PACKS_ROOT}")

    for pack_dir in packs:
        name = pack_dir.name
        manifest_path = pack_dir / "manifest.yaml"
        if not manifest_path.exists():
            errors.append(f"{name}: missing manifest.yaml")
            continue
        try:
            manifest = _load_manifest(manifest_path)
        except Exception as exc:
            errors.append(f"{name}: manifest.yaml failed to parse: {exc}")
            continue
        for issue in validator.iter_errors(manifest):
            errors.append(f"{name}: manifest schema violation: {issue.message}")

        entrypoint = str(manifest.get("python_entrypoint") or "")
        if ":" not in entrypoint:
            errors.append(f"{name}: python_entrypoint must be 'module.path:ClassName'")
        elif _entrypoint_module_file(entrypoint) is None:
            errors.append(f"{name}: python_entrypoint module not found in tree: {entrypoint}")

        simulated = bool((manifest.get("detection") or {}).get("simulate"))
        if not simulated and not (pack_dir / "README.md").exists():
            errors.append(f"{name}: hardware pack must ship a README.md")

        device_ids: set[str] = set()
        for source_path in sorted(pack_dir.glob("*.py")):
            source = source_path.read_text(encoding="utf-8")
            device_ids.update(re.findall(r"^\s*DEVICE_ID\s*=\s*\"([^\"]+)\"", source, re.MULTILINE))
            for offender in _cancel_actions_missing_approval(source):
                errors.append(
                    f"{name}: {source_path.name}: {offender} is a CANCEL-class action "
                    "without approval_required=True (destructive-action floor)"
                )
        if len(device_ids) != 1:
            errors.append(f"{name}: expected exactly one DEVICE_ID declaration, found {sorted(device_ids)}")

    if errors:
        print("Repository contract violations:")
        for err in errors:
            print(f"- {err}")
        return 1

    print(f"Repository contract checks passed for {len(packs)} device packs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
