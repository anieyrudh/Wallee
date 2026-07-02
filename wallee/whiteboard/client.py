"""Redis-backed whiteboard with TTL, ring buffers, and trend computation."""

import json
import logging
import time
from collections.abc import Mapping
import redis

logger = logging.getLogger(__name__)


def compute_trend(history: list[float], threshold: float = 0.1) -> str:
    """Compute trend from ring buffer values. Newest first."""
    if len(history) < 2:
        return "insufficient data"
    delta = history[0] - history[-1]
    if abs(delta) < threshold:
        return "stable"
    direction = "rising" if delta > 0 else "falling"
    return f"{direction} {delta:+.1f} over {len(history)} readings"


class Whiteboard:
    def __init__(self, redis_url: str = "redis://localhost:6379", _redis=None):
        """Initialize whiteboard. Pass _redis for testing with fakeredis."""
        if _redis is not None:
            self.r = _redis
        else:
            self.r = redis.Redis.from_url(redis_url, decode_responses=True)

    def _now(self) -> float:
        return time.time()

    @staticmethod
    def _resolve_history_depth(history_depth, key: str) -> int:
        if isinstance(history_depth, Mapping):
            return int(history_depth.get(key, 0) or 0)
        return int(history_depth or 0)

    def _queue_publish(
        self,
        pipe,
        key: str,
        value,
        ttl: int | None = None,
        history_depth: int | Mapping[str, int] = 0,
        published_at: float | None = None,
    ):
        encoded = json.dumps(value)
        if ttl:
            pipe.set(key, encoded, ex=ttl)
        else:
            pipe.set(key, encoded)

        depth = self._resolve_history_depth(history_depth, key)
        if depth > 0:
            history_key = f"{key}:history"
            history_ts_key = f"{key}:history_ts"
            ts_encoded = json.dumps(published_at if published_at is not None else self._now())
            pipe.lpush(history_key, encoded)
            pipe.ltrim(history_key, 0, depth - 1)
            pipe.lpush(history_ts_key, ts_encoded)
            pipe.ltrim(history_ts_key, 0, depth - 1)

    def publish(self, key: str, value, ttl: int | None = None, history_depth: int | Mapping[str, int] = 0):
        """Publish a value atomically with optional TTL and history ring buffer."""
        published_at = self._now()
        with self.r.pipeline(transaction=True) as pipe:
            self._queue_publish(pipe, key, value, ttl=ttl, history_depth=history_depth, published_at=published_at)
            pipe.execute()

    def publish_many(
        self,
        values: dict[str, object],
        ttl: int | None = None,
        history_depth: int | Mapping[str, int] = 0,
    ):
        """Publish multiple keys atomically in one Redis transaction."""
        if not values:
            return

        published_at = self._now()
        with self.r.pipeline(transaction=True) as pipe:
            for key, value in values.items():
                self._queue_publish(pipe, key, value, ttl=ttl, history_depth=history_depth, published_at=published_at)
            pipe.execute()

    def read(self, key: str):
        """Read a single key. Returns None if expired, missing, or wrong type."""
        try:
            val = self.r.get(key)
        except redis.ResponseError:
            # WRONGTYPE — key exists but is not a string (e.g., list)
            return None
        if val is None:
            return None
        return json.loads(val)

    def delete(self, key: str) -> None:
        """Remove a key. Used for explicit human-cleared flags like safety.estop."""
        self.r.delete(key)

    def read_history(self, key: str) -> list:
        """Read ring buffer for a key. Newest first."""
        try:
            vals = self.r.lrange(f"{key}:history", 0, -1)
        except redis.ResponseError:
            return []
        return [json.loads(v) for v in vals]

    def read_all(self) -> dict:
        """Read all string-type keys (skip lists, sets, etc.)."""
        result = {}
        for key in self.r.scan_iter(match="*"):
            if key.endswith(":history"):
                continue
            try:
                key_type = self.r.type(key)
                if key_type != "string":
                    continue
                val = self.r.get(key)
                if val is not None:
                    result[key] = json.loads(val)
            except (redis.ResponseError, json.JSONDecodeError):
                continue
        return result

    def read_all_with_trends(self) -> dict:
        """Read all keys with trend annotations for numeric histories."""
        state = self.read_all()
        for key in list(state.keys()):
            history = self.read_history(key)
            if history and len(history) >= 2:
                if all(isinstance(v, (int, float)) for v in history):
                    state[f"{key}:trend"] = compute_trend(history)
        return state
