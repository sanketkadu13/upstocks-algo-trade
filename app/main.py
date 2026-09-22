from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import daily_range_recorder, price_poller, schedules
from app.api import (
    routes_auth,
    routes_autologin,
    routes_ipo,
    routes_misc,
    routes_ops,
    routes_strategies,
)
from app.ipo import monitor_task as ipo_monitor_task
from app.auth_watchdog import AuthWatchdog
from app.broker import get_broker
from app.config import STATIC_DIR, settings
from app.order_gateway import get_gateway
from app.price_cache import cache
from app.strategy_engine import StrategyEngine
from app.upstox_feed import UpstoxFeed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")


async def _engine_tick_loop(engine: StrategyEngine) -> None:
    while True:
        try:
            await engine.tick()
        except Exception as e:
            cache.log_error("engine_tick", f"{type(e).__name__}: {e}")
            logger.exception("engine tick failed")
        await asyncio.sleep(1.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    broker = get_broker()
    gateway = get_gateway(broker)
    feed = UpstoxFeed(get_access_token=broker.access_token)
    engine = StrategyEngine(broker, gateway, feed)
    watchdog = AuthWatchdog(broker)

    app.state.broker = broker
    app.state.gateway = gateway
    app.state.feed = feed
    app.state.engine = engine
    app.state.auth_watchdog = watchdog

    cache.set_subscribed({settings.index_key, settings.vix_key})

    tasks = [
        asyncio.create_task(feed.run_forever(), name="ws_feed"),
        asyncio.create_task(price_poller.run_forever(broker), name="price_poller"),
        asyncio.create_task(_engine_tick_loop(engine), name="engine_tick"),
        asyncio.create_task(daily_range_recorder.run_forever(), name="daily_range_recorder"),
        asyncio.create_task(watchdog.run_forever(), name="auth_watchdog"),
        asyncio.create_task(ipo_monitor_task.run_forever(feed), name="ipo_monitor"),
        asyncio.create_task(schedules.run_forever(broker, engine, gateway), name="schedules"),
    ]
    logger.info("background tasks started: %s", [t.get_name() for t in tasks])
    if gateway.killed:
        logger.warning("NOTE: kill switch is ARMED — no orders will be placed until released")

    yield

    await feed.stop()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="NIFTY Options Engine (Upstox)", lifespan=lifespan)

app.include_router(routes_strategies.router)
app.include_router(routes_auth.router)
app.include_router(routes_misc.router)
app.include_router(routes_ipo.router)
app.include_router(routes_ops.router)
app.include_router(routes_autologin.router)

@app.middleware("http")
async def no_cache_dashboard(request, call_next):
    """Never let a browser cache the dashboard.

    A trading UI showing a stale build is actively dangerous — you could be
    reading last week's layout while the engine has changed underneath it —
    and the assets are a few KB served from localhost, so caching buys
    nothing here.
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/") or path == "/":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "dashboard" / "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
