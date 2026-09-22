"""Tests for the maths that decides when real money moves.

Run: .venv\\Scripts\\python.exe -m pytest tests/test_mtm.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.mtm import (  # noqa: E402
    Leg,
    TriggerState,
    compute_mtm,
    evaluate_exit,
    exit_price_for,
    leg_mtm,
    limit_exit_price,
    round_to_tick,
    update_trigger_state,
)


def q(ltp, bid=None, ask=None, stale=False):
    return {"ltp": ltp, "bid": bid, "ask": ask, "stale": stale}


# -- leg maths -------------------------------------------------------------

def test_short_leg_profits_when_premium_falls():
    assert leg_mtm(100.0, 80.0, -75) == 1500.0


def test_short_leg_loses_when_premium_rises():
    assert leg_mtm(100.0, 120.0, -75) == -1500.0


def test_long_leg_profits_when_price_rises():
    assert leg_mtm(100.0, 120.0, 75) == 1500.0


# -- exit basis ------------------------------------------------------------

def test_short_exits_at_ask_long_exits_at_bid():
    assert exit_price_for(q(10.0, bid=9.5, ask=10.5), qty=-75) == 10.5
    assert exit_price_for(q(10.0, bid=9.5, ask=10.5), qty=75) == 9.5


def test_falls_back_to_ltp_without_a_book():
    assert exit_price_for(q(10.0), qty=-75) == 10.0


def test_exit_basis_is_worse_than_ltp_for_a_short():
    """The whole point of dual-basis: LTP flatters a short position."""
    legs = [Leg("K", "CE", qty=-75, avg_entry=20.0)]
    snap = compute_mtm(legs, {"K": q(10.0, bid=9.9, ask=11.0)})
    assert snap.combined_ltp == 750.0  # (20 - 10) * 75
    assert snap.combined_exit == 675.0  # (20 - 11) * 75
    assert snap.slippage == 75.0


# -- incomplete snapshots --------------------------------------------------

def test_snapshot_incomplete_when_a_leg_has_no_quote():
    legs = [Leg("A", "CE", -75, 20.0), Leg("B", "PE", -75, 20.0)]
    snap = compute_mtm(legs, {"A": q(10.0), "B": None})
    assert not snap.complete
    assert snap.priced_legs == 1


def test_no_trigger_fires_on_incomplete_snapshot():
    """A missing quote on one leg must never read as a profit."""
    legs = [Leg("A", "CE", -75, 20.0), Leg("B", "PE", -75, 20.0)]
    snap = compute_mtm(legs, {"A": q(1.0), "B": None})
    cfg = {"profit_target": 100.0, "profit_target_enabled": True}
    reason, _ = evaluate_exit(cfg, snap, TriggerState())
    assert reason is None


# -- triggers --------------------------------------------------------------

BASE_CFG = {
    "profit_target": 3000.0,
    "profit_target_enabled": True,
    "profit_target_basis": "exit",
    "loss_limit": 3000.0,
    "loss_limit_enabled": True,
    "loss_limit_basis": "exit",
}


def snap_at(pnl: float):
    """One short leg contrived to sit at an exact P&L."""
    legs = [Leg("K", "CE", qty=-75, avg_entry=100.0)]
    price = 100.0 - (pnl / 75)
    return compute_mtm(legs, {"K": q(price, bid=price, ask=price)})


def test_profit_target_fires():
    reason, _ = evaluate_exit(BASE_CFG, snap_at(3100), TriggerState())
    assert reason == "PROFIT_TARGET"


def test_loss_limit_fires():
    reason, _ = evaluate_exit(BASE_CFG, snap_at(-3100), TriggerState())
    assert reason == "LOSS_LIMIT"


def test_nothing_fires_in_between():
    reason, _ = evaluate_exit(BASE_CFG, snap_at(500), TriggerState())
    assert reason is None


def test_disabled_target_does_not_fire():
    cfg = {**BASE_CFG, "profit_target_enabled": False}
    reason, _ = evaluate_exit(cfg, snap_at(9999), TriggerState())
    assert reason is None


def test_target_takes_precedence_over_stop():
    """Both can't be true at once here, but order matters if config is odd."""
    cfg = {**BASE_CFG, "profit_target": 100.0, "loss_limit": 100.0}
    reason, _ = evaluate_exit(cfg, snap_at(500), TriggerState())
    assert reason == "PROFIT_TARGET"


