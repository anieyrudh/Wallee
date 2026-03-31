# Prusa Core One+ Pack

This pack is the **real-hardware reference pack** for the lean Wallee v6 runtime.

It keeps the v6 design boundary intact:

- the **planner** sees one physical printer and a small set of outcome-level verbs
- the **pack** hides the transport details
- the **engine** remains generic and deterministic

## Why this is one pack, not three

The current v5 repository models the Prusa deployment through several interfaces:
HTTP via PrusaLink, a UDP metrics stream, and USB serial diagnostics.  That is
useful at the implementation layer, but it is the wrong abstraction for the
planner.

In v6 this becomes **one pack for one physical printer**.
The pack privately composes three adapters:

- **HTTP** - authoritative control and baseline telemetry
- **UDP metrics** - optional high-rate telemetry and liveness hints
- **USB serial** - optional diagnostics-only source for chamber and ambient
  temperatures

This keeps the planner's world model simple and aligns with the first-principles
rule that the core should reason about *devices and outcomes*, not about vendor
transport fragments.

## What the pack exposes to the planner

The pack normalizes raw machine state into facts such as:

- `printer_1.mode`
- `printer_1.health`
- `printer_1.job_active`
- `printer_1.current_file`
- `printer_1.part_present`
- `printer_1.safe_to_unload`
- `printer_1.requested_file`
- `printer_1.requested_file_present`

And it exposes two planner-visible resources:

- `printer_1.bed`
- `printer_1.usb_storage`

## Admitted verbs

The pack intentionally exposes only verbs that pass the V6 admission rule:

1. outcome-level
2. observable
3. bounded
4. reusable

Current exposed verbs:

- `PAUSE_PROCESS`
- `RESUME_PROCESS`
- `STOP_PROCESS`
- `START_PROCESS`
- builtin `WAIT_UNTIL`
- builtin `CALL_HUMAN`

## What the pack deliberately does *not* expose

The pack does **not** currently expose arbitrary raw motion or arbitrary numeric
G-code tuning to the planner.

Examples intentionally kept out of the frontier:

- raw `G0/G1` head moves
- arbitrary `M104` / `M140` temperature commands
- arbitrary flow and speed factor tuning
- raw chamber heater commands

Those are useful machine commands, but they do not yet meet the lean V6 bar for
planner-visible verbs.  If a workflow truly needs one of them, add a bounded,
observable verb and document why it belongs in the frontier.

## Safety choices in this pack

### HTTP is the control authority

The pack uses PrusaLink HTTP as the only control path for planner-selected
verbs.  That keeps control on the most reliable and observable interface.

### Serial is diagnostics-only

The current Core One USB CDC link is known to be unstable in the v5 reference
setup.  Because chamber and ambient temperatures are useful but not safety
critical for the current workflow set, serial is treated as slow, optional,
best-effort diagnostics.

### `safe_to_unload` is a conservative proxy

The printer does not provide a direct part-temperature sensor.
The pack therefore computes `safe_to_unload` from:

- bed temperature
- nozzle temperature
- optional chamber temperature when available
- printer mode and health

That is intentionally a conservative approximation.  The pack would rather say
"not safe yet" too often than confidently invent a part temperature it cannot
measure.

## Environment variables

See [SETUP.md](SETUP.md) for the full list.  The most important ones are:

- `PRUSA_CORE_ONE_HOST`
- `PRUSA_CORE_ONE_API_KEY`
- `PRUSA_CORE_ONE_ENABLE_METRICS`
- `PRUSA_CORE_ONE_ENABLE_SERIAL`
- `PRUSA_CORE_ONE_SERIAL_PORT`

Exact semantic aliases from v5 are supported for bring-up compatibility:

- `PRUSALINK_HOST` -> `PRUSA_CORE_ONE_HOST`
- `PRUSALINK_API_KEY` -> `PRUSA_CORE_ONE_API_KEY`

If both canonical and alias names are present, the `PRUSA_CORE_ONE_*` value
wins.  No other v5 env names are interpreted by this pack.

For a runnable template, see [`.env.example`](/Users/anieyrudh/Desktop/Wallee2/wallee_v6/.env.example).

## Files in this directory

- `manifest.yaml` - pack declaration used by the registry
- `adapters.py` - HTTP, UDP metrics, and serial adapter code
- `pack.py` - normalization, candidate action generation, and realization
- `SETUP.md` - operator setup checklist
- `CAPABILITIES.md` - capability map and design notes

## Testing

This pack is covered by tests in `tests/test_prusa_core_one_adapters.py` and
`tests/test_prusa_core_one_pack.py`.

The tests are intentionally hardware-free and inject fakes for all transports.
That gives fast feedback for pack logic while keeping the real network and
serial paths isolated to the adapters.
