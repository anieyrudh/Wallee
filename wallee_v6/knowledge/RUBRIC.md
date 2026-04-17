# Planning Rubric

Choose the best bounded decision using this order:

1. One legal frontier action strongly supported by current live evidence.
2. If a recent legal action verified but fresh later observations still show the same visible issue, choose one additional legal bounded follow-up action best matched to the remaining issue.
3. Otherwise `NO_ACTION` only when no single legal action is clearly supported.
4. `CALL_HUMAN` only for true recovery, ambiguity, or escalation cases.

Tie-break rules:
- Prefer the action better supported by notebook or vision when legality and blockers already allow both.
- Prefer the action less contradicted by recent results.
- Prefer one bounded action backed by fresh repeated issue signals over waiting for perfect certainty.
- Prefer one additional legal follow-up action over repeated waiting when a recent action verified and fresh later observations still show the same issue.
- Never bundle families.
- Never treat supporting context as permission by itself.
