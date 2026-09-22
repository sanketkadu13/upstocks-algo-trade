"""NSE instrument master: download, cache, and resolve NIFTY option contracts.

Maps (NIFTY, expiry, strike, CE/PE) -> instrument_key + lot size. Never
hardcode lot size — it's read from this master (spec requirement).
"""
from __future__ import annotations

import gzip
import json
import logging
import time
from datetime import date, datetime

import httpx

from app.config import INSTRUMENTS_CACHE, INSTRUMENTS_RAW_GZ, settings

logger = logging.getLogger("instruments")

INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

_cache: dict | None = None  # {"downloaded_at": ts, "nifty_options": [...]}


def _download() -> list[dict]:
    logger.info("Downloading instrument master from %s", INSTRUMENTS_URL)
    with httpx.Client(timeout=60.0) as client:
        resp = client.get(INSTRUMENTS_URL)
        resp.raise_for_status()
    INSTRUMENTS_RAW_GZ.write_bytes(resp.content)
    raw = gzip.decompress(resp.content)
    data = json.loads(raw)
    return data


def _filter_nse_equities(data: list[dict]) -> list[dict]:
    """Tradable NSE cash-segment stocks.

    EQ and BE are kept as separate rows: a freshly listed stock often sits in
    the BE (book-entry/trade-for-trade) series for its first months, so an IPO
    breakout watcher that only looked at EQ would miss exactly the names it
    cares about. Upstox keys these by ISIN, so the series change doesn't
    invalidate the instrument_key the way a symbol-suffix scheme would.
    """
    out = []
    for row in data:
        if row.get("segment") != "NSE_EQ":
            continue
        if row.get("instrument_type") not in ("EQ", "BE"):
            continue
        out.append(
            {
                "instrument_key": row.get("instrument_key"),
                "tradingsymbol": row.get("trading_symbol") or row.get("tradingsymbol"),
                "name": row.get("name"),
                "isin": row.get("isin"),
                "instrument_type": row.get("instrument_type"),
                "security_type": row.get("security_type"),
                "tick_size": row.get("tick_size"),
                "lot_size": row.get("lot_size"),
            }
        )
    return out


def _filter_nifty_options(data: list[dict]) -> list[dict]:
    out = []
    for row in data:
        if row.get("segment") != "NSE_FO":
            continue
        if row.get("instrument_type") not in ("CE", "PE"):
            continue
        # Upstox instrument master uses "name" for the underlying (e.g. "NIFTY")
        if row.get("name") != "NIFTY":
            continue
        out.append(
            {
                "instrument_key": row.get("instrument_key"),
                "tradingsymbol": row.get("trading_symbol") or row.get("tradingsymbol"),
                "strike": row.get("strike_price") or row.get("strike"),
                "option_type": row.get("instrument_type"),
                "expiry": row.get("expiry"),  # epoch ms in Upstox master
                "lot_size": row.get("lot_size"),
            }
        )
    return out


def _normalize_expiry(expiry_val) -> str:
    """Upstox instrument master expiry is epoch ms. Returns YYYY-MM-DD."""
    if isinstance(expiry_val, str):
        return expiry_val[:10]
    return datetime.utcfromtimestamp(int(expiry_val) / 1000).strftime("%Y-%m-%d")


def load(force_refresh: bool = False, max_age_hours: float = 20.0) -> dict:
    global _cache
    if _cache is not None and not force_refresh:
        return _cache

    if not force_refresh and INSTRUMENTS_CACHE.exists():
        age_h = (time.time() - INSTRUMENTS_CACHE.stat().st_mtime) / 3600.0
        if age_h < max_age_hours:
            try:
                _cache = json.loads(INSTRUMENTS_CACHE.read_text())
                return _cache
            except Exception as e:
                logger.warning("Failed reading instrument cache, redownloading: %s", e)

    data = _download()
    nifty_options = _filter_nifty_options(data)
    for row in nifty_options:
        row["expiry"] = _normalize_expiry(row["expiry"])
    equities = _filter_nse_equities(data)

    _cache = {"downloaded_at": time.time(), "nifty_options": nifty_options, "equities": equities}
    INSTRUMENTS_CACHE.write_text(json.dumps(_cache))
    logger.info(
        "Instrument master loaded: %d NIFTY option contracts, %d NSE equities",
        len(nifty_options),
        len(equities),
    )
    return _cache


_equity_index: dict[str, dict] | None = None


def _equities_by_symbol() -> dict[str, dict]:
    """Built once per process instead of scanning the master per lookup — the
    reference implementation re-downloaded the whole dump for every symbol,
    which made a 50-symbol bulk add download it 50 times."""
    global _equity_index
    if _equity_index is not None:
        return _equity_index
    data = load()
    index: dict[str, dict] = {}
    for row in data.get("equities", []):
        sym = (row.get("tradingsymbol") or "").upper()
        if not sym:
            continue
        # Prefer the EQ series row when a symbol exists in both.
        if sym not in index or row.get("instrument_type") == "EQ":
            index[sym] = row
    _equity_index = index
    return index


def resolve_equity(symbol: str) -> dict | None:
    """NSE stock symbol -> instrument row (instrument_key, isin, series...)."""
    return _equities_by_symbol().get((symbol or "").strip().upper())


def available_expiries() -> list[str]:
    """All distinct NIFTY option expiries, sorted ascending (YYYY-MM-DD)."""
    data = load()
    expiries = sorted({row["expiry"] for row in data["nifty_options"]})
    return expiries


def option_expiries() -> dict:
    """{'weekly_current', 'weekly_next', 'monthly'} -> expiry date string.

    Monthly = the last available expiry within its own calendar month among
    the sorted expiry list (works whether Upstox tags monthly separately or
    not, since NIFTY's month-end weekly IS the monthly contract).
    """
    expiries = available_expiries()
    today = date.today().isoformat()
    future = [e for e in expiries if e >= today]
    if not future:
        return {"weekly_current": None, "weekly_next": None, "monthly": None}

    weekly_current = future[0]
    weekly_next = future[1] if len(future) > 1 else None

    by_month: dict[str, str] = {}
    for e in future:
        ym = e[:7]
        by_month[ym] = e  # last one wins since sorted ascending -> month-end expiry
    monthly = by_month[weekly_current[:7]]

    return {"weekly_current": weekly_current, "weekly_next": weekly_next, "monthly": monthly}


def resolve_option(expiry: str, strike: int, option_type: str) -> dict | None:
    data = load()
    strike = float(strike)
    for row in data["nifty_options"]:
        if row["expiry"] == expiry and row["option_type"] == option_type and float(row["strike"]) == strike:
            return {
                "tradingsymbol": row["tradingsymbol"],
                "instrument_key": row["instrument_key"],
                "lot_size": int(row["lot_size"]) if row["lot_size"] else None,
            }
    logger.warning("No contract found for NIFTY %s %s %s", expiry, strike, option_type)
    return None


def nifty_lot_size() -> int:
    """Read lot size from the master rather than hardcoding it."""
    data = load()
    for row in data["nifty_options"]:
        if row.get("lot_size"):
            return int(row["lot_size"])
    return 75  # last-resort fallback only if master has no lot_size at all
