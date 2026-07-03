# v7 design sketch — the general-purpose physical-agent harness

Status: direction sketch, July 2026. Follows
[DESIGN_RESEARCH_2026-07.md](DESIGN_RESEARCH_2026-07.md); supersedes nothing
yet. v7 is an **evolution behind the pinned contracts, not a rewrite** — the
v6 refactor proved that goldens + cassettes + contract suites make even
tree-scale surgery provable, and v7 uses the same method.

## Thesis

Wallee v6 proved the safety architecture on one printer. v7 makes the same
architecture a **general-purpose harness**: any machine, N machines, other
runtimes above it — while keeping the posture that already beats
personal-agent harnesses structurally. Calibration against OpenClaw (the
ergonomics benchmark): its incident record (RCE CVE, injection-via-browsing,
~21k exposed instances, memory poisoning) comes from surface Wallee refuses
to have — free-form tools, browsing, open-ended memory in the prompt path.
v7 keeps the posture, steals the ergonomics, and generalizes the device
model. "More robust" is delivered as published numbers (gate block-rates),
not a claim.

## Pillar 1 — Capabilities are the core's only vocabulary (keystone)

Replace device knowledge in the core with a **typed, versioned capability
vocabulary**:

- Capability schemas: `process_control@1` (pause/resume/cancel semantics),
  `thermal_setpoint@1` (bounded-trim grammar), `motion_enable@1`,
  `camera_observe@1`, `scalar_telemetry@1`, … Each schema carries its
  **safety defaults in the type** — `process_control@1` itself declares
  cancel-class verbs approval-gated, moving the destructive-action floor
  from a repo-contract regex into the schema. A pack cannot forget it.
- A pack = manifest (capabilities implemented + config) + thin adapters
  mapping capability calls onto the device's real API.
- Frontier compilation, prompt view, gates, verification, and a typed
  FactKey registry (Prune item 14 lands here) operate on capabilities only;
  fact keys become `<device>.<capability>.<field>`.
- Consequences: **more printer types by construction** (Moonraker/Klipper,
  OctoPrint, Bambu-LAN packs are each mostly HTTP mapping onto the same
  three capabilities); **other hardware is additive** (`relay@1`,
  `dosing_pump@1`, `spindle@1` — new schemas, zero core change).
- This pillar absorbs three standing debts as ONE deliberate
  behavior-change milestone: the core-purity eviction (337 refs), the
  TuningDescriptor redesign, and the cassette re-record.

## Pillar 2 — Safety plane as a versioned protocol

The watchdog already talks to the runtime only via files (heartbeat mtime,
`profile.json`, `estop.latch.json`, request files) — it is a protocol, not
Python. v7 formalizes it: a versioned spec + conformance tests, with three
implementations over time:

1. **Python** (stays): the sim/test reference.
2. **`wallee-sentinel` (Rust, same host)**: a single static binary
   (~0.5–1.5k LOC; serde + minimal HTTP; monotonic loop; systemd
   `sd_notify` + `WatchdogSec=` so systemd's hardware watchdog supervises
   the sentinel). Buys: no interpreter/venv on the appliance image,
   millisecond crash-restart, no GC in the one process that must never
   pause, an afternoon-auditable dependency tree. Provable by running the
   existing contract suite (SIGKILL e2e, latch semantics) against the
   binary through the same file protocol. Cost: cargo cross-compile in CI +
   Rust literacy for one component. Optional; deployment-grade.
