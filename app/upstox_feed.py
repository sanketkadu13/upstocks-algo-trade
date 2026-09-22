"""Upstox v3 WebSocket market-data feed client.

Priority #1 module. Runs for the life of the process as an asyncio task:
authorize -> connect -> subscribe -> decode ticks into the shared PriceCache.
On any disconnect/error it reconnects with exponential backoff and
resubscribes the full current key set, so a dropped socket never means a
frozen dashboard (the poll-fallback loop in price_poller.py covers the gap
in the meantime).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

import httpx
import websockets
from websockets.asyncio.client import connect as ws_connect

from app.config import settings
from app.price_cache import cache

logger = logging.getLogger("upstox_feed")


def _decode_feed_response(raw: bytes):
    from app.proto import MarketDataFeed_pb2 as pb

    fr = pb.FeedResponse()
    fr.ParseFromString(raw)
    return fr


class UpstoxFeed:
    def __init__(self, get_access_token) -> None:
        """get_access_token: zero-arg callable returning the current bearer token."""
        self._get_access_token = get_access_token
        self._keys: set[str] = set()
        self._ws = None
        self._connected = False
        self._reconnect_count = 0
        self._last_connected_at: float | None = None
        self._stop = False
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connected

    def status(self) -> dict:
        return {
            "connected": self._connected,
            "reconnect_count": self._reconnect_count,
            "last_connected_at": self._last_connected_at,
            "subscribed_keys": sorted(self._keys),
        }

    async def add_keys(self, keys: set[str]) -> None:
        new_keys = keys - self._keys
        if not new_keys:
            return
        self._keys |= new_keys
        cache.set_subscribed(self._keys)
        if self._connected and self._ws is not None:
            await self._send_subscribe(new_keys)

    async def _authorize(self) -> str:
        token = self._get_access_token()
        if not token:
            raise RuntimeError("no access token available")
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                settings.upstox_ws_authorize_url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            resp.raise_for_status()
            body = resp.json()
        uri = body["data"]["authorizedRedirectUri"]
        return uri

    async def _send_subscribe(self, keys: set[str]) -> None:
        if not keys:
            return
        msg = {
            "guid": str(uuid.uuid4()),
            "method": "sub",
            "data": {"mode": "full", "instrumentKeys": sorted(keys)},
        }
        await self._ws.send(json.dumps(msg).encode("utf-8"))

    async def run_forever(self) -> None:
        backoff = settings.ws_backoff_start_s
        while not self._stop:
            try:
                await self._run_once()
                backoff = settings.ws_backoff_start_s  # clean exit resets backoff
            except asyncio.CancelledError:
                raise
            except Exception as e:
                cache.log_error("upstox_feed", f"{type(e).__name__}: {e}")
                logger.warning("feed connection error: %s", e)
            finally:
                self._connected = False
            if self._stop:
                break
            logger.info("feed reconnecting in %.1fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, settings.ws_backoff_max_s)
            self._reconnect_count += 1

    async def _run_once(self) -> None:
        wss_uri = await self._authorize()
        async with ws_connect(wss_uri, max_size=2**22) as ws:
            self._ws = ws
            self._connected = True
            self._last_connected_at = time.time()
            logger.info("feed connected")
            if self._keys:
                await self._send_subscribe(self._keys)

            async for raw in ws:
                if isinstance(raw, str):
                    # server occasionally sends text status frames; ignore
                    continue
                try:
                    fr = _decode_feed_response(raw)
                except Exception as e:
                    cache.log_error("upstox_feed.decode", str(e))
                    continue
                now = time.time()
                for key, feed in fr.feeds.items():
                    ltp = self._extract_ltp(feed)
                    if ltp is not None:
                        bid, ask = self._extract_top_of_book(feed)
                        cache.update(key, ltp, source="ws", ts=now, bid=bid, ask=ask)

    @staticmethod
    def _extract_ltp(feed) -> float | None:
        which = feed.WhichOneof("FeedUnion")
        if which == "ltpc":
            return feed.ltpc.ltp
        if which == "fullFeed":
            ff = feed.fullFeed
            variant = ff.WhichOneof("FullFeedUnion")
            if variant == "marketFF":
                return ff.marketFF.ltpc.ltp
            if variant == "indexFF":
                return ff.indexFF.ltpc.ltp
        if which == "firstLevelWithGreeks":
            return feed.firstLevelWithGreeks.ltpc.ltp
        return None

    @staticmethod
    def _extract_top_of_book(feed) -> tuple[float | None, float | None]:
        """Best bid / best ask from the depth ladder, when the feed carries it.
        Indices are the exchange's level-1 quote; zeros mean 'no book'."""
        which = feed.WhichOneof("FeedUnion")
        quote = None
        if which == "fullFeed":
            ff = feed.fullFeed
            if ff.WhichOneof("FullFeedUnion") == "marketFF":
                levels = ff.marketFF.marketLevel.bidAskQuote
                quote = levels[0] if levels else None
        elif which == "firstLevelWithGreeks":
            quote = feed.firstLevelWithGreeks.firstDepth

        if quote is None:
            return None, None
        return (quote.bidP or None), (quote.askP or None)

    async def stop(self) -> None:
        self._stop = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
