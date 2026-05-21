"""Volatile whiteboard implementations.

The in-memory whiteboard is the default because it keeps local development
simple.  A Redis-backed implementation can be added behind the same interface
later without changing the world compiler or packs.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any


@dataclass(slots=True)
class WhiteboardSnapshot:
    """Immutable snapshot of whiteboard state."""
    values: dict[str, Any]
    sequence: int


class BaseWhiteboard:
    """Small interface shared by whiteboard implementations."""

    def publish(self, key: str, value: Any) -> int:  # pragma: no cover - interface
        raise NotImplementedError

    def batch_publish(self, mapping: dict[str, Any]) -> int:  # pragma: no cover - interface
        raise NotImplementedError

    def get(self, key: str, default: Any | None = None) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    def snapshot(self) -> WhiteboardSnapshot:  # pragma: no cover - interface
        raise NotImplementedError

    def changes_since(self, sequence: int) -> list[tuple[int, str, Any, Any, int]]:  # pragma: no cover - interface
        raise NotImplementedError


class InMemoryWhiteboard(BaseWhiteboard):
    """Thread-safe whiteboard for local simulation and tests."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._values: dict[str, Any] = {}
        self._seq = 0
        self._changes: list[tuple[int, str, Any, Any, int]] = []

    def publish(self, key: str, value: Any) -> int:
        """Publish one value and return the new sequence number."""
        with self._lock:
            old = self._values.get(key)
            self._seq += 1
            self._values[key] = value
            self._changes.append((self._seq, key, old, value, int(time.time() * 1000)))
            return self._seq

    def batch_publish(self, mapping: dict[str, Any]) -> int:
        """Publish multiple keys atomically from the caller's point of view."""
        with self._lock:
            last_seq = self._seq
            for key, value in mapping.items():
                old = self._values.get(key)
                self._seq += 1
                self._values[key] = value
                self._changes.append((self._seq, key, old, value, int(time.time() * 1000)))
                last_seq = self._seq
            return last_seq

    def get(self, key: str, default: Any | None = None) -> Any:
        """Return the current value for *key*."""
        with self._lock:
            return self._values.get(key, default)

    def snapshot(self) -> WhiteboardSnapshot:
        """Return a copy of all current values plus the latest sequence number."""
        with self._lock:
            return WhiteboardSnapshot(values=dict(self._values), sequence=self._seq)

    def changes_since(self, sequence: int) -> list[tuple[int, str, Any, Any, int]]:
        """Return changes strictly newer than *sequence*."""
        with self._lock:
            return [change for change in self._changes if change[0] > sequence]


class RedisWhiteboard(BaseWhiteboard):
    """Optional Redis implementation.

    The import happens lazily because the reference repository should still work
    without a Redis client installed.  This class is present to show the
    production integration point, not because the tests need a real Redis
    server.
    """

    def __init__(self, url: str = "redis://127.0.0.1:6379/0") -> None:
        try:
            import redis  # type: ignore
        except Exception as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("redis package is required for RedisWhiteboard") from exc
        self._client = redis.Redis.from_url(url, decode_responses=False)

    def publish(self, key: str, value: Any) -> int:  # pragma: no cover - optional integration
        raise NotImplementedError("RedisWhiteboard is a placeholder in the reference implementation")

    def batch_publish(self, mapping: dict[str, Any]) -> int:  # pragma: no cover - optional integration
        raise NotImplementedError("RedisWhiteboard is a placeholder in the reference implementation")

    def get(self, key: str, default: Any | None = None) -> Any:  # pragma: no cover - optional integration
        raw = self._client.get(key)
        return default if raw is None else raw

    def snapshot(self) -> WhiteboardSnapshot:  # pragma: no cover - optional integration
        raise NotImplementedError("RedisWhiteboard is a placeholder in the reference implementation")

    def changes_since(self, sequence: int) -> list[tuple[int, str, Any, Any, int]]:  # pragma: no cover - optional integration
        raise NotImplementedError("RedisWhiteboard is a placeholder in the reference implementation")
