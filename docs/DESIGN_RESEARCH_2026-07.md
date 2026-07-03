# Design research — July 2026

**Scope.** Nine threads: pack ergonomics, multi-machine fleets, OpenRouter
optimization, local models on Pi-class hardware, memory architecture, agent
harness design and self-evolution, language choice, a Wallee appliance OS,
and benchmarking against the field. Method: two adversarially-verified
deep-research passes (claims required 2-of-3 independent verification votes
to survive; refuted claims are listed where they constrain the narrative),
grounded against the actual code seams in this repository. Verdicts include
"unnecessary" where the evidence says so — that was the assignment.

**The one-paragraph conclusion.** The field has converged on Wallee's shape:
four independent 2024–2026 systems publish "LLM proposes, deterministic code
disposes" with numbers, and in one respect Wallee is *stricter* than any of
them — its action frontier is compiled by deterministic pack code with no
LLM anywhere in the trust chain, where RoboGuard and the Safety Chip both
put a (shielded) LLM inside spec generation. The highest-leverage
improvements are not architectural rewrites but: a pack SDK replicating the
Home-Assistant/Viam/OctoPrint state of practice; a runtime-per-machine +
thin-supervisor fleet story that reuses the existing safety plane; a
transport upgrade adopting OpenRouter's routing/fallback/accounting
features; and deterministic, evaluation-gated memory. The custom OS is
unnecessary (a preseeded image + read-only rootfs + A/B updates is the
proven ceiling); a whole-system language change is unnecessary (with one
narrow, optional Rust exception); and always-on self-evolution should be
rejected outright — the 2026 evidence for that rejection is unusually
strong.

---

## 1. Where Wallee stands against the published field (verified)

Four systems from 2024–2026 independently validate the architecture, each
adversarially verified from primary sources:

- **RoboGuard** (IEEE RA-L 2026, UPenn GRASP; arXiv:2503.07885) — a
  two-stage guardrail where a shielded root-of-trust LLM grounds predefined
  safety rules into LTL specifications and deterministic Büchi-automaton
  control synthesis enforces them. Reduces execution of unsafe plans **from
  >92% to <3%** (92.3% → 2.3–2.5% under the authors' own RoboPAIR jailbreak
  attacks, 210 simulation evaluations plus a real Clearpath Jackal) without
  degrading safe-plan performance. The load-bearing point: formal structure,
  not natural language, is the interface between the untrusted planner and
  the controller — exactly Wallee's PlanIR-over-frontier-ids gate.
- **The Safety Chip** (ICRA 2024, Brown; arXiv:2309.09919) — the clearest
  published proof that *prompting cannot enforce safety*: with constraints
  given in natural language for the LLM to self-enforce, safety collapsed as
  constraint count grew — **beyond five constraints, NL self-enforcement
  fell to 0% and Code-as-Policies to 40%, while the external deterministic
  LTL module held 100% safety at 98% task success** on a real Boston
  Dynamics Spot. (2023-era model, small samples — the durable result is the
  collapse pattern, not the exact rates.)
- **Qin et al. 2026** (arXiv:2604.07833) — formalizes essentially Wallee's
  architecture as "runtime governance externalized from agent cognition":
  capability packages (= packs), a governance layer performing admission /
  policy checking / execution monitoring / rollback / human override, and
  the triad "the agent proposes what it wants; the Capability Package
  defines what can be executed; the governance layer determines what may be
  executed now." 96.2%±2.7% interception of unauthorized actions over 1,000
  Gazebo/UR5e trials. (Preprint, simulation-only, companion metrics weaker —
  its value to Wallee is the shared vocabulary, not the headline number.)
- **Hafez et al.** (arXiv:2503.03911) — GPT-4o proposes navigation plans; a
  data-driven reachability layer verifies at runtime that all reachable
  trajectories stay safe. Two mechanisms worth studying (§10):
  **repair-before-reject** (a near-miss plan is first *adjusted* by
  projected gradient descent rather than discarded) and an **always-available
  verified fallback** (revert to the last verified-safe plan with an
  embedded braking maneuver).

For the watchdog specifically, Huang et al. (arXiv:2604.20193) is the
closest published analogue on commodity hardware: LLM confined to design
time; all runtime safety deterministic (C++17, `SCHED_FIFO`); symmetric
dual-modular redundancy across two RK3588 SoCs cross-monitoring via UART
heartbeats plus ADC probing of the peer's voltage rails (NPU hang detected
in 2.04 ms) — presented as a practical path toward ISO 13849 Cat 3 / PL d
on non-safety-PLC hardware. This validates Wallee's heartbeat-watchdog
split as the recognized dual-channel pattern and sketches what a hardened
evolution would look like (a second, cross-monitoring safety node).

