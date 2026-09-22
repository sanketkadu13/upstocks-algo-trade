"""Background loop that watches the IPO watchlist for breakouts."""
from __future__ import annotations

import asyncio
import logging

from app import notify
from app.ipo import watchlist
from app.market_calendar import is_market_open
from app.price_cache import cache

logger = logging.getLogger("ipo.monitor")

POLL_S = 15.0
IDLE_POLL_S = 120.0


async def run_forever(feed) -> None:
    while True:
        delay = POLL_S
        try:
            keys = watchlist.subscribed_keys()
            if keys:
                new = keys - cache.subscribed_keys()
                if new:
                    await feed.add_keys(new)
                cache.set_subscribed(cache.subscribed_keys() | keys)

            if not is_market_open():
                delay = IDLE_POLL_S
            elif keys:
                result = await asyncio.to_thread(watchlist.check_once, notify.send)
                if result["fired"]:
                    logger.info("IPO breakouts fired: %s", result["fired"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            cache.log_error("ipo_monitor", f"{type(e).__name__}: {e}")
            logger.exception("IPO monitor error")
        await asyncio.sleep(delay)
