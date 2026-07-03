# CLAUDE.md

Claude Code should treat `AGENTS.md` as the primary repository guidance.

Extra emphasis for Claude Code:
- when updating prompts, keep them short and structural
- do not introduce persona-heavy prompt text to chase reasoning quality
- if you change planning context shape, update `schemas/world_packet.schema.json` and tests
- when editing `prusa_core_one_plus`, preserve the design that one physical
  printer appears as one pack even though it privately composes HTTP, UDP, and
  optional serial diagnostics
