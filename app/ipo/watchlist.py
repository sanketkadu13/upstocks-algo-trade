"""IPO breakout watchlist and monitor.

Watches eligible recent listings and fires when price breaks the listing-day
high. The levels follow the reference strategy:

    entry  = price at the break
    stop   = listing-day low
    target = listing-day HIGH x 1.30   (anchored to the high, not the fill, so
             a gap-up entry doesn't silently shrink the target)

This module is alert-only by default. It never places an order on its own —
`auto_trade` exists as an explicit opt-in and still routes through
OrderGateway, so the kill switch applies.
"""
from __future__ import annotations

import logging
import math
import threading
from datetime import date, datetime

from app import audit, instruments
from app.config import DATA_DIR
from app.ipo import listings
from app.price_cache import cache
from app.storage import append_jsonl, atomic_json_dump, load_json, read_jsonl
from app.timeutil import ist_now

logger = logging.getLogger("ipo.watchlist")

WATCHLIST_FILE = DATA_DIR / "ipo_watchlist.json"
IPO_LEDGER_FILE = DATA_DIR / "ipo_ledger.jsonl"

DEFAULT_BUY_INR = 200_000.0
NEAR_ALERT_PCT = 1.0  # warn when within this % below the listing high

STATUS_ARMED = "armed"
STATUS_TRIGGERED = "triggered"
STATUS_DISARMED = "disarmed"

_lock = threading.RLock()


def _load() -> dict:
    return load_json(WATCHLIST_FILE, {}) or {}


def _save(data: dict) -> None:
    atomic_json_dump(WATCHLIST_FILE, data)


def all_entries() -> list[dict]:
    with _lock:
        rows = list(_load().values())
    return sorted(rows, key=lambda r: r.get("listing_date") or "", reverse=True)


def add(broker, symbol: str, buy_amount_inr: float = DEFAULT_BUY_INR, force: bool = False) -> dict:
    """Analyse the symbol and add it armed. Ineligible names (already broke
    their listing high) are refused unless explicitly forced."""
    profile = listings.analyze(broker, symbol)
    if not profile.get("ok"):
        return {"ok": False, "error": profile.get("error", "analysis failed")}

    if profile.get("listing_date_is_window_edge") and not force:
        return {
            "ok": False,
            "error": f"{profile['symbol']} has history older than the lookback window — "
                     "this is not a recent listing",
        }
    if not profile["eligible"] and not force:
        return {
            "ok": False,
            "error": f"{profile['symbol']} already broke its listing high on {profile['break_date']}",
        }

    record = {
        **profile,
        "buy_amount_inr": float(buy_amount_inr),
        "status": STATUS_ARMED if profile["eligible"] else STATUS_DISARMED,
        "added_at": ist_now().isoformat(timespec="seconds"),
        "near_alerted_at": None,
        "triggered_at": None,
        "entry_price": None,
        "qty": None,
    }
    with _lock:
        data = _load()
        data[profile["symbol"]] = record
        _save(data)
    cache.set_subscribed(cache.subscribed_keys() | {profile["instrument_key"]})
    audit.log_event("ipo_added", symbol=profile["symbol"], listing_high=profile["listing_high"])
    return {"ok": True, "entry": record}


def remove(symbol: str) -> dict:
    with _lock:
        data = _load()
        if symbol.upper() not in data:
            return {"ok": False, "error": "not in watchlist"}
        data.pop(symbol.upper())
        _save(data)
    audit.log_event("ipo_removed", symbol=symbol)
    return {"ok": True}


def set_status(symbol: str, status: str) -> dict:
    if status not in (STATUS_ARMED, STATUS_DISARMED):
        return {"ok": False, "error": "status must be armed or disarmed"}
    with _lock:
        data = _load()
        row = data.get(symbol.upper())
        if not row:
            return {"ok": False, "error": "not in watchlist"}
        row["status"] = status
        if status == STATUS_ARMED:
            # Re-arming clears the one-shot alert flags so it can fire again.
            row["near_alerted_at"] = None
            row["triggered_at"] = None
        _save(data)
    audit.log_event("ipo_status_changed", symbol=symbol, status=status)
    return {"ok": True, "entry": row}


def set_amount(symbol: str, amount: float) -> dict:
    with _lock:
        data = _load()
        row = data.get(symbol.upper())
        if not row:
            return {"ok": False, "error": "not in watchlist"}
        row["buy_amount_inr"] = float(amount)
        _save(data)
    return {"ok": True, "entry": row}


def refresh_levels(broker, symbol: str) -> dict:
    """Recompute listing analysis (e.g. after it breaks, to update state)."""
    with _lock:
        data = _load()
        row = data.get(symbol.upper())
        if not row:
            return {"ok": False, "error": "not in watchlist"}
    profile = listings.analyze(broker, symbol)
    if not profile.get("ok"):
        return {"ok": False, "error": profile.get("error")}
    with _lock:
        data = _load()
        row = data.get(symbol.upper(), {})
        row.update({k: v for k, v in profile.items() if k != "ok"})
        data[symbol.upper()] = row
        _save(data)
    return {"ok": True, "entry": row}


def subscribed_keys() -> set[str]:
    return {r["instrument_key"] for r in all_entries() if r.get("instrument_key")}


def enrich(row: dict) -> dict:
    """Attach live price and the distances the UI ranks on."""
    tick = cache.get(row.get("instrument_key")) if row.get("instrument_key") else None
    ltp = tick["ltp"] if tick else None
    high = row.get("listing_high")
    low = row.get("listing_low")
    out = dict(row)
    out["tick"] = tick
    out["ltp"] = ltp
    if ltp and high:
        out["distance_high_pct"] = round((ltp - high) / high * 100, 2)
    else:
        out["distance_high_pct"] = None
    if ltp and low:
        out["distance_low_pct"] = round((ltp - low) / low * 100, 2)
    else:
        out["distance_low_pct"] = None
    return out


