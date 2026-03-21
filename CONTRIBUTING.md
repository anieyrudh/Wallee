# Contributing to Wallee

Thanks for contributing. Wallee is a public architecture repo for safe LLM-mediated hardware control, so code, docs, and safety expectations need to stay aligned.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
```

Fill in only the values you need for the device packs you are working on.

## Validation

Run these before opening a pull request:

```bash
pytest -q
ruff check wallee scripts
python scripts/check_docs.py
python scripts/check_repo_contract.py
```

## Test layout

Wallee keeps two test layers in the same repository:

- `tests/` contains core/framework coverage for the agent, engine, parser, whiteboard, ledger, safety kernel, and built-in tools.
- `wallee/device_packs/*/tests/` contains shipped example device-pack coverage for the concrete hardware integrations in this repository.

Do not remove example device-pack tests just because they are machine-specific. They validate the example implementation, not the hardware-agnostic framework contract.

## Branch and PR expectations

- Work from a short-lived branch based on `main`.
- Keep changes narrowly scoped when possible.
- Include tests or explain why no test can cover the change.
- If you change user-facing behavior, update the relevant docs in the same pull request.
- If you change tool contracts, device-pack behavior, or architecture claims, update `README.md`, `ARCHITECTURE.md`, and any affected device-pack docs.

## Documentation rules

- Public docs should stay hardware-agnostic unless they are explicitly labeled as an example implementation.
- Device-pack specifics belong under `wallee/device_packs/`.
- Internal historical docs under `docs/internal/` may remain implementation-specific, but they should not silently contradict the public docs.

## Safety expectations

- The LLM is untrusted. Do not add code paths that let it dispatch hardware directly.
- Hardware actuators must not bypass engine gates.
- Device packs own machine-specific behavior. Keep the core generic unless a change is truly architectural.

## Pull request checklist

Before submitting, confirm:

- tests pass
- lint passes
- docs are updated
- new device packs include `README.md`
- new sensors and actuators are registered through the decorator and discoverable by the registry
