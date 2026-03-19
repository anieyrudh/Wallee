"""Boot sequence for Wallee — Phase 2: full harness.

Boot order (per spec):
  1. Safety kernel FIRST
  2. Redis (must be running)
  3. Ledger (SQLite, run migrations, reconcile)
  4. Device packs + sensor threads
  5. Engine (polls ledger, runs gates)
  6. Agent LAST
  7. CLI (background thread)
"""

import logging
import signal
import sys
import threading
from pathlib import Path

from wallee.config import load_config
from wallee.whiteboard.client import Whiteboard
from wallee.ledger.db import Ledger
from wallee.ledger.diary import Diary
from wallee.engine.dispatch import Engine
from wallee.engine.reconcile import reconcile
from wallee.safety.kernel import SafetyKernel
from wallee.tools.registry import ToolRegistry
from wallee.agent.llm_client import LLMClient
from wallee.agent.loop import AgentLoop
from wallee.agent.parser import configure_check_intervals
from wallee.tools.builtins.remember import configure_observations_dir
from wallee.tools.builtins.web_search import configure_web_search
from wallee.human.call_human import call_human
from wallee.human.cli import CLI
from wallee.ui.dashboard import DashboardServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Silence httpx request logging — it floods the CLI at 1Hz sensor polling
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("wallee.main")


