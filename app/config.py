"""Environment-driven configuration; no personal defaults in source."""
from __future__ import annotations

import os
from pathlib import Path


def env(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    return value if value is not None else default


def int_env(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(env("HUB_DATA_DIR", str(ROOT / "data")))
DATABASE = DATA_DIR / "hub.sqlite"
PRICING_FILE = DATA_DIR / "pricing.json"

SERVICE_PORT = int_env("HUB_PORT", 8791)
SERVICE_HOST = env("HUB_HOST", "127.0.0.1")

UPSTREAM_BASE_URL = env("HUB_UPSTREAM_BASE_URL", "").rstrip("/")
UPSTREAM_KEY = env("ALISA_AI_KEY", "")

BOT_TOKEN = env("HUB_BOT_TOKEN", "")
BOT_API_BASE = env("HUB_BOT_API_BASE", "https://api.telegram.org").rstrip("/")
OWNER_TELEGRAM_ID = int_env("HUB_OWNER_ID", 0)

# Billing day boundary: like most AI resellers, 03:00 UTC.
RESET_HOUR_UTC = int_env("HUB_RESET_HOUR_UTC", 3)

DEFAULT_DAILY_REQUESTS = int_env("HUB_DEFAULT_DAILY_REQUESTS", 300)
DEFAULT_DAILY_USD = float(env("HUB_DEFAULT_DAILY_USD", "2.0") or 0)

def upstream_public() -> str:
    """The HTTPS base under which /v1 is reachable from the internet."""
    return (os.getenv("HUB_PUBLIC_URL") or "").rstrip("/")
MAX_OUTPUT_TOKENS = int_env("HUB_MAX_OUTPUT_TOKENS", 4096)
MAX_CONTEXT_MESSAGES = int_env("HUB_MAX_CONTEXT_MESSAGES", 40)

UPSTREAM_TIMEOUT_SECONDS = float(env("HUB_UPSTREAM_TIMEOUT", "120"))