3. **Dissimilar-hardware sentinel (MCU / Pi Zero 2)** — the real safety
   upgrade is failure-mode independence, not language: heartbeats over
   USB-serial/GPIO, independent stop authority (HTTP over its own
   interface, or a **dry-contact relay into any machine's enable/ESTOP
   input** — hardware-grade stops for devices whose APIs we don't trust).
   Covers Pi kernel panic, SD death, PSU brownout. Certified path exists if
   ever needed (Ferrocene: qualified compiler ASIL D / SIL 3; certified
   libcore Dec 2025; Armv7E-M targets — Cortex-M4/M7-class, not RP2040's
   M0+).

The core stays Python throughout — cognition in a productive language, the
compiled layer only where shared fate or hard timing demands it (the
layering every comparable system uses: HA, Viam, ROS 2, Klipper).

## Pillar 3 — Kernel/cortex split

Make the trust boundary physical in the package layout:

- **`wallee-kernel`** (trusted computing base): engine gates, journal,
  approvals, safety-protocol client, capability registry. Target <5k LOC,
  absolute file-size caps, frozen contracts, mypy-strict everywhere, no
  network beyond stop/approval transports.
- **`wallee-cortex`** (planner harness): world compilation, prompt
  assembly, transport, memory retrieval, evals. Iterates fast; everything
  it emits passes through kernel gates.
- Packs via the SDK + per-pack conformance (research report §2).

This split is also the pre-condition for the (later, gated) self-evolution
proposer lane: cortex may eventually be modified aggressively because the
kernel is the unmodifiable out-of-band evaluator the DGM literature calls
for.

## Pillar 4 — Ergonomics without the attack surface

- `wallee up`: zero → running sim in one command; pack detection probes
  attach real hardware behind a flag.
- Pack SDK: `wallee pack new` / `wallee pack certify` +
  per-pack quality manifest (HA-Bronze analog); registry only when
  third-party packs exist (Viam model). Simulator-first certification is
  the differentiator no benchmarked ecosystem requires.
- Appliance image: rpi-image-gen preseed, read-only rootfs + overlayfs,
  one persistent data partition (SQLite WAL + safety dir — the latch must
  survive reboot), journald volatile (the events table is the durable
  log), RTC coin cell, dev-mode escape hatch; RAUC A/B when field updates
  become real. The image is a packaging artifact — pip/dev/server modes
  remain fully supported on any systemd Linux.
- Operator gateway (Telegram/webhook) as **dumb pipes over the existing
  outbox/approval files**: inbound messages can only approve / reject /
  ESTOP / status against args-hash-bound records. No path from chat text
  to machine action that bypasses the gates.

## Pillar 5 — Robustness as published numbers

Red-team lane over the gates (RoboPAIR-style block-rate table in
VALIDATION.md), per-capability conformance goldens, transport hardening
(incl. the deterministic `response.model` allowlist gate), fleet
templating (runtime-per-machine, advisory SQLite coordinator), memory as a
typed append-only ledger in the existing SQLite (Corpus pattern:
closed kinds, supersede-with-rationale, deterministic retrieval;
eval-gated before it ships), bounded deterministic repair-within-frontier.

## Sequencing

1. **v6.6 first** — hardware sign-off + merge; then the research "now"
   tier (transport upgrade, pack SDK v1, red-team lane). None of it needs
   v7.
2. **M1 — capability vocabulary + FactKey** retrofitted under the existing
   packs; the one deliberate prompt-affecting re-record.
3. **M2 — kernel/cortex split** behind goldens; safety protocol doc;
   Rust sentinel.
4. **M3 — proof of generality**: second printer family (Moonraker or
   Bambu) + operator gateway + appliance image.
5. **v7.x** — fleet coordinator, memory ledger, MCU sentinel, proposer
   lane (each behind its trigger).

## What the general-purpose framing does NOT change

The research verdicts stand — custom OS: unnecessary (image + RO-root +
A/B is the ceiling); wholesale language change: unnecessary (Rust only as
the sentinel, optional); memory frameworks: rejected (typed ledger +
curated file + deterministic retrieval); Pi-local LLM fallback planner:
rejected (prefill latency + FC reliability; heuristic degraded mode is
safer); runtime self-modification: rejected permanently. What changes is
priority: the capability vocabulary stops being a refactor nicety and
becomes v7's defining feature.
