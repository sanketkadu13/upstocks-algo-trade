"""Fast check for priority #1: does the live WS feed actually tick?

Connects the feed alone (no orders, no strategy), subscribes to the NIFTY
index, and prints each tick's age for ~30s. Run this first, before anything
else, whenever touching feed code or rotating credentials.

Usage: .venv/Scripts/python.exe scripts/feed_smoke_test.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.broker import get_broker
from app.config import settings
from app.price_cache import cache
from app.upstox_feed import UpstoxFeed


async def main() -> None:
    broker = get_broker()
    if not broker.is_authenticated():
        print(f"NOT AUTHENTICATED. Visit this URL to log in, then rerun with a fresh .env token:\n{broker.login_url()}")
        return

    feed = UpstoxFeed(get_access_token=broker.access_token)
    feed_task = asyncio.create_task(feed.run_forever())
    await feed.add_keys({settings.index_key})

    print(f"Subscribed to {settings.index_key}. Watching for 30s...\n")
    start = time.time()
    last_seen_ts = None
    while time.time() - start < 30:
        await asyncio.sleep(1)
        tick = cache.get(settings.index_key)
        status = feed.status()
        if tick:
            fresh = tick["ts"] != last_seen_ts
            last_seen_ts = tick["ts"]
            print(
                f"[{time.strftime('%H:%M:%S')}] ltp={tick['ltp']:.2f} age={tick['age_s']:.1f}s "
                f"source={tick['source']} {'(NEW TICK)' if fresh else ''} "
                f"feed_connected={status['connected']} reconnects={status['reconnect_count']}"
            )
        else:
            print(f"[{time.strftime('%H:%M:%S')}] no tick yet — feed_connected={status['connected']}")

    for err in cache.recent_errors(10):
        print("ERROR:", err)

    await feed.stop()
    feed_task.cancel()
    print("\nDone. If ltp kept updating with age staying low, the feed is healthy.")
    print("If market is closed, ticks may be sparse/absent — that's expected outside trading hours.")


if __name__ == "__main__":
    asyncio.run(main())
