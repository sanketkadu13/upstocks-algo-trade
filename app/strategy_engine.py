"""Orchestrates strategies: entry, live monitoring, and exit.

Design rules this file exists to enforce:

  * Every order goes through OrderGateway — never the broker directly.
  * An exit is claimed under a lock before any network call, so two triggers
    firing in the same second cannot double-exit a position.
  * Live legs are persisted on every change, because an in-memory-only
    position becomes an untracked live short the moment the process restarts.
  * A leg that fails to enter is surfaced loudly rather than silently
    abandoning a half-built strangle.
"""
from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from app import audit, instruments, market_data, notify, strategies_store
from app.broker import AuthRequired, BrokerBase
from app.config import DATA_DIR, settings
from app.mtm import (
    Leg,
    MtmSnapshot,
    TriggerState,
    compute_mtm,
    evaluate_exit,
    limit_exit_price,
    update_trigger_state,
)
from app.order_gateway import OrderBlocked, OrderGateway
from app.price_cache import cache
from app.storage import atomic_json_dump, load_json
from app.strategy.charges import LegFill, compute_charges
from app.strategy import ledger
from app.timeutil import ist_now, parse_hhmm

logger = logging.getLogger("strategy_engine")

RUNTIME_FILE = DATA_DIR / "runtime_state.json"

STATUS_IDLE = "idle"
STATUS_LIVE = "live"
STATUS_CLOSED = "closed"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"

CHASE_STEPS = (0.005, 0.015, 0.03)  # widen the limit on each unfilled retry
CHASE_WAIT_S = 3.0
# How often (in ~1s ticks) to reconcile tracked legs against the broker. Kept
# short because every second of drift is a second in which the engine could
# act on a position the user has already closed by hand.
EXTERNAL_CHECK_EVERY_TICKS = 15


@dataclass
class Runtime:
    sid: str
    status: str = STATUS_IDLE
    legs: list[Leg] = field(default_factory=list)
    trigger: TriggerState = field(default_factory=TriggerState)
    session_id: str | None = None
    entry_time: str | None = None
    exit_time: str | None = None
    exit_reason: str | None = None
    detail: str | None = None
    snapshot: MtmSnapshot | None = None
    exit_in_flight: bool = False
    tick_count: int = 0

    def to_persisted(self) -> dict:
        return {
            "status": self.status,
            "session_id": self.session_id,
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "exit_reason": self.exit_reason,
            "detail": self.detail,
            "trigger": self.trigger.to_dict(),
            "legs": [
                {
                    "instrument_key": l.instrument_key,
                    "symbol": l.symbol,
                    "qty": l.qty,
                    "avg_entry": l.avg_entry,
                    "lot_size": l.lot_size,
                }
                for l in self.legs
            ],
        }


