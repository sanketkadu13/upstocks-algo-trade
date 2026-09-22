"""Background scan of the NSE equity master for recent listings.

Runs as a cancellable job rather than inside a request: a full sweep is one
historical call per symbol across ~2,900 symbols, which is minutes of work
and must not hold an HTTP worker (the reference implementation's equivalent
endpoint blocked for 30s+ on every call).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date

from app.config import DATA_DIR
from app.ipo import listings
from app.storage import atomic_json_dump, load_json

logger = logging.getLogger("ipo.scanner")

SCAN_CACHE = DATA_DIR / "ipo_scan.json"
RATE_LIMIT_SLEEP_S = 0.35  # stay under the broker's request ceiling

_state = {
    "status": "idle",  # idle | running | done | cancelled | error
    "checked": 0,
    "total": 0,
    "found": 0,
    "current": None,
    "error": None,
    "started_at": None,
    "finished_at": None,
}
_lock = threading.RLock()
_cancel = threading.Event()
_thread: threading.Thread | None = None


def status() -> dict:
    with _lock:
        s = dict(_state)
    cached = load_json(SCAN_CACHE, {}) or {}
    s["cached_at"] = cached.get("scanned_at")
    s["cached_count"] = len(cached.get("listings", []))
    return s


def results() -> list[dict]:
    return (load_json(SCAN_CACHE, {}) or {}).get("listings", [])


def cancel() -> dict:
    _cancel.set()
    return {"ok": True}


def start(broker, max_days_since_listing: int = 400) -> dict:
    global _thread
    with _lock:
        if _state["status"] == "running":
            return {"ok": False, "error": "a scan is already running"}
        _cancel.clear()
        _state.update(
            {"status": "running", "checked": 0, "total": 0, "found": 0,
             "current": None, "error": None,
             "started_at": time.time(), "finished_at": None}
        )
    _thread = threading.Thread(target=_run, args=(broker, max_days_since_listing), daemon=True)
    _thread.start()
    return {"ok": True}


def _run(broker, max_days_since_listing: int) -> None:
    from app import instruments

    try:
        symbols = [r["tradingsymbol"] for r in instruments.load().get("equities", [])]
        with _lock:
            _state["total"] = len(symbols)

        found: list[dict] = []
        for i, sym in enumerate(symbols):
            if _cancel.is_set():
                with _lock:
                    _state.update({"status": "cancelled", "finished_at": time.time()})
                return

            with _lock:
                _state.update({"checked": i + 1, "current": sym, "found": len(found)})

            inst = instruments.resolve_equity(sym)
            if not inst:
                continue
            try:
                candles = listings.fetch_daily_candles(broker, inst["instrument_key"])
            except Exception:
                continue
            finally:
                time.sleep(RATE_LIMIT_SLEEP_S)

            if not candles or not listings.is_probably_recent_listing(candles):
                continue
            profile = listings.analyze(broker, sym, candles=candles)
            if not profile.get("ok") or profile["days_since_listing"] > max_days_since_listing:
                continue
            found.append(profile)

        found.sort(key=lambda r: r["listing_date"], reverse=True)
        atomic_json_dump(
            SCAN_CACHE,
            {"scanned_at": date.today().isoformat(), "listings": found},
        )
        with _lock:
            _state.update({"status": "done", "found": len(found), "finished_at": time.time(), "current": None})
        logger.info("IPO scan complete: %d recent listings", len(found))

    except Exception as e:
        logger.exception("IPO scan failed")
        with _lock:
            _state.update({"status": "error", "error": str(e), "finished_at": time.time()})
