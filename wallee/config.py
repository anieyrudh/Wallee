"""Reads .env and provides typed configuration to all components."""

import os
from pathlib import Path
from dataclasses import dataclass, field


DEFAULT_OPENROUTER_MODEL = "google/gemini-3.1-pro-preview"
DEFAULT_MIN_CHECK_INTERVAL_S = 10
DEFAULT_MAX_CHECK_INTERVAL_S = 30
DEFAULT_MAX_CHECK_INTERVAL_IDLE_S = 120
DEFAULT_CHECK_INTERVAL_S = 15
DEFAULT_HUMAN_INTENT_TTL_S = 600
DEFAULT_HUMAN_URGENT_TTL_S = 600
DEFAULT_HUMAN_IMAGE_TTL_S = 600
DEFAULT_HUMAN_ESTOP_TTL_S = 600
DEFAULT_AGENT_LAST_DECISION_TTL_S = 600
DEFAULT_ENGINE_APPROVAL_TIMEOUT_S = 300.0


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader. No external dependency needed."""
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Remove surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            else:
                # Strip inline comments (only for unquoted values)
                if " #" in value:
                    value = value[:value.index(" #")].strip()
            # Don't overwrite existing env vars
            if key not in os.environ:
                os.environ[key] = value


@dataclass(frozen=True)
class Config:
    # LLM
    openrouter_api_key: str = ""
    openrouter_model: str = DEFAULT_OPENROUTER_MODEL

    # Redis
    redis_url: str = "redis://localhost:6379"

    # Prusa (Phase 3)
    prusalink_host: str = ""
    prusalink_api_key: str = ""

    # Telegram (Phase 4)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_allowed_user_ids: list = field(default_factory=list)

    # Paths
    data_dir: Path = field(default_factory=lambda: Path("/var/lib/wallee"))
    log_dir: Path = field(default_factory=lambda: Path("/var/log/wallee"))

    # Agent behavior
    agent_poll_interval_s: float = 5.0
    agent_heartbeat_interval_s: float = 1.0
    agent_heartbeat_ttl_s: int = 3
    agent_min_check_interval_s: int = DEFAULT_MIN_CHECK_INTERVAL_S
    agent_max_check_interval_s: int = DEFAULT_MAX_CHECK_INTERVAL_S
    agent_max_check_interval_idle_s: int = DEFAULT_MAX_CHECK_INTERVAL_IDLE_S
    agent_default_check_interval_s: int = DEFAULT_CHECK_INTERVAL_S
    agent_last_decision_ttl_s: int = DEFAULT_AGENT_LAST_DECISION_TTL_S
    engine_poll_interval_s: float = 0.5
    engine_approval_timeout_s: float = DEFAULT_ENGINE_APPROVAL_TIMEOUT_S
    human_intent_ttl_s: int = DEFAULT_HUMAN_INTENT_TTL_S
    human_urgent_ttl_s: int = DEFAULT_HUMAN_URGENT_TTL_S
    human_image_ttl_s: int = DEFAULT_HUMAN_IMAGE_TTL_S
    human_estop_ttl_s: int = DEFAULT_HUMAN_ESTOP_TTL_S
    default_max_proposal_age_ms: int = 30000

    # Device packs to load (comma-separated in .env)
    device_packs: list = field(default_factory=lambda: ["host_pi"])

    # Dashboard (8081 to avoid conflict with camera server on 8080)
    dashboard_port: int = 8081


def load_config(env_path: Path | None = None) -> Config:
    """Load config from .env file and environment variables."""
    if env_path is None:
        # Look for .env in project root (parent of wallee/ package)
        env_path = Path(__file__).parent.parent / ".env"
    _load_dotenv(env_path)

    return Config(
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        openrouter_model=os.environ.get("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL),
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379"),
        prusalink_host=os.environ.get("PRUSALINK_HOST", ""),
        prusalink_api_key=os.environ.get("PRUSALINK_API_KEY", ""),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        telegram_allowed_user_ids=[
            user_id.strip()
            for user_id in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
            if user_id.strip()
        ],
        data_dir=Path(os.environ.get("WALLEE_DATA_DIR", "/var/lib/wallee")),
        log_dir=Path(os.environ.get("WALLEE_LOG_DIR", "/var/log/wallee")),
        agent_poll_interval_s=float(os.environ.get("AGENT_POLL_INTERVAL_S", "5")),
        agent_heartbeat_interval_s=float(os.environ.get("AGENT_HEARTBEAT_INTERVAL_S", "1")),
        agent_heartbeat_ttl_s=int(os.environ.get("AGENT_HEARTBEAT_TTL_S", "3")),
        agent_min_check_interval_s=int(os.environ.get("AGENT_MIN_CHECK_INTERVAL_S", str(DEFAULT_MIN_CHECK_INTERVAL_S))),
        agent_max_check_interval_s=int(os.environ.get("AGENT_MAX_CHECK_INTERVAL_S", str(DEFAULT_MAX_CHECK_INTERVAL_S))),
        agent_max_check_interval_idle_s=int(os.environ.get("AGENT_MAX_CHECK_INTERVAL_IDLE_S", str(DEFAULT_MAX_CHECK_INTERVAL_IDLE_S))),
        agent_default_check_interval_s=int(os.environ.get("AGENT_DEFAULT_CHECK_INTERVAL_S", str(DEFAULT_CHECK_INTERVAL_S))),
        agent_last_decision_ttl_s=int(os.environ.get("AGENT_LAST_DECISION_TTL_S", str(DEFAULT_AGENT_LAST_DECISION_TTL_S))),
        engine_poll_interval_s=float(os.environ.get("ENGINE_POLL_INTERVAL_S", "0.5")),
        engine_approval_timeout_s=float(os.environ.get("ENGINE_APPROVAL_TIMEOUT_S", str(DEFAULT_ENGINE_APPROVAL_TIMEOUT_S))),
        human_intent_ttl_s=int(os.environ.get("HUMAN_INTENT_TTL_S", str(DEFAULT_HUMAN_INTENT_TTL_S))),
        human_urgent_ttl_s=int(os.environ.get("HUMAN_URGENT_TTL_S", str(DEFAULT_HUMAN_URGENT_TTL_S))),
        human_image_ttl_s=int(os.environ.get("HUMAN_IMAGE_TTL_S", str(DEFAULT_HUMAN_IMAGE_TTL_S))),
        human_estop_ttl_s=int(os.environ.get("HUMAN_ESTOP_TTL_S", str(DEFAULT_HUMAN_ESTOP_TTL_S))),
        default_max_proposal_age_ms=int(os.environ.get("DEFAULT_MAX_PROPOSAL_AGE_MS", "30000")),
        device_packs=[p.strip() for p in os.environ.get("DEVICE_PACKS", "host_pi").split(",") if p.strip()],
        dashboard_port=int(os.environ.get("DASHBOARD_PORT", "8081")),
    )
