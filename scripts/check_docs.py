#!/usr/bin/env python3
"""Check Markdown links, Mermaid fences, and local absolute paths."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
FENCE_RE = re.compile(r"^```(?P<lang>[^\s`]*)\s*$")
PUBLIC_DOCS = [REPO_ROOT / "README.md", REPO_ROOT / "ARCHITECTURE.md"]
SKIP_DIR_PARTS = {".git", ".venv", ".pytest_cache", "wallee-audit"}


def tracked_markdown_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    files = []
    for rel in result.stdout.splitlines():
        path = REPO_ROOT / rel
        if any(part in SKIP_DIR_PARTS for part in path.parts):
            continue
        files.append(path)
    return files


def strip_target(target: str) -> str:
    cleaned = target.strip().strip("<>").strip()
    if " " in cleaned and not cleaned.startswith(("http://", "https://")):
        cleaned = cleaned.split(" ", 1)[0]
    return cleaned.split("#", 1)[0]


def check_links(files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            raw_target = match.group(1).strip()
            if raw_target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if raw_target.startswith("/"):
                errors.append(f"{path.relative_to(REPO_ROOT)}: local absolute link: {raw_target}")
                continue
            target = strip_target(raw_target)
            if not target:
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                errors.append(
                    f"{path.relative_to(REPO_ROOT)}: broken local link: {raw_target}"
                )
    return errors


def check_mermaid_fences(files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8").splitlines()
        in_fence = False
        mermaid_count = 0
        for line in text:
            match = FENCE_RE.match(line)
            if not match:
                continue
            lang = match.group("lang")
            if not in_fence:
                in_fence = True
                if lang == "mermaid":
                    mermaid_count += 1
            else:
                in_fence = False
        if in_fence:
            errors.append(f"{path.relative_to(REPO_ROOT)}: unclosed fenced block")
        if path in PUBLIC_DOCS and mermaid_count == 0:
            errors.append(f"{path.relative_to(REPO_ROOT)}: expected at least one mermaid block")
    return errors


def main() -> int:
    files = tracked_markdown_files()
    errors = []
    errors.extend(check_links(files))
    errors.extend(check_mermaid_fences(PUBLIC_DOCS))
    if errors:
        print("Documentation checks failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"Documentation checks passed for {len(files)} Markdown files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
