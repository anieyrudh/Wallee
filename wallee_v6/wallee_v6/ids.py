"""Small helpers for stable IDs and hashes.

These helpers are intentionally centralised because canonical hashing is a
cross-cutting contract.  If different modules hash the same logical payload in
slightly different ways, approval binding and idempotency guarantees silently
collapse.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


def canonical_json(data: Any) -> str:
    """Return a stable JSON representation suitable for hashing.

    Stable hashing matters for approval binding and idempotency.  Using compact
    separators and sorted keys keeps the representation deterministic across
    Python runs and platforms.
    """
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    """Return a lowercase SHA-256 hex digest for *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def action_args_hash(verb: str, args: dict[str, Any]) -> str:
    """Hash the semantic action payload.

    The hash intentionally excludes mutable runtime metadata such as timestamps
    or UI annotations.  Human approvals should bind to the thing that would
    change the physical world, not to logging details.
    """
    return sha256_hex(canonical_json({"verb": verb, "args": args}))


def idempotency_key(plan_id: str, action_id: str, verb: str, args_hash: str) -> str:
    """Create a stable idempotency key for one logical execution attempt."""
    return sha256_hex(
        canonical_json(
            {
                "plan_id": plan_id,
                "action_id": action_id,
                "verb": verb,
                "args_hash": args_hash,
            }
        )
    )


def new_id(prefix: str) -> str:
    """Create a compact, readable identifier with a stable prefix."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