A peer-reviewed 2025 Frontiers review of 30 real-world LLM-robotic systems
supplies the comparison set; within it, Code-as-Policies' safety story —
LLM emits arbitrary Python that a human *may* inspect — is strictly weaker
than Wallee's machine-enforced closed frontier.

**Refuted framings (0–3 verification votes; do not repeat them):** that
pre-execution filtering is "table stakes" and post-action verification the
real differentiator; that SayCan-style value-function filtering is the
field's dominant pattern; that the surveyed literature lacks audit/oversight
mechanisms (i.e., no claiming Wallee exceeds *all* published systems).

**What this means:** the architecture needs no re-foundation. The honest
gaps the comparison exposes are (a) no published red-team of a
closed-frontier system the way RoboPAIR red-teamed RoboGuard — Wallee's
seeded drills could grow into that harness (§10), and (b) all Wallee's
disposers are veto-style; the repair-within-frontier idea is the one
mechanism the field has that Wallee lacks (§10).

## 2. Pack SDK: replicate the state of practice, don't invent (verified)

Three mature ecosystems independently converged on the same authoring triad,
all verified live in July 2026:

1. **First-party codegen scaffold** — Home Assistant:
   `python3 -m script.scaffold integration`; Viam: `viam module generate`
   (language/visibility/resource-subtype flags); OctoPrint: an official
   Cookiecutter template wrapped by `octoprint dev plugin:new`. Scaffolds
   are the *expected entry point* everywhere. **Verified negative:** the HA
   scaffold does **not** auto-generate conformance tests (claim refuted 1–2)
   — the scaffold and the conformance suite are separate deliverables.
2. **A mandatory, machine-checkable conformance baseline with a declarative
   per-rule manifest** — HA's Bronze/Silver/Gold/Platinum quality scale
   (ADR-0022): every new core integration must meet Bronze; per-integration
   `quality_scale.yaml` records each rule as done/exempt *with the exemption
   reason*; tier changes go through review. Actively enforced (promotions
   shipped in HA 2026.7).
3. **A semver'd, namespaced registry with explicit visibility** — Viam's
   model: `namespace:name` module IDs, SemVer 2.0.0,
   private/public/public-unlisted, `meta.json` generated at registration.

**Wallee mapping (grounded in this repo).** The pack *contract* is already
small — four abstract methods (`publish_raw_state`, `normalize`,
`candidate_actions`, `_realize`); the journal/idempotency barrier is
inherited from `BasePack.execute`. The sim pack is ~150 lines. What makes
the Prusa pack ~14k lines is optional hardware tooling (smoke suite 4.8k,
job notebook 1.7k, vision 1.3k) — not the contract. So the SDK is
packaging, not redesign:

- `wallee pack new <name>` scaffold: manifest.yaml, the four methods with
  honest TODOs, a paired sim driver, a conformance test file, README/
  CAPABILITIES stubs. (Cookiecutter-simple; no new mechanism.)
- `wallee pack certify <name>`: runs the existing machinery against the
  candidate pack — manifest schema validation, repo-contract rules
  (DEVICE_ID, CANCEL approval floor, README), a generic frontier-contract
  suite (every candidate action bounded/typed/hazard-classed, verify or
  expected_delta present, no free-text args), golden-trace capture over the
  pack's own sim driver, and the adversarial gate corpus re-run against the
  pack's frontier. This is HA's Bronze gate in Wallee's idiom — and most of
  the checks already exist as repo-wide scripts; certify is a per-pack
  re-aggregation.
- A `quality_scale.yaml`-equivalent per pack (done/exempt-with-reason per
  rule) so conformance state is auditable — consistent with the existing
  MANIFEST/ratchet ethos.
- **Defer the registry** until third-party packs exist (Viam's model is the
  blueprint when they do). A `docs/PACK_AUTHORING.md` walking through the
  sim-pack-first flow covers the gap meanwhile.
- The **simulator-first rule is the safety innovation Wallee can add** to
  the triad: certification requires a working sim driver, so every pack is
  testable (and CI-covered, and golden-traceable) without hardware. None of
  the three benchmarked ecosystems requires this.

<!-- PASS2:FLEET -->
<!-- PASS2:OPENROUTER -->
<!-- PASS2:LOCAL_MODELS -->
<!-- PASS2:MEMORY -->
<!-- PASS2:SELF_EVOLUTION -->
<!-- PASS2:LANGUAGE -->
<!-- PASS2:OS -->
<!-- PASS2:RECOMMENDATIONS -->
