# What & why

<!-- One or two sentences. Link the plan item / issue that schedules this. -->

# Gate checklist (mirrors the required checks — run `./scripts/gate.sh`)

- [ ] `tests` — suite green, coverage floor holds; goldens byte-identical
      (any re-bless is a separate `[re-bless]` commit)
- [ ] `gates` — ruff, forbidden patterns, hygiene clean
- [ ] `mypy` — strict scope clean
- [ ] `safety-invariants` — contract suite green; no xfail weakened without
      its plan-item link
- [ ] `adversarial-gates` / `sim-evals` — gate + trajectory lanes green
- [ ] `cassette-replay` — prompt/payload unchanged (any re-record is a
      separate `[re-record]` commit)
- [ ] `schema-sync` — schemas regenerated, never hand-edited
- [ ] `docs-contract` / `repo-contract` — docs, pack conformance, purity and
      file-size ratchets green (ratchets lowered if code shrank)

# Safety impact

<!-- Which of the six promises does this touch, if any? What proves it still holds? -->
