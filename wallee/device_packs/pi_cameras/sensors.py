"""Camera sensor tools with auto-discovery.

Nozzle camera: 3DO endoscope via ustreamer (localhost:8083).
Buddy cameras: Prusa WiFi cameras discovered by MAC prefix (88:49:2d)
via the ARP/neighbor table. No hardcoded IPs — cameras are found each
discovery cycle, so DHCP changes and plug/unplug are handled automatically.
"""

import base64
import logging
import os
import subprocess
import tempfile
import threading as _threading
import time

import httpx

from wallee.device_packs.pi_cameras.vision_analysis import read_vision_analysis  # noqa: F401
from wallee.tools.decorator import tool

logger = logging.getLogger(__name__)

# Buddy camera MAC prefix (Shenzhen Bilian / Prusa WiFi cameras)
BUDDY_MAC_PREFIX = "88:49:2d"

# Discovery cache
_discovered_buddies: list[str] = []  # list of IPs
_last_discovery: float = 0
_discovered_nozzle_port: str | None = None
_last_nozzle_discovery: float = 0
_camera_lock = _threading.Lock()
DISCOVERY_INTERVAL = 60  # re-scan every 60 seconds

# Nozzle camera
_nozzle_client: httpx.Client | None = None
_nozzle_port: str | None = None

# Staleness tracking
_stale_state: dict[str, tuple[int | None, int]] = {}  # key → (last_hash, stale_count)
STALE_THRESHOLD = 3


def _candidate_nozzle_ports() -> list[str]:
    ports = []
    env_port = os.environ.get("NOZZLE_CAMERA_PORT", "").strip()
    if env_port:
        ports.append(env_port)
    for port in ("8083", "8080", "8084", "8082", "8085", "8090"):
        if port not in ports:
            ports.append(port)
    return ports


def discover_nozzle_camera_port(force: bool = False) -> str | None:
    global _discovered_nozzle_port, _last_nozzle_discovery

    now = time.monotonic()
    with _camera_lock:
        if not force and (now - _last_nozzle_discovery) < DISCOVERY_INTERVAL and _discovered_nozzle_port:
            return _discovered_nozzle_port

    for port in _candidate_nozzle_ports():
        try:
            response = httpx.get(f"http://localhost:{port}/snapshot", timeout=1.5)
            content_type = response.headers.get("content-type", "")
            if response.status_code == 200 and (response.content.startswith(b"\xff\xd8") or "image/jpeg" in content_type):
                with _camera_lock:
                    _discovered_nozzle_port = port
                    _last_nozzle_discovery = now
                    return _discovered_nozzle_port
        except Exception:
            continue

    with _camera_lock:
        _discovered_nozzle_port = None
        _last_nozzle_discovery = now
        return None


def _get_nozzle_client() -> httpx.Client:
    global _nozzle_client, _nozzle_port
    with _camera_lock:
        if _nozzle_client is not None:
            return _nozzle_client

    port = discover_nozzle_camera_port() or os.environ.get("NOZZLE_CAMERA_PORT", "8083")
    with _camera_lock:
        if _nozzle_client is None:
            _nozzle_port = str(port)
            _nozzle_client = httpx.Client(base_url=f"http://localhost:{_nozzle_port}", timeout=5.0)
        return _nozzle_client


