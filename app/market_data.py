"""Thin data-layer helpers strategies use instead of touching the broker/cache
directly. All reads go through the shared PriceCache — never a second cache.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from app.broker import BrokerBase
from app.config import DAILY_RANGE_FILE, settings
from app.price_cache import cache

logger = logging.getLogger("market_data")

# Beyond this, a self-recorded range is stale enough that trading off it is
# worse than refusing to trade.
MAX_FALLBACK_AGE_DAYS = 5


def nifty_spot() -> dict | None:
    return cache.get(settings.index_key)


def india_vix() -> dict | None:
    return cache.get(settings.vix_key)


def _record_daily_range(day: str, high: float, low: float) -> None:
    data = {}
    if DAILY_RANGE_FILE.exists():
        try:
            data = json.loads(DAILY_RANGE_FILE.read_text())
        except Exception:
            data = {}
    data[day] = {"high": high, "low": low}
    DAILY_RANGE_FILE.write_text(json.dumps(data))


def record_today_range_from_ticks(high: float, low: float) -> None:
    """Called by an EOD snapshot job so tomorrow's fallback has real data."""
    _record_daily_range(date.today().isoformat(), high, low)


def prev_day_range(broker: BrokerBase) -> dict:
    """Previous trading day's NIFTY high/low. Tries Upstox historical daily
    candle first; falls back to our own self-recorded store."""
    today = date.today()
    for back in range(1, 8):
        d = today - timedelta(days=back)
        try:
            candles = broker.historical_candles(
                settings.index_key, "day", d.isoformat(), d.isoformat()
            )
        except Exception as e:
            cache.log_error("market_data.prev_day_range", f"historical fetch failed: {e}")
            candles = []
        if candles:
            # Upstox candle row: [ts, open, high, low, close, volume, oi]
            row = candles[0]
            return {"date": d.isoformat(), "high": row[2], "low": row[3], "source": "upstox_historical"}

    if DAILY_RANGE_FILE.exists():
        try:
            data = json.loads(DAILY_RANGE_FILE.read_text())
            if data:
                last_day = sorted(data.keys())[-1]
                row = data[last_day]
                age_days = (today - date.fromisoformat(last_day)).days
                # A range from weeks ago is worse than none: strikes derived
                # from it can sit far from the money, and the number looks
                # perfectly plausible in the UI. Refuse rather than mislead.
                if age_days > MAX_FALLBACK_AGE_DAYS:
                    cache.log_error(
                        "market_data.prev_day_range",
                        f"self-recorded range is {age_days} days old ({last_day}) — too stale to use",
                    )
                else:
                    return {
                        "date": last_day,
                        "high": row["high"],
                        "low": row["low"],
                        "source": "self_recorded_fallback",
                        "age_days": age_days,
                    }
        except Exception as e:
            cache.log_error("market_data.prev_day_range", f"fallback file read failed: {e}")

    logger.error("prev_day_range: no historical data and no fallback file available")
    return {"date": None, "high": None, "low": None, "source": "unavailable"}
