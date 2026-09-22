"""The single source of truth for 'what's the last known price of X'.

Priority #1 requirement: never silently serve a stale price as if it were
fresh. Every read returns an explicit age + stale flag alongside the value,
computed at read time (not at write time) so staleness is always accurate.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

from app.config import settings


@dataclass
class Tick:
    ltp: float
    ts: float  # time.time() when this price was observed
    source: str  # "ws" | "poll"
    # Top of book, when the feed gives it (WS "full" mode does; REST poll doesn't).
    # Exit-basis MTM and limit pricing need these — an OTM option's spread is the
    # real cost of the trade, so marking at LTP flatters every P&L number.
    bid: float | None = None
    ask: float | None = None


@dataclass
class ErrorLogEntry:
    ts: float
    where: str
    reason: str


class PriceCache:
    """Thread-safe. Written by the WS feed task and the poll-fallback task,
    read by strategy engine + API handlers. One instance for the process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ticks: dict[str, Tick] = {}
        self._errors: deque[ErrorLogEntry] = deque(maxlen=200)
        self._subscribed_keys: set[str] = set()

    # -- writes --------------------------------------------------------
    def update(
        self,
        key: str,
        ltp: float,
        source: str,
        ts: float | None = None,
        bid: float | None = None,
        ask: float | None = None,
    ) -> None:
        if ltp is None:
            return
        with self._lock:
            prev = self._ticks.get(key)
            # A REST poll has no depth; don't let it wipe the last known book.
            if bid is None and ask is None and prev is not None:
                bid, ask = prev.bid, prev.ask
            self._ticks[key] = Tick(
                ltp=ltp,
                ts=ts if ts is not None else time.time(),
                source=source,
                bid=bid,
                ask=ask,
            )

    def log_error(self, where: str, reason: str) -> None:
        with self._lock:
            self._errors.appendleft(ErrorLogEntry(ts=time.time(), where=where, reason=reason))

    def set_subscribed(self, keys: Iterable[str]) -> None:
        with self._lock:
            self._subscribed_keys = set(keys)

    def add_subscribed(self, keys: Iterable[str]) -> set[str]:
        """Returns the keys that are newly added (weren't already subscribed)."""
        with self._lock:
            new = set(keys) - self._subscribed_keys
            self._subscribed_keys |= new
            return new

    # -- reads -----------------------------------------------------------
    def get(self, key: str) -> dict | None:
        with self._lock:
            tick = self._ticks.get(key)
        if tick is None:
            return None
        age = time.time() - tick.ts
        return {
            "key": key,
            "ltp": tick.ltp,
            "ts": tick.ts,
            "age_s": round(age, 1),
            "stale": age > settings.tick_stale_after_s,
            "source": tick.source,
            "bid": tick.bid,
            "ask": tick.ask,
        }

    def exit_price(self, key: str, qty: int) -> float | None:
        """What this position would realistically fill at right now: a long
        exits by hitting the bid, a short by lifting the ask. Falls back to
        LTP when the book is unknown."""
        with self._lock:
            tick = self._ticks.get(key)
        if tick is None:
            return None
        if qty >= 0:
            return tick.bid if tick.bid else tick.ltp
        return tick.ask if tick.ask else tick.ltp

    def get_many(self, keys: Iterable[str]) -> dict[str, dict | None]:
        return {k: self.get(k) for k in keys}

    def age(self, key: str) -> float | None:
        with self._lock:
            tick = self._ticks.get(key)
        if tick is None:
            return None
        return time.time() - tick.ts

    def subscribed_keys(self) -> set[str]:
        with self._lock:
            return set(self._subscribed_keys)

    def stale_or_missing(self, keys: Iterable[str], threshold_s: float) -> list[str]:
        out = []
        now = time.time()
        with self._lock:
            for k in keys:
                tick = self._ticks.get(k)
                if tick is None or (now - tick.ts) > threshold_s:
                    out.append(k)
        return out

    def recent_errors(self, limit: int = 20) -> list[dict]:
        with self._lock:
            errs = list(self._errors)[:limit]
        return [{"ts": e.ts, "where": e.where, "reason": e.reason} for e in errs]


cache = PriceCache()
