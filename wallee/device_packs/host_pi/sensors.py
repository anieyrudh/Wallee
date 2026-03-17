"""Host Pi sensor tools — reads CPU temp, system stats, USB, and network."""

import logging
import subprocess
from pathlib import Path

import psutil

from wallee.tools.decorator import tool

logger = logging.getLogger(__name__)

THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0/temp")


@tool(kind="sensor", refresh_hz=1.0, history_depth=10)
def read_cpu_temp():
    """Read CPU temperature from thermal zone (millidegrees C on Linux)."""
    try:
        if THERMAL_ZONE.exists():
            raw = THERMAL_ZONE.read_text().strip()
            temp_c = int(raw) / 1000.0
        else:
            # Fallback for non-Linux (macOS dev): use psutil if available
            temps = psutil.sensors_temperatures()
            if temps:
                # Use first available sensor
                for name, entries in temps.items():
                    if entries:
                        temp_c = entries[0].current
                        break
                else:
                    return {"error": "no temperature sensors found"}
            else:
                return {"error": "no temperature sensors available"}

        if temp_c < -40 or temp_c > 120:
            raise ValueError(f"CPU temp {temp_c} outside sane range")

        return {"host.cpu_temp": round(temp_c, 1)}
    except Exception as e:
        logger.error(f"read_cpu_temp error: {e}")
        return {"error": str(e)}


@tool(kind="sensor", refresh_hz=0.5, history_depth=10)
def read_system_stats():
    """Read CPU load, memory %, disk %, and uptime."""
    try:
        cpu_pct = psutil.cpu_percent(interval=0)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        boot_time = psutil.boot_time()
        import time
        uptime_s = time.time() - boot_time

        return {
            "host.cpu_percent": cpu_pct,
            "host.memory_percent": round(mem.percent, 1),
            "host.disk_percent": round(disk.percent, 1),
            "host.uptime_hours": round(uptime_s / 3600, 1),
        }
    except Exception as e:
        logger.error(f"read_system_stats error: {e}")
        return {"error": str(e)}


@tool(kind="sensor", refresh_hz=0.1, history_depth=0)
def read_usb_devices():
    """List connected USB devices."""
    try:
        usb_path = Path("/sys/bus/usb/devices")
        if usb_path.exists():
            devices = []
            for dev in usb_path.iterdir():
                product_file = dev / "product"
                manufacturer_file = dev / "manufacturer"
                if product_file.exists():
                    product = product_file.read_text().strip()
                    manufacturer = ""
                    if manufacturer_file.exists():
                        manufacturer = manufacturer_file.read_text().strip()
                    devices.append(f"{manufacturer} {product}".strip())
            return {"host.usb_devices": devices}
        else:
            # Fallback: try lsusb command
            result = subprocess.run(
                ["lsusb"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
                return {"host.usb_devices": lines}
            # No USB enumeration available (e.g., macOS without lsusb)
            result = subprocess.run(
                ["system_profiler", "SPUSBDataType", "-detailLevel", "mini"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return {"host.usb_devices": [result.stdout[:500]]}
            return {"host.usb_devices": []}
    except Exception as e:
        logger.error(f"read_usb_devices error: {e}")
        return {"error": str(e)}


@tool(kind="sensor", refresh_hz=0.2, history_depth=0)
def read_network_interfaces():
    """Read network interface status and addresses."""
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        interfaces = {}
        for name, addr_list in addrs.items():
            if name.startswith("lo"):
                continue
            info = {"up": stats.get(name, None) and stats[name].isup}
            for addr in addr_list:
                if addr.family.name == "AF_INET":
                    info["ipv4"] = addr.address
                elif addr.family.name == "AF_INET6":
                    info.setdefault("ipv6", addr.address)
            interfaces[name] = info
        return {"host.network_interfaces": interfaces}
    except Exception as e:
        logger.error(f"read_network_interfaces error: {e}")
        return {"error": str(e)}
