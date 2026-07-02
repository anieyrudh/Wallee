#!/usr/bin/env python3
"""Fail if the working tree contains patterns that must never be committed.

This is the machine-enforced backstop for the drift that Phase 0 cleaned up:
absolute developer-machine paths, real private/VPN IP addresses, device MAC
addresses and serials, SSH key paths, and API-key-shaped strings. It runs both
as a pre-commit hook and as a required CI job, so an AI agent that bypasses the
local hook still cannot merge a violation once branch protection is on.

Placeholders are allowed on purpose:
  - RFC 5737 documentation ranges 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24
  - the public camera vendor OUI prefix 88:49:2d (product knowledge the
    discovery logic filters on, not personal data)
  - locally-administered example MACs (02:00:00:00:00:xx)

Usage:
  python scripts/check_forbidden_patterns.py [paths...]
With no paths, every git-tracked file is scanned.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Binary / vendored / generated content we never scan.
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".pack", ".idx",
                 ".rev", ".bgcode", ".gcode", ".woff", ".woff2"}
SKIP_PATH_SUBSTRINGS = ("egg-info/",)

# This checker and its test intentionally contain the very patterns they ban
# (as documentation and as fixtures), so they are exempt from scanning.
SELF_EXEMPT = {
    "scripts/check_forbidden_patterns.py",
    "tests/test_forbidden_patterns.py",
}

# Allowlisted placeholder values that would otherwise trip a rule.
_ALLOWED_LITERALS = re.compile(
    r"192\.0\.2\.\d{1,3}"          # RFC 5737 TEST-NET-1
    r"|198\.51\.100\.\d{1,3}"      # RFC 5737 TEST-NET-2
    r"|203\.0\.113\.\d{1,3}"       # RFC 5737 TEST-NET-3
    r"|88:49:2d(?::[0-9a-fA-F]{2}){3}"   # public camera vendor OUI + synthetic suffix
    r"|02(?::00){4}:[0-9a-fA-F]{2}"      # locally-administered example MACs
)

# (rule name, compiled pattern). A line matches a rule only if, after removing
# every allowlisted literal, the pattern still matches.
RULES: list[tuple[str, re.Pattern[str]]] = [
    ("absolute developer path (/Users or /home/<name>)",
     re.compile(r"/Users/[A-Za-z]|/home/(?!user\b|pi\b|<)\w+")),
    ("private/VPN IP address (use RFC 5737 placeholders)",
     re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
                r"|192\.168\.\d{1,3}\.\d{1,3}"
                r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
                r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b")),
    ("MAC address (use a placeholder or the documented vendor OUI)",
     re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b")),
    ("SSH private-key flag pointing at a home directory",
     re.compile(r"-i\s+~?/[\w./-]*\.ssh/")),
    ("OpenRouter/Bearer API key literal",
     re.compile(r"sk-or-[A-Za-z0-9]|Bearer\s+[A-Za-z0-9]{16,}")),
]


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [REPO_ROOT / line for line in out.splitlines() if line]


def _rel(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _should_scan(path: Path) -> bool:
    rel = _rel(path)
    if rel in SELF_EXEMPT:
        return False
    if any(part in SKIP_DIRS for part in path.parts):
        return False
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    if any(sub in rel for sub in SKIP_PATH_SUBSTRINGS):
        return False
    return True


def scan(paths: list[Path]) -> list[str]:
    violations: list[str] = []
    for path in paths:
        if not path.is_file() or not _should_scan(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = _rel(path)
        for lineno, line in enumerate(text.splitlines(), start=1):
            cleaned = _ALLOWED_LITERALS.sub("", line)
            for rule_name, pattern in RULES:
                if pattern.search(cleaned):
                    violations.append(f"{rel}:{lineno}: {rule_name}: {line.strip()[:120]}")
    return violations


def main(argv: list[str]) -> int:
    if argv:
        paths = [Path(a).resolve() for a in argv]
    else:
        paths = _tracked_files()
    violations = scan(paths)
    if violations:
        print("Forbidden patterns found:")
        for v in violations:
            print(f"- {v}")
        print(
            "\nUse a placeholder instead: RFC 5737 IPs (192.0.2.x), <PLACEHOLDER> "
            "names in docs, and keep secrets out of the tree entirely."
        )
        return 1
    print(f"No forbidden patterns in {len(paths)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
