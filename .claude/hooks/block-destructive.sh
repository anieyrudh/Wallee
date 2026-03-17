#!/bin/bash
# Block destructive shell commands before they execute.
# Exit 2 = block, exit 0 = allow.

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

if [ -z "$COMMAND" ]; then
  exit 0
fi

# Destructive patterns to block
BLOCKED_PATTERNS=(
  "rm -rf"
  "rm -fr"
  "rm -Rf"
  "rm -fR"
  "git reset --hard"
  "git clean -f"
  "git checkout -- ."
  "git push --force"
  "git push -f"
  "git branch -D"
  "dd if="
  "mkfs"
  "> /dev/"
  ":(){ :|"
)

for pattern in "${BLOCKED_PATTERNS[@]}"; do
  if echo "$COMMAND" | grep -qF "$pattern"; then
    echo "BLOCKED: Command contains destructive pattern '$pattern'" >&2
    exit 2
  fi
done

exit 0
