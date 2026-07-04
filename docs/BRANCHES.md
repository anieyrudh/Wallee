# Branch & version map

The one place that says which branch is what. Read this before starting work,
cutting a tag, or picking a merge base.

## The lines

| Line | Branch | Version | What it is | Status |
|---|---|---|---|---|
| **pre-refactor** | `main` | unversioned (old dual-tree layout) | The codebase as it stood before the hardening refactor. | Superseded the moment the v6.6 line merges. |
| **refactored v6** | `v6.6/refactored` | **`6.6.0`** | The complete contract-first refactor: single-tree `wallee/` package, 11 canonical CI checks, seeded-violation drill, full docs. **This is "the refactored version."** | ✅ Complete (~35 commits over `main`). Awaiting the physical hardware sign-off, then merges to `main` and is tagged **`v6.6.0`**. |
| **v7** | `v7/mainline` | `7.0.0aN` → `7.0.0` | The general-purpose physical-agent harness (capability vocabulary, kernel/cortex split, Rust sentinel, second device family). | ⏳ **Does not exist yet.** Cut from `main` *after* v6.6 merges (see [`V7_EXECUTION_PLAN.md`](V7_EXECUTION_PLAN.md) §0). |

## The naming history (why this file exists)

The refactored-v6 branch was **renamed** from `claude/refactor-phase-0` to
`v6.6/refactored` so the branch name says what it is. If you see
`claude/refactor-phase-0` in an old commit message, link, or note, it means
**this** branch — the whole, finished v6.6 line, not a partial branch and not
v7.

Watch the "Phase 0" collision: the old name referred to the **v6 refactor's
own Phase 0–5** (the original
[`REFACTOR_EXECUTION_PLAN.md`](REFACTOR_EXECUTION_PLAN.md) phases). The
[v7 plan](V7_EXECUTION_PLAN.md) *also* has a "Phase 0" — a **different, later**
one that will run on `v7/mainline`. The two are unrelated.

A stale branch `claude/project-analysis-improvements-4ay1eo` held the first
two planning commits that seeded the refactor; those commits live on in
`v6.6/refactored`, so it is slated for deletion. (Both it and the old
`claude/refactor-phase-0` ref may linger on the remote until deleted by hand —
they carry nothing that isn't in `v6.6/refactored`.)

## Where the v7 plans live right now

The v7 design lives as **documents** on `v6.6/refactored` —
[`V7_DESIGN_SKETCH.md`](V7_DESIGN_SKETCH.md),
[`V7_EXECUTION_PLAN.md`](V7_EXECUTION_PLAN.md), and the
[`DESIGN_RESEARCH_2026-07.md`](DESIGN_RESEARCH_2026-07.md) behind them. They
are plans, **not v7 code**. They ride along when v6.6 merges to `main`, and
`v7/mainline` is cut from that merged `main`. No v7 source exists on any
branch yet.

## Merge / tag sequence

1. Physical hardware sign-off on `v6.6/refactored`
   ([`DEPLOYMENT.md`](DEPLOYMENT.md) §7 attended checklist). Nothing merges
   before this — a green CI is necessary but not sufficient for a machine that
   moves.
2. Merge `v6.6/refactored` → `main`; tag **`v6.6.0`**.
3. Cut `legacy/v5` branch + `legacy/v5-final` tag from the last pre-deletion
   `main` commit; update the required-check set; add the `OPENROUTER_API_KEY`
   secret (see [`ROADMAP.md`](ROADMAP.md) "Structural").
4. Cut `v7/mainline` from `main`. v7 work begins there.
