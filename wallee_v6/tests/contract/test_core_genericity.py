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

from .conftest import PLAN

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "wallee_v6"

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
    "dashboard.py": "read-only HTTP surface (currently not wired into main)",
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
    reason=f"Phase 3 (Prusa eviction): core modules still carry device knowledge — {PLAN} §6",
)
def test_no_core_module_references_device_ids():
    offenders: dict[str, int] = {}
    for module in CORE_MODULES:
        hits = len(DEVICE_KNOWLEDGE.findall(module.read_text(encoding="utf-8")))
        if hits:
            offenders[module.name] = hits
    assert not offenders, f"device knowledge in core: {offenders}"


@pytest.mark.xfail(
    strict=True,
    reason=f"Phase 3 (registry hardening): two packs may not claim one DEVICE_ID — {PLAN} §6",
)
def test_duplicate_device_id_claims_are_impossible():
    from wallee_v6.packs.prusa_core_one_plus.pack import PrusaCoreOnePack
    from wallee_v6.packs.sim_printer.pack import SimPrinterPack

    ids_distinct = SimPrinterPack.DEVICE_ID != PrusaCoreOnePack.DEVICE_ID
    registry_source = (PACKAGE_ROOT / "registry.py").read_text(encoding="utf-8")
    registry_rejects = "DEVICE_ID" in registry_source and "claim" in registry_source
    # Either the shipped packs stop colliding, or the registry must reject
    # co-enablement of packs that claim the same device id.
    assert ids_distinct or registry_rejects


@pytest.mark.xfail(
    strict=True,
    reason=f"Phase 3 (frontier unification): the planner may be validated against actions it was never shown — {PLAN} §6",
)
def test_prompt_frontier_matches_executable_frontier(runtime):
    from wallee_v6.models import PlanIR

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
