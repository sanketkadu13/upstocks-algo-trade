"""Environment/settings loading. Single source of truth for paths and secrets."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
STATIC_DIR = ROOT_DIR / "static"

load_dotenv(ROOT_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    broker: str = os.getenv("BROKER", "upstox")

    upstox_api_key: str = os.getenv("UPSTOX_API_KEY", "")
    upstox_api_secret: str = os.getenv("UPSTOX_API_SECRET", "")
    upstox_redirect_uri: str = os.getenv("UPSTOX_REDIRECT_URI", "http://127.0.0.1:8000/api/auth/callback")
    upstox_access_token_seed: str = os.getenv("UPSTOX_ACCESS_TOKEN", "")

    # Automated daily login (drives the official Upstox OAuth page headlessly).
    # Leave blank to disable and fall back to clicking through the login manually.
    upstox_mobile: str = os.getenv("UPSTOX_MOBILE", "")
    upstox_pin: str = os.getenv("UPSTOX_PIN", "")
    upstox_totp_secret: str = os.getenv("UPSTOX_TOTP_SECRET", "")
    auto_login_enabled: bool = _bool("AUTO_LOGIN_ENABLED", True)
    auto_login_retry_s: float = 60.0  # min gap between login attempts
    auto_login_max_backoff_s: float = 900.0

    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8000"))

    upstox_base_url: str = "https://api.upstox.com"
    # v3 feed: GET this with Authorization: Bearer <token> -> {data:{authorizedRedirectUri}}
    upstox_ws_authorize_url: str = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
    # v2 REST (stable, matches Upstox's long-documented surface used for polling/orders)
    upstox_ltp_url: str = "https://api.upstox.com/v2/market-quote/ltp"
    upstox_login_dialog_url: str = "https://api.upstox.com/v2/login/authorization/dialog"
    upstox_token_url: str = "https://api.upstox.com/v2/login/authorization/token"
    upstox_order_place_url: str = "https://api.upstox.com/v2/order/place"
    upstox_order_modify_url: str = "https://api.upstox.com/v2/order/modify"
    upstox_order_cancel_url: str = "https://api.upstox.com/v2/order/cancel"
    upstox_order_details_url: str = "https://api.upstox.com/v2/order/details"
    upstox_positions_url: str = "https://api.upstox.com/v2/portfolio/short-term-positions"
    upstox_historical_url: str = "https://api.upstox.com/v2/historical-candle"

    index_key: str = "NSE_INDEX|Nifty 50"
    vix_key: str = "NSE_INDEX|India VIX"

    # Price cache reliability knobs (spec: poll fallback after 5s, UI-stale after 10s)
    tick_stale_after_s: float = 10.0
    poll_fallback_after_s: float = 5.0
    poll_interval_s: float = 2.0
    ws_backoff_start_s: float = 1.0
    ws_backoff_max_s: float = 30.0


settings = Settings()

DATA_DIR.mkdir(exist_ok=True)

TOKEN_FILE = DATA_DIR / "upstox_token.json"
INSTRUMENTS_CACHE = DATA_DIR / "instruments_NSE.json"
INSTRUMENTS_RAW_GZ = DATA_DIR / "NSE.json.gz"
DAILY_RANGE_FILE = DATA_DIR / "nifty_daily_range.json"
TAP_CONFIG_FILE = DATA_DIR / "tap_config.json"
TAP_STATE_FILE = DATA_DIR / "tap_state.json"
TAP_LEDGER_FILE = DATA_DIR / "tap_ledger.jsonl"
AUTO_LOGIN_DEBUG_DIR = DATA_DIR / "auto_login_debug"
