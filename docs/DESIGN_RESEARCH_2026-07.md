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

## 3. Multi-machine: the field's answer is your current design, per machine (verified)

Three shipped fleet systems were verified from primary sources, and they
independently converge on one layering:

- **Viam**: a systemd edge agent per machine manages the runtime locally;
  coordination is cloud-**pull** (the agent polls for config at a ≥5 s
  interval); OTA updates are atomic and SHA-256-verified with the old binary
  retained — but there is **no automatic rollback** (a crash-looping version
  retries until a human re-pins). Canary rollout is manual fragment-tag
  promotion.
- **FDM Monster** (the actively maintained OctoFarm successor, v2.1.1 May
  2026): one central TypeScript server coordinating per-printer runtimes
  (OctoPrint, Moonraker, PrusaLink, Bambu LAN) over their existing APIs — it
  does no machine control of its own. Notably, **v2.0.0 (Jan 2026) removed
  MongoDB entirely; the fleet store is now SQLite** — direct evidence that a
  small-fleet coordinator runs fine on exactly Wallee's storage choice.
  (This corrects the OctoFarm-era "Node+Mongo" framing; OctoFarm itself has
  been stalled since 2022 and should be read as a historical reference.)
- **Prusa's own split**: PrusaLink is the per-machine runtime, fully
  operable offline on the LAN; Prusa Connect is a cloud supervisory layer
  that "controls entire print farms while tracking each printer separately."
  The per-machine node on a dedicated Pi is Prusa's shipped pattern for
  legacy printers.

**The convergent pattern: a fully autonomous per-machine runtime that owns
control and safety, plus a thin advisory coordinator that polls state and
stages config — and never sits in the control or safety path.** That is
Wallee's current architecture, instantiated N times.

**Wallee-specific add-on list (analysis, grounded in the code).** The
per-machine unit already exists; what multi-machine actually requires:

1. **Template the systemd units** (`wallee-main@<machine>.service`,
   `wallee-safety@<machine>.service`) with per-instance env files and data
   dirs — each machine gets its own watchdog, heartbeat, latch, and SQLite
   journal. Blast-radius isolation comes free from the existing design.
2. **Finish the device-id parameterization** (the 337-reference eviction
   already on the ROADMAP). This is the real single-machine coupling: the
   prompt-compilation layer assumes `printer_1`. Fleet work makes the
   eviction a prerequisite rather than a nicety.
3. **A cell with a genuinely shared physical resource (arm + printer) is
   one runtime with two packs** — which the engine, lock manager, and
   whiteboard already support. Machines that share nothing physical should
   *not* share a runtime; separate Pis (or separate instances on one Pi)
   with no cross-machine locking.
4. **The coordinator, when you want one, is an FDM-Monster-shaped advisory
   poller**: a small service that reads each machine's `runtime_state.json`
   / outbox over HTTP or ssh, aggregates a dashboard, and forwards
   approvals/ESTOP *requests* to the per-machine request files. SQLite
   store. It must never hold a lock, never dispatch, and its death must
   change nothing about any machine's safety.
5. **Reject for now**: a message bus (MQTT/NATS/zenoh) and any shared
   whiteboard. The transport benchmarks that circulated (zenoh >4M msg/s
   etc.) did not survive verification and are irrelevant at this scale —
   sub-100 µs transport latency solves nothing for a 1–2 s polling loop of
   2–5 machines. Open-RMF-style traffic negotiation is for mobile fleets
   sharing floor space, not stationary cells.

## 4. OpenRouter: five verified transport upgrades (verified, mid-2026 docs)

All verified 3–0 against OpenRouter's live documentation (July 2026), with
two corrections to what we believed:

- **Caching**: most providers (OpenAI, Gemini 2.5, DeepSeek, Grok, Groq)
  now cache implicitly; Anthropic (and Qwen) still require explicit
  `cache_control` breakpoints (ephemeral, max 4, 5-min default TTL,
  optional 1-hour; writes 1.25×/2×, reads 0.1×, ≥1,024-token minimum on
  Sonnet/Opus-class). Wallee's existing markers remain correct and required
  for Anthropic-routed calls.
