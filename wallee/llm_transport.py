"""Injectable HTTP transport for the OpenRouter planner.

The planner's reasoning pipeline (payload assembly, schema validation,
normalization/suppression policies) is deterministic; only the HTTP exchange
is not. Extracting the exchange behind ``PlannerTransport`` lets tests replay
recorded traffic through the *production* planner:

- ``LiveOpenRouterTransport``  — the real urllib call (behavior unchanged).
- ``RecordingTransport``       — wraps another transport and writes a cassette
                                 per call; refuses to persist secret-shaped
                                 content.
- ``ReplayTransport``          — resolves a cassette by request fingerprint.
                                 A miss means the outbound request changed —
                                 which turns any prompt/payload drift into a
                                 red test instead of a silent behavior change.

Cassette fingerprints hash the canonical JSON of
``{model, messages, response_format}`` — the semantic content of the request.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any, Protocol
from urllib import error, request

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

CASSETTE_VERSION = 1

# Nothing secret-shaped may ever be serialized into a cassette.
_SECRET_SHAPES = re.compile(r"sk-or-[A-Za-z0-9]|Bearer\s+[A-Za-z0-9]{8,}|api[_-]?key\"?\s*[:=]\s*\"[^\"]{8,}", re.IGNORECASE)


# The world packet embeds wall-clock freshness fields (e.g. `now_at`);
# fingerprints hash the STRUCTURAL request content, so volatile timestamps
# are normalized out. Everything else — prompt files, developer prompt,
# world shaping, schema, model — still changes the fingerprint.
_VOLATILE_TIMESTAMPS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")


def request_fingerprint(payload: dict[str, Any]) -> str:
    """Stable hash of the semantic request content."""
    semantic = {
        "model": payload.get("model"),
        "messages": payload.get("messages"),
        "response_format": payload.get("response_format"),
    }
    canonical = json.dumps(semantic, sort_keys=True, separators=(",", ":"))
    canonical = _VOLATILE_TIMESTAMPS.sub("<TS>", canonical)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PlannerTransport(Protocol):
    def post(self, payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
        """Send the request payload; return (raw_body, response_headers).

        Implementations raise ``urllib.error.HTTPError`` for HTTP-level
        failures (carrying body and headers) and ``urllib.error.URLError``
        for network-level failures, matching the live path's semantics.
        """
        ...  # pragma: no cover - protocol


class LiveOpenRouterTransport:
    """The real HTTP exchange, verbatim from the pre-seam planner."""

    def __init__(self, api_key: str, timeout_s: float, url: str = OPENROUTER_URL) -> None:
        self.api_key = api_key
        self.timeout_s = max(1.0, float(timeout_s))
        self.url = url

    def post(self, payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        with request.urlopen(req, timeout=self.timeout_s) as response:
            return response.read(), dict(response.headers.items())


@dataclass
class Cassette:
    """One recorded request/response exchange."""

    fingerprint: str
    request_payload: dict[str, Any]
    response_status: int
    response_headers: dict[str, str]
    response_body: dict[str, Any]
    meta: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "cassette_version": CASSETTE_VERSION,
            "request": {
                "url": OPENROUTER_URL,
                "fingerprint": self.fingerprint,
                "payload": self.request_payload,
            },
            "response": {
                "status": self.response_status,
                "headers": self.response_headers,
                "body": self.response_body,
            },
            "meta": self.meta,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Cassette":
        return cls(
            fingerprint=data["request"]["fingerprint"],
            request_payload=data["request"]["payload"],
            response_status=int(data["response"].get("status", 200)),
            response_headers=dict(data["response"].get("headers", {})),
            response_body=data["response"]["body"],
            meta=dict(data.get("meta", {})),
        )


def write_cassette(path: Path, cassette: Cassette) -> None:
    serialized = json.dumps(cassette.to_json(), indent=2, sort_keys=True)
    if _SECRET_SHAPES.search(serialized):
        raise ValueError(f"refusing to write cassette with secret-shaped content: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized + "\n", encoding="utf-8")


def load_cassettes(root: Path) -> list[tuple[Path, Cassette]]:
    return [
        (path, Cassette.from_json(json.loads(path.read_text(encoding="utf-8"))))
        for path in sorted(root.rglob("*.json"))
    ]


class CassetteMiss(AssertionError):
    """The outbound request matches no recorded cassette.

    Raised as an AssertionError subtype on purpose: in CI this reads as
    "the prompt/payload changed — re-record deliberately", not as a
    transport hiccup.
    """


class RecordingTransport:
    def __init__(self, inner: PlannerTransport, out_dir: Path, case_id: str) -> None:
        self.inner = inner
        self.out_dir = out_dir
        self.case_id = case_id
        self._counter = 0

    def post(self, payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
        raw_body, headers = self.inner.post(payload)
        body = json.loads(raw_body.decode("utf-8"))
        cassette = Cassette(
            fingerprint=request_fingerprint(payload),
            request_payload=_strip_auth(payload),
            response_status=200,
            response_headers=_replay_safe_headers(headers),
            response_body=body,
            meta={"case_id": self.case_id, "recorded": "live"},
        )
        write_cassette(self.out_dir / f"{self.case_id}.{self._counter:02d}.json", cassette)
        self._counter += 1
        return raw_body, headers


class ReplayTransport:
    def __init__(self, cassettes: list[Cassette]) -> None:
        self._by_fingerprint = {c.fingerprint: c for c in cassettes}

    def post(self, payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
        fingerprint = request_fingerprint(payload)
        cassette = self._by_fingerprint.get(fingerprint)
        if cassette is None:
            raise CassetteMiss(self._miss_message(fingerprint, payload))
        if cassette.response_status >= 400:
            headers = Message()
            for key, value in cassette.response_headers.items():
                headers[key] = value
            raise error.HTTPError(
                OPENROUTER_URL,
                cassette.response_status,
                "replayed HTTP error",
                headers,
                io.BytesIO(json.dumps(cassette.response_body).encode("utf-8")),
            )
        return (
            json.dumps(cassette.response_body).encode("utf-8"),
            dict(cassette.response_headers),
        )

    def _miss_message(self, fingerprint: str, payload: dict[str, Any]) -> str:
        known = ", ".join(sorted(c.meta.get("case_id", "?") for c in self._by_fingerprint.values()))
        return (
            f"no cassette matches request fingerprint {fingerprint} "
            f"(model={payload.get('model')!r}, {len(payload.get('messages', []))} messages). "
            f"Known cassettes: [{known}]. The outbound prompt/payload changed — "
            "regenerate deliberately with scripts/record_cassettes.py and commit the "
            "diff in a separate [re-record] commit."
        )


def _strip_auth(payload: dict[str, Any]) -> dict[str, Any]:
    # Payloads never carry auth today; keep the barrier explicit anyway.
    return json.loads(json.dumps(payload))


_REPLAY_HEADER_ALLOWLIST = {"content-type", "x-request-id", "x-openrouter-provider"}


def _replay_safe_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() in _REPLAY_HEADER_ALLOWLIST}
