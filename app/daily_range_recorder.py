"""Samples the live NIFTY tick throughout the day and records today's
observed high/low at EOD, so tomorrow's prev_day_range() fallback has real
self-recorded data even if the Upstox historical endpoint is unavailable.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date

from app import market_data
from app.timeutil import ist_now

logger = logging.getLogger("daily_range_recorder")

SAMPLE_INTERVAL_S = 15
RECORD_HOUR, RECORD_MIN = 15, 31  # just after the strategy's 15:20 EOD exit


async def run_forever() -> None:
    today = date.today().isoformat()
    high: float | None = None
    low: float | None = None
    recorded_today = False

    while True:
        try:
            now_date = date.today().isoformat()
            if now_date != today:
                today, high, low, recorded_today = now_date, None, None, False

            tick = market_data.nifty_spot()
            if tick and not tick["stale"]:
                ltp = tick["ltp"]
                high = ltp if high is None else max(high, ltp)
                low = ltp if low is None else min(low, ltp)

            now = ist_now()
            if not recorded_today and (now.hour, now.minute) >= (RECORD_HOUR, RECORD_MIN):
                if high is not None and low is not None:
                    market_data.record_today_range_from_ticks(high, low)
                    logger.info("recorded today's NIFTY range: high=%.2f low=%.2f", high, low)
                recorded_today = True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("daily range recorder error: %s", e)

        await asyncio.sleep(SAMPLE_INTERVAL_S)