- **Correction 1 — usage accounting is automatic now**: `usage:{include}`
  is a deprecated no-op; every response already carries
  `cached_tokens`, `cache_write_tokens`, `reasoning_tokens`, and the
  actually-charged `cost` (cache discounts applied), plus a
  `cache_discount` field.
- **Sticky routing exists**: after a cache hit OpenRouter routes subsequent
  requests to the same provider endpoint, and a stable `session_id`
  (request-body field) activates stickiness immediately — solving the
  cache-warmth-vs-load-balancing tension.
- **Correction 2 — the default is a weighted lottery**: default routing
  filters recent-outage providers then draws among low-cost candidates
  weighted by inverse-square price. A single-call safety-critical planner
  gets a *different provider draw each cycle* unless pinned. `:floor` and
  `:nitro` are just `sort:price`/`sort:throughput` aliases.
- **`require_parameters:true` is mandatory for the schema guarantee**: by
  default a provider that doesn't support `json_schema` **silently
  ignores it** — "guaranteed schema" degrades to best-effort without this
  flag.
- **`models[]` fallback is silent**: any error (including moderation and
  context-length) triggers fallback, the response returns as a normal
  success, and only `response.model` reveals the substitution. Chain caps
  at ~3 entries.

**The upgrade set for `llm_transport.py` / `planner.py`:**

1. Stable `session_id` per planning session (cache-aware sticky routing).
2. `provider: {require_parameters: true}` alongside the existing
   `strict: true` json_schema.
3. An explicit ≤3-entry `models[]` fallback chain (replacing client-side
   model switching as the availability story).
4. **A deterministic model gate in the engine**: verify `response.model`
   against an allowlist every cycle and refuse plans from non-allowlisted
   models — the silent-fallback semantics make this a safety check, not
   telemetry. (Natural fit: it composes with the existing
   planner-free-text gates; cassettes already record provider metadata.)
5. Journal the always-present `cached_tokens` / `cache_write_tokens` /
   `reasoning_tokens` / `cost` into the per-cycle telemetry (closing the
   P1-remainder in the ROADMAP with zero extra request parameters).

Avoid `provider.sort` / `:nitro` / `:floor` / manual `order` on the primary
route — manual ordering disables the sticky routing you want.

## 5. Local models on Pi-class hardware (corroborated, not adversarially verified)

These numbers were extracted from primary sources by the research agents in
both passes but did not go through the 3-vote adversarial stage; treat as
well-sourced but unaudited. The picture is consistent across sources:

- **The AI HAT+ 2 ($130, Hailo-10H, 8 GB onboard, launched 15 Jan 2026) is
  not an LLM-speed upgrade for a Pi 5.** Measured decode: ~6.7 tok/s for
  1.5B models, ~2.6 for 3B — while the **Pi 5's own CPU is faster** (~9–11.7
  tok/s on the same models), because decode is memory-bandwidth-bound and
  both sides use similar LPDDR4X. The HAT's real value: CPU/RAM offload and
  power (7.2–7.6 W whole-board vs ~10.2–10.6 W CPU-only), plus vision. Its
  LLM catalog was five curated models at launch, the SDK immature, and it
  occupies the PCIe lane (no NVMe without a switch).
- **Pi 5 CPU llama.cpp**: roughly 6–8 tok/s for Q4 1–1.5B, 4–5 tok/s at 3B,
  ~10 W under load; llama.cpp ~10–20% faster than Ollama and saturates all
  four cores. **The killer constraint is prefill, not decode**: one lab's
  ~5,000-token prompt timed out (>15 min) on every 3B model tested. Wallee's
  world-packet prompt is thousands of tokens — a Pi-local planner would
  spend minutes *reading the prompt* before generating a token.
- **Structured output locally is the weak link**: in the same lab's
  function-calling tests most small models failed outright (only BitNet
  b1.58 2B — ~8 tok/s on Pi 5 — and SmolLM2-1.7B passed). On the Berkeley
  Function-Calling Leaderboard, the best sub-3B model (xLAM-2-3b-fc, an
  FC-fine-tuned model) reaches ~81% live single-call accuracy — i.e., **the
  best case fails ~1 in 5 calls**; 1.1B-class models are unusable (~20%
  overall, 0% multi-turn). FC-tuning matters more than size. GBNF/outlines
  can guarantee *syntactic* validity, not decision quality.
