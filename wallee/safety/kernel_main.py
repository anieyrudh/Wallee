"""Safety kernel — runs as an independent OS process."""

import argparse
import json
import logging
import os
import time

import redis

from wallee.safety.estop import estop_printer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [safety] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("wallee.safety.kernel")


def _parse_json(raw: str, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Failed to parse JSON argument for safety kernel; using default")
        return default


def _estop_printer(host: str, api_key: str = "", primary_request: dict | None = None,
                   fallback_request: dict | None = None):
    """Execute the configured stop requests."""
    if not host:
        return
    estop_printer(host, api_key, primary_request=primary_request, fallback_request=fallback_request)


def run_kernel(
    redis_url: str = "redis://localhost:6379",
    check_interval: float = 2.0,
    heartbeat_timeout: float = 30.0,
    control_host: str = "",
    control_api_key: str = "",
    primary_stop: dict | None = None,
    fallback_stop: dict | None = None,
    fault_monitors: list[dict] | None = None,
):
    """Main safety loop. Runs indefinitely."""
    r = redis.Redis.from_url(redis_url, decode_responses=True)
    r.ping()
    logger.info(f"Safety kernel process started (PID={os.getpid()})")

    agent_alerted = False
    engine_alerted = False
    fault_alerted: dict[str, bool] = {}
    estop_alerted = False
    boot_time = time.monotonic()
    boot_grace_s = 15.0  # suppress heartbeat alerts while components start
    monitors = fault_monitors or []

    while True:
        try:
            in_grace = (time.monotonic() - boot_time) < boot_grace_s

            # Check ESTOP flag
            estop_raw = r.get("safety.estop")
            if estop_raw and estop_raw != "false":
                if not estop_alerted:
                    logger.critical("ESTOP flag active on whiteboard")
                    _estop_printer(control_host, control_api_key,
                                   primary_request=primary_stop,
                                   fallback_request=fallback_stop)
                    estop_alerted = True
            else:
                estop_alerted = False

            # Check agent heartbeat
            agent_hb_raw = r.get("agent.heartbeat")
            if agent_hb_raw is None:
                if not agent_alerted and not in_grace:
                    logger.warning("Agent heartbeat missing")
                    r.set("safety.agent_stale", "true", ex=60)
                    agent_alerted = True
            else:
                try:
                    age = time.monotonic() - float(agent_hb_raw)
                    if age > heartbeat_timeout:
                        if not agent_alerted:
                            logger.warning(f"Agent heartbeat stale ({age:.0f}s old)")
                            r.set("safety.agent_stale", "true", ex=60)
                            agent_alerted = True
                    else:
                        agent_alerted = False
                except (ValueError, TypeError):
                    pass

            # Check engine heartbeat
            engine_hb_raw = r.get("engine.heartbeat")
            if engine_hb_raw is None:
                if not engine_alerted and not in_grace:
                    logger.warning("Engine heartbeat missing")
                    r.set("safety.engine_stale", "true", ex=60)
                    engine_alerted = True
            else:
                try:
                    age = time.monotonic() - float(engine_hb_raw)
                    if age > heartbeat_timeout:
                        if not engine_alerted:
                            logger.warning(f"Engine heartbeat stale ({age:.0f}s old)")
                            r.set("safety.engine_stale", "true", ex=60)
                            engine_alerted = True
                    else:
                        engine_alerted = False
                except (ValueError, TypeError):
                    pass

            # Check configured fault monitors
            for monitor in monitors:
                key = monitor.get("key", "")
                label = monitor.get("label", "configured fault")
                raw_value = r.get(key) if key else None
                if raw_value and raw_value != "0":
                    if not fault_alerted.get(key):
                        logger.critical(f"SAFETY FAULT: {label} ({key}={raw_value})")
                        _estop_printer(control_host, control_api_key,
                                       primary_request=primary_stop,
                                       fallback_request=fallback_stop)
                        fault_alerted[key] = True
                else:
                    fault_alerted[key] = False

            time.sleep(check_interval)

        except redis.ConnectionError:
            logger.error("Redis connection lost — retrying in 5s")
            time.sleep(5)
        except Exception as e:
            logger.error(f"Safety kernel error: {e}")
            time.sleep(check_interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Wallee safety kernel (standalone process)")
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://localhost:6379"))
    parser.add_argument("--control-host", default="")
    parser.add_argument("--control-api-key", default="")
    parser.add_argument("--primary-stop-json", default="")
    parser.add_argument("--fallback-stop-json", default="")
    parser.add_argument("--fault-monitors-json", default="[]")
    parser.add_argument("--check-interval", type=float, default=2.0)
    parser.add_argument("--heartbeat-timeout", type=float, default=30.0)
    args = parser.parse_args()

    run_kernel(
        redis_url=args.redis_url,
        control_host=args.control_host,
        control_api_key=args.control_api_key,
        primary_stop=_parse_json(args.primary_stop_json, {"method": "POST", "path": "/api/v1/gcode", "json": {"command": "M25"}}),
        fallback_stop=_parse_json(args.fallback_stop_json, {"method": "DELETE", "path": "/api/v1/job"}),
        fault_monitors=_parse_json(args.fault_monitors_json, []),
        check_interval=args.check_interval,
        heartbeat_timeout=args.heartbeat_timeout,
    )
