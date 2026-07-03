#!/usr/bin/env bash
# The one local command surface: run the same gates CI runs, in the same
# shapes. `./scripts/gate.sh fast` runs the quick static subset; no argument
# runs everything (matching the required-check set minus the version matrix).
set -euo pipefail
cd "$(dirname "$0")/.."

run() {
  echo
  echo "==> $*"
  "$@"
}

# --- fast static gates (mirrors the `gates`, `docs-contract`, `repo-contract`,
# --- `schema-sync`, and `mypy` checks) ------------------------------------
run ruff check .
run python scripts/check_forbidden_patterns.py
run python scripts/check_docs.py
run python scripts/check_repo_contract.py
run python scripts/check_core_purity.py
run python scripts/check_file_sizes.py
run python scripts/check_contract_manifest.py
run python scripts/gen_schemas.py
run python scripts/gen_agents_gate_block.py --check
run mypy wallee/coerce.py wallee/ids.py wallee/predicates.py \
    wallee/runtime_control.py wallee/safety.py wallee/stop_transport.py \
    wallee/whiteboard.py

if [[ "${1:-}" == "fast" ]]; then
  echo
  echo "gate.sh fast: static gates green (tests skipped — run ./scripts/gate.sh before pushing)"
  exit 0
fi

# --- the test lanes (mirrors `tests`, `safety-invariants`,
# --- `adversarial-gates`, `sim-evals`, `cassette-replay`) ------------------
run python -m pytest tests -q --cov=wallee --cov-report=term --cov-fail-under=72
run python -m pytest tests/contract -q --strict-markers

echo
echo "gate.sh: all local gates green"
