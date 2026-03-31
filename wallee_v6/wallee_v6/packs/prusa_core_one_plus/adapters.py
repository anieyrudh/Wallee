"""Transport adapters and parsers for the Prusa Core One+ pack.

This module intentionally keeps the transport details in one place so the pack
logic can stay focused on manufacturing semantics.  The adapters are designed to
be *thin but opinionated*:

- HTTP is the authoritative control path and the primary state source.
- UDP metrics are opportunistic telemetry.  Missing metrics should not stop
  planning because the printer can still be controlled safely without them.
- USB serial is diagnostics-only because the current reference deployment found
  the Core One CDC ACM link to be unstable.  Serial therefore runs with slow
  polling, aggressive backoff, and best-effort semantics.

That split follows first principles: use the most reliable channel for actions,
use the highest-rate channel for optional observability, and keep the flaky
channel away from anything safety critical.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import socket
import time
from typing import Any, Protocol
from urllib import error, parse, request


_M105_TOKEN_RE = re.compile(r"(?P<key>[A-Z@]+):(?P<actual>-?\d+(?:\.\d+)?)(?:/(?P<target>-?\d+(?:\.\d+)?))?")
_PRINTABLE_SUFFIXES = (".gcode", ".bgcode", ".gco", ".bgc")


def _resolve_env(primary: str, *aliases: str) -> str | None:
    """Return the first non-blank environment value, preferring *primary*."""
    candidates = (primary, *aliases)
    for name in candidates:
        value = os.environ.get(name)
        if value is None:
            continue
        text = value.strip()
        if text:
            return text
    return None


@dataclass(slots=True)
class PrusaCoreOneSettings:
    """Runtime settings for the Prusa Core One+ pack.

    The settings are read directly from environment variables because the lean
    reference runtime keeps configuration shallow.  Adding a full pack-specific
    configuration framework would be more code than the real hardware evidence
    currently justifies.
    """

    host: str
    api_key: str | None
    http_timeout_s: float
    file_action_limit: int
    state_transition_timeout_s: float
    safe_to_unload_bed_c: float
    safe_to_touch_nozzle_c: float
    serial_enabled: bool
    serial_port: str | None
    serial_baud: int
    serial_timeout_s: float
    serial_poll_interval_s: float
    serial_error_backoff_s: float
    metrics_enabled: bool
    metrics_bind_host: str
    metrics_port: int

    @classmethod
    def from_env(cls) -> "PrusaCoreOneSettings":
        """Create settings from environment variables.

        The defaults deliberately bias toward safety and reliability:

        - control runs over HTTP with short timeouts
        - serial diagnostics are disabled until explicitly enabled
        - frontier file actions are capped so prompts stay small
        """
        host = (_resolve_env("PRUSA_CORE_ONE_HOST", "PRUSALINK_HOST") or "").rstrip("/")
        if host and not host.startswith(("http://", "https://")):
            host = f"http://{host}"

        return cls(
            host=host,
            api_key=_resolve_env("PRUSA_CORE_ONE_API_KEY", "PRUSALINK_API_KEY"),
            http_timeout_s=float(os.environ.get("PRUSA_CORE_ONE_HTTP_TIMEOUT_S", "2.0")),
            file_action_limit=int(os.environ.get("PRUSA_CORE_ONE_FILE_ACTION_LIMIT", "2")),
            state_transition_timeout_s=float(os.environ.get("PRUSA_CORE_ONE_STATE_TRANSITION_TIMEOUT_S", "6.0")),
            safe_to_unload_bed_c=float(os.environ.get("WALLEE_SAFE_TO_UNLOAD_TEMP_C", "35.0")),
            safe_to_touch_nozzle_c=float(os.environ.get("PRUSA_CORE_ONE_SAFE_NOZZLE_TOUCH_C", "50.0")),
            serial_enabled=os.environ.get("PRUSA_CORE_ONE_ENABLE_SERIAL", "0").strip() not in {"0", "false", "False"},
            serial_port=os.environ.get("PRUSA_CORE_ONE_SERIAL_PORT") or None,
            serial_baud=int(os.environ.get("PRUSA_CORE_ONE_SERIAL_BAUD", "115200")),
            serial_timeout_s=float(os.environ.get("PRUSA_CORE_ONE_SERIAL_TIMEOUT_S", "1.0")),
            serial_poll_interval_s=float(os.environ.get("PRUSA_CORE_ONE_SERIAL_POLL_INTERVAL_S", "15.0")),
            serial_error_backoff_s=float(os.environ.get("PRUSA_CORE_ONE_SERIAL_BACKOFF_S", "30.0")),
            metrics_enabled=os.environ.get("PRUSA_CORE_ONE_ENABLE_METRICS", "1").strip() not in {"0", "false", "False"},
            metrics_bind_host=os.environ.get("PRUSA_CORE_ONE_METRICS_BIND_HOST", "0.0.0.0"),
            metrics_port=int(os.environ.get("PRUSA_CORE_ONE_METRICS_PORT", "8514")),
        )


@dataclass(slots=True)
class PrintableFile:
    """One file visible through the PrusaLink USB file API."""

    path: str
    display_name: str
    size_bytes: int | None = None
    printable: bool = True


class SupportsPrusaHttp(Protocol):
    """Protocol for the HTTP control adapter.

    Tests use this protocol to inject fakes without importing network code.
    """

    def get_status(self) -> dict[str, Any]: ...

    def get_info(self) -> dict[str, Any]: ...

    def list_usb_files(self) -> list[PrintableFile]: ...

    def pause_job(self, job_id: int | None = None) -> None: ...

    def resume_job(self, job_id: int | None = None) -> None: ...

    def cancel_job(self, job_id: int | None = None) -> None: ...

    def start_print(self, file_path: str) -> None: ...

    def post_gcode(self, command: str) -> None: ...


class SupportsSerialDiagnostics(Protocol):
    """Protocol for optional serial diagnostics."""

    def maybe_poll(self) -> dict[str, float] | None: ...

    def close(self) -> None: ...


class SupportsMetrics(Protocol):
    """Protocol for optional UDP metrics."""

    def drain(self) -> dict[str, float | int | bool]: ...

    def close(self) -> None: ...


class PrusaLinkHttpClient:
    """Small PrusaLink HTTP client.

    The client intentionally implements only the endpoints the v6 pack actually
    uses.  A generic generated OpenAPI client would bring a lot of surface area
    and indirection without making the runtime safer.
    """

    def __init__(self, settings: PrusaCoreOneSettings) -> None:
        self.settings = settings
        if not settings.host:
            raise ValueError("PRUSA_CORE_ONE_HOST must be set to enable the real Prusa pack")

    def get_status(self) -> dict[str, Any]:
        return self._request_json("GET", "/api/v1/status")

    def get_info(self) -> dict[str, Any]:
        return self._request_json("GET", "/api/v1/info")

    def list_usb_files(self) -> list[PrintableFile]:
        payload = self._request_json("GET", "/api/v1/files/usb")
        return flatten_prusalink_file_tree(payload)

    def pause_job(self, job_id: int | None = None) -> None:
        """Pause an active job.

        The job-id-specific route is preferred because it gives stronger server
        semantics on newer firmware.  The generic command route is kept as a
        compatibility fallback because the current v5 deployment history has
        shown that Prusa firmware surfaces can differ across versions.
        """
        if job_id is not None:
            try:
                self._request_no_content("PUT", f"/api/v1/job/{job_id}/pause")
                return
            except RuntimeError:
                pass
        self._request_no_content("PUT", "/api/v1/job", payload={"command": "PAUSE"})

    def resume_job(self, job_id: int | None = None) -> None:
        if job_id is not None:
            try:
                self._request_no_content("PUT", f"/api/v1/job/{job_id}/resume")
                return
            except RuntimeError:
                pass
        self._request_no_content("PUT", "/api/v1/job", payload={"command": "RESUME"})

    def cancel_job(self, job_id: int | None = None) -> None:
        if job_id is not None:
            try:
                self._request_no_content("DELETE", f"/api/v1/job/{job_id}")
                return
            except RuntimeError:
                pass
        self._request_no_content("DELETE", "/api/v1/job")

    def start_print(self, file_path: str) -> None:
        self._request_no_content("POST", f"/api/v1/files/usb/{quote_printer_path(file_path)}")

    def post_gcode(self, command: str) -> None:
        self._request_no_content("POST", "/api/v1/gcode", payload={"command": command})

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._request(method, path, payload=payload)
        if response.status == 204:
            return {}
        data = response.read().decode("utf-8")
        if not data:
            return {}
        try:
            return json.loads(data)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Expected JSON response from {path}, got: {data[:120]!r}") from exc

    def _request_no_content(self, method: str, path: str, payload: dict[str, Any] | None = None) -> None:
        response = self._request(method, path, payload=payload)
        # A lot of PrusaLink control endpoints use 204 as the normal success code.
        # Treating that as success here keeps the pack code free from status-code
        # trivia and concentrates the compatibility handling in one place.
        if response.status not in {200, 201, 204}:
            raise RuntimeError(f"Unexpected status {response.status} for {method} {path}")

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None):
        url = f"{self.settings.host}{path}"
        headers = {"Accept": "application/json"}
        if self.settings.api_key:
            headers["X-Api-Key"] = self.settings.api_key
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = request.Request(url, data=body, method=method, headers=headers)
        try:
            return request.urlopen(req, timeout=self.settings.http_timeout_s)
        except error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise RuntimeError(f"PrusaLink {method} {path} failed with HTTP {exc.code}: {body_text[:200]!r}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"PrusaLink {method} {path} failed: {exc.reason}") from exc


class NoopMetricsReceiver:
    """Null-object metrics receiver used when UDP metrics are disabled."""

    def drain(self) -> dict[str, float | int | bool]:
        return {}

    def close(self) -> None:
        return None


class PrusaMetricsReceiver:
    """Best-effort UDP metrics receiver.

    The printer's UDP stream is useful for liveness and richer telemetry, but it
    is not required for safe control.  The receiver therefore degrades to an
    empty cache if binding fails or the stream goes quiet.
    """

    def __init__(self, settings: PrusaCoreOneSettings) -> None:
        self.settings = settings
        self._cache: dict[str, float | int | bool] = {}
        self._last_datagram_monotonic = 0.0
        self._socket: socket.socket | None = None

        if not settings.metrics_enabled:
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        try:
            sock.bind((settings.metrics_bind_host, settings.metrics_port))
        except OSError:
            # If the port is already in use, prefer degraded observability over a
            # hard startup failure.  The printer can still be controlled via HTTP.
            sock.close()
            return
        self._socket = sock

    def drain(self) -> dict[str, float | int | bool]:
        if self._socket is None:
            return dict(self._cache)

        while True:
            try:
                payload, _addr = self._socket.recvfrom(65535)
            except BlockingIOError:
                break
            except OSError:
                break
            self._last_datagram_monotonic = time.monotonic()
            for line in payload.decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                measurement, fields = parse_influx_line(line)
                for key, value in fields.items():
                    full_key = f"{measurement}.{key}" if measurement else key
                    self._cache[full_key] = value
                    # Keep a plain alias too.  Latest-write-wins is fine here
                    # because metrics are advisory; if two measurements expose
                    # the same field name, the fully-qualified key remains the
                    # lossless source.
                    self._cache[key] = value
        if self._last_datagram_monotonic:
            self._cache["metrics_fresh"] = (time.monotonic() - self._last_datagram_monotonic) < 2.0
        return dict(self._cache)

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None


class NoopSerialDiagnostics:
    """Null-object serial diagnostics adapter."""

    def maybe_poll(self) -> dict[str, float] | None:
        return None

    def close(self) -> None:
        return None


class PrusaSerialDiagnostics:
    """Best-effort serial diagnostics adapter.

    Serial is intentionally *not* the authoritative control path.  The current
    Wallee v5 reference deployment observed that the Core One USB CDC link can
    produce garbled text and temporary disconnects.  The adapter therefore polls
    slowly, keeps the last good sample, and backs off after errors instead of
    thrashing the port.
    """

    def __init__(self, settings: PrusaCoreOneSettings) -> None:
        self.settings = settings
        self._last_good: dict[str, float] | None = None
        self._last_poll_monotonic = 0.0
        self._backoff_until = 0.0

    def maybe_poll(self) -> dict[str, float] | None:
        now = time.monotonic()
        if now < self._backoff_until:
            return self._last_good
        if self._last_poll_monotonic and (now - self._last_poll_monotonic) < self.settings.serial_poll_interval_s:
            return self._last_good

        self._last_poll_monotonic = now
        try:
            sample = self._poll_once()
        except Exception:
            self._backoff_until = now + self.settings.serial_error_backoff_s
            return self._last_good

        self._last_good = sample
        return sample

    def _poll_once(self) -> dict[str, float]:
        if not self.settings.serial_enabled or not self.settings.serial_port:
            raise RuntimeError("serial diagnostics disabled")
        try:
            import serial  # type: ignore
        except Exception as exc:
            raise RuntimeError("pyserial is required when PRUSA_CORE_ONE_ENABLE_SERIAL=1") from exc

        deadline = time.monotonic() + self.settings.serial_timeout_s
        buffer = ""
        with serial.Serial(self.settings.serial_port, baudrate=self.settings.serial_baud, timeout=0.1) as port:
            port.reset_input_buffer()
            port.write(b"M105\n")
            port.flush()
            while time.monotonic() < deadline:
                chunk = port.read(4096)
                if not chunk:
                    continue
                buffer += chunk.decode("utf-8", errors="replace")
                if "T:" in buffer and ("ok" in buffer or "B:" in buffer):
                    break
        return parse_m105_response(buffer)

    def close(self) -> None:
        return None


def parse_m105_response(text: str) -> dict[str, float]:
    """Parse a Prusa `M105` response.

    Returns a flat dictionary using the same semantic names the pack publishes.
    The parser ignores unknown keys rather than failing because firmware can add
    extra channels over time.
    """
    field_map = {
        "T": ("temp_nozzle_c", "target_nozzle_c"),
        "B": ("temp_bed_c", "target_bed_c"),
        "X": ("temp_chamber_c", "target_chamber_c"),
        "A": ("temp_ambient_c", "target_ambient_c"),
        "@": ("heater_nozzle_pwm", None),
        "B@": ("heater_bed_pwm", None),
        "C@": ("heater_chamber_pwm", None),
        "HBR@": ("fan_heatbreak_pwm", None),
    }
    result: dict[str, float] = {}
    for match in _M105_TOKEN_RE.finditer(text):
        key = match.group("key")
        names = field_map.get(key)
        if names is None:
            continue
        actual = float(match.group("actual"))
        result[names[0]] = actual
        if names[1] is not None and match.group("target") is not None:
            result[names[1]] = float(match.group("target"))
    if not result:
        raise ValueError(f"Could not parse M105 response: {text!r}")
    return result


def parse_influx_line(line: str) -> tuple[str, dict[str, float | int | bool]]:
    """Parse one best-effort Influx line protocol record.

    The parser intentionally supports only the field types we need for the pack:
    integers, floats, and booleans.  Strings are ignored because the Core One
    metrics path is used for numeric telemetry and freshness, not rich metadata.
    """
    measurement_and_tags, _, rest = line.partition(" ")
    field_part = rest.split(" ", 1)[0] if rest else ""
    measurement = measurement_and_tags.split(",", 1)[0].strip()

    fields: dict[str, float | int | bool] = {}
    for token in field_part.split(","):
        if not token or "=" not in token:
            continue
        key, raw_value = token.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key:
            continue
        if raw_value.endswith("i") and raw_value[:-1].lstrip("-").isdigit():
            fields[key] = int(raw_value[:-1])
            continue
        if raw_value.lower() in {"true", "t"}:
            fields[key] = True
            continue
        if raw_value.lower() in {"false", "f"}:
            fields[key] = False
            continue
        try:
            fields[key] = float(raw_value)
        except ValueError:
            continue
    return measurement, fields


def flatten_prusalink_file_tree(payload: Any) -> list[PrintableFile]:
    """Flatten a PrusaLink file-tree payload into printable file entries.

    PrusaLink can return either a root object with `children`, a bare list, or a
    single-node object depending on endpoint and firmware version.  The pack only
    needs a normalized list of files, so this helper accepts all of those shapes
    and discards the directory scaffolding.
    """
    results: list[PrintableFile] = []

    def walk(node: Any, parent: str = "") -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, parent)
            return
        if not isinstance(node, dict):
            return

        children = node.get("children")
        path = str(node.get("path") or node.get("name") or "")
        if path and not path.startswith("/"):
            joined = f"{parent.rstrip('/')}/{path.lstrip('/')}" if parent else f"/{path.lstrip('/')}"
        else:
            joined = path or parent

        if isinstance(children, list):
            for child in children:
                walk(child, joined)
            return

        candidate_path = joined or parent
        display_name = str(node.get("display_name") or Path(candidate_path).name)
        size = node.get("size")
        printable = display_name.lower().endswith(_PRINTABLE_SUFFIXES) or candidate_path.lower().endswith(_PRINTABLE_SUFFIXES)
        if printable:
            normalized = candidate_path
            if not normalized.startswith("/"):
                normalized = f"/{normalized.lstrip('/')}"
            results.append(
                PrintableFile(
                    path=normalized,
                    display_name=display_name,
                    size_bytes=int(size) if isinstance(size, (int, float)) else None,
                    printable=True,
                )
            )

    walk(payload)

    deduped: dict[str, PrintableFile] = {}
    for entry in results:
        deduped.setdefault(entry.path, entry)
    return list(deduped.values())


def quote_printer_path(file_path: str) -> str:
    """Quote a printer file path for use inside `/api/v1/files/usb/{path}`.

    The helper accepts either `/usb/foo.bgcode`, `usb/foo.bgcode`, or
    `foo.bgcode` and always returns the *path segment* relative to `usb/`.
    """
    normalized = file_path.strip()
    if normalized.startswith("/usb/"):
        normalized = normalized[5:]
    elif normalized.startswith("usb/"):
        normalized = normalized[4:]
    normalized = normalized.lstrip("/")
    return parse.quote(normalized, safe="/")
