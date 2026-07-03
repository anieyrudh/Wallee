# Validation — what is proven, by what, and what is not yet

This document is the honest ledger of Wallee's verification: every lane,
what it actually demonstrates, and the claims that still await hardware.
The design rule throughout: **safety claims are held by deterministic,
model-independent checks; model quality is measured separately and never
gates safety.**

## The lanes

| Lane | Where | What it proves |
|---|---|---|
| Unit + integration tests | `tests/` (422 passed, 1 deliberate xfail) | component behavior and the composed sim runtime |
| Entrypoint e2e | `tests/test_entrypoint_e2e.py` | `python -m wallee.main` boots, reconciles, cycles — wiring, not just parts |
| Contract suite | `tests/contract/` (37 invariants, MANIFEST-pinned) | the six safety promises as executable assertions |
| Adversarial gates | `tests/contract/test_gate_adversarial.py` | hostile PlanIRs (G-code injection, path traversal, hallucinated ids, poisoned vision prose) die at the gates with zero journal rows |
| Golden traces | `tests/goldens/` | exact plan→gate→dispatch→journal shapes; refactors must be byte-identical |
| Cassette replay | `tests/fixtures/cassettes/` | the real outbound prompt/payload replays byte-stable offline through the production planner |
| Replay corpus | `tests/replay/` (60 scenarios) | the legacy hazard corpus, translated to v6 key-space: untranslatable tools are inexpressible, off-frontier actions refused, ESTOP blocks all, CALL_HUMAN reaches the outbox only |
| Fault injection | `tests/sim_evals/` | mid-trajectory faults (pack crash, interlock trip, stale heartbeat, verification lie, kill-mid-side-effect) degrade safely |
| SIGKILL e2e | contract suite | the independent watchdog stops the sim machine when the runtime is killed mid-job |
| Coverage floor | `tests` CI job | 74% today, 72% floor, ratchet-up only |
| Type safety | `mypy` CI job | strict typing on the safety-relevant core modules |
| Seeded-violation drill | `scripts/seeded_violation_drill.py` | the guards themselves fire — 7/7 ([record](internal/DRILL_2026-07.md)) |

Run everything locally with `./scripts/gate.sh`.

## The drill deserves a sentence

A guard that has never fired is a claim, not a control. The seeded drill
plants seven deliberate violations (oversized core file, device knowledge
in a clean module, F821, laptop path, LAN IP, deleting the safety-promise
test file, hand-editing a golden) and requires each to fail on its **named**
check. Its first run caught its own bad seeding — an edit that matched
nothing and proved nothing — which is exactly the failure mode the ritual
exists to expose. Both the pass and the catch are recorded.

## Model quality (measured, not trusted)

Planner judgment is a quality metric, never a safety mechanism:

- **Historical baseline.** The legacy replay harness scored a live model at
  **47/60** on the original corpus
  ([full report](internal/REPLAY_BASELINE_2026-03.md)) — strongest on
  human-interaction and edge cases, weakest on telemetry reasoning. Those
  scenarios now live on, translated, as the gate-assertion suite above.
- **Live lane.** A weekly workflow (`live-eval`) feeds the translated
  corpus to the live configured model via `scripts/run_live_eval.py` and
  records pass-rate trend artifacts. It is budget-capped and never gates a
  merge: a bad model week is signal, not a broken build.
- **Deterministic replay.** The cassette lane replays recorded traffic
  through the production planner code path — prompt drift is caught
  structurally, without sampling (pass^k over a deterministic component
  measures nothing).

## What is NOT yet proven

Stated plainly, because a validation doc that only lists green checks is
marketing:

- **Hardware sign-off.** Everything above runs against simulated hardware
  and recorded traffic. The physical checklist in
  [`DEPLOYMENT.md`](DEPLOYMENT.md) §7 — real ESTOP timing, watchdog stop on
  a real print, cold-start behavior — must be executed attended on the Pi
  before `v6.6.0` is tagged. Until then, treat every "stops the machine"
  claim as *stops the simulated machine*.
- **Unattended operation.** The operator notification channel is not yet
  rebuilt for v6; attended-operation-only is the signed rule
  ([`SECURITY.md`](../SECURITY.md)).
- **Vision vocabulary.** Catastrophic states without a first-class finding
  type (detachment, collision) currently rely on the model tagging
  `spaghetti`; the enum extension is roadmap work.
- **The remaining ratchets.** Seven files still exceed the 800-line cap and
  337 device-knowledge references remain in core modules — both enforced
  shrink-only, both recorded per-file in [`ROADMAP.md`](ROADMAP.md).
