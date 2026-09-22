"""Realistic intraday option-selling charges. Rates are approximations of
typical discount-broker/exchange rates as of 2025 — check periodically since
STT/exchange/SEBI rates do get revised by regulators."""
from __future__ import annotations

from dataclasses import dataclass

BROKERAGE_FLAT = 20.0
BROKERAGE_PCT = 0.0003  # 0.03%
STT_SELL_PCT = 0.001  # 0.1% on sell-side premium
EXCHANGE_TXN_PCT = 0.0003503  # ~NSE F&O options
GST_PCT = 0.18  # on brokerage + exchange txn + SEBI charges
STAMP_DUTY_BUY_PCT = 0.00003  # 0.003% on buy-side premium
SEBI_PCT = 0.0000001  # ₹10 per crore of turnover


@dataclass
class LegFill:
    side: str  # "SELL" | "BUY"
    price: float
    qty: int


def _brokerage(turnover: float) -> float:
    return min(BROKERAGE_FLAT, turnover * BROKERAGE_PCT)


def compute_charges(legs: list[LegFill]) -> dict:
    """legs = the 4 fills of one closed strangle (SELL CE, SELL PE, BUY CE, BUY PE).
    Returns {gross_pnl_before_charges is NOT computed here (caller does that),
    brokerage, stt, exchange_txn, gst, stamp_duty, sebi, total_charges}."""
    brokerage = 0.0
    stt = 0.0
    exchange_txn = 0.0
    stamp_duty = 0.0
    sebi = 0.0

    for leg in legs:
        turnover = leg.price * leg.qty
        brokerage += _brokerage(turnover)
        exchange_txn += turnover * EXCHANGE_TXN_PCT
        sebi += turnover * SEBI_PCT
        if leg.side == "SELL":
            stt += turnover * STT_SELL_PCT
        else:
            stamp_duty += turnover * STAMP_DUTY_BUY_PCT

    gst = (brokerage + exchange_txn + sebi) * GST_PCT
    total = brokerage + stt + exchange_txn + gst + stamp_duty + sebi

    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exchange_txn": round(exchange_txn, 2),
        "gst": round(gst, 2),
        "stamp_duty": round(stamp_duty, 2),
        "sebi": round(sebi, 2),
        "total_charges": round(total, 2),
    }
