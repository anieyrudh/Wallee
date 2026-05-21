# CODEX.md

Codex should treat `AGENTS.md` as the primary repository guidance.

Extra emphasis for Codex:
- preserve the commit barrier ordering around `DISPATCHED` and `IN_FLIGHT`
- keep modules deep; avoid creating wrapper files with one or two methods
- if you add a new pack, add at least one integration-style test that uses the real pack class
- for the Prusa pack, keep control on HTTP unless there is strong evidence that a
  serial or metrics path is both necessary and more reliable
