from __future__ import annotations

import argparse
import fcntl
import json
import os
import select
import sys
import termios
import time


_SPAM_PATTERNS = (
    "FIRMWARE_NAME:",
    "SOURCE_CODE_URL:",
    "PROTOCOL_VERSION:",
    "MACHINE_TYPE:",
    "EXTRUDER_COUNT:",
    "UUID:",
    "Cap:",
)
_RESPONSE_CAPTURE_WINDOW_S = 2.5


def _is_spam(line: str) -> bool:
    return any(pattern in line for pattern in _SPAM_PATTERNS)


def _is_garbled(line: str) -> bool:
    printable = sum(1 for c in line if 32 <= ord(c) <= 126)
    return not line or printable / max(len(line), 1) < 0.7


def _emit(payload: dict[str, object]) -> int:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()
    return 0


def _configure_port(fd: int, *, baud: int) -> None:
    baud_flag = getattr(termios, f"B{baud}", None)
    if baud_flag is None:
        raise RuntimeError(f"unsupported baud rate: {baud}")
    attrs = termios.tcgetattr(fd)
    attrs[0] = termios.IGNPAR
    attrs[1] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    if hasattr(termios, "CRTSCTS"):
        attrs[2] &= ~termios.CRTSCTS
    if hasattr(termios, "HUPCL"):
        attrs[2] &= ~termios.HUPCL
    attrs[3] = 0
    attrs[4] = baud_flag
    attrs[5] = baud_flag
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def _capture_lines(fd: int) -> list[str]:
    deadline = time.monotonic() + _RESPONSE_CAPTURE_WINDOW_S
    buffer = bytearray()
    lines: list[str] = []
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        readable, _, _ = select.select([fd], [], [], min(0.2, remaining))
        if not readable:
            continue
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            continue
        if not chunk:
            continue
        buffer.extend(chunk)
        while b"\n" in buffer:
            raw_line, _, rest = buffer.partition(b"\n")
            buffer = bytearray(rest)
            text = raw_line.decode("utf-8", errors="replace").strip()
            if not text or _is_spam(text) or _is_garbled(text):
                continue
            lines.append(text)
            if text.startswith("ok"):
                return lines
    if buffer:
        text = bytes(buffer).decode("utf-8", errors="replace").strip()
        if text and not _is_spam(text) and not _is_garbled(text):
            lines.append(text)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One isolated Prusa serial exchange.")
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, required=True)
    parser.add_argument("--timeout-s", type=float, required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--session-key")
    parser.add_argument("--capture-response", action="store_true")
    args = parser.parse_args(argv)

    started = time.monotonic()
    fd: int | None = None
    try:
        fd = os.open(args.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _configure_port(fd, baud=args.baud)
        time.sleep(min(max(args.timeout_s, 0.1), 0.2))
        termios.tcflush(fd, termios.TCIFLUSH)
        os.write(fd, (args.command.rstrip() + "\n").encode("utf-8"))
        lines = _capture_lines(fd) if args.capture_response else []
        return _emit(
            {
                "ok": True,
                "command": args.command,
                "capture_response": args.capture_response,
                "session_key": args.session_key,
                "lines": lines,
                "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
                "emitted_before_teardown": True,
            }
        )
    except Exception as exc:
        return _emit(
            {
                "ok": False,
                "command": args.command,
                "capture_response": args.capture_response,
                "session_key": args.session_key,
                "error": repr(exc),
            }
        )
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(fd)
            except Exception:
                # Closing /dev/ttyACM0 can hang on this firmware path; the caller
                # treats an already-emitted JSON payload as success and may kill
                # the helper if teardown stalls.
                pass


if __name__ == "__main__":
    raise SystemExit(main())