def _ping_sweep_subnet():
    """Send a quick ping sweep to populate the ARP table with responding hosts."""
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=3,
        )
        for part in result.stdout.split():
            if part.count(".") == 3:
                subnet = part.rsplit(".", 1)[0] + ".0/24"
                subprocess.run(
                    ["nmap", "-sn", "-T4", "--max-retries", "1", subnet],
                    capture_output=True, timeout=10,
                )
                return

        subprocess.run(
            ["ping", "-b", "-c", "1", "-W", "1", "255.255.255.255"],
            capture_output=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as e:
        logger.debug(f"Ping sweep failed (non-fatal): {e}")


def discover_buddy_cameras() -> list[str]:
    """Scan ARP/neighbor table for Prusa buddy cameras by MAC prefix."""
    global _discovered_buddies, _last_discovery

    now = time.monotonic()
    with _camera_lock:
        if now - _last_discovery < DISCOVERY_INTERVAL and _discovered_buddies:
            return list(_discovered_buddies)

    _ping_sweep_subnet()

    ips = []
    try:
        result = subprocess.run(
            ["ip", "neigh", "show"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and BUDDY_MAC_PREFIX in line.lower():
                ip = parts[0]
                state = parts[-1]
                if state in ("REACHABLE", "STALE", "DELAY"):
                    ips.append(ip)
    except Exception as e:
        logger.error(f"Buddy camera discovery failed: {e}")

    with _camera_lock:
        previous_ips = list(_discovered_buddies)
    if ips != previous_ips:
        logger.info(f"Buddy cameras discovered: {ips}" if ips else "No buddy cameras found")

    with _camera_lock:
        _discovered_buddies = ips
        _last_discovery = now
        return list(_discovered_buddies)


def _capture_http_jpeg(client: httpx.Client, path: str, max_width: int = 0) -> bytes | None:
    """Fetch a JPEG snapshot via HTTP. Optionally resize."""
    try:
        resp = client.get(path)
        if resp.status_code != 200 or len(resp.content) < 100:
            return None
        data = resp.content
        if max_width > 0 and len(data) > 50000:
            try:
                from PIL import Image
                from io import BytesIO
                img = Image.open(BytesIO(data))
                if img.width > max_width:
                    ratio = max_width / img.width
                    new_size = (max_width, int(img.height * ratio))
                    img = img.resize(new_size, Image.LANCZOS)
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=80)
                data = buf.getvalue()
            except ImportError:
                pass
        return data
    except Exception as e:
        logger.error(f"Camera HTTP {path} failed: {e}")
        return None


def _capture_rtsp_jpeg(ip: str, max_width: int = 640) -> bytes | None:
    """Capture a single JPEG frame from an RTSP stream via ffmpeg."""
    url = f"rtsp://{ip}/live"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name

        result = subprocess.run(
            ["ffmpeg", "-y", "-rtsp_transport", "tcp", "-i", url,
             "-vframes", "1", "-vf", f"scale={max_width}:-1",
             "-q:v", "3", "-f", "image2", tmp_path],
            capture_output=True, timeout=5,
        )

        if result.returncode != 0 or not os.path.exists(tmp_path):
            return None

        with open(tmp_path, "rb") as f:
            data = f.read()

        return data if len(data) >= 100 else None

    except subprocess.TimeoutExpired:
        return None
    except FileNotFoundError:
        logger.error("ffmpeg not installed")
        return None
    except Exception as e:
        logger.error(f"RTSP capture {ip} failed: {e}")
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _check_stale(key: str, jpeg: bytes) -> bool:
    """Check if frame is stale. Returns True if stale."""
    frame_hash = hash(jpeg)
    with _camera_lock:
        last_hash, count = _stale_state.get(key, (None, 0))

        if frame_hash == last_hash:
            count += 1
        else:
            count = 0

        _stale_state[key] = (frame_hash, count)
        return count >= STALE_THRESHOLD


def _make_frame_result(key_prefix: str, jpeg: bytes | None) -> dict:
    """Build the result dict for a camera frame capture."""
    if jpeg is None:
        return {f"{key_prefix}_status": "offline"}

    if _check_stale(key_prefix, jpeg):
        return {f"{key_prefix}_status": "stale"}

    b64 = base64.b64encode(jpeg).decode("ascii")
    return {
        f"{key_prefix}_frame": b64,
        f"{key_prefix}_frame_size": len(jpeg),
        f"{key_prefix}_status": "live",
    }


# ---------------------------------------------------------------------------
# Sensor tools
# ---------------------------------------------------------------------------

@tool(kind="sensor", refresh_hz=1.0, history_depth=3)
def read_nozzle_camera() -> dict:
    """Capture from the 3DO nozzle endoscope (640x480 JPEG via ustreamer).

    Use this to inspect: first layer adhesion, nozzle condition, stringing,
    print surface quality, filament flow, and layer alignment.
    """
    client = _get_nozzle_client()
    jpeg = _capture_http_jpeg(client, "/snapshot", max_width=640)
    result = _make_frame_result("camera.nozzle", jpeg)
    if _nozzle_port is not None:
        result["camera.nozzle_port"] = _nozzle_port
    return result


@tool(kind="sensor", refresh_hz=0.1, history_depth=3)
def read_buddy_cameras() -> dict:
    """Capture from Prusa Buddy WiFi cameras (auto-discovered by MAC prefix).

    Buddy cameras are separate WiFi devices with RTSP streams. IPs are
    discovered via the ARP table — no hardcoded addresses. Cameras that
    are unplugged or change IP are handled automatically.
    """
    buddies = discover_buddy_cameras()
    result = {"camera.buddy_count": len(buddies)}

    for i, ip in enumerate(sorted(buddies)):
        key = f"camera.buddy{i+1}"
        jpeg = _capture_rtsp_jpeg(ip, max_width=640)
        result.update(_make_frame_result(key, jpeg))
        result[f"{key}_ip"] = ip

    # Mark missing cameras as offline
    for i in range(len(buddies), 3):
        result[f"camera.buddy{i+1}_status"] = "offline"

    return result