def main():
    logger.info("Wallee starting — Phase 2 (full harness)")

    # 1. Load config
    cfg = load_config()
    if not cfg.openrouter_api_key:
        logger.error("OPENROUTER_API_KEY not set. Create a .env file from .env.example.")
        sys.exit(1)

    configure_check_intervals(
        cfg.agent_min_check_interval_s,
        cfg.agent_max_check_interval_s,
        cfg.agent_max_check_interval_idle_s,
        cfg.agent_default_check_interval_s,
    )
    configure_observations_dir(cfg.data_dir)
    configure_web_search(cfg.openrouter_api_key, cfg.openrouter_model)

    from wallee.device_packs.pi_cameras.vision_analysis import configure_vision
    configure_vision(cfg.openrouter_api_key, cfg.vision_model, cfg.redis_url)

    # Ensure data directory exists (fall back to local dir if system dir not writable)
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        from dataclasses import replace
        fallback = Path.home() / ".wallee" / "data"
        fallback.mkdir(parents=True, exist_ok=True)
        cfg = replace(cfg, data_dir=fallback)
        logger.warning(f"Cannot write to {cfg.data_dir}, using {fallback}")

    # 2. Connect to Redis (whiteboard)
    logger.info(f"Connecting to Redis: {cfg.redis_url}")
    wb = Whiteboard(redis_url=cfg.redis_url)
    try:
        wb.r.ping()
        logger.info("Redis connected")
    except Exception as e:
        logger.error(f"Cannot connect to Redis: {e}")
        sys.exit(1)

    # 3. Safety kernel starts FIRST
    def _call_human_fn(msg, severity="critical"):
        call_human(msg, severity, outbox_dir=cfg.data_dir / "outbox")

    safety = SafetyKernel(wb, call_human_fn=_call_human_fn)
    safety_thread = threading.Thread(target=safety.run, daemon=True, name="safety-kernel")
    safety_thread.start()
    logger.info("Safety kernel started (heartbeat monitor)")

    # 4. Ledger + reconcile
    ledger_path = cfg.data_dir / "ledger.db"
    ledger = Ledger(ledger_path)
    logger.info(f"Ledger initialized: {ledger_path}")

    # Run reconcile on boot (check for pre-crash in-flight actions)
    def _get_diary(device_group):
        return Diary(device_group, cfg.data_dir)

    reconcile(ledger, _get_diary, call_human_fn=_call_human_fn)
    logger.info("Reconcile complete (boot)")

    # 5. Load device packs + built-in tools
    registry = ToolRegistry()
    registry.load_builtins()
    for pack_name in cfg.device_packs:
        registry.load_pack(f"wallee.device_packs.{pack_name}")
        logger.info(f"Loaded device pack: {pack_name}")
    logger.info(f"Loaded {len(registry.list_sensors())} sensors, {len(registry.list_actuators())} actuators")

    # 6. Engine
    engine = Engine(
        whiteboard=wb,
        ledger=ledger,
        tools=registry,
        data_dir=str(cfg.data_dir),
        poll_interval=cfg.engine_poll_interval_s,
        approval_timeout=cfg.engine_approval_timeout_s,
    )
    engine_thread = threading.Thread(target=engine.run, daemon=True, name="engine")
    engine_thread.start()
    logger.info("Engine started (polling ledger)")

    # 7. Agent (starts LAST)
    llm = LLMClient(api_key=cfg.openrouter_api_key, model=cfg.openrouter_model,
                     temperature=cfg.llm_temperature)
    knowledge_dir = Path(__file__).parent / "knowledge"

    agent = AgentLoop(
        whiteboard=wb,
        llm=llm,
        tools=registry,
        knowledge_dir=knowledge_dir,
        poll_interval=cfg.agent_poll_interval_s,
        heartbeat_interval=cfg.agent_heartbeat_interval_s,
        heartbeat_ttl=cfg.agent_heartbeat_ttl_s,
        last_decision_ttl=cfg.agent_last_decision_ttl_s,
        ledger=ledger,
        data_dir=cfg.data_dir,
    )

    # Store agent ref for wake wiring — before starting sensors
    _agent_loop = agent

    # Start sensor background threads (after agent, so wake_fn is available)
    registry.start_sensors(wb, wake_fn=_agent_loop.wake)
    logger.info("Sensor publishers started")

    agent_thread = threading.Thread(target=agent.run, daemon=True, name="agent-loop")
    agent_thread.start()
    logger.info("Agent loop started")

    # Graceful shutdown
    def shutdown(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        agent.stop()
        engine.stop()
        safety.stop()
        registry.stop_sensors()
        ledger.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # 8. Dashboard (read-only web UI)
    dashboard = DashboardServer(wb, host="0.0.0.0", port=cfg.dashboard_port)
    dashboard.start()
    logger.info(f"Dashboard at http://0.0.0.0:{cfg.dashboard_port}")

    # 9. Telegram bot (if configured)
    telegram_bot = None
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        try:
            from wallee.human.telegram import TelegramBot
            telegram_bot = TelegramBot(
                token=cfg.telegram_bot_token,
                chat_id=cfg.telegram_chat_id,
                allowed_user_ids=cfg.telegram_allowed_user_ids,
                intent_ttl=cfg.human_intent_ttl_s,
                urgent_ttl=cfg.human_urgent_ttl_s,
                image_ttl=cfg.human_image_ttl_s,
                estop_ttl=cfg.human_estop_ttl_s,
                whiteboard=wb,
                ledger=ledger,
                safety_kernel=safety,
                wake_agent_fn=_agent_loop.wake,
            )
            telegram_bot.start()
            engine.approval_notifier = telegram_bot.send_approval_request
            # Wire Telegram into agent's call_human and safety kernel's call_human
            def _telegram_call_human(msg, severity="info"):
                try:
                    telegram_bot.send(msg, severity)
                except Exception as e:
                    logger.error(f"Telegram send failed: {e}")
                # Also write to outbox as backup
                call_human(msg, severity, outbox_dir=cfg.data_dir / "outbox",
                           telegram_fn=None)  # don't recurse
            agent.call_human_fn = _telegram_call_human
            safety.call_human_fn = _telegram_call_human
            from wallee.tools.builtins.call_human_tool import set_call_human_fn
            set_call_human_fn(_telegram_call_human)
            logger.info("Telegram bot started and wired to agent + safety kernel + call_human tool")
        except Exception as e:
            logger.warning(f"Telegram bot failed to start: {e}")
    else:
        logger.info("Telegram not configured (no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID)")

    # 10. CLI or headless mode
    import sys
    if sys.stdin.isatty():
        logger.info("All systems running — starting CLI")
        cli = CLI(wb, ledger, intent_ttl=cfg.human_intent_ttl_s, urgent_ttl=cfg.human_urgent_ttl_s, estop_ttl=cfg.human_estop_ttl_s, wake_agent_fn=_agent_loop.wake)
        try:
            cli.run()
        except (EOFError, KeyboardInterrupt):
            pass
    else:
        logger.info("All systems running — headless mode (no TTY)")
        # Block main thread until signal
        stop_event = threading.Event()
        original_shutdown = shutdown
        def headless_shutdown(signum, frame):
            stop_event.set()
            original_shutdown(signum, frame)
        signal.signal(signal.SIGINT, headless_shutdown)
        signal.signal(signal.SIGTERM, headless_shutdown)
        stop_event.wait()

    # If CLI exits, shut down everything
    if telegram_bot:
        telegram_bot.stop()
    dashboard.stop()
    shutdown(0, None)


if __name__ == "__main__":
    main()
