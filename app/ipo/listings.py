"""Listing-day analysis for recently listed stocks.

The strategy this supports: a newly listed stock that has *never* traded above
its listing-day high is "eligible"; the entry signal is the first break of
that high, with the listing-day low as the stop.

Where the listing data comes from is a deliberate departure from the
reference implementation, which scraped an undocumented Chittorgarh endpoint
(versioned magic URL, tilde-prefixed keys, rate-limited to partial results)
and then papered over the gaps with hand-maintained CSVs. Instead we derive
everything from Upstox's own daily candles: the first candle a symbol ever
has *is* its listing day, which gives listing date, open/high/low/close and
the entire post-listing path from one authenticated API we already depend on.
No scraping, nothing to re-maintain when a third party changes its HTML.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from app import instruments
from app.broker import BrokerBase

logger = logging.getLogger("ipo.listings")

# How far back a "recent listing" can be and still interest us.
LOOKBACK_YEARS = 3
TARGET_PCT_FROM_HIGH = 30.0  # target = listing high x 1.30


def _candle_date(row) -> date:
    ts = row[0]
    if isinstance(ts, str):
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    return datetime.fromtimestamp(ts / 1000).date()


def fetch_daily_candles(broker: BrokerBase, instrument_key: str, years: int = LOOKBACK_YEARS) -> list:
    """Oldest-first daily candles. Upstox returns newest-first."""
    to_date = date.today().isoformat()
    from_date = (date.today() - timedelta(days=365 * years)).isoformat()
    candles = broker.historical_candles(instrument_key, "day", to_date, from_date)
    return sorted(candles, key=_candle_date)


def analyze(broker: BrokerBase, symbol: str, candles: list | None = None) -> dict:
    """Full listing profile for one symbol."""
    inst = instruments.resolve_equity(symbol)
    if not inst:
        return {"symbol": symbol, "ok": False, "error": "not found in NSE instrument master"}

    try:
        if candles is None:
            candles = fetch_daily_candles(broker, inst["instrument_key"])
    except Exception as e:
        return {"symbol": symbol, "ok": False, "error": f"historical fetch failed: {e}"}

    if not candles:
        return {"symbol": symbol, "ok": False, "error": "no historical candles returned"}

    first = candles[0]
    listing_date = _candle_date(first)
    listing_open, listing_high, listing_low, listing_close = (
        float(first[1]),
        float(first[2]),
        float(first[3]),
        float(first[4]),
    )

    # Did it ever trade above the listing-day high afterwards?
    break_date = None
    break_price = None
    max_high_after = None
    for row in candles[1:]:
        high = float(row[2])
        max_high_after = high if max_high_after is None else max(max_high_after, high)
        if break_date is None and high > listing_high:
            break_date = _candle_date(row).isoformat()
            break_price = high

    last_close = float(candles[-1][4])
    days_since = (date.today() - listing_date).days

    # A long-listed stock's history simply starts wherever our lookback window
    # began, so its "first candle" is not a listing at all. Callers must be
    # able to tell a genuine recent listing from a truncated history.
    recent = is_probably_recent_listing(candles)

    return {
        "ok": True,
        "is_recent_listing": recent,
        "listing_date_is_window_edge": not recent,
        "symbol": inst["tradingsymbol"],
        "instrument_key": inst["instrument_key"],
        "name": inst.get("name"),
        "isin": inst.get("isin"),
        "series": inst.get("instrument_type"),
        "listing_date": listing_date.isoformat(),
        "days_since_listing": days_since,
        "sessions_after_listing": len(candles) - 1,
        "listing_open": listing_open,
        "listing_high": listing_high,
        "listing_low": listing_low,
        "listing_close": listing_close,
        "max_high_after_listing": max_high_after,
        "already_broke": break_date is not None,
        "break_date": break_date,
        "break_price": break_price,
        "eligible": break_date is None,
        "last_close": last_close,
        "target_price": round(listing_high * (1 + TARGET_PCT_FROM_HIGH / 100.0), 2),
        "stop_price": listing_low,
        "range_pct": round((listing_high - listing_low) / listing_low * 100, 2) if listing_low else None,
    }


def is_probably_recent_listing(candles: list, max_days: int = 365 * LOOKBACK_YEARS) -> bool:
    """Cheap filter: a long-listed stock's history starts at the window edge,
    a recent listing's starts later."""
    if not candles:
        return False
    first = _candle_date(candles[0])
    window_start = date.today() - timedelta(days=max_days)
    # If the first candle is comfortably after the requested window start, the
    # stock simply didn't exist before then.
    return (first - window_start).days > 5


def scan_recent_listings(
    broker: BrokerBase,
    symbols: list[str] | None = None,
    max_days_since_listing: int = 400,
    progress=None,
) -> list[dict]:
    """Walk the equity master looking for recent listings.

    This is deliberately rate-limit friendly: one historical call per symbol,
    and callers are expected to run it as a background job rather than inside
    a request.
    """
    if symbols is None:
        symbols = [row["tradingsymbol"] for row in instruments.load().get("equities", [])]

    found: list[dict] = []
    total = len(symbols)
    for i, sym in enumerate(symbols):
        if progress:
            progress(i, total, sym, len(found))
        inst = instruments.resolve_equity(sym)
        if not inst:
            continue
        try:
            candles = fetch_daily_candles(broker, inst["instrument_key"])
        except Exception as e:
            logger.debug("%s: historical failed (%s)", sym, e)
            continue
        if not candles or not is_probably_recent_listing(candles):
            continue
        profile = analyze(broker, sym, candles=candles)
        if not profile.get("ok"):
            continue
        if profile["days_since_listing"] > max_days_since_listing:
            continue
        found.append(profile)

    found.sort(key=lambda r: r["listing_date"], reverse=True)
    return found