class StrategyEngine:
    def __init__(self, broker: BrokerBase, gateway: OrderGateway, feed) -> None:
        self.broker = broker
        self.gateway = gateway
        self.feed = feed
        self._lock = threading.RLock()
        self.strategies: dict[str, dict] = strategies_store.load_all()
        self.runtimes: dict[str, Runtime] = {}
        self.date = date.today().isoformat()
        self.prev_range: dict = {}
        self._prev_range_attempt = 0.0
        self._feed_synced = False
        # Planned (not yet entered) legs, refreshed periodically so the
        # dashboard can show live premiums for the strikes we intend to sell
        # and so an entry never has to wait for a first tick.
        self._plans: dict[str, dict] = {}
        self._plans_refreshed = 0.0
        self._bootstrap()

    PLAN_REFRESH_S = 60.0

    # ---------------------------------------------------------------- setup
    def _bootstrap(self) -> None:
        if not self.strategies:
            sid = "s1"
            self.strategies = {sid: strategies_store.new_strategy(sid)}
            strategies_store.save_all(self.strategies)

        saved = load_json(RUNTIME_FILE, {}) or {}
        same_day = saved.get("date") == self.date
        for sid in self.strategies:
            rt = Runtime(sid=sid)
            data = (saved.get("runtimes") or {}).get(sid) if same_day else None
            if data:
                rt.status = data.get("status", STATUS_IDLE)
                rt.session_id = data.get("session_id")
                rt.entry_time = data.get("entry_time")
                rt.exit_time = data.get("exit_time")
                rt.exit_reason = data.get("exit_reason")
                rt.detail = data.get("detail")
                t = data.get("trigger") or {}
                rt.trigger = TriggerState(**{k: t.get(k) for k in TriggerState().to_dict()})
                rt.legs = [Leg(**l) for l in data.get("legs", [])]
                if rt.status == STATUS_LIVE and rt.legs:
                    logger.warning("recovered LIVE strategy %s with %d legs", sid, len(rt.legs))
            self.runtimes[sid] = rt
        if same_day:
            self.prev_range = saved.get("prev_range", {}) or {}

    def _persist(self) -> None:
        with self._lock:
            atomic_json_dump(
                RUNTIME_FILE,
                {
                    "date": self.date,
                    "prev_range": self.prev_range,
                    "runtimes": {sid: rt.to_persisted() for sid, rt in self.runtimes.items()},
                },
            )

    def reload_strategies(self) -> None:
        with self._lock:
            self.strategies = strategies_store.load_all()
            for sid in self.strategies:
                self.runtimes.setdefault(sid, Runtime(sid=sid))

    # ------------------------------------------------------------- market data
    def ensure_prev_range(self) -> dict:
        """Previous-day high/low, preferring the broker's own historical data.

        The self-recorded file is a last resort, and critically it is NOT
        treated as final: if the broker was unauthenticated when we first
        asked, we'd otherwise cache a possibly weeks-old range and compute
        every strike from it for the rest of the day. So a fallback value is
        kept only until an authoritative one can be fetched.
        """
        have = self.prev_range.get("high") is not None
        authoritative = self.prev_range.get("source") == "upstox_historical"
        if have and authoritative:
            return self.prev_range
        if time.time() - self._prev_range_attempt < 30:
            return self.prev_range

        self._prev_range_attempt = time.time()
        fresh = market_data.prev_day_range(self.broker)

        if fresh.get("high") is None:
            return self.prev_range  # keep whatever we had rather than blanking it

        if have and not authoritative and fresh.get("source") != "upstox_historical":
            return self.prev_range  # still only a fallback; nothing gained

        if have and not authoritative:
            logger.info(
                "prev-day range upgraded from %s (%s) to %s (%s)",
                self.prev_range.get("date"), self.prev_range.get("source"),
                fresh.get("date"), fresh.get("source"),
            )
        self.prev_range = fresh
        self._persist()
        return self.prev_range

    def planned_strikes(self) -> dict:
        pr = self.ensure_prev_range()
        if pr.get("high") is None:
            return {}
        return {
            "ce_strike": int(math.ceil(pr["high"] / 50.0) * 50),
            "pe_strike": int(math.floor(pr["low"] / 50.0) * 50),
            "prev_high": pr["high"],
            "prev_low": pr["low"],
        }

    def resolve_expiry(self, choice: str) -> str | None:
        return instruments.option_expiries().get(choice)

    def plan_entry(self, sid: str) -> dict:
        """Dry run: what would this strategy sell right now, and at what price."""
        cfg = self.strategies[sid]
        strikes = self.planned_strikes()
        if not strikes:
            return {"ok": False, "error": "previous-day range unavailable"}
        expiry = self.resolve_expiry(cfg["auto_entry_expiry"])
        if not expiry:
            return {"ok": False, "error": f"no expiry for {cfg['auto_entry_expiry']}"}
        try:
            ce = self.broker.resolve_option("NIFTY", expiry, strikes["ce_strike"], "CE")
            pe = self.broker.resolve_option("NIFTY", expiry, strikes["pe_strike"], "PE")
        except Exception as e:
            return {"ok": False, "error": str(e)}

        lot = ce["lot_size"] or instruments.nifty_lot_size()
        qty = lot * int(cfg["lots"])
        return {
            "ok": True,
            "expiry": expiry,
            "lot_size": lot,
            "qty_per_leg": qty,
            **strikes,
            "ce": {**ce, "tick": cache.get(ce["instrument_key"])},
            "pe": {**pe, "tick": cache.get(pe["instrument_key"])},
        }

    # ------------------------------------------------------------------ guards
    def skip_reason(self, sid: str, expiry: str | None) -> str | None:
        cfg = self.strategies[sid]
        if cfg.get("skip_expiry_day") and expiry == date.today().isoformat():
            return "expiry day (0-DTE)"
        vix = market_data.india_vix()
        if vix and vix["ltp"] > float(cfg.get("vix_max", 0) or 0) > 0:
            return f"India VIX {vix['ltp']:.1f} above limit {cfg['vix_max']}"
        return None

    # ------------------------------------------------------------------- entry
    def enter(self, sid: str, source: str = "manual") -> dict:
        with self._lock:
            rt = self.runtimes[sid]
            if rt.status in (STATUS_LIVE,):
                return {"ok": False, "error": "already live"}
            if rt.exit_in_flight:
                return {"ok": False, "error": "exit in progress"}
            cfg = dict(self.strategies[sid])
            rt.status = "entering"

        plan = self.plan_entry(sid)
        if not plan.get("ok"):
            self._set_status(sid, STATUS_ERROR, detail=plan.get("error"))
            return plan

        skip = self.skip_reason(sid, plan["expiry"])
        if skip:
            self._set_status(sid, STATUS_SKIPPED, detail=skip)
            audit.log_event("entry_skipped", sid=sid, reason=skip, source=source)
            return {"ok": False, "skipped": skip}

        qty = plan["qty_per_leg"]
        paper = cfg["mode"] != "live"
        legs: list[Leg] = []
        failures: list[str] = []

        for side_key in ("ce", "pe"):
            contract = plan[side_key]
            key = contract["instrument_key"]
            tick = cache.get(key)
            if not tick:
                failures.append(f"{side_key.upper()}: no live price")
                continue
            try:
                result = self.gateway.place_order(
                    instrument_key=key,
                    quantity=qty,
                    transaction_type="SELL",
                    order_type="MARKET",
                    paper=paper,
                    context=f"{sid}:entry:{source}",
                    symbol=contract["tradingsymbol"],
                )
                fill = tick["ltp"] if paper else self.gateway.fill_price(result.get("order_id"), tick["ltp"])
                legs.append(
                    Leg(
                        instrument_key=key,
                        symbol=contract["tradingsymbol"],
                        qty=-qty,
                        avg_entry=float(fill),
                        lot_size=plan["lot_size"],
                    )
                )
            except OrderBlocked as e:
                failures.append(f"{side_key.upper()}: {e}")
                break
            except AuthRequired:
                failures.append(f"{side_key.upper()}: broker re-auth required")
                break
            except Exception as e:
                failures.append(f"{side_key.upper()}: {e}")

        if failures and legs:
            # Half-built strangle: one leg is live and naked. Never leave this
            # silent — record it, keep tracking the filled leg so it can still
            # be squared off, and shout.
            detail = f"PARTIAL ENTRY — filled {len(legs)}/2 legs. Failures: {'; '.join(failures)}"
            logger.critical("%s: %s", sid, detail)
            audit.log_event("entry_partial", sid=sid, filled=len(legs), failures=failures)
            cache.log_error("strategy_engine.entry", f"{sid}: {detail}")
            notify.notify_critical(f"PARTIAL ENTRY — {cfg.get('name', sid)}", detail)
            with self._lock:
                rt = self.runtimes[sid]
                rt.legs = legs
                rt.status = STATUS_LIVE
                rt.detail = detail
                rt.session_id = uuid.uuid4().hex[:12]
                rt.entry_time = ist_now().isoformat(timespec="seconds")
            self._persist()
            self._subscribe_legs(legs)
            return {"ok": False, "partial": True, "error": detail, "legs": len(legs)}

        if failures:
            self._set_status(sid, STATUS_ERROR, detail="; ".join(failures))
            return {"ok": False, "error": "; ".join(failures)}

        with self._lock:
            rt = self.runtimes[sid]
            rt.legs = legs
            rt.status = STATUS_LIVE
            rt.trigger = TriggerState()
            rt.session_id = uuid.uuid4().hex[:12]
            rt.entry_time = ist_now().isoformat(timespec="seconds")
            rt.exit_reason = None
            rt.exit_time = None
            rt.detail = None
        self._persist()
        self._subscribe_legs(legs)

        audit.log_event(
            "entry_filled",
            sid=sid,
            source=source,
            mode=cfg["mode"],
            expiry=plan["expiry"],
            legs=[{"symbol": l.symbol, "qty": l.qty, "avg": l.avg_entry} for l in legs],
        )
        logger.info("%s entered: %s", sid, [(l.symbol, l.avg_entry) for l in legs])
        notify.notify_entry(
            cfg.get("name", sid), cfg["mode"],
            [{"symbol": l.symbol, "qty": l.qty, "avg_entry": l.avg_entry} for l in legs],
            plan["expiry"],
        )
        return {"ok": True, "legs": [l.__dict__ for l in legs], "expiry": plan["expiry"]}

    def _subscribe_legs(self, legs: list[Leg]) -> None:
        keys = {l.instrument_key for l in legs}
        cache.set_subscribed(cache.subscribed_keys() | keys)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.feed.add_keys(keys))
        except RuntimeError:
            pass  # called from a worker thread; the tick loop will sync it

    # -------------------------------------------------------------------- exit
    def exit(self, sid: str, reason: str = "MANUAL", detail: str = "") -> dict:
        """Claim the exit under the lock first — two triggers in the same
        second must not both send buy orders."""
        with self._lock:
            rt = self.runtimes[sid]
            if rt.status != STATUS_LIVE or not rt.legs:
                return {"ok": False, "error": "not live"}
            if rt.exit_in_flight:
                return {"ok": False, "error": "exit already in progress"}
            rt.exit_in_flight = True
            cfg = dict(self.strategies[sid])
            legs = list(rt.legs)
            session_id = rt.session_id
            entry_time = rt.entry_time
            trigger_state = rt.trigger

        try:
            return self._do_exit(sid, cfg, legs, reason, detail, session_id, entry_time, trigger_state)
        finally:
            with self._lock:
                self.runtimes[sid].exit_in_flight = False

    def held_quantities(self) -> dict[str, int] | None:
        """Signed quantities actually held at the broker, or None if unknown."""
        try:
            positions = self.broker.positions()
        except Exception as e:
            logger.warning("could not read positions: %s", e)
            return None
        held: dict[str, int] = {}
        for p in positions:
            key = p.get("instrument_token") or p.get("instrument_key")
            qty = int(p.get("quantity") or 0)
            if key and qty:
                held[key] = qty
        return held

    def _do_exit(self, sid, cfg, legs, reason, detail, session_id, entry_time, trigger_state) -> dict:
        paper = cfg["mode"] != "live"
        fills: dict[str, float] = {}
        failures: list[str] = []
        externally_closed: list[Leg] = []

        # Never send an exit for a leg we no longer hold. If it was squared off
        # by hand in the Upstox app, a "buy to close" would not close anything —
        # it would OPEN a fresh long. This is the failure the whole check exists
        # to prevent, and it is most likely to bite at the EOD square-off,
        # hours after the manual exit.
        if not paper:
            held = self.held_quantities()
            if held is None:
                logger.warning("%s: exiting without position verification (positions API unavailable)", sid)
                cache.log_error("strategy_engine.exit", f"{sid}: could not verify positions before exit")
            else:
                still_open, gone = [], []
                for leg in legs:
                    if held.get(leg.instrument_key):
                        still_open.append(leg)
                    else:
                        gone.append(leg)

                if gone:
                    names = ", ".join(l.symbol for l in gone)
                    msg = (f"{len(gone)} leg(s) already closed outside the app ({names}) — "
                           f"no exit order sent for them")
                    logger.warning("%s: %s", sid, msg)
                    audit.log_event("exit_skipped_not_held", sid=sid, legs=[l.symbol for l in gone], reason=reason)
                    externally_closed = gone

                if not still_open:
                    # Everything was closed by hand. Record it and place nothing.
                    return self._close_externally(sid, cfg, legs, session_id, entry_time,
                                                  trigger_state, reason)
                legs = still_open

        for leg in legs:
            tick = cache.get(leg.instrument_key)
            if not tick:
                failures.append(f"{leg.symbol}: no price, cannot exit")
                continue
            try:
                if paper:
                    # Simulate at the realistic exit side of the book, not LTP.
                    from app.mtm import exit_price_for

                    fills[leg.instrument_key] = exit_price_for(tick, leg.qty)
                    self.gateway.place_order(
                        instrument_key=leg.instrument_key,
                        quantity=abs(leg.qty),
                        transaction_type="BUY" if leg.qty < 0 else "SELL",
                        paper=True,
                        context=f"{sid}:exit:{reason}",
                        symbol=leg.symbol,
                    )
                else:
                    fills[leg.instrument_key] = self._exit_leg_live(sid, leg, tick, reason)
            except OrderBlocked as e:
                failures.append(f"{leg.symbol}: {e}")
            except Exception as e:
                failures.append(f"{leg.symbol}: {e}")
                logger.exception("exit failed for %s", leg.symbol)

        if failures:
            msg = f"EXIT INCOMPLETE — {'; '.join(failures)}"
            logger.critical("%s: %s", sid, msg)
            audit.log_event("exit_failed", sid=sid, reason=reason, failures=failures)
            cache.log_error("strategy_engine.exit", f"{sid}: {msg}")
            notify.notify_critical(f"EXIT INCOMPLETE — {cfg.get('name', sid)}", msg)
            # Keep whatever is still open tracked so it can be retried.
            with self._lock:
                rt = self.runtimes[sid]
                rt.legs = [l for l in rt.legs if l.instrument_key not in fills]
                rt.detail = msg
                if not rt.legs:
                    rt.status = STATUS_CLOSED
            self._persist()
            return {"ok": False, "error": msg, "closed_legs": len(fills)}

        gross = sum(
            (leg.avg_entry - fills[leg.instrument_key]) * abs(leg.qty)
            if leg.qty < 0
            else (fills[leg.instrument_key] - leg.avg_entry) * abs(leg.qty)
            for leg in legs
        )
        charge_legs = [LegFill("SELL" if l.qty < 0 else "BUY", l.avg_entry, abs(l.qty)) for l in legs]
        charge_legs += [
            LegFill("BUY" if l.qty < 0 else "SELL", fills[l.instrument_key], abs(l.qty)) for l in legs
        ]
        charges = compute_charges(charge_legs)
        net = gross - charges["total_charges"]

        now = ist_now()
        record = {
            "session_id": session_id,
            "date": date.today().isoformat(),
            "strategy_id": sid,
            "strategy_name": cfg.get("name"),
            "mode": cfg["mode"],
            "entry_time": entry_time,
            "exit_time": now.isoformat(timespec="seconds"),
            "exit_reason": reason,
            "exit_detail": detail,
            "legs": [
                {
                    "symbol": l.symbol,
                    "instrument_key": l.instrument_key,
                    "qty": l.qty,
                    "entry": l.avg_entry,
                    "exit": fills[l.instrument_key],
                }
                for l in legs
            ],
            "peak_pnl": trigger_state.peak_day,
            "trough_pnl": trigger_state.trough_day,
            "gross_pnl": round(gross, 2),
            **charges,
            "net_pnl": round(net, 2),
        }
        ledger.append(record)
        audit.log_event("exit_filled", sid=sid, reason=reason, gross=round(gross, 2), net=round(net, 2))

        with self._lock:
            rt = self.runtimes[sid]
            rt.status = STATUS_CLOSED
            rt.legs = []
            rt.exit_reason = reason
            rt.exit_time = now.isoformat(timespec="seconds")
            rt.detail = detail or None
        self._persist()
        logger.info("%s exited (%s): gross %.2f net %.2f", sid, reason, gross, net)
        notify.notify_exit(cfg.get("name", sid), cfg["mode"], reason,
                           round(gross, 2), charges["total_charges"], round(net, 2))
        return {"ok": True, "gross_pnl": round(gross, 2), "net_pnl": round(net, 2), "reason": reason}

    def _close_externally(self, sid, cfg, legs, session_id, entry_time, trigger_state, reason) -> dict:
        """The position was closed outside this app. Book it from the last
        known prices, flag the estimate, and place no orders."""
        from app.mtm import exit_price_for

        est: dict[str, float] = {}
        for leg in legs:
            tick = cache.get(leg.instrument_key)
            est[leg.instrument_key] = exit_price_for(tick, leg.qty) if tick else leg.avg_entry

        gross = sum(
            (leg.avg_entry - est[leg.instrument_key]) * abs(leg.qty)
            if leg.qty < 0
            else (est[leg.instrument_key] - leg.avg_entry) * abs(leg.qty)
            for leg in legs
        )
        charge_legs = [LegFill("SELL" if l.qty < 0 else "BUY", l.avg_entry, abs(l.qty)) for l in legs]
        charge_legs += [LegFill("BUY" if l.qty < 0 else "SELL", est[l.instrument_key], abs(l.qty)) for l in legs]
        charges = compute_charges(charge_legs)
        net = gross - charges["total_charges"]

        now = ist_now()
        ledger.append(
            {
                "session_id": session_id,
                "date": date.today().isoformat(),
                "strategy_id": sid,
                "strategy_name": cfg.get("name"),
                "mode": cfg["mode"],
                "entry_time": entry_time,
                "exit_time": now.isoformat(timespec="seconds"),
                "exit_reason": "EXTERNAL",
                "exit_detail": f"closed outside the app (engine was about to exit for: {reason})",
                "exit_price_estimated": True,
                "legs": [
                    {
                        "symbol": l.symbol,
                        "instrument_key": l.instrument_key,
                        "qty": l.qty,
                        "entry": l.avg_entry,
                        "exit": est[l.instrument_key],
                    }
                    for l in legs
                ],
                "peak_pnl": trigger_state.peak_day,
                "trough_pnl": trigger_state.trough_day,
                "gross_pnl": round(gross, 2),
                **charges,
                "net_pnl": round(net, 2),
            }
        )
        audit.log_event("exit_external_no_orders", sid=sid, legs=[l.symbol for l in legs], intended_reason=reason)

        with self._lock:
            rt = self.runtimes[sid]
            rt.status = STATUS_CLOSED
            rt.legs = []
            rt.exit_reason = "EXTERNAL"
            rt.exit_time = now.isoformat(timespec="seconds")
            rt.detail = "position was closed outside the app — no exit orders were sent"
        self._persist()

        msg = (f"{cfg.get('name', sid)} was already closed outside the app. "
               f"No orders were placed. Booked at last known prices (net ₹{net:,.0f}, estimated).")
        logger.warning("%s: %s", sid, msg)
        notify.notify_problem("Exit skipped — position not held", msg)
        return {"ok": True, "external": True, "orders_placed": 0, "net_pnl": round(net, 2)}

    def _exit_leg_live(self, sid: str, leg: Leg, tick: dict, reason: str) -> float:
        """Marketable limit, then chase wider if it doesn't fill.

        A plain market order on a thin OTM option can fill far from the quote;
        a limit that walks out in steps keeps a bound on that while still
        getting out.
        """
        side = "BUY" if leg.qty < 0 else "SELL"
        qty = abs(leg.qty)
        order_id = None
        last_price = tick["ltp"]

        for attempt, aggression in enumerate(CHASE_STEPS):
            fresh = cache.get(leg.instrument_key) or tick
            price = limit_exit_price(fresh, leg.qty, aggression)
            last_price = price
            if order_id is None:
                result = self.gateway.place_order(
                    instrument_key=leg.instrument_key,
                    quantity=qty,
                    transaction_type=side,
                    order_type="LIMIT",
                    price=price,
                    context=f"{sid}:exit:{reason}",
                    symbol=leg.symbol,
                )
                order_id = result.get("order_id")
            else:
                try:
                    self.broker.modify_order(order_id, price=price, order_type="LIMIT")
                    audit.log_event("exit_order_chased", sid=sid, symbol=leg.symbol,
                                    order_id=order_id, attempt=attempt + 1, price=price)
                except Exception as e:
                    logger.warning("chase modify failed for %s: %s", leg.symbol, e)

            filled, avg = self._await_fill(order_id, CHASE_WAIT_S)
            if filled:
                return avg or price

        # Still unfilled after the ladder — fall back to a market order so we
        # are not left holding a position we decided to close.
        logger.warning("%s: %s unfilled after chase, sending market order", sid, leg.symbol)
        if order_id:
            try:
                self.broker.cancel_order(order_id)
            except Exception as e:
                logger.warning("cancel before market fallback failed: %s", e)
        result = self.gateway.place_order(
            instrument_key=leg.instrument_key,
            quantity=qty,
            transaction_type=side,
            order_type="MARKET",
            context=f"{sid}:exit:{reason}:market_fallback",
            symbol=leg.symbol,
        )
        return self.gateway.fill_price(result.get("order_id"), last_price)

    def _await_fill(self, order_id: str | None, timeout_s: float) -> tuple[bool, float | None]:
        if not order_id:
            return False, None
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                status = self.broker.order_status(order_id)
                state = str(status.get("status", "")).lower()
                if state in ("complete", "completed", "filled"):
                    avg = status.get("average_price")
                    return True, float(avg) if avg else None
                if state in ("cancelled", "rejected"):
                    return False, None
            except Exception as e:
                logger.warning("order_status poll failed: %s", e)
            time.sleep(0.5)
        return False, None

    # ------------------------------------------------------------------- tick
    async def tick(self) -> None:
        await self._sync_feed_keys()
        self._rollover_if_new_day()
        await self._refresh_plans()

        for sid in list(self.strategies):
            try:
                self._tick_one(sid)
            except Exception as e:
                logger.exception("tick failed for %s", sid)
                cache.log_error("strategy_engine.tick", f"{sid}: {e}")

    async def _refresh_plans(self) -> None:
        """Resolve each idle strategy's intended legs and subscribe them, so
        premiums are already live when the entry fires."""
        if time.time() - self._plans_refreshed < self.PLAN_REFRESH_S:
            return
        self._plans_refreshed = time.time()

        keys: set[str] = set()
        for sid in list(self.strategies):
            rt = self.runtimes.get(sid)
            if rt and rt.status == STATUS_LIVE:
                continue
            try:
                plan = self.plan_entry(sid)
            except Exception as e:
                logger.warning("plan refresh failed for %s: %s", sid, e)
                continue
            self._plans[sid] = plan
            if plan.get("ok"):
                keys.add(plan["ce"]["instrument_key"])
                keys.add(plan["pe"]["instrument_key"])

        if keys:
            await self.feed.add_keys(keys)
            cache.set_subscribed(cache.subscribed_keys() | keys)

    def _tick_one(self, sid: str) -> None:
        cfg = self.strategies.get(sid)
        rt = self.runtimes.get(sid)
        if not cfg or not rt:
            return

        if rt.status == STATUS_LIVE and rt.legs:
            quotes = {l.instrument_key: cache.get(l.instrument_key) for l in rt.legs}
            snapshot = compute_mtm(rt.legs, quotes)
            rt.snapshot = snapshot
            rt.tick_count += 1

            if snapshot.complete:
                rt.trigger = update_trigger_state(rt.trigger, cfg, snapshot)
                reason, detail = evaluate_exit(cfg, snapshot, rt.trigger)
                if not reason and self._is_past_eod(cfg):
                    reason, detail = "EOD_SQUAREOFF", "intraday square-off time reached"
                if reason:
                    self.exit(sid, reason, detail)
                    return

            if rt.tick_count % EXTERNAL_CHECK_EVERY_TICKS == 0:
                self._detect_external_squareoff(sid)
            if rt.tick_count % 10 == 0:
                self._persist()

        elif rt.status in (STATUS_IDLE, STATUS_SKIPPED):
            self._maybe_auto_enter(sid, cfg, rt)

    def _is_past_eod(self, cfg: dict) -> bool:
        if not cfg.get("eod_squareoff_enabled", True):
            return False
        try:
            h, m = parse_hhmm(cfg.get("eod_squareoff_time", "15:15"))
        except Exception:
            h, m = 15, 15
        now = ist_now()
        return (now.hour, now.minute) >= (h, m)

    def _maybe_auto_enter(self, sid: str, cfg: dict, rt: Runtime) -> None:
        if not cfg.get("auto_entry_enabled") or not cfg.get("enabled", True):
            return
        if cfg.get("auto_entry_last_fired") == self.date:
            return
        now = ist_now()
        try:
            h, m = parse_hhmm(cfg["auto_entry_time"])
        except Exception:
            return
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now < start:
            return
        grace = int(cfg.get("auto_entry_grace_minutes", 10) or 10)
        window_end = start + timedelta(minutes=grace)

        # Claim the day before attempting, and persist the claim. A crash
        # mid-entry then costs a missed trade rather than a duplicate one.
        with self._lock:
            self.strategies[sid]["auto_entry_last_fired"] = self.date
            strategies_store.save_all(self.strategies)

        if now > window_end:
            msg = f"auto-entry window missed ({now.strftime('%H:%M')} > {window_end.strftime('%H:%M')})"
            logger.warning("%s: %s", sid, msg)
            audit.log_event("auto_entry_missed", sid=sid, detail=msg)
            cache.log_error("strategy_engine.auto_entry", f"{sid}: {msg}")
            return

        audit.log_event("auto_entry_fired", sid=sid, at=now.isoformat(timespec="seconds"))
        self.enter(sid, source="auto")

    def _detect_external_squareoff(self, sid: str) -> None:
        """If a leg was closed in the Upstox app, stop pretending we hold it."""
        cfg = self.strategies[sid]
        if cfg["mode"] != "live":
            return
        try:
            positions = self.broker.positions()
        except Exception:
            return
        held = {p.get("instrument_token"): p for p in positions if p.get("quantity")}
        with self._lock:
            rt = self.runtimes[sid]
            gone = [l for l in rt.legs if l.instrument_key not in held]
            if not gone:
                return
            rt.legs = [l for l in rt.legs if l.instrument_key in held]
            detail = f"leg(s) closed outside the app: {', '.join(l.symbol for l in gone)}"
            rt.detail = detail
            if not rt.legs:
                rt.status = STATUS_CLOSED
                rt.exit_reason = "EXTERNAL"
        logger.warning("%s: %s", sid, detail)
        audit.log_event("external_squareoff", sid=sid, detail=detail)
        notify.notify_problem(f"Position closed outside the app — {cfg.get('name', sid)}", detail)
        self._persist()

    def _rollover_if_new_day(self) -> None:
        today = date.today().isoformat()
        if today == self.date:
            return
        still_open = [sid for sid, rt in self.runtimes.items() if rt.status == STATUS_LIVE and rt.legs]
        if still_open:
            # Never silently forget a live position at midnight.
            msg = f"positions still open at day rollover: {still_open} — square off manually"
            cache.log_error("strategy_engine.rollover", msg)
            notify.notify_critical("OPEN POSITION AT DAY ROLLOVER", msg)
            return
        self.date = today
        self.prev_range = {}
        for rt in self.runtimes.values():
            if rt.status in (STATUS_CLOSED, STATUS_SKIPPED, STATUS_ERROR):
                self.runtimes[rt.sid] = Runtime(sid=rt.sid)
        self._persist()

    async def _sync_feed_keys(self) -> None:
        if self._feed_synced:
            return
        keys = {settings.index_key, settings.vix_key}
        for rt in self.runtimes.values():
            keys |= {l.instrument_key for l in rt.legs}
        await self.feed.add_keys(keys)
        cache.set_subscribed(cache.subscribed_keys() | keys)
        self._feed_synced = True

    # ------------------------------------------------------------------- views
    def _set_status(self, sid: str, status: str, detail: str | None = None) -> None:
        with self._lock:
            rt = self.runtimes[sid]
            rt.status = status
            rt.detail = detail
        self._persist()

    def active_items(self) -> list[dict]:
        """Everything that is, or could become, a real order. The safety view."""
        items = []
        for sid, cfg in self.strategies.items():
            rt = self.runtimes[sid]
            live = rt.status == STATUS_LIVE and bool(rt.legs)
            armed = bool(cfg.get("auto_entry_enabled")) and cfg.get("auto_entry_last_fired") != self.date
            real_money = cfg["mode"] == "live" and (live or armed)
            if not (live or armed):
                continue
            items.append(
                {
                    "kind": "strategy",
                    "id": sid,
                    "name": cfg.get("name"),
                    "state": "live" if live else f"armed for {cfg.get('auto_entry_time')}",
                    "mode": cfg["mode"],
                    "real_money": real_money,
                    "legs": [l.symbol for l in rt.legs],
                    "mtm": rt.snapshot.combined_exit if rt.snapshot else None,
                    "concerning": real_money,
                }
            )
        return sorted(items, key=lambda i: (not i["concerning"], i["name"] or ""))

    def strategy_view(self, sid: str) -> dict:
        cfg = self.strategies[sid]
        rt = self.runtimes[sid]
        snap = rt.snapshot

        plan = self._plans.get(sid)
        plan_view = None
        if plan and plan.get("ok"):
            plan_view = {
                "expiry": plan["expiry"],
                "qty_per_leg": plan["qty_per_leg"],
                "ce_strike": plan["ce_strike"],
                "pe_strike": plan["pe_strike"],
                "ce": {"symbol": plan["ce"]["tradingsymbol"], "tick": cache.get(plan["ce"]["instrument_key"])},
                "pe": {"symbol": plan["pe"]["tradingsymbol"], "tick": cache.get(plan["pe"]["instrument_key"])},
            }
        elif plan:
            plan_view = {"error": plan.get("error")}

        return {
            "plan": plan_view,
            **cfg,
            "status": rt.status,
            "detail": rt.detail,
            "entry_time": rt.entry_time,
            "exit_time": rt.exit_time,
            "exit_reason": rt.exit_reason,
            "session_id": rt.session_id,
            "trigger": rt.trigger.to_dict(),
            "mtm": {
                "combined_ltp": snap.combined_ltp if snap else None,
                "combined_exit": snap.combined_exit if snap else None,
                "slippage": snap.slippage if snap else None,
                "complete": snap.complete if snap else False,
                "legs": snap.legs if snap else [],
            },
        }

    def full_state(self) -> dict:
        return {
            "date": self.date,
            "prev_range": self.prev_range,
            "planned": self.planned_strikes(),
            "spot": market_data.nifty_spot(),
            "vix": market_data.india_vix(),
            "kill_switch": self.gateway.kill_state(),
            "strategies": [self.strategy_view(sid) for sid in sorted(self.strategies)],
            "active": self.active_items(),
            "ledger": {
                "closed": ledger.read_today(),
                "totals": ledger.totals(ledger.read_today()),
            },
        }
