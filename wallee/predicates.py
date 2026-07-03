"""Deterministic predicate evaluation for legality and verification.

The predicate language is intentionally tiny: atoms plus `all`, `any`, and
`not`.  Small languages are easier to test exhaustively and far less likely to
grow hidden semantics that the operator cannot reason about during an incident.
"""

from __future__ import annotations

from typing import Any, Mapping


_ATOM_OPS = {
    "==": lambda left, right: left == right,
    "!=": lambda left, right: left != right,
    ">": lambda left, right: left > right,
    ">=": lambda left, right: left >= right,
    "<": lambda left, right: left < right,
    "<=": lambda left, right: left <= right,
}


def atom(fact: str, op: str, value: Any) -> dict[str, Any]:
    """Create an atomic predicate dictionary."""
    return {"fact": fact, "op": op, "value": value}


def all_of(*predicates: dict[str, Any]) -> dict[str, Any]:
    """Create an `all` predicate."""
    return {"all": list(predicates)}


def any_of(*predicates: dict[str, Any]) -> dict[str, Any]:
    """Create an `any` predicate."""
    return {"any": list(predicates)}


def negate(predicate: dict[str, Any]) -> dict[str, Any]:
    """Create a `not` predicate."""
    return {"not": predicate}


class PredicateEvaluator:
    """Evaluate predicate dictionaries against a fact mapping.

    Missing facts fail closed.  That is a deliberate safety property: if the
    system cannot observe the world state it needs, it should not guess that the
    world is safe enough to act in.
    """

    def evaluate(self, predicate: dict[str, Any] | None, facts: Mapping[str, Any]) -> bool:
        """Return ``True`` when *predicate* holds for *facts*.

        Parameters
        ----------
        predicate:
            JSON-like predicate dictionary.  `None` means "always true".
        facts:
            Mapping of normalized fact name to value.
        """
        if predicate is None:
            return True

        if "all" in predicate:
            return all(self.evaluate(child, facts) for child in predicate["all"])

        if "any" in predicate:
            return any(self.evaluate(child, facts) for child in predicate["any"])

        if "not" in predicate:
            return not self.evaluate(predicate["not"], facts)

        fact_name = predicate.get("fact")
        op = predicate.get("op")
        expected = predicate.get("value")

        if fact_name is None or op not in _ATOM_OPS:
            raise ValueError(f"invalid predicate structure: {predicate!r}")

        if fact_name not in facts:
            return False

        actual = facts[fact_name]
        return _ATOM_OPS[op](actual, expected)

    def describe(self, predicate: dict[str, Any] | None) -> str:
        """Return a compact human-readable description of a predicate."""
        if predicate is None:
            return "always"
        if "all" in predicate:
            return " AND ".join(f"({self.describe(child)})" for child in predicate["all"])
        if "any" in predicate:
            return " OR ".join(f"({self.describe(child)})" for child in predicate["any"])
        if "not" in predicate:
            return f"NOT ({self.describe(predicate['not'])})"
        return f"{predicate['fact']} {predicate['op']} {predicate['value']!r}"
