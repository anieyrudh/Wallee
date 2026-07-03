# Schemas — the durable JSON contracts

Three JSON Schemas define Wallee's durable interfaces. They are **generated
from their code sources** and pinned by the `schema-sync` CI check
(`scripts/gen_schemas.py`): hand-editing a schema, or changing its source
model without regenerating, is a red build.

Regenerate after changing a source model:

```bash
python scripts/gen_schemas.py --write   # or via ./scripts/gate.sh (check mode)
```

## `schemas/plan_ir.schema.json` — the planner's only output

Source: `wallee.models.PlanIR` (pydantic).

The complete language the planner may speak: a `decision`
(`EXECUTE` / `NO_ACTION` / `CALL_HUMAN`), a `sequence` of at most **3**
frontier action ids (duplicates rejected), an optional bounded
`tuning_choice` (family/direction/magnitude — materialized to exactly one
frontier action or refused), a required `why`, and an optional
`call_human_message`. There is deliberately **no arguments object**: action
args are fixed at frontier-compilation time by the pack, so a compromised
planner has no free-form channel. The schema is enforced twice — as the
provider's strict `response_format` and again with pydantic on parse.

## `schemas/world_packet.schema.json` — what the planner sees

Source: `wallee.models.world_packet_prompt_schema()`.

The prompt view of the compiled world: goal, device summaries, facts,
blockers, the frontier (id, verb, description, hazard class — never
execute refs or raw args), decision signals (tuning action space, family
blockers, the vision signal with its **closed** `finding_types` enum,
freshness, consequence of the last action), recent results, and pending
human messages. Operator-only actions, oversized JSON facts, and
internal refs are excluded from this view by construction — the schema is
the audit surface for "what could the model have known".

## `schemas/manifest.schema.json` — how a pack declares itself

Source: `wallee.models.PackManifest` (pydantic).

Every `wallee/packs/*/manifest.yaml` must validate (`schema-sync` and
`repo-contract` both check): `pack_id`, `display_name`, `category`,
`python_entrypoint` (must point at a real module in-tree), optional
`detection` (e.g. `simulate: true`), `resources`, and the declarative
`safety_profile` — the transport + request that stops this machine, which
is persisted at runtime so the **watchdog can stop the hardware even if
the runtime never started**.

## Where the other contracts live

Not everything durable is JSON Schema:

- **SQLite schema** — `wallee/runtime_db.py`, versioned by the
  transactional `schema_migrations` framework; ad-hoc ALTERs are banned.
- **The contract-suite MANIFEST** — `tests/contract/MANIFEST` pins the
  invariant list itself.
- **Ratchet allowlists** — `.core-purity-allowlist.json` and
  `.file-size-allowlist.json` (shrink-only, see the glossary).
- **Prompt knowledge** — `knowledge/CONTRACT.md`, `RUBRIC.md`, `EXAMPLES`:
  versioned prose the planner reads; changing it turns `cassette-replay`
  red until deliberately re-recorded.