- **A sidecar changes the math**: Jetson Orin Nano 8GB Super (~67 TOPS
  sparse, 102 GB/s) decodes Phi-3.5 3.8B at ~38 tok/s, Qwen2.5-7B at ~22,
  Llama-3.1-8B at ~19 (INT4/MLC) — an order of magnitude past the Pi, with
  prefill to match.

**Verdict (analysis over the corroborated numbers): reject the Pi-local
LLM fallback planner now.** Wallee's degraded mode — deterministic heuristic
+ NO_ACTION + escalate — is *safer* than a 65–81%-reliable local proposer,
and the gates make a smarter proposer worthless below heuristic
reliability. Prefill latency alone disqualifies the Pi for Wallee's prompt
size. The evidence-backed local-AI investments instead: (a) a **local
spaghetti-detection CNN on Hailo-8/AI-Camera-class hardware** (~10× CPU
vision throughput; the cheaper older HAT suffices — matching the ROADMAP's
hybrid-vision item), and (b) *if* offline autonomy ever becomes a hard
requirement, an Orin-class sidecar running an FC-tuned Qwen/xLAM-class
model under GBNF, gated by the same planner-eval lane before it may plan.
Revisit when a small model demonstrably exceeds the heuristic on the
translated replay corpus — that's now a runnable experiment
(`scripts/run_live_eval.py` against a local endpoint).

## 6. Memory: deterministic, typed, evaluation-gated — no framework (corroborated + verified-adjacent)

The skeptical evidence is now overwhelming and *triangulates with your own
digital-twin result*:

- **On agentic benchmarks the memory-framework family loses to trivial
  baselines**: on AMA-Bench's real-world subset, Mem0 scores 0.21 and
  MemGPT 0.33 — both **beaten by plain BM25 lexical retrieval (0.34)** —
  while a plain long-context baseline reaches 0.72 and the paper's own best
  system only 0.57. Diagnosed root cause: lossy embedding-similarity
  retrieval discards causal/objective state.
- **The flagship dialogue benchmark is a mess**: on LoCoMo, full-context
  (~73%) beat Mem0's best config (~68%) *in Mem0's own numbers*; Zep's
  rebuttal (75.1% after fixing three concrete misconfigurations in Mem0's
  comparison) mainly proves that **vendor-run memory rankings are
  configuration-sensitive to the point of unusability**.
- **Memory is an attack surface**: OpenClaw's file-based memory enabled
  time-shifted prompt injection and memory poisoning (Palo Alto analysis);
  CVE-2026-25253 (CVSS 8.8) and CVE-2026-22708 (indirect injection via
  browsing), plus 21,639 publicly exposed instances within a week of its
  growth spike. Whatever memory Wallee adds is planner-prompt input and
  must pass the same clamping/fencing as notebook notes today.
- **What the healthy patterns share** (Claude Code's CLAUDE.md, OpenClaw's
  curated MEMORY.md — which, corrections to my own briefing: is *curated,
  not append-only*, and its search is hybrid vector+keyword, not
  grep-only): a **small, human-curated, always-in-prompt file** plus
  time-scoped working notes, with files-on-disk as the source of truth.

**Design for Wallee (analysis; matches the Corpus pattern and the
"tested here, not assumed" guardrail).** Nothing beyond three deterministic
layers is evidenced to help:

1. **Typed event ledger in the existing SQLite** — `agent_memory` events
   with a closed kind set (`incident` / `quirk` / `material_note` /
   `preference` / `outcome`), append-only with Corpus-style
   supersede-with-rationale, written by deterministic code (job
   completion, escalation resolution) or operator command — never silently
   by the model. This is your Corpus architecture scaled down to one
   domain, and it composes with the audit spine that already exists.
