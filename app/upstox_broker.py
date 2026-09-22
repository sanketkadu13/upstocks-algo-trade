"""Concrete Upstox implementation of BrokerBase. Direct REST calls via httpx
(see plan notes: gives us explicit error visibility the SDK doesn't).
"""
from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Callable
from urllib.parse import urlencode

import httpx

from app import instruments
from app.broker import AuthRequired, BrokerBase
from app.config import TOKEN_FILE, settings
from app.timeutil import ist_now as _ist_now

logger = logging.getLogger("upstox_broker")


def _token_expiry_for(obtained_at: datetime) -> datetime:
    """Upstox tokens expire ~03:30 IST the day after they're issued (or same
    day if issued after 03:30 already puts you past the prior expiry)."""
    expiry_day = obtained_at.date() + timedelta(days=1)
    return datetime(expiry_day.year, expiry_day.month, expiry_day.day, 3, 30)


def _jwt_expiry_ist(token: str) -> datetime | None:
    """Authoritative expiry straight from the token's own `exp` claim.

    Needed because a token pasted into .env carries no record of when it was
    issued — assuming "obtained just now" would make a long-dead token look
    valid, so every API call 401s while the app insists it's logged in.
    Returns None if the token isn't a parseable JWT.
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload_b64))["exp"]
        return datetime.utcfromtimestamp(int(exp)) + timedelta(hours=5, minutes=30)
    except Exception:
        return None


class UpstoxBroker(BrokerBase):
    def __init__(self) -> None:
        self._access_token: str | None = None
        self._token_obtained_at: datetime | None = None
        self._load_token()

    # -- auth ---------------------------------------------------------
    def _load_token(self) -> None:
        if TOKEN_FILE.exists():
            try:
                data = json.loads(TOKEN_FILE.read_text())
                self._access_token = data["access_token"]
                self._token_obtained_at = datetime.fromisoformat(data["obtained_at"])
                return
            except Exception as e:
                logger.warning("failed reading token file: %s", e)
        if settings.upstox_access_token_seed:
            self._access_token = settings.upstox_access_token_seed
            self._token_obtained_at = _ist_now()

    def _save_token(self, access_token: str) -> None:
        self._access_token = access_token
        self._token_obtained_at = _ist_now()
        TOKEN_FILE.write_text(
            json.dumps({"access_token": access_token, "obtained_at": self._token_obtained_at.isoformat()})
        )

    def login_url(self) -> str:
        params = {
            "response_type": "code",
            "client_id": settings.upstox_api_key,
            "redirect_uri": settings.upstox_redirect_uri,
        }
        return f"{settings.upstox_login_dialog_url}?{urlencode(params)}"

    def exchange_code(self, code: str) -> None:
        resp = httpx.post(
            settings.upstox_token_url,
            data={
                "code": code,
                "client_id": settings.upstox_api_key,
                "client_secret": settings.upstox_api_secret,
                "redirect_uri": settings.upstox_redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json()
        self._save_token(body["access_token"])

    def is_authenticated(self) -> bool:
        if not self._access_token or not self._token_obtained_at:
            return False
        # Prefer the token's own exp claim; fall back to the issue-date rule
        # only for tokens we can't parse.
        expiry = _jwt_expiry_ist(self._access_token) or _token_expiry_for(self._token_obtained_at)
        return _ist_now() < expiry

    def login(self, force: bool = False) -> bool:
        if not force and self.is_authenticated():
            return True
        raise AuthRequired(login_url=self.login_url(), message="Upstox token missing/expired — daily re-auth needed")

    def access_token(self) -> str | None:
        return self._access_token if self.is_authenticated() else None

    def _auth_headers(self) -> dict:
        if not self.is_authenticated():
            raise AuthRequired(login_url=self.login_url())
        return {"Authorization": f"Bearer {self._access_token}", "Accept": "application/json"}

    # -- market data ----------------------------------------------------
    def index_ltp(self, name: str) -> float | None:
        key = settings.index_key if name.upper() == "NIFTY" else name
        prices = self.ltp([key])
        return prices.get(key)

    def option_expiries(self, name: str) -> dict:
        return instruments.option_expiries()

    def resolve_option(self, name: str, expiry: str, strike: int, option_type: str) -> dict:
        result = instruments.resolve_option(expiry, strike, option_type)
        if result is None:
            raise ValueError(f"No NIFTY {expiry} {strike}{option_type} contract found in instrument master")
        return result

    def ltp(self, keys: list[str]) -> dict[str, float]:
        if not keys:
            return {}
        try:
            resp = httpx.get(
                settings.upstox_ltp_url,
                params={"instrument_key": ",".join(keys)},
                headers=self._auth_headers(),
                timeout=10.0,
            )
            resp.raise_for_status()
            body = resp.json()
        except AuthRequired:
            raise
        except Exception as e:
            raise RuntimeError(f"LTP fetch failed for {len(keys)} keys: {e}") from e

        out: dict[str, float] = {}
        for _, row in body.get("data", {}).items():
            ik = row.get("instrument_token")
            ltp = row.get("last_price")
            if ik is not None and ltp is not None:
                out[ik] = ltp
        return out

    def historical_candles(self, instrument_key: str, interval: str, to_date: str, from_date: str) -> list:
        url = f"{settings.upstox_historical_url}/{instrument_key}/{interval}/{to_date}/{from_date}"
        resp = httpx.get(url, headers=self._auth_headers(), timeout=20.0)
        resp.raise_for_status()
        return resp.json().get("data", {}).get("candles", [])

    def intraday_candles(self, instrument_key: str, interval: str) -> list:
        url = f"{settings.upstox_historical_url}/intraday/{instrument_key}/{interval}"
        resp = httpx.get(url, headers=self._auth_headers(), timeout=20.0)
        resp.raise_for_status()
        return resp.json().get("data", {}).get("candles", [])

    # -- orders -----------------------------------------------------------
    def place_order(
        self,
        instrument_key: str,
        quantity: int,
        transaction_type: str,
        order_type: str = "MARKET",
        price: float = 0,
        product: str = "I",
        validity: str = "DAY",
    ) -> dict:
        payload = {
            "quantity": quantity,
            "product": product,
            "validity": validity,
            "price": price,
            "tag": "tap-strangle",
            "instrument_token": instrument_key,
            "order_type": order_type,
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
        }
        resp = httpx.post(
            settings.upstox_order_place_url,
            json=payload,
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def order_status(self, order_id: str) -> dict:
        resp = httpx.get(
            settings.upstox_order_details_url,
            params={"order_id": order_id},
            headers=self._auth_headers(),
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def modify_order(self, order_id: str, price: float, quantity: int | None = None,
                     order_type: str = "LIMIT") -> dict:
        payload: dict = {"order_id": order_id, "order_type": order_type, "price": price, "validity": "DAY"}
        if quantity is not None:
            payload["quantity"] = quantity
        resp = httpx.put(
            settings.upstox_order_modify_url,
            json=payload,
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def cancel_order(self, order_id: str) -> dict:
        resp = httpx.delete(
            settings.upstox_order_cancel_url,
            params={"order_id": order_id},
            headers=self._auth_headers(),
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def funds(self) -> dict:
        resp = httpx.get(
            "https://api.upstox.com/v2/user/get-funds-and-margin",
            params={"segment": "SEC"},
            headers=self._auth_headers(),
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def profile(self) -> dict:
        resp = httpx.get(
            "https://api.upstox.com/v2/user/profile",
            headers=self._auth_headers(),
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def token_expiry_ist(self) -> datetime | None:
        return _jwt_expiry_ist(self._access_token) if self._access_token else None

    def positions(self) -> list[dict]:
        resp = httpx.get(settings.upstox_positions_url, headers=self._auth_headers(), timeout=10.0)
        resp.raise_for_status()
        return resp.json().get("data", [])

    def live_price_feed(self, keys: list[str], on_tick: Callable[[str, float, float], None]):
        raise NotImplementedError("Use app.upstox_feed.UpstoxFeed directly (async, managed by main.py lifespan)")
