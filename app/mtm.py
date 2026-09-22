"""Mark-to-market and exit-trigger maths. Deliberately pure — no broker, no
cache, no clock — so the rules that decide when real money moves can be
tested directly.

Two bases are computed for every position:

  ltp  — marked at last traded price. Optimistic: you cannot actually
         transact at the last print.
  exit — marked at the price you'd really get out at (a long hits the bid,
         a short lifts the ask). This is what the triggers default to.

The gap between them is the slippage number surfaced in the UI. On a cheap
OTM option it is routinely a double-digit percentage of the premium, which is
why exiting on an LTP-based target can book a loss you thought was a win.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Leg:
    instrument_key: str
    symbol: str
    qty: int  # signed: negative = short
    avg_entry: float
    lot_size: int = 1


def leg_mtm(avg_entry: float, price: float, qty: int) -> float:
    """Short profits when price falls; long when it rises. `qty` is signed and
    already includes lot size."""
    if qty < 0:
        return (avg_entry - price) * abs(qty)
    return (price - avg_entry) * abs(qty)


@dataclass
class MtmSnapshot:
    combined_ltp: float
    combined_exit: float
    slippage: float
    legs: list[dict]
    priced_legs: int
    total_legs: int

    @property
    def complete(self) -> bool:
        """False when some leg has no price — triggers must not fire on a
        partial picture, or a missing quote looks like a profit."""
        return self.total_legs > 0 and self.priced_legs == self.total_legs


def compute_mtm(legs: list[Leg], quotes: dict[str, dict | None]) -> MtmSnapshot:
    """`quotes` maps instrument_key -> {"ltp", "bid", "ask", ...} (or None)."""
    combined_ltp = 0.0
    combined_exit = 0.0
    priced = 0
    out_legs: list[dict] = []

    for leg in legs:
        q = quotes.get(leg.instrument_key)
        if not q or q.get("ltp") is None:
            out_legs.append(
                {
                    "instrument_key": leg.instrument_key,
                    "symbol": leg.symbol,
                    "qty": leg.qty,
                    "avg_entry": leg.avg_entry,
                    "ltp": None,
                    "exit_price": None,
                    "mtm_ltp": None,
                    "mtm_exit": None,
                    "priced": False,
                }
            )
            continue

        ltp = float(q["ltp"])
        exit_price = exit_price_for(q, leg.qty)
        m_ltp = leg_mtm(leg.avg_entry, ltp, leg.qty)
        m_exit = leg_mtm(leg.avg_entry, exit_price, leg.qty)

        combined_ltp += m_ltp
        combined_exit += m_exit
        priced += 1

        out_legs.append(
            {
                "instrument_key": leg.instrument_key,
                "symbol": leg.symbol,
                "qty": leg.qty,
                "avg_entry": leg.avg_entry,
                "ltp": ltp,
                "bid": q.get("bid"),
                "ask": q.get("ask"),
                "exit_price": exit_price,
                "mtm_ltp": round(m_ltp, 2),
                "mtm_exit": round(m_exit, 2),
                "stale": bool(q.get("stale")),
                "priced": True,
            }
        )

    return MtmSnapshot(
        combined_ltp=round(combined_ltp, 2),
        combined_exit=round(combined_exit, 2),
        slippage=round(combined_ltp - combined_exit, 2),
        legs=out_legs,
        priced_legs=priced,
        total_legs=len(legs),
    )


def exit_price_for(quote: dict, qty: int) -> float:
    """A long exits into the bid, a short buys back at the ask."""
    ltp = float(quote["ltp"])
    if qty >= 0:
        bid = quote.get("bid")
        return float(bid) if bid else ltp
    ask = quote.get("ask")
    return float(ask) if ask else ltp


@dataclass
class TriggerState:
    """Carried across ticks for one live position."""

    peak_mtm: float | None = None  # peak since the trail armed
    trail_sl: float | None = None
    lock_floor: float | None = None
    peak_day: float | None = None
    trough_day: float | None = None

    def to_dict(self) -> dict:
        return {
            "peak_mtm": self.peak_mtm,
            "trail_sl": self.trail_sl,
            "lock_floor": self.lock_floor,
            "peak_day": self.peak_day,
            "trough_day": self.trough_day,
        }


def update_trigger_state(state: TriggerState, cfg: dict, snapshot: MtmSnapshot) -> TriggerState:
    """Advance trailing stop and profit lock. Never lowers a locked floor."""
    sl_value = _basis_value(snapshot, cfg.get("loss_limit_basis", "exit"))

    # Explicit None checks throughout: a genuine 0.0 P&L is a real value and
    # must not be mistaken for "not yet set".
    state.peak_day = sl_value if state.peak_day is None else max(state.peak_day, sl_value)
    state.trough_day = sl_value if state.trough_day is None else min(state.trough_day, sl_value)

    if cfg.get("trail_enabled"):
        if state.peak_mtm is None and sl_value >= float(cfg.get("trail_activate_at", 0)):
            state.peak_mtm = sl_value  # arm
        if state.peak_mtm is not None:
            state.peak_mtm = max(state.peak_mtm, sl_value)
            state.trail_sl = state.peak_mtm - float(cfg.get("trail_by", 0))

    if cfg.get("lock_profit_enabled") and state.lock_floor is None:
        if sl_value >= float(cfg.get("lock_profit_trigger", 0)):
            state.lock_floor = float(cfg.get("lock_profit_lock_at", 0))

    return state


def evaluate_exit(cfg: dict, snapshot: MtmSnapshot, state: TriggerState) -> tuple[str | None, str]:
    """Returns (reason, human_detail). Reason is None when nothing fires.

    Never fires on an incomplete snapshot — a missing quote on one leg of a
    strangle would otherwise read as a large profit and exit the other leg
    into a gap.
    """
    if not snapshot.complete:
        return None, "waiting for all legs to be priced"

    target_value = _basis_value(snapshot, cfg.get("profit_target_basis", "exit"))
    sl_value = _basis_value(snapshot, cfg.get("loss_limit_basis", "exit"))

    if cfg.get("profit_target_enabled", True):
        target = float(cfg.get("profit_target", 0))
        if target > 0 and target_value >= target:
            return "PROFIT_TARGET", f"P&L {target_value:.0f} reached target {target:.0f}"

    if cfg.get("loss_limit_enabled", True):
        limit = float(cfg.get("loss_limit", 0))
        if limit > 0 and sl_value <= -limit:
            return "LOSS_LIMIT", f"P&L {sl_value:.0f} hit stop -{limit:.0f}"

    if state.trail_sl is not None and sl_value <= state.trail_sl:
        return "TRAILING_SL", f"P&L {sl_value:.0f} fell to trailing stop {state.trail_sl:.0f}"

    if state.lock_floor is not None and sl_value <= state.lock_floor:
        return "PROFIT_LOCK", f"P&L {sl_value:.0f} fell to locked floor {state.lock_floor:.0f}"

    return None, ""


def _basis_value(snapshot: MtmSnapshot, basis: str) -> float:
    return snapshot.combined_exit if basis == "exit" else snapshot.combined_ltp


def round_to_tick(price: float, tick: float = 0.05) -> float:
    return round(round(price / tick) * tick, 2)


def limit_exit_price(quote: dict, qty: int, aggression: float = 0.005) -> float:
    """Marketable limit: cross the spread slightly so it fills, but keep a
    bound so a thin book can't fill us at an absurd price the way a pure
    market order can.
    """
    ltp = float(quote["ltp"])
    if qty < 0:  # buying back a short
        ask = quote.get("ask") or ltp
        return round_to_tick(float(ask) * (1 + aggression))
    bid = quote.get("bid") or ltp
    return round_to_tick(float(bid) * (1 - aggression))
