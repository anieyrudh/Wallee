"""Redis-backed whiteboard with TTL, ring buffers, and trend computation."""

import json
import logging
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


def compute_differential(history: list[float], interval_s: float) -> str:
    """Rate of change per second."""
    if len(history) < 2:
        return "insufficient data"
    delta = history[0] - history[-1]
    time_span = interval_s * (len(history) - 1)
    rate = delta / time_span if time_span > 0 else 0
    return f"{rate:+.3f}/s"


class Whiteboard:
    def __init__(self, redis_url: str = "redis://localhost:6379", _redis=None):
        """Initialize whiteboard. Pass _redis for testing with fakeredis."""
        if _redis is not None:
            self.r = _redis
        else:
            self.r = redis.Redis.from_url(redis_url, decode_responses=True)

    def publish(self, key: str, value, ttl: int | None = None, history_depth: int = 0):
        """Publish a value with optional TTL and history ring buffer."""
        encoded = json.dumps(value)
        if ttl:
            self.r.set(key, encoded, ex=ttl)
        else:
            self.r.set(key, encoded)
        if history_depth > 0:
            history_key = f"{key}:history"
            self.r.lpush(history_key, encoded)
            self.r.ltrim(history_key, 0, history_depth - 1)

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

    def read_history(self, key: str) -> list:
        """Read ring buffer for a key. Newest first."""
        try:
            vals = self.r.lrange(f"{key}:history", 0, -1)
        except redis.ResponseError:
            return []
        return [json.loads(v) for v in vals]

    def read_all(self) -> dict:
        """Read all string-type keys (skip lists, sets, etc.)."""
        all_keys = self.r.keys("*")
        result = {}
        for key in sorted(all_keys):
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