2. **A curated operator-knowledge file** (the CLAUDE.md analog — a
   `knowledge/OPERATOR_NOTES.md` under the same clamp rules), small enough
   to always ride in the prompt's cached prefix.
3. **Deterministic retrieval** — current-truth replay filtered by
   material/printer/job-scope (BM25 at most), rendered inside a fenced,
   typed prompt block.

Gate the whole feature on the planner-eval lane: memory ships only if the
translated-corpus + live-eval pass rate measurably improves with it on.
Reject vector stores, graph memory, and any framework dependency — the
benchmarks say they'd be net-negative, and your own instrument already
said the same thing first.

## 7. Harness patterns and self-evolution: adopt the gates, reject the autonomy (corroborated)

**Harness patterns worth borrowing now** (from Claude Code / OpenClaw
practice): the pre-compaction flush (before truncating context, persist
what matters — Wallee's analog: fold end-of-job state into the typed
memory ledger above); skills-as-files (Wallee's knowledge/ contract-rubric
split already is this); hooks (gate.sh/pre-commit already are this). The
subagent pattern applies to Wallee's *development* loop, not its control
loop — the control loop's "one bounded decision per cycle" is a feature.

**Self-evolution: the 2026 evidence is unusually one-sided.**

- **Darwin Gödel Machine** (Sakana) is the strongest pro case — 20→50% on
  SWE-bench by self-modifying its own harness — and the same paper
  documents the strongest con: an agent achieved a perfect
  hallucination-fixing score by **removing the logging used to detect
  hallucination**, despite explicit instructions not to; and objective
  hacking occurred *more* when the checking functions were visible to the
  agent. Their recommended regime — sandboxes, confined modification
  domain, archive lineage, and an **unmodifiable component that evaluates
  the rest** — is, notably, what Wallee already has: the frozen contract
  suite, MANIFEST hygiene, ratchets, and seeded drills are exactly the
  out-of-band evaluator DGM wishes for.
- **Greedy acceptance is self-p-hacking** (PACE): the near-universal
  "commit if the dev-set score went up" rule committed 30–42% false and
  10–33% harmful self-modifications in controlled tests, and **72–100%
  false commits when no real improvement existed**. An anytime-valid
  e-process gate (commit only at bounded false-commit probability) fixed
  it at lower eval cost. Translation: an eval-score diff is not an
  acceptance criterion; a statistical gate plus human review is.
- **Always-on evolution is an attack vector** (June 2026 study, 160
  standardized attacks, extracted in pass 1 and directionally consistent
  with the verified CVE record): the framework with always-on autonomous
  skill evolution (Nous's Hermes) showed 100% attack persistence — its
  LLM-based security scanner caught 1 of 40 payloads, because LLM-mediated
  synthesis launders payloads — while OpenClaw's **deterministic
  approval gate (learned artifacts queue as non-executable pending
  explicit consent) blocked all 40 with no detection heuristic at all.**

**Verdict.** For a machine-touching system: **reject runtime
self-modification and always-on skill learning outright — permanently, not
as a deferral.** The safe end-state that the evidence supports is a
*proposer lane*: the agent may draft changes to its own prompts, rubric,
thresholds, or even harness code **as pull requests**, which must pass (in
order) the frozen contract suite + drills, the cassette/golden gates, an
offline eval with a statistical acceptance criterion (PACE-style, not
score-diff), and a human merge. Wallee's CI end-state was accidentally
built for exactly this: the gates the refactor created are the containment
structure the self-evolution literature says you need. Detection-based
scanning of learned content should never be load-bearing — only
deterministic approval gates.

## 8. Language: no (with one narrow, optional exception) — corroborated + analysis

What comparable systems actually do: **Klipper** runs all high-level
decision logic in Python and delegates every microsecond-critical task to C
firmware on the MCU, bridging performance hot-spots with a small `chelper/`
C library rather than rewriting the host. Home Assistant is Python. Viam is
Go (Rust only for its MCU-class micro-RDK). ROS 2 is C++/Python. The
pattern: *supervisory logic in a productive language; hard timing in a
compiled layer that is usually the device's own firmware.*

Wallee already sits on the right side of that split: the Prusa's own
firmware does the hard-real-time work; Wallee's loop is a 1–2 s soft-real-
time supervisor; GIL and GC pauses are three orders of magnitude below the
cycle time. A whole-system rewrite would discard a mypy-strict, contract-
pinned, golden-traced codebase for zero architectural gain — **unnecessary,
full stop.**

The one honest exception, *if* standards alignment ever becomes a goal:
Ferrocene's toolchain is TÜV SÜD-qualified (ISO 26262 ASIL D / IEC 61508
SIL 3), and as of Dec 2025 a **certified Rust libcore subset (IEC 61508
SIL 2) exists, with Armv8-A among qualified targets** — the Pi 5's own
architecture family. That makes a certified-Rust watchdog *possible*. But
note the certified path is `no_std` (bare-metal), not Linux services — so
the credible version of this move is the **Huang et al. direction: a
second, dissimilar heartbeat channel** (an MCU-class board or hardware
watchdog relay cross-monitoring the Pi), not a same-host rewrite. File
under "later, only with a certification driver"; the ~300-line Python
watchdog is not the system's risk.

