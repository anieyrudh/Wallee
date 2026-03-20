"""UDP listener for Prusa metrics stream (InfluxDB line protocol in Syslog RFC 5424).

The printer pushes ~50 UDP packets/sec to port 8514, each containing a syslog-wrapped
batch of InfluxDB line protocol metrics. This module provides:
- UDPListener: binds a UDP socket and receives packets in a background thread
- parse_syslog_header: extracts sequence number and timestamp from RFC 5424 header
- parse_influx_line: parses a single InfluxDB line protocol metric
- MetricsBuffer: thread-safe buffer for latest metric values
"""

import logging
import re
import socket
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# InfluxDB value type patterns
_INT_RE = re.compile(r'^(-?\d+)i$')
_FLOAT_RE = re.compile(r'^-?\d+\.?\d*(?:[eE][+-]?\d+)?$')
_STR_RE = re.compile(r'^"(.*)"$')
_BOOL_TRUE = frozenset({"T", "t", "True", "true"})
_BOOL_FALSE = frozenset({"F", "f", "False", "false"})


@dataclass
class Metric:
    """A single parsed metric point."""
    name: str
    tags: dict[str, str]
    fields: dict[str, int | float | str | bool]
    timestamp: int | None = None  # microsecond offset


def parse_influx_value(raw: str) -> int | float | str | bool:
    """Parse an InfluxDB field value with type inference.

    Types: 42i → int, 3.14 → float, "hello" → str, T/F → bool.
    """
    m = _INT_RE.match(raw)
    if m:
        return int(m.group(1))

    m = _STR_RE.match(raw)
    if m:
        return m.group(1)

    if raw in _BOOL_TRUE:
        return True
    if raw in _BOOL_FALSE:
        return False

    if _FLOAT_RE.match(raw):
        return float(raw)

    return raw  # fallback: return as string


def parse_influx_line(line: str) -> Metric | None:
    """Parse a single InfluxDB line protocol line.

    Format: measurement[,tag=val...] field=val[,field=val...] [timestamp]
    """
    line = line.strip()
    if not line:
        return None

    # Split into: measurement+tags, fields, optional timestamp
    parts = line.split(" ")
    if len(parts) < 2:
        return None

    # Parse measurement and tags
    measurement_part = parts[0]
    if "," in measurement_part:
        chunks = measurement_part.split(",")
        name = chunks[0]
        tags = {}
        for chunk in chunks[1:]:
            if "=" in chunk:
                k, _, v = chunk.partition("=")
                tags[k] = v
    else:
        name = measurement_part
        tags = {}

    # Parse fields
    field_part = parts[1]
    fields = {}
    for pair in field_part.split(","):
        if "=" not in pair:
            # Single value with no key — use "v" as default
            fields["v"] = parse_influx_value(pair)
            continue
        k, _, v = pair.partition("=")
        fields[k] = parse_influx_value(v)

    # Parse optional timestamp
    timestamp = None
    if len(parts) >= 3:
        try:
            timestamp = int(parts[2])
        except ValueError:
            pass

    return Metric(name=name, tags=tags, fields=fields, timestamp=timestamp)


def parse_syslog_header(raw: str) -> tuple[dict, str]:
    """Parse RFC 5424 syslog header from a metrics packet.

    Returns (header_dict, remaining_content).
    Header format: <14>1 - MAC buddy - - - msg=SEQ,tm=TS,v=4 FIRST_LINE
    """
    header = {}

    if not raw.startswith("<"):
        return header, raw

    # Find end of structured data: "msg=...,tm=...,v=..."
    # The header ends after the "v=N " part, then the first metric line follows
    idx = raw.find("v=4 ")
    if idx < 0:
        idx = raw.find("v=3 ")
    if idx < 0:
        return header, raw

    header_str = raw[:idx + 3]  # include "v=N"
    remaining = raw[idx + 4:]   # skip "v=N "

    # Extract fields
    msg_match = re.search(r'msg=(\d+)', header_str)
    if msg_match:
        header["seq"] = int(msg_match.group(1))

    tm_match = re.search(r'tm=(\d+)', header_str)
    if tm_match:
        header["timestamp_us"] = int(tm_match.group(1))

    return header, remaining


class MetricsBuffer:
    """Thread-safe buffer holding the latest value for each metric.

    The UDP listener writes to this buffer; sensor tools read from it.
    """

    def __init__(self):
        self._data: dict[str, Metric] = {}
        self._lock = threading.Lock()
        self._last_seq = -1
        self._packets_received = 0
        self._metrics_parsed = 0

    def update(self, metric: Metric):
        """Store/update a metric. Thread-safe."""
        # Build a key from name + tags for uniqueness
        key = metric.name
        if metric.tags:
            tag_str = ",".join(f"{k}={v}" for k, v in sorted(metric.tags.items()))
            key = f"{metric.name},{tag_str}"

        with self._lock:
            self._data[key] = metric
            self._metrics_parsed += 1

    def get(self, key: str) -> Metric | None:
        """Get latest metric by key (e.g., 'temp_noz,a=1,n=0'). Thread-safe."""
        with self._lock:
            return self._data.get(key)

    def get_field(self, key: str, field: str = "v") -> int | float | str | bool | None:
        """Get a single field value from a metric. Convenience method."""
        m = self.get(key)
        if m is None:
            return None
        return m.fields.get(field)

    def get_all(self) -> dict[str, Metric]:
        """Snapshot of all current metrics."""
        with self._lock:
            return dict(self._data)

    def record_packet(self, seq: int):
        """Track packet reception stats."""
        with self._lock:
            self._last_seq = seq
            self._packets_received += 1

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "packets_received": self._packets_received,
                "metrics_parsed": self._metrics_parsed,
                "last_seq": self._last_seq,
                "unique_metrics": len(self._data),
            }


class UDPListener:
    """Listens on a UDP port and parses incoming Prusa metrics packets.

    Runs a single background thread that receives packets, parses syslog+InfluxDB,
    and updates the shared MetricsBuffer.
    """

    def __init__(self, bind_addr: str = "0.0.0.0", port: int = 8514, buffer: MetricsBuffer | None = None):
        self.bind_addr = bind_addr
        self.port = port
        self.buffer = buffer or MetricsBuffer()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self):
        """Start listening in a background thread."""
        if self._running:
            return

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.bind_addr, self.port))
        self._sock.settimeout(1.0)  # allow clean shutdown

        self._running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True, name="udp-metrics")
        self._thread.start()
        logger.info(f"UDP metrics listener started on {self.bind_addr}:{self.port}")

    def _listen_loop(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                if self._running:
                    logger.error("UDP socket error", exc_info=True)
                break

            try:
                self._process_packet(data.decode("utf-8", errors="replace"))
            except Exception:
                logger.error("Failed to process metrics packet", exc_info=True)

    def _process_packet(self, raw: str):
        """Parse a single UDP packet containing syslog-wrapped InfluxDB metrics."""
        header, content = parse_syslog_header(raw)
        seq = header.get("seq", -1)
        self.buffer.record_packet(seq)

        # The first metric line is appended to the header line
        # Remaining metrics are on separate lines
        lines = content.split("\n")
        for line in lines:
            line = line.strip()
            if not line:
                continue
            metric = parse_influx_line(line)
            if metric:
                self.buffer.update(metric)

    def stop(self):
        """Stop the listener."""
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        logger.info("UDP metrics listener stopped")
