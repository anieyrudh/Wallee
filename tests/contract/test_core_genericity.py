"""P4 contracts: device packs own hardware behavior; the core stays generic.

These are separated from `test_safety_invariants.py` because their fixes are
scheduled for the Phase-3 eviction work, not Phase 2 — keeping this file's
xfails alive must not block the Phase-2 "zero xfails in the safety file" gate
(per-file milestones, Integration Decision I-7 in the execution plan).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from .conftest import PLAN, REPO_ROOT

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "wallee"

# Core = everything in the package except packs/. These modules must not know
# any device id or Prusa concept; that knowledge belongs to packs.
CORE_MODULES = sorted(
    p for p in PACKAGE_ROOT.glob("*.py") if p.name != "__init__.py"
)

DEVICE_KNOWLEDGE = re.compile(r"printer_1|PRUSA|_TRIM_")

# Network/process I/O allowed in core, each with a reason. Anything else
# performing I/O in core is a boundary violation.
NETWORK_ALLOWLIST = {
    "planner.py": "raises/handles urllib errors from the transport",
    "llm_transport.py": "the LLM HTTP transport itself (live/recording/replay seam)",
    "stop_transport.py": "the generic machine-stop transport (safety arm; stdlib-only)",
    "registry.py": "pack presence detection probes (to become manifest-driven)",
}


def test_no_core_module_performs_network_io():
    violations = []
    network_markers = re.compile(
        r"^\s*(?:import\s+(?:socket|serial|httpx)\b|from\s+(?:socket|serial|httpx)\b|from\s+urllib\s+import|import\s+urllib\b)",
        re.MULTILINE,
    )
    for module in CORE_MODULES:
        if module.name in NETWORK_ALLOWLIST:
            continue
        if network_markers.search(module.read_text(encoding="utf-8")):
            violations.append(module.name)
    assert not violations, f"unexpected network/serial imports in core: {violations}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Phase 3/4 (Prusa eviction): the decision-signal compilation is the "
        "planner's prompt; the full move needs the structured TuningDescriptor "
        "redesign and cassette re-record. Meanwhile scripts/check_core_purity.py "
        f"ratchets the count down and blocks new leakage — {PLAN} §6"
    ),
)
def test_no_core_module_references_device_ids():
    offenders: dict[str, int] = {}
    for module in CORE_MODULES:
        hits = len(DEVICE_KNOWLEDGE.findall(module.read_text(encoding="utf-8")))
        if hits:
            offenders[module.name] = hits
    assert not offenders, f"device knowledge in core: {offenders}"


def test_core_purity_ratchet_is_enforced():
    """The shrink-only allowlist exists and the checker passes against it.

    This is the machine-enforced boundary that holds until the eviction above
    reaches zero: a new device reference in an already-clean core module, or an
    allowlist entry that fails to shrink when the code did, fails CI.
    """
    import subprocess
    import sys

    repo_root = REPO_ROOT
    allowlist = REPO_ROOT / ".core-purity-allowlist.json"
    assert allowlist.exists(), "the core-purity ratchet allowlist must exist"
    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "check_core_purity.py")],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_duplicate_device_id_claims_are_impossible(tmp_path, monkeypatch):
    # sim_printer and prusa_core_one_plus both claim "printer_1"; enabling both
    # must be rejected, not silently merged.
    from wallee.config import Config
    from wallee.registry import PackRegistry

    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WALLEE_ENABLED_PACKS", "sim_printer,prusa_core_one_plus")
    monkeypatch.delenv("WALLEE_SIMULATION", raising=False)
    monkeypatch.setenv("PRUSA_CORE_ONE_HOST", "192.0.2.10")  # let the real pack instantiate
    config = Config.from_env(repo_root=PACKAGE_ROOT.parent)
    registry = PackRegistry(config)
    with pytest.raises(ValueError, match="claim device id"):
        registry.load()


def test_prompt_frontier_matches_executable_frontier(runtime):
    from wallee.models import PlanIR

    compiler, engine = runtime["compiler"], runtime["engine"]
    world = compiler.compile("Unload cooled part from printer_1 into tray_A")
    if len(world.frontier) < 2:
        pytest.skip("need at least two frontier actions to truncate")

    world.prompt_frontier_limit = 1
    shown = {a["id"] for a in world.prompt_view()["frontier"]}
    hidden = [a for a in world.frontier if a.action_id not in shown]
    assert hidden, "truncation should hide at least one action"

    plan = PlanIR(decision="EXECUTE", sequence=[hidden[0].action_id], why="never shown to the planner")
    with pytest.raises(ValueError):
        engine.validate_plan(world, plan)
