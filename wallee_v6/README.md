# Wallee v6 (Lean Reference Implementation)

Wallee v6 is a **lean, context-compiled physical AI kernel** for a small manufacturing cell.

This codebase implements the architecture described in `docs/ARCHITECTURE.md`:

- compile the live world into a **small world packet**
- compile a **frontier of legal next actions**
- let an LLM choose from that frontier using strict JSON
- run deterministic execution, approval, verification, and replan logic locally
- keep safety outside the planner

This repository now ships **two kinds of packs**:

- simulation packs for local development and tests
- a real-hardware **Prusa Core One+** reference pack that folds HTTP, UDP metrics,
  and optional USB serial diagnostics into one V6 device abstraction

The code intentionally favors **deeper modules and fewer moving parts**. That follows John Ousterhout's advice: remove accidental complexity first, then make the remaining modules deep enough that callers do not need to understand every low-level detail.

## Why this version is lean

This repository deliberately deletes a lot of tempting architecture:

- no digital twin
- no provider adapter layer
- no custom DSL as the canonical execution contract
- no streaming planner for control decisions
- no microservice swarm
- no persona-heavy prompt

Instead, it uses one main runtime, one safety process, one durable runtime database, and an optional Redis-compatible whiteboard abstraction.

## Repository map

- `wallee_v6/` - runtime code
- `wallee_v6/packs/prusa_core_one_plus/` - real-hardware Prusa Core One+ pack
- `knowledge/` - short contract, rubric, examples, incidents
- `schemas/` - JSON schemas for durable contracts
- `docs/` - architecture notes, ADRs, and human guide source
- `deploy/systemd/` - example service units
- `tests/` - unit and integration-style tests that act as executable documentation

## Quick start

### 1. Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Run the simulation once

```bash
python -m wallee_v6.main --simulate --once
```

### 3. Run a short multi-cycle simulation

```bash
python -m wallee_v6.main --simulate --cycles 5
```

### 4. Run the tests

```bash
pytest
```

## Choosing the planner backend

By default the code runs a **heuristic planner** so the repository is runnable without cloud credentials.

To use OpenRouter:

```bash
export WALLEE_PLANNER_BACKEND=openrouter
export OPENROUTER_API_KEY=...
export OPENROUTER_MODEL=openai/gpt-5-mini
python -m wallee_v6.main --simulate --once
```

The OpenRouter planner uses:

- strict JSON schema output
- non-streaming responses for control decisions
- stable leading prompt segments to preserve prompt-cache friendliness
- low temperature by default

## Running the Prusa Core One+ pack

The real-hardware pack stays close to the V6 design principles:

- one physical printer appears as one pack
- HTTP is the authoritative control path
- UDP metrics are optional observability
- USB serial is optional diagnostics only
- the planner sees outcome-level verbs, not raw G-code

### Minimal environment

```bash
export WALLEE_ENABLED_PACKS=prusa_core_one_plus
export WALLEE_SIMULATION=0
export PRUSA_CORE_ONE_HOST=http://192.168.0.195
export PRUSA_CORE_ONE_API_KEY=your_prusalink_api_key
```

For local bring-up, start from [`.env.example`](/Users/anieyrudh/Desktop/Wallee2/wallee_v6/.env.example).

Exact v5-compatible aliases are supported for the Prusa host and API key only:

- `PRUSALINK_HOST` -> `PRUSA_CORE_ONE_HOST`
- `PRUSALINK_API_KEY` -> `PRUSA_CORE_ONE_API_KEY`

If both are set, the canonical v6 `PRUSA_CORE_ONE_*` value wins.

### Optional metrics and serial

```bash
export PRUSA_CORE_ONE_ENABLE_METRICS=1
export PRUSA_CORE_ONE_METRICS_PORT=8514

export PRUSA_CORE_ONE_ENABLE_SERIAL=1
export PRUSA_CORE_ONE_SERIAL_PORT=/dev/ttyACM0
```

Read these before using the pack on real hardware:

- `wallee_v6/packs/prusa_core_one_plus/README.md`
- `wallee_v6/packs/prusa_core_one_plus/SETUP.md`
- `wallee_v6/packs/prusa_core_one_plus/CAPABILITIES.md`
- `docs/adrs/ADR-0004-prusa-core-one-plus-pack.md`

Bring-up assumptions remain unchanged:

- HTTP is the authoritative control path
- UDP metrics are advisory only
- USB serial is diagnostics-only unless proven stable enough for control
- the planner does not receive arbitrary raw G-code access

## Safety-critical invariants

These rules are repeated in code comments, tests, and docs because breaking any of them would change the trust model:

1. The planner never sends raw hardware commands directly.
2. The planner only chooses from a frontier of legal actions.
3. `action_runs` and `exec_journal` are separate meanings, even when stored in the same SQLite WAL file.
4. The executor marks `DISPATCHED` before pack execution.
5. The pack runtime commits `IN_FLIGHT` before any hardware side effect.
6. Verification checks `expected_delta` against the real post-state.
7. Schema errors are retried once; they are **not** treated as physical replans.
8. Safety lives outside the planner.

## Adding a new pack

1. Copy one of the example packs under `wallee_v6/packs/`.
2. Create a `manifest.yaml`.
3. Implement `normalize()`, `candidate_actions()`, and `_realize()`.
4. Add tests.
5. Update `knowledge/HARDWARE`-adjacent docs if the new pack changes operator assumptions.

Read `AGENTS.md` before editing runtime behavior.

## Documentation

- Architecture guide: `docs/ARCHITECTURE.md`
- ADRs: `docs/adrs/`
- Human guide landing page: `docs/HUMAN_GUIDE.md`
- Human guide PDF: `Wallee_V6_Prusa_Core_One_Pack_Guide.pdf`
- Coding-agent guide: `AGENTS.md`
- Claude-specific note: `CLAUDE.md`
- Codex-specific note: `CODEX.md`