def view() -> list[dict]:
    return [enrich(r) for r in all_entries()]


def _fire_trigger(row: dict, ltp: float, notifier=None) -> dict:
    high = row["listing_high"]
    qty = int(math.floor(float(row.get("buy_amount_inr") or DEFAULT_BUY_INR) / ltp)) if ltp else 0
    target = round(high * (1 + listings.TARGET_PCT_FROM_HIGH / 100.0), 2)
    stop = row["listing_low"]

    record = {
        "ts": ist_now().isoformat(timespec="seconds"),
        "date": date.today().isoformat(),
        "symbol": row["symbol"],
        "name": row.get("name"),
        "listing_high": high,
        "listing_low": stop,
        "break_price": ltp,
        "breakout_pct": round((ltp - high) / high * 100, 2),
        "entry_price": ltp,
        "stop_price": stop,
        "target_price": target,
        "stop_pct": round((stop - ltp) / ltp * 100, 2),
        "target_pct_from_entry": round((target - ltp) / ltp * 100, 2),
        "qty": qty,
        "notional": round(qty * ltp, 2),
        "is_paper": True,  # this module never places an order by itself
    }
    append_jsonl(IPO_LEDGER_FILE, record)
    audit.log_event("ipo_breakout", symbol=row["symbol"], price=ltp, qty=qty)
    logger.info("IPO BREAKOUT %s @ %.2f (listing high %.2f)", row["symbol"], ltp, high)

    if notifier:
        try:
            notifier(
                f"IPO BREAKOUT {row['symbol']} @ {ltp:.2f}\n"
                f"listing high {high:.2f} ({record['breakout_pct']:+.2f}%)\n"
                f"stop {stop:.2f} ({record['stop_pct']:.1f}%) · target {target:.2f}\n"
                f"would buy {qty} (~₹{record['notional']:,.0f}) — no order placed"
            )
        except Exception as e:
            logger.warning("IPO notify failed: %s", e)
    return record


def check_once(notifier=None) -> dict:
    """One pass over armed entries. Returns a summary of what happened."""
    fired: list[str] = []
    near: list[str] = []

    with _lock:
        data = _load()
        armed = [(sym, dict(row)) for sym, row in data.items() if row.get("status") == STATUS_ARMED]

    for sym, row in armed:
        tick = cache.get(row.get("instrument_key"))
        if not tick or tick.get("stale"):
            continue
        ltp = tick["ltp"]
        high = row.get("listing_high")
        if not high or not ltp:
            continue

        if ltp > high:
            record = _fire_trigger(row, ltp, notifier)
            with _lock:
                data = _load()
                live = data.get(sym)
                if live:
                    live.update(
                        {
                            "status": STATUS_TRIGGERED,
                            "triggered_at": record["ts"],
                            "entry_price": ltp,
                            "qty": record["qty"],
                            "already_broke": True,
                            "eligible": False,
                        }
                    )
                    _save(data)
            fired.append(sym)
            continue

        gap_pct = (high - ltp) / high * 100
        if 0 <= gap_pct <= NEAR_ALERT_PCT and not row.get("near_alerted_at"):
            with _lock:
                data = _load()
                live = data.get(sym)
                if live:
                    live["near_alerted_at"] = ist_now().isoformat(timespec="seconds")
                    _save(data)
            near.append(sym)
            audit.log_event("ipo_near_breakout", symbol=sym, gap_pct=round(gap_pct, 2))
            if notifier:
                try:
                    notifier(f"IPO NEAR BREAKOUT {sym} @ {ltp:.2f} — {gap_pct:.2f}% below listing high {high:.2f}")
                except Exception:
                    pass

    return {"fired": fired, "near": near, "checked": len(armed)}


def ledger(limit: int = 100) -> list[dict]:
    return list(reversed(read_jsonl(IPO_LEDGER_FILE, limit=limit)))


def outcomes(broker) -> list[dict]:
    """Walk each triggered name forward to see whether target or stop hit.

    Conservative on ambiguity: if a single session tagged both the stop and
    the target, the stop is assumed to have come first.
    """
    out = []
    for record in ledger(limit=500):
        sym = record["symbol"]
        inst = instruments.resolve_equity(sym)
        if not inst:
            continue
        try:
            candles = listings.fetch_daily_candles(broker, inst["instrument_key"], years=2)
        except Exception:
            continue
        trig_date = record["date"]
        result, hit_date, days = "open", None, None
        for i, row in enumerate(c for c in candles if listings._candle_date(c).isoformat() >= trig_date):
            high, low = float(row[2]), float(row[3])
            hit_stop = low <= record["stop_price"]
            hit_target = high >= record["target_price"]
            if hit_stop:
                result, hit_date, days = "stop", listings._candle_date(row).isoformat(), i
                break
            if hit_target:
                result, hit_date, days = "target", listings._candle_date(row).isoformat(), i
                break
        last_close = float(candles[-1][4]) if candles else None
        pnl_pct = (
            round((last_close - record["entry_price"]) / record["entry_price"] * 100, 2)
            if last_close and record.get("entry_price")
            else None
        )
        out.append({**record, "outcome": result, "hit_date": hit_date, "days_to_hit": days,
                    "last_close": last_close, "open_pnl_pct": pnl_pct})
    return out
