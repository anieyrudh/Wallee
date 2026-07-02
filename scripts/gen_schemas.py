#!/usr/bin/env python3
"""Keep wallee_v6/schemas/*.json in lockstep with their code sources.

Source of truth is CODE, generated outward:

  plan_ir.schema.json       <- PlanIR.model_json_schema()
  manifest.schema.json      <- PackManifest.model_json_schema()
  world_packet.schema.json  <- models.world_packet_prompt_schema()

`--check` (default, CI job `schema-sync`) regenerates in memory and fails on
any diff — a hand-edited schema file or a model change without a schema
commit both go red. `--write` regenerates the files.

Also validated:
  - every packs/*/manifest.yaml parses and validates against the manifest
    schema (a typo'd manifest key must fail loudly, not silently disable a
    pack);
  - the constraints the transport schema cannot carry live in pydantic:
    the plan horizon cap and duplicate-id rejection on PlanIR.sequence.
    (Provider strict-JSON modes reject `maxItems`/`uniqueItems`, so the
    transport schema stays permissive and pydantic is the enforcement —
    this check pins that the enforcement actually exists.)

Run from the repo root inside the v6 environment:
  python scripts/gen_schemas.py [--write]
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
V6_ROOT = REPO_ROOT / "wallee_v6"
SCHEMA_DIR = V6_ROOT / "schemas"

sys.path.insert(0, str(V6_ROOT))

import jsonschema  # noqa: E402
import pydantic  # noqa: E402
import yaml  # noqa: E402

from wallee_v6.models import PackManifest, PlanIR, world_packet_prompt_schema  # noqa: E402


def generated_schemas() -> dict[str, dict]:
    return {
        "plan_ir.schema.json": PlanIR.model_json_schema(),
        "manifest.schema.json": PackManifest.model_json_schema(),
        "world_packet.schema.json": world_packet_prompt_schema(),
    }


def check_sync() -> list[str]:
    errors: list[str] = []
    for name, generated in generated_schemas().items():
        path = SCHEMA_DIR / name
        if not path.exists():
            errors.append(f"{name}: missing on disk — run scripts/gen_schemas.py --write")
            continue
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        if on_disk != generated:
            disk_text = json.dumps(on_disk, indent=2, sort_keys=True).splitlines()
            gen_text = json.dumps(generated, indent=2, sort_keys=True).splitlines()
            diff = "\n".join(difflib.unified_diff(disk_text, gen_text, "on-disk", "generated", lineterm="", n=2))
            errors.append(
                f"{name}: drifted from its code source — run scripts/gen_schemas.py --write "
                f"and commit the diff\n{diff[:2000]}"
            )
    return errors


def check_manifests() -> list[str]:
    errors: list[str] = []
    manifest_schema = PackManifest.model_json_schema()
    validator = jsonschema.Draft202012Validator(manifest_schema)
    manifests = sorted((V6_ROOT / "wallee_v6" / "packs").glob("*/manifest.yaml"))
    if not manifests:
        errors.append("no pack manifests found — packs/*/manifest.yaml expected")
    for manifest_path in manifests:
        rel = manifest_path.relative_to(REPO_ROOT).as_posix()
        try:
            data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            errors.append(f"{rel}: unparseable YAML: {exc}")
            continue
        for issue in validator.iter_errors(data):
            errors.append(f"{rel}: {issue.message}")
        try:
            PackManifest.model_validate(data)
        except pydantic.ValidationError as exc:
            errors.append(f"{rel}: pydantic rejects manifest: {exc.errors()[0]['msg']}")
    return errors


def check_pydantic_side_constraints() -> list[str]:
    """The bounds the provider-facing schema cannot express must exist in code."""
    errors: list[str] = []
    try:
        PlanIR(decision="EXECUTE", sequence=[f"A_{i}" for i in range(4)], why="x")
        errors.append("PlanIR accepted a 4-action sequence — the 3-action horizon cap is gone")
    except pydantic.ValidationError:
        pass
    try:
        PlanIR(decision="EXECUTE", sequence=["A_X", "A_X"], why="x")
        errors.append("PlanIR accepted duplicate sequence ids — duplicate rejection is gone")
    except pydantic.ValidationError:
        pass
    return errors


def write_schemas() -> None:
    for name, generated in generated_schemas().items():
        path = SCHEMA_DIR / name
        path.write_text(json.dumps(generated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(REPO_ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="regenerate the schema files on disk")
    args = parser.parse_args()

    if args.write:
        write_schemas()

    errors = check_sync() + check_manifests() + check_pydantic_side_constraints()
    if errors:
        print("Schema-sync checks failed:")
        for err in errors:
            print(f"- {err}")
        return 1
    print("Schemas in sync with code; all pack manifests valid; pydantic-side bounds intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
