"""The exact sequence the user asked about:

    engine opens the position  ->  user squares it off by hand in Upstox
                               ->  15:00 square-off fires

Nothing may be sent to the exchange at that point. A "buy to close" against
a position you no longer hold does not close anything — it OPENS a fresh
long, which is how you end a flat day holding something you never wanted.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.strategy_engine as se  # noqa: E402
from app.mtm import Leg, TriggerState  # noqa: E402

CE = Leg("NSE_FO|1", "NIFTY 23500 CE", qty=-65, avg_entry=72.50, lot_size=65)
PE = Leg("NSE_FO|2", "NIFTY 23250 PE", qty=-65, avg_entry=64.15, lot_size=65)


class Gateway:
    def __init__(self):
        self.orders = []

    def place_order(self, **kw):
        self.orders.append(kw)
        return {"order_id": f"o{len(self.orders)}"}

    def fill_price(self, oid, fallback, retries=3):
        return fallback

    @property
    def killed(self):
        return False


class Broker:
    """`held` is mutated mid-test to represent the user's manual exit."""

    def __init__(self, held):
        self.held = dict(held)

    def positions(self):
        return [{"instrument_token": k, "quantity": q} for k, q in self.held.items()]

    def order_status(self, oid):
        return {"status": "complete", "average_price": 1.0}


@pytest.fixture
def make_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(se, "RUNTIME_FILE", tmp_path / "rt.json")
    monkeypatch.setattr(se.ledger, "append", lambda rec: None)
    monkeypatch.setattr(se.audit, "log_event", lambda *a, **k: None)
    for fn in ("notify_problem", "notify_exit", "notify_critical", "send_async"):
        monkeypatch.setattr(se.notify, fn, lambda *a, **k: None)
    monkeypatch.setattr(se.strategies_store, "save_all", lambda s: None)
    monkeypatch.setattr(
        se.cache, "get",
        lambda key: {"ltp": 70.0, "bid": 69.8, "ask": 70.2, "stale": False, "key": key, "age_s": 0.1},
    )

    def _make(mode="live"):
        eng = se.StrategyEngine.__new__(se.StrategyEngine)
        eng.broker = Broker({CE.instrument_key: -65, PE.instrument_key: -65})
        eng.gateway = Gateway()
        eng.feed = None
        eng._lock = threading.RLock()
        eng.strategies = {"s1": {
            "id": "s1", "name": "10 AM Strangle", "mode": mode, "lots": 1,
            "eod_squareoff_enabled": True, "eod_squareoff_time": "15:00",
            "profit_target": 2250, "loss_limit": 2000,
            "profit_target_enabled": True, "loss_limit_enabled": True,
            "profit_target_basis": "exit", "loss_limit_basis": "exit",
        }}
        rt = se.Runtime(sid="s1")
        rt.status = se.STATUS_LIVE
        rt.legs = [CE, PE]
        rt.session_id = "sess"
        rt.entry_time = "2026-09-27T09:50:00"
        rt.trigger = TriggerState()
        eng.runtimes = {"s1": rt}
        eng.date = "2026-09-27"
        eng.prev_range = {}
        eng._prev_range_attempt = 0.0
        eng._plans = {}
        eng._plans_refreshed = 9e9
        eng._persist = lambda: None
        return eng

    return _make


def test_eod_sends_nothing_after_a_manual_exit(make_engine):
    eng = make_engine("live")

    eng.broker.held.clear()                    # you square off in Upstox
    result = eng.exit("s1", reason="EOD_SQUAREOFF")   # 15:00 fires

    assert eng.gateway.orders == [], "engine sent orders for a position it no longer held"
    assert result["orders_placed"] == 0
    assert eng.runtimes["s1"].exit_reason == "EXTERNAL"
    assert eng.runtimes["s1"].status == se.STATUS_CLOSED


def test_reconciliation_notices_the_manual_exit_before_eod(make_engine):
    """Within ~15s the engine should already know the position is gone."""
    eng = make_engine("live")

    eng.broker.held.clear()
    eng._detect_external_squareoff("s1")

    rt = eng.runtimes["s1"]
    assert rt.status == se.STATUS_CLOSED
    assert rt.legs == []
    assert rt.exit_reason == "EXTERNAL"

    # and a later square-off is then a complete no-op
    eng.exit("s1", reason="EOD_SQUAREOFF")
    assert eng.gateway.orders == []


def test_monitor_mode_also_notices_the_manual_exit(make_engine):
    """Monitor mode is where you always exit by hand, so it must detect it."""
    eng = make_engine("monitor")

    eng.broker.held.clear()
    eng._detect_external_squareoff("s1")

    assert eng.runtimes["s1"].status == se.STATUS_CLOSED
    assert eng.gateway.orders == []


def test_partial_manual_exit_closes_only_what_is_still_held(make_engine):
    """You closed the CE yourself; the PE must still be squared off at EOD."""
    eng = make_engine("live")

    del eng.broker.held[CE.instrument_key]
    eng.exit("s1", reason="EOD_SQUAREOFF")

    sent = [o["instrument_key"] for o in eng.gateway.orders]
    assert sent == [PE.instrument_key]


def test_paper_mode_is_unaffected(make_engine):
    """Paper has no broker position; verification must not block its exit."""
    eng = make_engine("paper")

    eng.broker.held.clear()
    eng.exit("s1", reason="EOD_SQUAREOFF")

    assert len(eng.gateway.orders) == 2
