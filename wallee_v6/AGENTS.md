# AGENTS.md

This file is the high-signal orientation guide for coding agents working on Wallee v6.5.

## Mission of the repository

Keep Wallee v6.5 **lean, deterministic, and physically grounded**.

The planner is allowed to be smart.
The control plane is required to be boring.

## Architectural non-negotiables

1. **No raw device control from the planner**
   - The planner chooses from frontier action IDs.
   - Canonical control contract is `PlanIR` JSON.

2. **No safety in prompt text**
   - Hard safety belongs in legality checks, approvals, verification, and the safety kernel.
   - Prompt text can explain goals and rubrics, but must not become the last line of defense.

3. **Keep `action_runs` separate from `exec_journal`**
   - `action_runs` = official logical workflow state.
   - `exec_journal` = physical side-effect may have started.
   - Do not collapse these into one ambiguous field.

4. **Commit barriers matter**
   - Engine marks `DISPATCHED` before execution.
   - Pack runtime commits `IN_FLIGHT` before hardware side effects.
   - This ordering is intentionally redundant and must not be “simplified away.”

5. **The frontier is the planner boundary**
   - If an action is not in the frontier, the planner is not allowed to select it.
   - Do not add “just one escape hatch” that lets the planner bypass this.

6. **Replan is not generation retry**
   - Invalid JSON or invalid action IDs: retry generation once.
   - Verification mismatch, timeout, lost lock, expired approval, or material world change: replan.

7. **Prefer deeper modules**
   - Follow Ousterhout: a few modules with strong abstractions beat many shallow wrappers.
   - If a refactor adds files but does not remove cognitive load, it is probably wrong.

## Expected workflow for code changes

If you change behavior that affects runtime safety, do all of the following in the same change:

- update inline comments for any non-obvious invariant
- update the relevant docstrings
- update `README.md` or `docs/ARCHITECTURE.md`
- update or add tests
- add or update an ADR if the change alters a non-trivial design choice

## Where to put things

- `wallee_v6/models.py` - core data contracts
- `wallee_v6/predicates.py` - deterministic predicate evaluation
- `wallee_v6/runtime_db.py` - only durable runtime truth
- `wallee_v6/planning_context.py` - world packet + frontier compilation
- `wallee_v6/planner.py` - planner adapters (heuristic + OpenRouter)
- `wallee_v6/engine.py` - approval, execute, verify, replan
- `wallee_v6/packs/` - hardware-specific implementations
- `wallee_v6/safety.py` - heartbeat and interlock logic

## What not to add casually

- a second durable database
- a custom planner DSL
- provider-specific prompt adapters
- streaming control decisions
- a digital twin
- long narrative memory files in the planning prompt
- network I/O in safety or deterministic control paths

## Testing expectations

At minimum, new behavior should preserve:

- legal frontier generation
- world packet truncation limits
- approval binding by args hash and expiry
- `IN_FLIGHT` written before hardware side effects
- verification mismatch causing replan
- safety heartbeat timeout causing interlock

If you touch any of those, add or update tests.

## Pack authoring notes

A verb is admitted to the planner frontier only if it is:

1. outcome-level
2. observable
3. bounded
4. reusable

If it fails one of those tests, keep it pack-local and do not expose it to the planner.

## Prusa Core One+ pack notes

The `prusa_core_one_plus` pack is the reference example of how to fold several
vendor interfaces into one v6 device abstraction.

When editing that pack:

- keep **HTTP** as the authoritative control path
- keep **UDP metrics** advisory only
- keep **serial** diagnostics-only unless you have hard evidence the CDC path is
  stable enough for control
- do not expose arbitrary raw G-code to the planner
- do not make `safe_to_unload` look more precise than the hardware can really
  observe

If you need a new Prusa planner-visible verb, explain in the ADR or PR notes why
it passes the four admission tests.
