# AGENTS.md — the one authoritative agent guide

This is the single authoritative orientation file for AI agents (and humans)
working on Wallee. `CLAUDE.md` and `CODEX.md` are pointers here. Historical
agent guides live under `docs/history/` and describe retired designs — do not
follow them.

## What Wallee is

Wallee lets an LLM operate physical hardware (today: a Prusa CORE One/+ 3D
printer from a Raspberry Pi 5) **without trusting the model**. The planner
proposes; deterministic code validates, dispatches, verifies, and can stop the
machine independently. The mission: keep the runtime **lean, deterministic,
and physically grounded**. The planner is allowed to be smart; the control
plane is required to be boring.

Pipeline: packs publish raw state → `WorldCompiler` builds a `WorldPacket`
(facts + a closed frontier of `LegalAction`s) → the planner returns `PlanIR`
(EXECUTE / NO_ACTION / CALL_HUMAN over frontier ids only) → the engine
validates, gates on approval + interlock, journals, executes through the
owning pack, verifies the post-world → the independent `SafetyKernel` plus
the out-of-process watchdog can stop the machine at any time.

## The six promises and the checks that hold them

Every promise is executable. If you weaken one, a *named* CI check goes red.

| # | Promise | Held by |
|---|---|---|
| P1 | The LLM proposes; deterministic code disposes | `safety-invariants` (`tests/contract/`) |
| P2 | Closed, bounded action space validated outside the prompt | `safety-invariants`, `adversarial-gates` |
| P3 | Independent safety layer stops the machine even if control dies | `safety-invariants`, `sim-evals` |
| P4 | Packs own hardware; the core stays generic | `repo-contract` (core purity ratchet) |
| P5 | Durable audit trail, crash-recoverable | `safety-invariants`, `sim-evals` |
| P6 | Human approval gates hazards; a human can always ESTOP | `safety-invariants` |

## Architectural non-negotiables

1. **No raw device control from the planner.** The planner chooses frontier
   action ids; the canonical contract is `PlanIR` JSON. There is no free-form
   args channel, and there must never be one.
2. **No safety in prompt text.** Hard safety lives in legality checks,
   approvals, verification, and the safety kernel. Deterministic gates never
   read model free-text (only the closed `finding_types` enum).
3. **`action_runs` ≠ `exec_journal`.** Logical workflow state vs "a physical
   side effect may have started". Never collapse them.
4. **Commit barriers matter.** `DISPATCHED` before execution; `IN_FLIGHT`
   before hardware. The redundancy is intentional; do not "simplify" it away.
5. **The frontier is the planner boundary.** Not in the frontier (and
   prompt-visible) → not selectable. No escape hatches.
6. **Replan is not generation retry.** Bad JSON/ids: retry generation once.
   Verification mismatch, timeout, lost lock, expired approval, material
   world change: replan.
7. **Prefer deeper modules.** A few strong abstractions beat many shallow
   wrappers; a refactor that adds files without removing cognitive load is
   probably wrong.

## Hard rules (each is machine-enforced)

- Never mix golden/cassette re-blesses with source changes — separate
  `[re-bless]` / `[re-record]` commits (`tests`, `cassette-replay`).
- Never touch `tests/contract/` xfails without linking the plan item that
  schedules the fix (`safety-invariants` manifest hygiene).
- Never hand-edit generated artifacts — regenerate (`schema-sync`,
  `repo-contract`).
- Never write literal private IPs, MACs, serials, home paths, or keys —
  placeholders only (`gates` forbidden-patterns).
- New device knowledge stays in `wallee/packs/` — the core-purity ratchet
  only shrinks (`repo-contract`).
- No production file grows past its size allowance; new files cap at 800
  lines (`repo-contract`).
- Extraction/refactor PRs carry no logic changes: goldens byte-identical.

## The gate

Run `./scripts/gate.sh` before pushing — it is the one local command surface
and mirrors CI (`./scripts/gate.sh fast` for the quick subset). The CI
checks, kept in sync with `.github/workflows/ci.yml` by
`scripts/gen_agents_gate_block.py --check`:

<!--gate-block:start-->
| Check | What it holds |
|---|---|
| `tests` | full suite + goldens + entrypoint e2e, 72% coverage floor |
| `gates` | pre-commit set: ruff, hygiene, forbidden patterns, docs, repo contract, purity, file sizes |
| `mypy` | strict typing on the safety-relevant core modules |
| `safety-invariants` | the six-promise contract suite + manifest/xfail hygiene |
| `adversarial-gates` | hostile PlanIRs + translated hazard corpus die at the gates |
| `sim-evals` | offline fault-injection trajectories over the sim printer |
| `cassette-replay` | recorded planner traffic replays byte-stable |
| `schema-sync` | JSON schemas match their code sources |
| `docs-contract` | links, fences, doc placement rules |
| `repo-contract` | pack conformance, forbidden patterns, purity + size ratchets |
| `pip-audit` | dependency vulnerabilities |
<!--gate-block:end-->

## Repository map

- `wallee/models.py` — core data contracts (WorldPacket, PlanIR, LegalAction)
- `wallee/planning_context.py` — world compilation + frontier
- `wallee/planner.py` — planner adapters (heuristic + OpenRouter)
- `wallee/engine.py` — validate, gate, dispatch, verify, replan
- `wallee/safety.py` / `wallee/safety_watchdog.py` — interlock + watchdog
- `wallee/runtime_db.py` — the only durable runtime truth (SQLite)
- `wallee/packs/` — hardware packs (sim_printer, sim_arm, prusa_core_one_plus)
- `wallee/cli.py` / `composition.py` / `loop.py` / `artifacts.py` /
  `archive.py` — the runtime entry (via the stable `wallee/main.py` shim)
- `tests/contract/` — the six promises as tests (MANIFEST-pinned)
- `tests/replay/` — the translated 60-scenario corpus (keymap + gates)
- `tests/sim_evals/` — fault-injection trajectories
- `schemas/` — generated JSON schemas (`scripts/gen_schemas.py`)
- `knowledge/` — planner prompt contract/rubric/examples
- `deploy/systemd/` — the two services (never two watchdogs at once)
- `docs/` — DEPLOYMENT, ROADMAP, GLOSSARY, SCHEMAS, VALIDATION (SECURITY.md
  at root); `docs/internal/` and `docs/history/` are historical

## Pack authoring

A verb is admitted to the planner frontier only if it is **outcome-level,
observable, bounded, and reusable**; otherwise keep it pack-local. For the
`prusa_core_one_plus` reference pack: HTTP is the authoritative control path,
UDP metrics are advisory, serial is diagnostics-only; never expose raw G-code
to the planner; never make `safe_to_unload` look more precise than the
hardware can observe. One physical printer appears as one pack even though it
privately composes HTTP, UDP, and optional serial.

## What not to add casually

A second durable database; a custom planner DSL; provider-specific prompt
adapters; streaming control decisions; a digital twin; long narrative memory
in the planning prompt; network I/O in safety or deterministic control paths;
an orchestration framework.

## How to run things

```bash
pip install -e ".[dev]"          # once
./scripts/gate.sh                # everything CI runs (use `fast` for the quick set)
pytest tests -q                  # full suite
python -m wallee.main --simulate --once --goal "smoke test"   # sim cycle
```

Runtime state lives under `WALLEE_DATA_DIR` (default `.runtime/`); never
commit it. Secrets come from `.env` (see `.env.example`) — never inline.
When updating prompts, keep them short and structural; do not add
persona-heavy text to chase reasoning quality. If you change the planning
context shape, update `schemas/world_packet.schema.json` (regenerate) and the
tests together.
