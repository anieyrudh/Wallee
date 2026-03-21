# Security Policy

## Reporting a vulnerability

If you find a security issue, safety bypass, actuator-authentication flaw, credential leak, or ESTOP failure mode, do not open a public issue first.

Report it privately through GitHub Security Advisories if available. If that is not enabled, contact the maintainer privately through GitHub: [@anieyrudh](https://github.com/anieyrudh).

When reporting, include:

- affected component or file
- reproduction steps
- whether hardware access is required
- whether the issue can cause unsafe physical behavior
- whether credentials or operator approval can be bypassed

## Supported branch

Security fixes are expected on:

- `main`

Older branches may not receive fixes.

## Safety note

Wallee is software for mediated hardware control. Even with engine gates and an independent safety kernel, this repository should not be treated as a complete industrial safety system.

Current limitations include:

- ESTOP is software-mediated unless you add a hardware relay/interlock
- safety depends on the broader host stack, including Redis and machine reachability
- device packs can introduce hardware-specific risk if they are written carelessly

Review the active device pack, machine setup, and failure modes before running Wallee on physical hardware.
