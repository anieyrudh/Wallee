"""Tests for the forbidden-pattern checker (the drift backstop)."""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_forbidden_patterns.py"


def _scan(tmp_path: Path, content: str, name: str = "sample.md") -> int:
    f = tmp_path / name
    f.write_text(content)
    return subprocess.run([sys.executable, str(SCRIPT), str(f)]).returncode


@pytest.mark.parametrize("bad", [
    "path is /Users/someone/project/file.py",
    "printer at 192.168.0.195",
    "vpn 100.80.179.71",
    "mac F0:24:F9:C6:EE:2D",
    "scp -i ~/.ssh/wallee_pi host:/x .",
    "Authorization: Bearer sk-or-abc123def456ghijk",
])
def test_flags_violations(tmp_path, bad):
    assert _scan(tmp_path, bad) == 1


@pytest.mark.parametrize("ok", [
    "printer at 192.0.2.10 (TEST-NET)",
    "vpn 198.51.100.10",
    "camera OUI 88:49:2d:00:00:11",
    "example mac 02:00:00:00:00:10",
    "docs path /home/user/wallee and /home/pi/wallee",
    "just some normal prose about the architecture",
])
def test_allows_placeholders_and_prose(tmp_path, ok):
    assert _scan(tmp_path, ok) == 0


def test_repo_tree_is_clean():
    # The committed tree must pass its own checker.
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=REPO_ROOT)
    assert result.returncode == 0
