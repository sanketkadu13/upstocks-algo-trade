"""The scenario: the engine enters at 09:45 via API, the user closes both
legs by hand at 13:00, and at 15:15 the EOD square-off fires.

If the engine sends "buy to close" for options it no longer holds, it does
not close anything — it OPENS a fresh long position. These tests pin that
behaviour shut.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.mtm import Leg, TriggerState  # noqa: E402
from app.strategy_engine import STATUS_CLOSED, STATUS_LIVE, Runtime  # noqa: E402


CE = Leg("NSE_FO|1", "NIFTY 23400 CE", qty=-65, avg_entry=90.0, lot_size=65)
PE = Leg("NSE_FO|2", "NIFTY 23250 PE", qty=-65, avg_entry=16.0, lot_size=65)


class RecordingGateway:
    """Captures every order the engine tries to place."""

    def __init__(self):
        self.orders = []

    def place_order(self, **kw):
        self.orders.append(kw)
        return {"order_id": f"o{len(self.orders)}"}

    def fill_price(self, order_id, fallback, retries=3):
        return fallback

    @property
    def killed(self):
        return False

    def kill_state(self):
        return {"enabled": False, "changed_at": None}


class FakeBroker:
    """positions() returns whatever the test says is actually held."""

    def __init__(self, held):
        self.held = held

    def positions(self):
        return [{"instrument_token": k, "quantity": q} for k, q in self.held.items()]

    def order_status(self, oid):
        return {"status": "complete", "average_price": 1.0}


@pytest.fixture
def engine(tmp_path, monkeypatch):
    import app.strategy_engine as se

    monkeypatch.setattr(se, "RUNTIME_FILE", tmp_path / "runtime.json")
    monkeypatch.setattr(se.ledger, "TAP_LEDGER_FILE", tmp_path / "ledger.jsonl", raising=False)
    monkeypatch.setattr(se.ledger, "append", lambda rec: None)
    monkeypatch.setattr(se.audit, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(se.notify, "notify_problem", lambda *a, **k: None)
    monkeypatch.setattr(se.notify, "notify_exit", lambda *a, **k: None)
    monkeypatch.setattr(se.notify, "notify_critical", lambda *a, **k: None)
    monkeypatch.setattr(se.strategies_store, "save_all", lambda s: None)
    # a live price for every leg
    monkeypatch.setattr(
        se.cache, "get",
        lambda key: {"ltp": 10.0, "bid": 9.8, "ask": 10.2, "stale": False, "key": key, "age_s": 0.1},
    )

    def _make(held):
        eng = se.StrategyEngine.__new__(se.StrategyEngine)
        eng.broker = FakeBroker(held)
        eng.gateway = RecordingGateway()
        eng.feed = None
        import threading

        eng._lock = threading.RLock()
        eng.strategies = {
            "s1": {"id": "s1", "name": "Strangle", "mode": "live", "lots": 1,
                   "eod_squareoff_enabled": True, "eod_squareoff_time": "15:15"}
        }
        rt = Runtime(sid="s1")
        rt.status = STATUS_LIVE
        rt.legs = [CE, PE]
        rt.session_id = "sess1"
        rt.entry_time = "2026-09-21T09:45:00"
        rt.trigger = TriggerState(peak_day=500.0, trough_day=-100.0)
        eng.runtimes = {"s1": rt}
        eng.date = "2026-09-21"
        eng.prev_range = {}
        eng._plans = {}
        eng._persist = lambda: None
        return eng

    return _make


def test_no_orders_when_user_already_closed_both_legs(engine):
    """The headline case: both legs gone, EOD fires, nothing may be sent."""
    eng = engine(held={})  # broker reports no open positions

    result = eng.exit("s1", reason="EOD_SQUAREOFF")

    assert eng.gateway.orders == [], "engine placed orders for a position it did not hold"
    assert result["ok"] is True
    assert result["external"] is True
    assert result["orders_placed"] == 0
    assert eng.runtimes["s1"].status == STATUS_CLOSED
    assert eng.runtimes["s1"].exit_reason == "EXTERNAL"


def test_only_the_still_held_leg_is_exited(engine):
    """User closed the CE by hand; the PE must still be squared off."""
    eng = engine(held={PE.instrument_key: -65})

    eng.exit("s1", reason="EOD_SQUAREOFF")

    sent = [o["instrument_key"] for o in eng.gateway.orders]
    assert PE.instrument_key in sent
    assert CE.instrument_key not in sent, "sent an order for the manually closed leg"


def test_both_legs_held_exits_normally(engine):
    """Control: nothing was closed by hand, so both legs get exit orders."""
    eng = engine(held={CE.instrument_key: -65, PE.instrument_key: -65})

    eng.exit("s1", reason="EOD_SQUAREOFF")

    sent = {o["instrument_key"] for o in eng.gateway.orders}
    assert sent == {CE.instrument_key, PE.instrument_key}


def test_closed_strategy_cannot_be_exited_again(engine):
    """A second trigger after the position is closed must be a no-op."""
    eng = engine(held={})
    eng.exit("s1", reason="EOD_SQUAREOFF")
    eng.gateway.orders.clear()

    again = eng.exit("s1", reason="EOD_SQUAREOFF")

    assert again["ok"] is False
    assert eng.gateway.orders == []


def test_closed_strategy_does_not_auto_reenter(engine):
    """After an external close the engine must not open a fresh position."""
    eng = engine(held={})
    eng.exit("s1", reason="EOD_SQUAREOFF")

    cfg = eng.strategies["s1"]
    cfg.update({"auto_entry_enabled": True, "auto_entry_time": "09:45",
                "auto_entry_grace_minutes": 10, "enabled": True})
    rt = eng.runtimes["s1"]

    eng._maybe_auto_enter("s1", cfg, rt)

    assert eng.gateway.orders == [], "auto-entry re-opened a manually closed position"
    assert rt.status == STATUS_CLOSED


def test_exit_proceeds_when_positions_api_is_unavailable(engine, monkeypatch):
    """If we cannot verify, attempting the exit is safer than abandoning it."""
    eng = engine(held={CE.instrument_key: -65, PE.instrument_key: -65})
    monkeypatch.setattr(eng, "held_quantities", lambda: None)

    eng.exit("s1", reason="MANUAL")

    assert len(eng.gateway.orders) == 2


def test_paper_mode_does_not_consult_broker_positions(engine):
    """Paper has no real position; verification would wrongly block it."""
    eng = engine(held={})
    eng.strategies["s1"]["mode"] = "paper"

    eng.exit("s1", reason="EOD_SQUAREOFF")

    assert len(eng.gateway.orders) == 2
    assert all(o.get("paper") for o in eng.gateway.orders)
