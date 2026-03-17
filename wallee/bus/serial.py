"""Generic serial bus for USB-connected devices.

Open-send-read-close pattern for devices that disconnect frequently.
Opens the port fresh for each command, sends, reads response, and closes.

Device-specific logic (VID:PID detection, port patterns) is passed in
by the device pack — not hardcoded here.

Key features:
- Blacklisted commands enforced at bus level
- Garble detection (discard lines with non-printable characters)
- Configurable spam filtering
- Mutex for single command at a time
- Auto-reconnect with configurable wait
"""

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)


def _is_garbled(line: str) -> bool:
    """Detect garbled serial output (corrupted bytes from USB instability)."""
    if not line:
        return True
    printable = sum(1 for c in line if 32 <= ord(c) <= 126)
    if len(line) > 0 and printable / len(line) < 0.7:
        return True
    return False


def find_serial_port(
    vid_pid: str | None = None,
    by_id_pattern: str | None = None,
    fallback_path: str | None = "/dev/ttyACM0",
) -> str | None:
    """Find a serial port by USB VID:PID or /dev/serial/by-id/ pattern.

    Args:
        vid_pid: USB vendor ID prefix to match (e.g., "2c99" for Prusa).
        by_id_pattern: Substring to match in /dev/serial/by-id/ entries.
        fallback_path: Last-resort path to check (e.g., /dev/ttyACM0).
    """
    by_id = "/dev/serial/by-id"
    if by_id_pattern and os.path.isdir(by_id):
        for entry in os.listdir(by_id):
            if by_id_pattern in entry:
                link = os.path.join(by_id, entry)
                real = os.path.realpath(link)
                if os.path.exists(real):
                    return real

    if fallback_path and os.path.exists(fallback_path):
        return fallback_path

    return None


class SerialBus:
    """Serial communication using open-send-read-close pattern.

    Args:
        port: Explicit port path, or None for auto-detection.
        baud: Baud rate (default 115200).
        timeout: Read timeout per line (default 0.3s).
        blacklisted_commands: Set of commands that must never be sent.
        spam_patterns: Set of substrings to filter from response lines.
        find_port_fn: Callable returning port path for auto-detection.
    """

    def __init__(
        self,
        port: str | None = None,
        baud: int = 115200,
        timeout: float = 0.3,
        reconnect_wait: float = 2.0,
        max_reconnect_wait: float = 10.0,
        blacklisted_commands: frozenset | None = None,
        spam_patterns: frozenset | None = None,
        find_port_fn=None,
    ):
        self._port = port
        self._baud = baud
        self._timeout = timeout
        self._reconnect_wait = reconnect_wait
        self._max_reconnect_wait = max_reconnect_wait
        self._blacklisted = blacklisted_commands or frozenset()
        self._spam_patterns = spam_patterns or frozenset()
        self._find_port_fn = find_port_fn or (lambda: None)
        self._lock = threading.Lock()

    @property
    def port(self) -> str | None:
        """Current port path, auto-detected if not set."""
        if self._port:
            return self._port
        return self._find_port_fn()

    def send_command(self, gcode: str) -> dict:
        """Send a command and return the response.

        Returns dict with:
        - {"status": "success", "lines": [...]} on success
        - {"error": "reason"} on failure

        Thread-safe via mutex. Enforces blacklist.
        """
        gcode = gcode.strip().upper()

        cmd = gcode.split()[0] if gcode else ""
        if cmd in self._blacklisted:
            return {"error": f"BLACKLISTED: {cmd} is forbidden (safety risk)"}

        with self._lock:
            return self._send_locked(gcode)

    def _is_spam(self, line: str) -> bool:
        """Check if a line matches any configured spam pattern."""
        for pattern in self._spam_patterns:
            if pattern in line:
                return True
        return False

    def _send_locked(self, gcode: str) -> dict:
        """Send with port held under lock."""
        try:
            import serial as pyserial
        except ImportError:
            return {"error": "pyserial not installed"}

        port_path = self.port
        if not port_path:
            return {"error": "No serial port found"}

        if not os.path.exists(port_path):
            port_path = self._wait_for_port()
            if not port_path:
                return {"error": f"Serial port not available after {self._max_reconnect_wait}s"}

        try:
            ser = pyserial.Serial(port_path, self._baud, timeout=self._timeout, dsrdtr=False)
            ser.rts = False
        except Exception as e:
            return {"error": f"Failed to open {port_path}: {e}"}

        try:
            time.sleep(0.1)
            ser.reset_input_buffer()

            ser.write(f"{gcode}\n".encode())

            lines = []
            end = time.time() + 2.0
            while time.time() < end:
                try:
                    raw = ser.readline()
                except Exception:
                    break
                if not raw:
                    continue

                try:
                    text = raw.decode("utf-8", errors="replace").strip()
                except Exception:
                    continue

                if not text:
                    continue
                if _is_garbled(text):
                    continue
                if self._is_spam(text):
                    continue

                lines.append(text)

                if text.startswith("ok"):
                    break

            return {"status": "success", "lines": lines}

        except Exception as e:
            return {"error": f"Serial communication failed: {e}"}
        finally:
            try:
                ser.close()
            except Exception:
                pass

    def _wait_for_port(self) -> str | None:
        """Wait for the USB port to reappear after a disconnect."""
        end = time.time() + self._max_reconnect_wait
        while time.time() < end:
            port = self._find_port_fn() if not self._port else self._port
            if port and os.path.exists(port):
                time.sleep(0.3)
                return port
            time.sleep(self._reconnect_wait)
        return None
