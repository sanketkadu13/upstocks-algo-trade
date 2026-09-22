"""Tests for the IPO breakout levels and listing analysis."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ipo import listings, watchlist  # noqa: E402


def candle(d: date, o, h, l, c, v=1000):
    return [d.isoformat() + "T00:00:00+05:30", o, h, l, c, v, 0]


class FakeBroker:
    def __init__(self, candles):
        self._candles = candles

    def historical_candles(self, key, interval, to_date, from_date):
        # Upstox returns newest-first; analyze() must sort.
        return list(reversed(self._candles))


def _patch_resolve(monkeypatch, symbol="NEWCO"):
    monkeypatch.setattr(
        listings.instruments,
        "resolve_equity",
        lambda s: {
            "instrument_key": "NSE_EQ|INE000TEST01",
            "tradingsymbol": symbol,
            "name": "New Co Ltd",
            "isin": "INE000TEST01",
            "instrument_type": "EQ",
        },
    )


def test_listing_day_is_the_first_candle(monkeypatch):
    _patch_resolve(monkeypatch)
    start = date.today() - timedelta(days=30)
    candles = [
        candle(start, 100, 120, 95, 110),          # listing day
        candle(start + timedelta(days=1), 110, 115, 105, 108),
        candle(start + timedelta(days=2), 108, 118, 104, 112),
    ]
    r = listings.analyze(FakeBroker(candles), "NEWCO")
    assert r["ok"]
    assert r["listing_date"] == start.isoformat()
    assert r["listing_high"] == 120
    assert r["listing_low"] == 95


def test_eligible_while_listing_high_holds(monkeypatch):
    _patch_resolve(monkeypatch)
    start = date.today() - timedelta(days=10)
    candles = [
        candle(start, 100, 120, 95, 110),
        candle(start + timedelta(days=1), 110, 119.9, 105, 108),  # never exceeds 120
    ]
    r = listings.analyze(FakeBroker(candles), "NEWCO")
    assert r["eligible"] is True
    assert r["already_broke"] is False
    assert r["break_date"] is None


def test_break_of_listing_high_is_detected(monkeypatch):
    _patch_resolve(monkeypatch)
    start = date.today() - timedelta(days=10)
    break_day = start + timedelta(days=2)
    candles = [
        candle(start, 100, 120, 95, 110),
        candle(start + timedelta(days=1), 110, 118, 105, 108),
        candle(break_day, 115, 130, 112, 128),  # exceeds 120
    ]
    r = listings.analyze(FakeBroker(candles), "NEWCO")
    assert r["eligible"] is False
    assert r["already_broke"] is True
    assert r["break_date"] == break_day.isoformat()
    assert r["break_price"] == 130


def test_target_is_anchored_to_listing_high_not_entry(monkeypatch):
    """A gap-up entry must not shrink the target."""
    _patch_resolve(monkeypatch)
    start = date.today() - timedelta(days=5)
    candles = [candle(start, 100, 200, 90, 180)]
    r = listings.analyze(FakeBroker(candles), "NEWCO")
    assert r["target_price"] == 260.0  # 200 * 1.30, regardless of any fill
    assert r["stop_price"] == 90


def test_long_listed_stock_is_flagged_not_a_recent_listing(monkeypatch):
    """History that starts at the lookback window edge is a truncated series,
    not an IPO — the guard that stops RELIANCE being treated as a listing."""
    _patch_resolve(monkeypatch, "OLDCO")
    window_start = date.today() - timedelta(days=365 * listings.LOOKBACK_YEARS)
    candles = [
        candle(window_start, 100, 120, 95, 110),
        candle(window_start + timedelta(days=1), 110, 115, 105, 108),
    ]
    r = listings.analyze(FakeBroker(candles), "OLDCO")
    assert r["is_recent_listing"] is False
    assert r["listing_date_is_window_edge"] is True


def test_recent_listing_is_flagged_as_such(monkeypatch):
    _patch_resolve(monkeypatch)
    start = date.today() - timedelta(days=20)
    r = listings.analyze(FakeBroker([candle(start, 100, 120, 95, 110)]), "NEWCO")
    assert r["is_recent_listing"] is True


def test_trigger_levels_and_sizing(tmp_path, monkeypatch):
    monkeypatch.setattr(watchlist, "IPO_LEDGER_FILE", tmp_path / "ipo_ledger.jsonl")
    monkeypatch.setattr(watchlist.audit, "log_event", lambda *a, **k: None)

    row = {
        "symbol": "NEWCO",
        "name": "New Co",
        "listing_high": 200.0,
        "listing_low": 150.0,
        "buy_amount_inr": 200_000.0,
    }
    rec = watchlist._fire_trigger(row, ltp=210.0)

    assert rec["entry_price"] == 210.0
    assert rec["stop_price"] == 150.0
    assert rec["target_price"] == 260.0            # listing high x 1.30
    assert rec["qty"] == 952                        # floor(200000 / 210)
    assert rec["breakout_pct"] == 5.0               # (210-200)/200
    assert rec["is_paper"] is True                  # never places a real order
