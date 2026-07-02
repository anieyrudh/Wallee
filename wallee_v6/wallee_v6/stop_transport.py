"""Generic, non-AI machine-stop transport.

This is the physical arm of the safety interlock. It is stdlib-only (no
pydantic, no httpx, no pack imports) so the independent out-of-process
watchdog can import and use it without dragging in the runtime — the stronger
independence story for promise 3. Packs contribute declarative
``safety_profile`` data (hosts/keys named by env var, request dicts); this
module resolves and sends it.

A ``file`` transport (append a record to a JSONL log) is provided for
simulation and tests, so the SIGKILL/interlock tests exercise the real stop
path with no hardware.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request


@dataclass
class StopOutcome:
    ok: bool
    detail: str


def _resolve(profile: dict[str, Any], env: dict[str, str] | None) -> tuple[str, str]:
    env = env if env is not None else dict(os.environ)
    host = env.get(profile.get("host_env") or "", "") if profile.get("host_env") else ""
    api_key = env.get(profile.get("api_key_env") or "", "") if profile.get("api_key_env") else ""
    return host, api_key


def _send_http(profile: dict[str, Any], env: dict[str, str] | None, timeout_s: float) -> StopOutcome:
    host, api_key = _resolve(profile, env)
    if not host:
        return StopOutcome(False, "no control host resolved from env")
    base = host if host.startswith("http") else f"http://{host}"
    header_name = profile.get("api_key_header") or "X-Api-Key"
    headers = {header_name: api_key} if api_key else {}
    attempts = [profile.get("primary_request"), profile.get("fallback_request")]
    last = "no request configured"
    for spec in attempts:
        if not spec:
            continue
        method = str(spec.get("method", "POST")).upper()
        path = spec.get("path", "")
        payload = spec.get("json")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = request.Request(f"{base}{path}", data=data, method=method, headers={**headers, "Content-Type": "application/json"})
        try:
            with request.urlopen(req, timeout=timeout_s) as resp:
                if resp.status < 300:
                    return StopOutcome(True, f"{method} {path} -> HTTP {resp.status}")
                last = f"{method} {path} -> HTTP {resp.status}"
        except error.HTTPError as exc:
            if exc.code < 300:
                return StopOutcome(True, f"{method} {path} -> HTTP {exc.code}")
            last = f"{method} {path} -> HTTP {exc.code}"
        except (error.URLError, OSError) as exc:
            last = f"{method} {path} -> {exc}"
    return StopOutcome(False, f"all stop attempts failed: {last}")


def _send_file(profile: dict[str, Any], data_dir: Path) -> StopOutcome:
    target = data_dir / "safety" / "stop_commands.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {"wall_ts": time.time(), "request": profile.get("primary_request")}
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return StopOutcome(True, f"stop appended to {target}")


def execute_stop(
    profile: dict[str, Any],
    *,
    data_dir: Path | str,
    env: dict[str, str] | None = None,
    timeout_s: float = 5.0,
) -> StopOutcome:
    """Send the stop described by *profile*. Never raises."""
    transport = str(profile.get("transport", "none"))
    try:
        if transport == "http":
            return _send_http(profile, env, timeout_s)
        if transport == "file":
            return _send_file(profile, Path(data_dir))
        return StopOutcome(False, f"no-op transport {transport!r}")
    except Exception as exc:  # transport must never raise into the caller
        return StopOutcome(False, f"stop transport error: {exc}")