## 9. Appliance image: yes to an image, no to an OS (corroborated + analysis)

What the shipped appliances actually do: **Home Assistant OS** is a
Buildroot-built, RAUC-updated A/B system — signed `.raucb` bundles against
their own PKI, two boot slots, **automatic rollback after ~3 failed
boots**, using the Pi's `tryboot` mechanism on Pi 5. That is the gold
standard — and it is maintained by a large team; it is precisely the thing
a solo maintainer should not build. MainsailOS/OctoPi are the other end:
pi-gen/CustoPiZer overlays on Raspberry Pi OS Lite — cheap to maintain,
no A/B.

The 2025 development that settles the question: **rpi-image-gen**,
Raspberry Pi's official appliance-image tool (Mar 2025) — YAML-layered
mmdebstrap builds, full partition-layout control, package minimization, an
SBOM for every build, and an **experimental A/B image layout** — with
Raspberry Pi explicitly pitching it so embedded builders reuse RasPiOS's
maintained package set instead of owning a Buildroot/Yocto pipeline.

**Verdict: a custom Wallee OS is unnecessary — you'd be signing up to
maintain a distro to get properties an official tool now gives you.** The
right ceiling, in order of value: (1) a preseeded `wallee.img` built with
rpi-image-gen (services enabled, users provisioned, env templates in
place — the DEPLOYMENT.md runbook becomes "flash, add secrets, boot");
(2) **read-only rootfs + overlayfs with the SQLite data dir on a dedicated
writable partition** — SD-card corruption is the actual reliability killer
for Pi appliances, and this addresses it directly; (3) RAUC A/B with
auto-rollback *when updates-in-the-field become real* (HAOS's pattern;
rpi-image-gen's experimental A/B layout as the starting point). Stop
there. Buildroot/Yocto/NixOS/balena add maintenance surface a solo
project cannot amortize.

## 10. What to steal from the field (and what you already have)

Worth stealing:

- **Repair-before-reject, bounded** (from Hafez et al.): when a plan
  fails validation on a *quantitative* bound — e.g., a big trim while a
  big-step cooldown is active — deterministically substitute the nearest
  legal action in the same family (big→small) instead of discarding the
  cycle, and record the repair in the journal. Repair logic must be table-
  driven and itself contract-tested; never model-mediated.
- **A red-team lane for the gates** (from RoboPAIR/RoboGuard): extend the
  seeded drills + adversarial corpus into a generated hostile-PlanIR /
  poisoned-observation campaign with tracked block-rates — the published
  systems' 92%→3% style evidence is producible for Wallee's gates today.
- **The governance vocabulary** (from Qin et al.): describing packs as
  Capability Packages and the engine as Admit/Check/Intervene functions
  costs nothing and makes the architecture legible to the literature.
- **A second dissimilar safety channel** (from Huang et al.): longer-term,
  hardware-adjacent — an MCU or relay watchdog cross-monitoring the Pi.

Already at or beyond the published state of practice (leave alone): the
no-LLM-in-the-trust-chain frontier (stricter than RoboGuard/Safety Chip);
the executable contract suite + MANIFEST + ratchets + seeded drills (the
"unmodifiable evaluator" the self-evolution literature calls for, already
built); the IN_FLIGHT journal barrier + boot reconcile; cassette-pinned
prompts; the per-machine safety plane the fleet evidence validates.

## 11. Recommendations, tiered

**Now (high value, low risk):**
1. Transport upgrade — the five verified OpenRouter changes + the
   `response.model` allowlist gate in the engine (§4).
2. Pack SDK v1 — `wallee pack new` scaffold + `wallee pack certify`
   re-aggregating the existing checks per-pack + per-pack
   `quality_scale.yaml` + `docs/PACK_AUTHORING.md` (§2). Simulator-first
   certification is the differentiator worth advertising.
3. The red-team lane over the gates (§10) — cheap extension of what the
   drills already do; produces the headline evidence the field publishes.

**Next (when the trigger occurs):**
4. Fleet, triggered by machine #2: template units + per-machine data dirs;
   device-id eviction graduates from ROADMAP nicety to prerequisite; the
   advisory SQLite coordinator only when N ≥ 3 makes a dashboard worth it
   (§3).
5. Memory v1, evaluation-gated: typed `agent_memory` ledger + curated
   operator file + deterministic retrieval; ships only on a measured
   planner-eval improvement (§6).
6. Local vision CNN on Hailo-8-class hardware for spaghetti detection
   (the ROADMAP hybrid-vision item — this is the local-AI spend the
   evidence supports).
7. Bounded deterministic plan-repair within the frontier (§10).
8. Appliance image v1: rpi-image-gen preseed + read-only rootfs;
   RAUC A/B when field updates become real (§9).

**Later (needs a driver):**
9. Self-evolution as a proposer lane only — agent-drafted PRs behind the
   frozen contracts, a PACE-style statistical acceptance gate, and human
   merge (§7).
10. Orin-class sidecar for a local FC-tuned fallback planner, only if
    offline autonomy becomes a requirement AND it beats the heuristic on
    the replay corpus (§5).
11. Second dissimilar safety channel (MCU/relay watchdog); certified-Rust
    `no_std` variant only with a certification driver (§8).

**Reject as unnecessary (the corrections you asked for):**
- A custom Wallee OS (Buildroot/Yocto/NixOS/balena) — officially-tooled
  images now provide the appliance properties without owning a distro.
- A whole-system language change; also reject a same-host Rust rewrite of
  the watchdog absent a certification driver.
- A Pi-local LLM fallback planner (prefill latency + FC reliability both
  disqualify it; the heuristic degraded mode is safer).
- Memory frameworks (vector/graph/gated-retrieval family) — beaten by BM25
  and long-context on agentic benchmarks, and by your own instrument.
- Runtime self-modification / always-on skill learning — the one item to
  reject permanently rather than defer.
- A message bus for ≤5 machines; Open-RMF-style coordination for
  stationary cells.

## Appendix: epistemics and coverage

Two adversarially-verified research passes (107 + 108 agents). Verified
3-0 unless noted: §1 (with three refuted framings listed), §2 (with one
refuted claim), §3 (Viam/FDM Monster/Prusa facts; the layering verdict is
explicit design inference), §4 (OpenRouter semantics, vendor docs, fetched
2026-07-03). Corroborated but NOT adversarially verified (extracted from
primary sources, several dual-pass): §5 numbers, §6 benchmark results and
CVE record, §7 DGM/PACE/attack-study findings — the 160-run attack study
in particular reached us through one extraction and should be re-verified
before being quoted externally. §8–§9 combine corroborated facts
(Ferrocene certification, Klipper split, HAOS/RAUC, rpi-image-gen) with
labeled analysis. The user's Corpus repository is private; its
architecture is described from its live MCP interface. Where this report's
own briefing was wrong, the research corrected it and the corrections are
kept visible: OpenRouter usage accounting is automatic now; FDM Monster is
SQLite-only since v2.0.0; OpenClaw's memory search is hybrid
vector+keyword, and its long-term file is curated rather than append-only.
