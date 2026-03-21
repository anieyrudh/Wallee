#!/usr/bin/env python3
"""Validate basic repository contracts for device packs and registry discovery."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def pack_dirs() -> list[Path]:
    base = REPO_ROOT / "wallee" / "device_packs"
    return sorted(
        path
        for path in base.iterdir()
        if path.is_dir() and (path / "__init__.py").exists()
    )


def decorated_names(module_name: str) -> list[str]:
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return []
    names = []
    for attr_name in dir(module):
        obj = getattr(module, attr_name)
        if callable(obj) and hasattr(obj, "_tool_meta"):
            names.append(obj._tool_meta["name"])
    return sorted(set(names))


def main() -> int:
    from wallee.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.load_builtins()
    errors: list[str] = []

    for pack_dir in pack_dirs():
        pack_name = pack_dir.name
        readme_path = pack_dir / "README.md"
        if not readme_path.exists():
            errors.append(f"{pack_name}: missing README.md")
            continue

        registry.load_pack(f"wallee.device_packs.{pack_name}")
        registered_names = {tool.name for tool in registry.list_all() if tool.pack_name == pack_name}
        expected_names = []
        expected_names.extend(decorated_names(f"wallee.device_packs.{pack_name}.sensors"))
        expected_names.extend(decorated_names(f"wallee.device_packs.{pack_name}.actuators"))
        expected_names = sorted(set(expected_names))

        missing = [name for name in expected_names if name not in registered_names]
        if missing:
            errors.append(f"{pack_name}: registry missing decorated tools: {', '.join(missing)}")

        readme_text = readme_path.read_text(encoding="utf-8")
        undocumented = [name for name in expected_names if name not in readme_text]
        if undocumented:
            errors.append(f"{pack_name}: README missing tool mentions: {', '.join(undocumented)}")

    if errors:
        print("Repository contract checks failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"Repository contract checks passed for {len(pack_dirs())} device packs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
