"""Fallback poller: if any subscribed key hasn't ticked over WS in >5s, poll
GET /market-quote/ltp for just those keys. This is what keeps the dashboard
from ever going fully stale even if the WS feed is degraded (spec priority #1).
"""
from __future__ import annotations

import asyncio
import logging

from app.broker import BrokerBase
from app.config import settings
from app.price_cache import cache

logger = logging.getLogger("price_poller")


async def run_forever(broker: BrokerBase) -> None:
    while True:
        try:
            await _poll_once(broker)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            cache.log_error("price_poller", f"{type(e).__name__}: {e}")
            logger.warning("poll loop error: %s", e)
        await asyncio.sleep(settings.poll_interval_s)


async def _poll_once(broker: BrokerBase) -> None:
    keys = cache.subscribed_keys()
    if not keys:
        return
    stale_keys = cache.stale_or_missing(keys, settings.poll_fallback_after_s)
    if not stale_keys:
        return
    try:
        prices = await asyncio.to_thread(broker.ltp, stale_keys)
    except Exception as e:
        cache.log_error("price_poller.ltp", f"failed for {len(stale_keys)} keys: {e}")
        return
    for key, price in prices.items():
        if price is not None:
            cache.update(key, price, source="poll")
    missing = set(stale_keys) - set(prices.keys())
    if missing:
        cache.log_error("price_poller.ltp", f"no price returned for {len(missing)} keys: {sorted(missing)[:5]}...")
