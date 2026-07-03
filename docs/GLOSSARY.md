# Glossary

Every term of art in Wallee, one place. Terms link to the code or doc that
owns them.

### Action run
The durable record of one attempt to execute one action
(`wallee/runtime_db.py`, table `action_runs`). Carries the status machine:
`PROPOSED → AUTHORIZED → WAITING_APPROVAL → DISPATCHED → DONE / FAILED /
ABORTED / REPLAN_REQUIRED / UNKNOWN`. Logical workflow truth — distinct from
the exec journal.

### Approval
A human authorization for one hazardous action run, bound to the exact
argument hash and expiring after a window. Recorded durably; an approval
arriving after expiry aborts the run with a re-approval escalation rather
than executing stale intent.

### Cassette
A recorded live planner HTTP exchange (`tests/fixtures/cassettes/`),
replayed offline through the production planner in CI. Fingerprints pin the
outbound prompt/payload: any drift is a red `cassette-replay` check until a
deliberate `[re-record]` commit.

### Contract suite / the six promises
`tests/contract/` — the executable form of Wallee's safety claims (P1–P6),
pinned by a MANIFEST so deleting, renaming, skipping, or weakening an
invariant fails CI (`scripts/check_contract_manifest.py`).

### Control lease
A process-level file lock (`wallee/runtime_control.py`) preventing two
runtime instances from dispatching to the same machine.

### Core purity (ratchet)
The machine-enforced boundary keeping device knowledge (`printer_1`,
vendor names, tuning-id grammar) out of the generic core
(`scripts/check_core_purity.py` + `.core-purity-allowlist.json`). Counts
may only shrink; zero means the P4 xfail flips green.

### Decision signals
The structured, compiled summary block inside the planner's prompt view
(tuning action space, family blockers, vision signal, freshness). The
planner reads it; deterministic gates read only its closed enums.

### Device pack
The unit of hardware ownership (`wallee/packs/<name>/`): raw-state
publishing, normalization, frontier compilation, execution, and a
declarative safety profile, declared by a schema-validated
`manifest.yaml`. One physical device = one pack.

### ESTOP latch
The durable interlock file (`estop.latch.json` under the safety dir).
Present = engaged. Written by kernel trip or watchdog; honored by both;
removed only by explicit attended `clear-estop`. Never expires.

### Exec journal
`exec_journal` rows record that a physical side effect **may have started**
(`IN_FLIGHT` committed before hardware is touched) and how it ended.
Idempotency keys make retries safe; boot reconcile finalizes stranded
`IN_FLIGHT` rows as unknown-outcome and escalates.

### Frontier
The complete, closed list of `LegalAction`s that are legal in the current
world. The planner may only choose frontier ids that were actually shown to
it (`planner_visible_action_ids`); everything else dies at `validate_plan`.

### Golden trace
A byte-exact recorded plan→gate→dispatch→journal trace over the sim packs
(`tests/goldens/`). Refactors must leave goldens identical; intentional
changes are separate `[re-bless]` commits.

### Hazard class
Per-action severity (`LOW`/`MEDIUM`/`HIGH`) declared by the pack.
Auto-approve applies to LOW only; CANCEL-class actions carry
`approval_required=True` by repository contract.

### Heartbeat / beacon
The control loop beats the in-process kernel and writes a beacon file each
cycle; the out-of-process watchdog keys on the beacon's mtime. Stale +
active job → the watchdog stops the machine and latches.

### Job notebook
The Prusa pack's read-only, G-code-derived context: per-section facts and
notes the planner sees as grounded hints (never as commands).

### PlanIR
The planner's only output channel (`schemas/plan_ir.schema.json` +
pydantic): decision, ≤3 frontier ids (or one tuning choice), and a why.
Free text in PlanIR never reaches pack execution.

### Replan
The engine's demand for a fresh plan after verification mismatch, lock
loss, approval expiry, or material world change. Distinct from generation
retry (bad JSON → retry once).

### Run scope
A token tying runs/artifacts to one job (`job:<id>:<file>` or
`state:<lifecycle>:<file>`), built by the one shared builder in
`wallee/planning_context.py`.

### SafetyKernel
The in-process interlock owner (`wallee/safety.py`): heartbeat timeout →
trip; trip → registered stop callbacks (the pack's stop transport) + durable
latch. Injectable clocks make its timing provable with fake time.

### Shrink-only ratchet
The pattern for machine-enforcing a boundary that can't be reached in one
step (core purity, file sizes): current violations are allowlisted at their
present count, growth fails, shrinkage must be recorded, empty = absolute.

### Stop transport
The generic, stdlib-only mechanism (`wallee/stop_transport.py`) that
delivers a machine stop from a pack's declarative `safety_profile` — used
identically by the kernel trip callback and the watchdog.

### Whiteboard
The in-process live-fact store (`wallee/whiteboard.py`): flat namespaced
keys, snapshot reads, a bounded change ring. Reconstructable; never durable
truth.

### WorldPacket
The compiled world handed to the planner: facts, device summaries,
resources, blockers, the frontier, recent results, pending human messages —
plus a prompt view that hides operator-only and oversized content.

### Watchdog
`wallee-watchdog`, the separate process (own systemd unit, own user) that
can stop the machine when the runtime cannot. The runtime `Requires=` it;
it never requires the runtime.
