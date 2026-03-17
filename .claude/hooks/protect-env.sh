#!/bin/bash
# Block edits to .env files (but allow .env.example).
# Exit 2 = block, exit 0 = allow.

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

if [ -z "$FILE_PATH" ]; then
  exit 0
fi

BASENAME=$(basename "$FILE_PATH")

# Allow .env.example (template file is safe to edit)
if [[ "$BASENAME" == ".env.example" ]]; then
  exit 0
fi

# Block any .env file (.env, .env.local, .env.production, etc.)
if [[ "$BASENAME" == ".env" || "$BASENAME" == .env.* ]]; then
  echo "BLOCKED: Cannot edit $BASENAME - .env files contain secrets and must be edited manually" >&2
  exit 2
fi

exit 0
