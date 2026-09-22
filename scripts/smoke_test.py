"""Proves the live order path before the strategy is ever allowed to run live:
buys 1 lot of a cheap, far-OTM NIFTY weekly option, confirms the fill via
positions/order_status, then squares it off. Places REAL orders with REAL
money — gated behind an explicit flag and a typed confirmation.

Usage:
  .venv/Scripts/python.exe scripts/smoke_test.py --i-understand-this-places-real-orders
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import instruments
from app.broker import get_broker


def pick_cheap_otm_call(broker, spot: float) -> dict:
    """Far OTM (spot + ~500) weekly CE — cheap, low delta, minimal capital at risk."""
    expiries = instruments.option_expiries()
    expiry = expiries["weekly_current"]
    if not expiry:
        raise RuntimeError("no weekly expiry available from instrument master")
    strike = int(round((spot + 500) / 50.0) * 50)
    contract = broker.resolve_option("NIFTY", expiry, strike, "CE")
    return {**contract, "expiry": expiry, "strike": strike}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--i-understand-this-places-real-orders",
        action="store_true",
        dest="confirmed",
        help="Required. Acknowledges this places a real order with real money.",
    )
    args = parser.parse_args()

    if not args.confirmed:
        print("Refusing to run: pass --i-understand-this-places-real-orders to proceed.")
        sys.exit(1)

    broker = get_broker()
    if not broker.is_authenticated():
        print(f"NOT AUTHENTICATED. Log in first:\n{broker.login_url()}")
        sys.exit(1)

    spot = broker.index_ltp("NIFTY")
    if not spot:
        print("Could not fetch NIFTY spot — aborting.")
        sys.exit(1)

    contract = pick_cheap_otm_call(broker, spot)
    print(f"NIFTY spot: {spot}")
    print(f"Target contract: {contract['tradingsymbol']} ({contract['instrument_key']}), lot={contract['lot_size']}")

    typed = input(f"Type the strike '{contract['strike']}' to confirm placing 1 real BUY MARKET lot: ")
    if typed.strip() != str(contract["strike"]):
        print("Confirmation text mismatch, aborting.")
        sys.exit(1)

    qty = contract["lot_size"]
    print(f"\nPlacing BUY MARKET order, qty={qty}...")
    buy_order = broker.place_order(contract["instrument_key"], qty, "BUY")
    order_id = buy_order.get("order_id")
    print(f"Order placed: {buy_order}")

    print("Polling order status for fill confirmation...")
    for _ in range(10):
        status = broker.order_status(order_id)
        print(f"  status={status.get('status')} avg_price={status.get('average_price')}")
        if status.get("status") in ("complete", "COMPLETE"):
            break
        time.sleep(1)

    positions = broker.positions()
    matched = [p for p in positions if p.get("instrument_token") == contract["instrument_key"]]
    print(f"Position after buy: {matched}")

    print(f"\nPlacing SELL MARKET order to square off, qty={qty}...")
    sell_order = broker.place_order(contract["instrument_key"], qty, "SELL")
    print(f"Square-off order placed: {sell_order}")

    order_id2 = sell_order.get("order_id")
    for _ in range(10):
        status = broker.order_status(order_id2)
        print(f"  status={status.get('status')} avg_price={status.get('average_price')}")
        if status.get("status") in ("complete", "COMPLETE"):
            break
        time.sleep(1)

    print("\nSmoke test complete. Verify the position is flat via broker.positions() / the Upstox app.")


if __name__ == "__main__":
    main()
