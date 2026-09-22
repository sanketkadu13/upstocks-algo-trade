from __future__ import annotations

import time

from fastapi import APIRouter, Request

from app.price_cache import cache

router = APIRouter()


@router.get("/api/health")
async def health(request: Request):
    engine = request.app.state.engine
    feed = request.app.state.feed
    broker = request.app.state.broker

    subscribed = cache.subscribed_keys()
    tick_ages = {}
    for key in subscribed:
        info = cache.get(key)
        tick_ages[key] = info["age_s"] if info else None

    watchdog = getattr(request.app.state, "auth_watchdog", None)

    return {
        "broker_authenticated": broker.is_authenticated(),
        "auto_login": watchdog.status() if watchdog else None,
        "feed": feed.status(),
        "subscribed_key_count": len(subscribed),
        "stale_key_count": sum(1 for a in tick_ages.values() if a is None or a > 10),
        "recent_errors": cache.recent_errors(20),
        "server_time": time.time(),
    }


@router.post("/api/refresh")
async def force_refresh(request: Request):
    broker = request.app.state.broker
    keys = list(cache.subscribed_keys())
    if not keys:
        return {"refreshed": 0}
    try:
        prices = broker.ltp(keys)
    except Exception as e:
        cache.log_error("api.refresh", str(e))
        return {"refreshed": 0, "error": str(e)}
    for k, p in prices.items():
        if p is not None:
            cache.update(k, p, source="poll")
    return {"refreshed": len(prices)}
