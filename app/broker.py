"""Normalized broker interface. Strategies call only this — never an SDK directly."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable

from app.config import settings


class AuthRequired(Exception):
    """Raised when the broker has no valid session and needs interactive re-auth."""

    def __init__(self, login_url: str, message: str = "Broker authentication required"):
        super().__init__(message)
        self.login_url = login_url


class BrokerBase(ABC):
    @abstractmethod
    def login(self, force: bool = False) -> bool:
        """Ensure a valid session. Returns True if authenticated. Raises
        AuthRequired if interactive login is needed and force can't fix it."""

    @abstractmethod
    def is_authenticated(self) -> bool:
        ...

    @abstractmethod
    def index_ltp(self, name: str) -> float | None:
        ...

    @abstractmethod
    def option_expiries(self, name: str) -> dict:
        """Returns {'weekly_current': date, 'weekly_next': date, 'monthly': date}."""

    @abstractmethod
    def resolve_option(self, name: str, expiry: str, strike: int, option_type: str) -> dict:
        """Returns {'tradingsymbol', 'instrument_key', 'lot_size'}."""

    @abstractmethod
    def ltp(self, keys: list[str]) -> dict[str, float]:
        """Batched REST LTP fetch. {instrument_key: price}."""

    @abstractmethod
    def place_order(
        self,
        instrument_key: str,
        quantity: int,
        transaction_type: str,  # "BUY" | "SELL"
        order_type: str = "MARKET",
        price: float = 0,
        product: str = "I",
        validity: str = "DAY",
    ) -> dict:
        ...

    @abstractmethod
    def order_status(self, order_id: str) -> dict:
        ...

    @abstractmethod
    def modify_order(self, order_id: str, price: float, quantity: int | None = None,
                     order_type: str = "LIMIT") -> dict:
        """Re-price a resting order (used by the exit chase ladder)."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> dict:
        ...

    @abstractmethod
    def positions(self) -> list[dict]:
        ...

    @abstractmethod
    def historical_candles(self, instrument_key: str, interval: str, to_date: str, from_date: str) -> list:
        ...

    @abstractmethod
    def live_price_feed(self, keys: list[str], on_tick: Callable[[str, float, float], None]):
        """Starts (or returns a handle to) the WebSocket feed. on_tick(key, ltp, ts)."""


_broker_singleton: BrokerBase | None = None


def get_broker() -> BrokerBase:
    global _broker_singleton
    if _broker_singleton is not None:
        return _broker_singleton
    if settings.broker == "upstox":
        from app.upstox_broker import UpstoxBroker

        _broker_singleton = UpstoxBroker()
    else:
        raise ValueError(f"Unknown BROKER={settings.broker!r}")
    return _broker_singleton
