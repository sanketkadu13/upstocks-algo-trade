"""The one and only path from this application to a real order.

Every module — strategies, manual UI actions, future additions — places orders
through this gateway, never through the broker directly. That buys three
things that are impossible to retrofit once orders are scattered:

  * a kill switch that genuinely cannot be bypassed,
  * an audit record of every order placed, blocked, or failed,
  * one place to enforce paper mode.

The reference implementation this is modelled on monkey-patched the broker's
place_order to achieve the same thing. An explicit gateway is the same
guarantee without patching a third-party object at import time.
"""
from __future__ import annotations

import logging
import threading

from app import audit, notify
from app.broker import AuthRequired, BrokerBase
from app.config import DATA_DIR
from app.storage import atomic_json_dump, load_json
from app.timeutil import ist_now

logger = logging.getLogger("order_gateway")

KILL_FILE = DATA_DIR / "emergency_kill.json"


class OrderBlocked(Exception):
    """Raised when the kill switch refuses an order."""


class OrderGateway:
    def __init__(self, broker: BrokerBase) -> None:
        self.broker = broker
        self._lock = threading.RLock()
        state = load_json(KILL_FILE, {}) or {}
        self._killed: bool = bool(state.get("enabled", False))
        self._killed_at = state.get("changed_at")
        if self._killed:
            logger.warning("KILL SWITCH IS ARMED — all order placement is blocked")

    # -- kill switch ----------------------------------------------------
    @property
    def killed(self) -> bool:
        with self._lock:
            return self._killed

    def kill_state(self) -> dict:
        with self._lock:
            return {"enabled": self._killed, "changed_at": self._killed_at}

    def set_kill(self, enabled: bool, source: str = "dashboard") -> dict:
        with self._lock:
            self._killed = bool(enabled)
            self._killed_at = ist_now().isoformat(timespec="seconds")
            state = {"enabled": self._killed, "changed_at": self._killed_at}
            atomic_json_dump(KILL_FILE, state)
        audit.log_event("emergency_kill_toggled", enabled=bool(enabled), source=source)
        headline = (
            "🛑 *KILL SWITCH ARMED* — all orders refused"
            if enabled
            else "✅ *Kill switch released* — orders allowed"
        )
        notify.send_async(f"{headline}\nby {source}")
        logger.warning("kill switch %s by %s", "ARMED" if enabled else "released", source)
        return state

    # -- the choke point --------------------------------------------------
    def place_order(
        self,
        instrument_key: str,
        quantity: int,
        transaction_type: str,
        order_type: str = "MARKET",
        price: float = 0,
        product: str = "I",
        validity: str = "DAY",
        *,
        paper: bool = False,
        context: str = "",
        symbol: str = "",
    ) -> dict:
        """Returns the broker payload, or a simulated one in paper mode.

        `context` is free text recorded in the audit trail so an order can be
        traced back to what asked for it (strategy id, manual action, etc).
        """
        common = {
            "instrument_key": instrument_key,
            "symbol": symbol,
            "qty": quantity,
            "side": transaction_type,
            "order_type": order_type,
            "price": price,
            "product": product,
            "context": context,
        }

        if self.killed:
            audit.log_event("place_order_blocked", reason="kill_switch_armed", **common)
            raise OrderBlocked("Kill switch is armed — order refused")

        if paper:
            audit.log_event("place_order_paper", **common)
            return {"order_id": None, "paper": True, **common}

        try:
            result = self.broker.place_order(
                instrument_key=instrument_key,
                quantity=quantity,
                transaction_type=transaction_type,
                order_type=order_type,
                price=price,
                product=product,
                validity=validity,
            )
        except AuthRequired:
            audit.log_event("place_order_failed", reason="auth_required", **common)
            raise
        except Exception as e:
            audit.log_event("place_order_failed", reason=f"{type(e).__name__}: {e}", **common)
            logger.error("order failed (%s): %s", context, e)
            raise

        audit.log_event("place_order_placed", order_id=result.get("order_id"), **common)
        return result

    def fill_price(self, order_id: str | None, fallback: float, retries: int = 3) -> float:
        """Real average fill price where the broker reports one.

        Recording the pre-trade LTP as the fill (as the reference code does)
        quietly misstates every P&L number by the spread, which on a cheap OTM
        option can be a fifth of the premium.
        """
        import time

        if not order_id:
            return fallback
        for _ in range(retries):
            try:
                status = self.broker.order_status(order_id)
                avg = status.get("average_price")
                if avg:
                    return float(avg)
            except Exception as e:
                logger.warning("order_status(%s) failed: %s", order_id, e)
            time.sleep(0.5)
        logger.warning("no fill price for order %s, using %.2f", order_id, fallback)
        return fallback


_gateway: OrderGateway | None = None


def get_gateway(broker: BrokerBase | None = None) -> OrderGateway:
    global _gateway
    if _gateway is None:
        from app.broker import get_broker

        _gateway = OrderGateway(broker or get_broker())
    return _gateway
