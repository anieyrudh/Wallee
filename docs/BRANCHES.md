# Branch & version map

The one place that says which branch is what. Read this before starting work,
cutting a tag, or picking a merge base.

## The lines

| Line | Branch | Version | What it is | Status |
|---|---|---|---|---|
| **pre-refactor** | `main` | unversioned (old dual-tree layout) | The codebase as it stood before the hardening refactor. | Superseded the moment the v6.6 line merges. |
| **refactored v6** | `claude/refactor-phase-0` | **`6.6.0`** | The complete contract-first refactor: single-tree `wallee/` package, 11 canonical CI checks, seeded-violation drill, full docs. **This is "the refactored version."** | ✅ Complete (~34 commits over `main`). Awaiting the physical hardware sign-off, then merges to `main` and is tagged **`v6.6.0`**. |
| **v7** | `v7/mainline` | `7.0.0aN` → `7.0.0` | The general-purpose physical-agent harness (capability vocabulary, kernel/cortex split, Rust sentinel, second device family). | ⏳ **Does not exist yet.** Cut from `main` *after* v6.6 merges (see [`V7_EXECUTION_PLAN.md`](V7_EXECUTION_PLAN.md) §0). |
| _(stale)_ | `claude/project-analysis-improvements-4ay1eo` | — | The first two planning commits (project analysis + refactor plan) that seeded the refactor. | 🗑️ Superseded — its commits are the first two of `claude/refactor-phase-0`. Safe to delete; kept only until v6.6 merges. |

## The naming trap (why this file exists)

`claude/refactor-phase-0` is named for **the v6 refactor's own Phase 0–5**
(the original [`REFACTOR_EXECUTION_PLAN.md`](REFACTOR_EXECUTION_PLAN.md)
phases). It is **the whole refactored v6.6 line**, start to finish — not a
partial branch, and **not** v7.

The [v7 plan](V7_EXECUTION_PLAN.md) *also* has a "Phase 0". That is a
**different, later** Phase 0 that will run on `v7/mainline`. The two are
unrelated. `refactor-phase-0` ≠ v7 Phase 0.

## Where the v7 plans live right now

The v7 design lives as **documents** on the refactored-v6 branch —
[`V7_DESIGN_SKETCH.md`](V7_DESIGN_SKETCH.md),
[`V7_EXECUTION_PLAN.md`](V7_EXECUTION_PLAN.md), and the
[`DESIGN_RESEARCH_2026-07.md`](DESIGN_RESEARCH_2026-07.md) behind them. They
are plans, **not v7 code**. They ride along when v6.6 merges to `main`, and
`v7/mainline` is cut from that merged `main`. No v7 source exists on any
branch yet.

## Merge / tag sequence

1. Physical hardware sign-off on `claude/refactor-phase-0`
   ([`DEPLOYMENT.md`](DEPLOYMENT.md) §7 attended checklist). Nothing merges
   before this — a green CI is necessary but not sufficient for a machine that
   moves.
2. Merge `claude/refactor-phase-0` → `main`; tag **`v6.6.0`**.
3. Cut `legacy/v5` branch + `legacy/v5-final` tag from the last pre-deletion
   `main` commit; update the required-check set; add the `OPENROUTER_API_KEY`
   secret (see [`ROADMAP.md`](ROADMAP.md) "Structural").
4. Cut `v7/mainline` from `main`. v7 work begins there.
