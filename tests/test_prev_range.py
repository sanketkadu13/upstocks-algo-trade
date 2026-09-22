"""Previous-day range drives every strike, so a wrong one is a wrong trade.

Regression: the engine once cached a 19-day-old self-recorded range (fetched
while the broker happened to be unauthenticated) and computed strikes from it
for the rest of the day. The number looked perfectly plausible in the UI.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.market_data as md  # noqa: E402


class BrokerNoHistory:
    """Historical endpoint unavailable (e.g. not authenticated)."""

    def historical_candles(self, *a, **kw):
        raise RuntimeError("Broker authentication required")


class BrokerWithHistory:
    def __init__(self, day: date, high: float, low: float):
        self.day, self.high, self.low = day, high, low

    def historical_candles(self, key, interval, to_date, from_date):
        if to_date == self.day.isoformat():
            return [[self.day.isoformat(), 0, self.high, self.low, 0, 0, 0]]
        return []


def _write_fallback(tmp_path, monkeypatch, day: date, high=100.0, low=90.0):
    f = tmp_path / "nifty_daily_range.json"
    f.write_text(json.dumps({day.isoformat(): {"high": high, "low": low}}))
    monkeypatch.setattr(md, "DAILY_RANGE_FILE", f)
    return f


def test_fresh_fallback_is_used_when_history_unavailable(tmp_path, monkeypatch):
    recent = date.today() - timedelta(days=2)
    _write_fallback(tmp_path, monkeypatch, recent, high=23400.0, low=23300.0)

    result = md.prev_day_range(BrokerNoHistory())

    assert result["source"] == "self_recorded_fallback"
    assert result["high"] == 23400.0
    assert result["age_days"] == 2


def test_stale_fallback_is_refused(tmp_path, monkeypatch):
    """The actual bug: a 19-day-old range must not be served as usable."""
    stale = date.today() - timedelta(days=19)
    _write_fallback(tmp_path, monkeypatch, stale, high=23936.4, low=23873.45)

    result = md.prev_day_range(BrokerNoHistory())

    assert result["high"] is None, "a 19-day-old range was served as if current"
    assert result["source"] == "unavailable"


def test_boundary_of_acceptable_fallback_age(tmp_path, monkeypatch):
    ok_age = date.today() - timedelta(days=md.MAX_FALLBACK_AGE_DAYS)
    _write_fallback(tmp_path, monkeypatch, ok_age)
    assert md.prev_day_range(BrokerNoHistory())["high"] is not None

    too_old = date.today() - timedelta(days=md.MAX_FALLBACK_AGE_DAYS + 1)
    _write_fallback(tmp_path, monkeypatch, too_old)
    assert md.prev_day_range(BrokerNoHistory())["high"] is None


def test_broker_history_wins_over_fallback(tmp_path, monkeypatch):
    _write_fallback(tmp_path, monkeypatch, date.today() - timedelta(days=1), high=1.0, low=1.0)
    yesterday = date.today() - timedelta(days=1)

    result = md.prev_day_range(BrokerWithHistory(yesterday, 23466.8, 23314.8))

    assert result["source"] == "upstox_historical"
    assert result["high"] == 23466.8
    assert result["low"] == 23314.8
