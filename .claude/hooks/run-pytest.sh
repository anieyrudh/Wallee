#!/bin/bash
# Auto-run pytest after file edits. Only runs for Python file changes.
# Non-blocking: exit 0 regardless so edits aren't blocked by test failures.

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

# Only run pytest when a Python file was edited
if [[ "$FILE_PATH" != *.py ]]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR" || exit 0

# Check if pytest is available
if ! command -v pytest &>/dev/null; then
  echo "pytest not found, skipping auto-test" >&2
  exit 0
fi

# Check if any test files exist
if ! find . -name "test_*.py" -o -name "*_test.py" 2>/dev/null | head -1 | grep -q .; then
  exit 0
fi

echo "--- Auto-running pytest ---"
pytest -x --tb=short -q 2>&1
TEST_EXIT=$?

if [ $TEST_EXIT -ne 0 ] && [ $TEST_EXIT -ne 5 ]; then
  # Exit code 5 = no tests collected (fine), anything else non-zero = failures
  echo "--- pytest failed (exit $TEST_EXIT) ---"
fi

# Always exit 0 so the hook doesn't block Claude
exit 0