# -- trailing stop ---------------------------------------------------------

TRAIL_CFG = {**BASE_CFG, "trail_enabled": True, "trail_activate_at": 1000.0, "trail_by": 400.0}


def test_trail_does_not_arm_below_activation():
    state = update_trigger_state(TriggerState(), TRAIL_CFG, snap_at(900))
    assert state.trail_sl is None


def test_trail_arms_and_follows_peak():
    state = TriggerState()
    state = update_trigger_state(state, TRAIL_CFG, snap_at(1200))
    assert state.trail_sl == 800.0  # 1200 - 400
    state = update_trigger_state(state, TRAIL_CFG, snap_at(2000))
    assert state.trail_sl == 1600.0


def test_trail_never_moves_down():
    state = TriggerState()
    state = update_trigger_state(state, TRAIL_CFG, snap_at(2000))
    state = update_trigger_state(state, TRAIL_CFG, snap_at(1500))
    assert state.trail_sl == 1600.0  # still anchored to the 2000 peak


def test_trail_fires_when_pnl_falls_back():
    state = TriggerState()
    state = update_trigger_state(state, TRAIL_CFG, snap_at(2000))
    reason, _ = evaluate_exit(TRAIL_CFG, snap_at(1500), state)
    assert reason == "TRAILING_SL"


# -- profit lock -----------------------------------------------------------

LOCK_CFG = {
    **BASE_CFG,
    "lock_profit_enabled": True,
    "lock_profit_trigger": 2000.0,
    "lock_profit_lock_at": 1000.0,
}


def test_lock_arms_only_after_trigger():
    state = update_trigger_state(TriggerState(), LOCK_CFG, snap_at(1500))
    assert state.lock_floor is None
    state = update_trigger_state(state, LOCK_CFG, snap_at(2100))
    assert state.lock_floor == 1000.0


def test_lock_floor_never_lowers():
    state = TriggerState()
    state = update_trigger_state(state, LOCK_CFG, snap_at(2100))
    state = update_trigger_state(state, LOCK_CFG, snap_at(50))
    assert state.lock_floor == 1000.0


def test_lock_fires_when_giving_back_profit():
    state = TriggerState()
    state = update_trigger_state(state, LOCK_CFG, snap_at(2100))
    reason, _ = evaluate_exit(LOCK_CFG, snap_at(900), state)
    assert reason == "PROFIT_LOCK"


def test_peak_and_trough_track_the_day():
    state = TriggerState()
    for pnl in (500, 1800, -200, 900):
        state = update_trigger_state(state, BASE_CFG, snap_at(pnl))
    assert state.peak_day == 1800.0
    assert state.trough_day == -200.0


def test_zero_pnl_is_a_real_value_not_unset():
    """Regression guard: `if not peak` would treat 0.0 as never-set."""
    state = update_trigger_state(TriggerState(), BASE_CFG, snap_at(0))
    assert state.peak_day == 0.0
    assert state.trough_day == 0.0


# -- pricing ---------------------------------------------------------------

def test_tick_rounding():
    assert round_to_tick(10.123) == 10.1
    assert round_to_tick(10.126) == 10.15


def test_limit_exit_crosses_the_spread_to_fill():
    # buying back a short: pay slightly above the ask
    assert limit_exit_price(q(10.0, bid=9.5, ask=10.5), qty=-75) > 10.5
    # selling a long: accept slightly below the bid
    assert limit_exit_price(q(10.0, bid=9.5, ask=10.5), qty=75) < 9.5
