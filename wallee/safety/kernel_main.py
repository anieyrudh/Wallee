"""Safety kernel — runs as an independent OS process.

Monitors agent/engine heartbeats and printer safety signals via Redis.
Can ESTOP the printer even if the agent process crashes.

Usage:
    python -m wallee.safety.kernel_main --redis-url redis://localhost:6379 \
        --printer-host 192.168.0.195 --printer-api-key KEY
"""

import argparse
import logging
import os
import time

import redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [safety] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("wallee.safety.kernel")


def _estop_printer(host: str, api_key: str = ""):
    """Emergency stop — send M25 pause directly to printer."""
    if not host:
        return
    import httpx
    base = host if host.startswith("http") else f"http://{host}"
    headers = {"X-Api-Key": api_key} if api_key else {}
    try:
        httpx.post(f"{base}/api/v1/gcode", json={"command": "M25"},
                   headers=headers, timeout=5)
        logger.critical("ESTOP: M25 sent to printer")
    except Exception as e:
        logger.error(f"ESTOP M25 failed: {e}")


def run_kernel(
    redis_url: str = "redis://localhost:6379",
    check_interval: float = 2.0,
    heartbeat_timeout: float = 30.0,
    printer_host: str = "",
    printer_api_key: str = "",
):
    """Main safety loop. Runs indefinitely."""
    r = redis.Redis.from_url(redis_url, decode_responses=True)
    r.ping()
    logger.info(f"Safety kernel process started (PID={os.getpid()})")

    agent_alerted = False
    engine_alerted = False
    oc_nozzle_alerted = False
    oc_input_alerted = False
    estop_alerted = False
    boot_time = time.monotonic()
    boot_grace_s = 15.0  # suppress heartbeat alerts while components start

    while True:
        try:
            in_grace = (time.monotonic() - boot_time) < boot_grace_s

            # Check ESTOP flag
            estop_raw = r.get("safety.estop")
            if estop_raw and estop_raw != "false":
                if not estop_alerted:
                    logger.critical("ESTOP flag active on whiteboard")
                    _estop_printer(printer_host, printer_api_key)
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

            # Check overcurrent flags
            oc_nozz_raw = r.get("printer.oc_nozzle")
            if oc_nozz_raw and oc_nozz_raw != "0":
                if not oc_nozzle_alerted:
                    logger.critical(f"OVERCURRENT: nozzle heater (oc_nozzle={oc_nozz_raw})")
                    _estop_printer(printer_host, printer_api_key)
                    oc_nozzle_alerted = True
            else:
                oc_nozzle_alerted = False

            oc_inp_raw = r.get("printer.oc_input")
            if oc_inp_raw and oc_inp_raw != "0":
                if not oc_input_alerted:
                    logger.critical(f"OVERCURRENT: input power (oc_input={oc_inp_raw})")
                    _estop_printer(printer_host, printer_api_key)
                    oc_input_alerted = True
            else:
                oc_input_alerted = False

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
    parser.add_argument("--printer-host", default=os.environ.get("PRUSALINK_HOST", ""))
    parser.add_argument("--printer-api-key", default=os.environ.get("PRUSALINK_API_KEY", ""))
    parser.add_argument("--check-interval", type=float, default=2.0)
    parser.add_argument("--heartbeat-timeout", type=float, default=30.0)
    args = parser.parse_args()

    run_kernel(
        redis_url=args.redis_url,
        printer_host=args.printer_host,
        printer_api_key=args.printer_api_key,
        check_interval=args.check_interval,
        heartbeat_timeout=args.heartbeat_timeout,
    )
