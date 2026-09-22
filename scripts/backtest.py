"""Backtest the 10 AM NIFTY short strangle over a historical date range.

APPROXIMATION, not a precise replay: Upstox does not retain historical
premiums for expired option contracts, so each leg's premium is priced with
Black-Scholes off the real historical NIFTY spot path (1-minute candles) using
a flat, configurable implied-vol assumption. Real IV moves (especially around
events) aren't captured. Weekly/monthly expiry *dates* are inferred from a
configurable expiry weekday rather than resolved from a historical instrument
master (which doesn't exist for expired contracts). Treat output as
directional, not as a P&L guarantee.

Usage:
  .venv/Scripts/python.exe scripts/backtest.py --from 2025-06-01 --to 2025-08-31
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.broker import get_broker
from app.config import settings
from app.strategy.charges import LegFill, compute_charges

IST = timezone(timedelta(hours=5, minutes=30))

TRADING_START = (10, 0)
EOD = (15, 20)
RISK_FREE_RATE = 0.07


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, sigma: float, option_type: str, r: float = RISK_FREE_RATE) -> float:
    if t_years <= 0:
        return max(0.0, (spot - strike) if option_type == "CE" else (strike - spot))
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / (sigma * math.sqrt(t_years))
    d2 = d1 - sigma * math.sqrt(t_years)
    if option_type == "CE":
        return spot * _norm_cdf(d1) - strike * math.exp(-r * t_years) * _norm_cdf(d2)
    return strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def round_ce(prev_high: float) -> int:
    return int(math.ceil(prev_high / 50.0) * 50)


def round_pe(prev_low: float) -> int:
    return int(math.floor(prev_low / 50.0) * 50)


def next_weekday_on_or_after(d: date, weekday: int) -> date:
    delta = (weekday - d.weekday()) % 7
    return d + timedelta(days=delta)


def month_end_weekday(d: date, weekday: int) -> date:
    if d.month == 12:
        next_month = date(d.year + 1, 1, 1)
    else:
        next_month = date(d.year, d.month + 1, 1)
    last_day = next_month - timedelta(days=1)
    delta = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=delta)


def candles_to_minute_series(candles: list) -> list[tuple[datetime, float]]:
    """Upstox candle row: [ts_iso, open, high, low, close, volume, oi].

    Timestamps come back tz-aware in IST. They are normalised to *naive* IST
    here so every datetime in this script lives in one representation —
    mixing naive and aware raises on the first subtraction, and silently
    shifts by 5.5 hours if you paper over it with a bare replace().
    """
    out = []
    for row in candles:
        raw = row[0]
        if isinstance(raw, str):
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            ts = raw
        if ts.tzinfo is not None:
            ts = ts.astimezone(IST).replace(tzinfo=None)
        out.append((ts, float(row[4])))  # close
    out.sort(key=lambda x: x[0])
    return out


def simulate_day(
    prev_high: float,
    prev_low: float,
    minute_series: list[tuple[datetime, float]],
    expiry: date,
    sigma: float,
    lot_size: int,
    lots: int,
    target: float,
    sl: float,
) -> dict | None:
    if not minute_series or expiry == minute_series[0][0].date():
        return {"skipped": "0-DTE"}

    entry_pts = [p for p in minute_series if (p[0].hour, p[0].minute) >= TRADING_START]
    if not entry_pts:
        return None
    entry_ts, entry_spot = entry_pts[0]

    ce_strike = round_ce(prev_high)
    pe_strike = round_pe(prev_low)

    def t_years(ts: datetime) -> float:
        expiry_dt = datetime.combine(expiry, datetime.min.time()).replace(hour=15, minute=30)
        return max((expiry_dt - ts).total_seconds(), 0) / (365 * 24 * 3600)

    ce_entry = bs_price(entry_spot, ce_strike, t_years(entry_ts), sigma, "CE")
    pe_entry = bs_price(entry_spot, pe_strike, t_years(entry_ts), sigma, "PE")
    qty = lot_size * lots

    exit_ts, exit_spot, exit_reason = entry_ts, entry_spot, "EOD"
    for ts, spot in entry_pts:
        if (ts.hour, ts.minute) > EOD:
            break
        ce_now = bs_price(spot, ce_strike, t_years(ts), sigma, "CE")
        pe_now = bs_price(spot, pe_strike, t_years(ts), sigma, "PE")
        pnl = (ce_entry - ce_now) * qty + (pe_entry - pe_now) * qty
        exit_ts, exit_spot = ts, spot
        if pnl >= target:
            exit_reason = "TARGET"
            break
        if pnl <= -sl:
            exit_reason = "SL"
            break

    ce_exit = bs_price(exit_spot, ce_strike, t_years(exit_ts), sigma, "CE")
    pe_exit = bs_price(exit_spot, pe_strike, t_years(exit_ts), sigma, "PE")
    gross = (ce_entry - ce_exit) * qty + (pe_entry - pe_exit) * qty

    legs = [
        LegFill("SELL", ce_entry, qty),
        LegFill("SELL", pe_entry, qty),
        LegFill("BUY", ce_exit, qty),
        LegFill("BUY", pe_exit, qty),
    ]
    charges = compute_charges(legs)
    net = gross - charges["total_charges"]

    return {
        "ce_strike": ce_strike,
        "pe_strike": pe_strike,
        "expiry": expiry.isoformat(),
        "exit_reason": exit_reason,
        "gross_pnl": round(gross, 2),
        **charges,
        "net_pnl": round(net, 2),
    }


def run_backtest(from_date: date, to_date: date, sigma: float, lot_size: int, lots: int, target: float, sl: float,
                  weekly_weekday: int, monthly_weekday: int):
    broker = get_broker()
    if not broker.is_authenticated():
        print(f"NOT AUTHENTICATED. Log in first:\n{broker.login_url()}")
        sys.exit(1)

    daily_candles = broker.historical_candles(settings.index_key, "day", to_date.isoformat(), from_date.isoformat())
    daily_candles.sort(key=lambda r: r[0])
    if len(daily_candles) < 2:
        print("Not enough historical daily candles returned for this range.")
        return

    results = {"weekly": [], "monthly": []}

    for i in range(1, len(daily_candles)):
        prev_row = daily_candles[i - 1]
        cur_row = daily_candles[i]
        prev_high, prev_low = float(prev_row[2]), float(prev_row[3])
        cur_date = datetime.fromisoformat(cur_row[0].replace("Z", "+00:00")).date()

        try:
            intraday = broker.historical_candles(settings.index_key, "1minute", cur_date.isoformat(), cur_date.isoformat())
        except Exception as e:
            print(f"{cur_date}: intraday fetch failed ({e}), skipping")
            continue
        minute_series = candles_to_minute_series(intraday)
        if not minute_series:
            continue

        weekly_expiry = next_weekday_on_or_after(cur_date, weekly_weekday)
        monthly_expiry = month_end_weekday(cur_date, monthly_weekday)
        if monthly_expiry < cur_date:
            monthly_expiry = month_end_weekday(cur_date.replace(day=28) + timedelta(days=4), monthly_weekday)

        for style, expiry in (("weekly", weekly_expiry), ("monthly", monthly_expiry)):
            res = simulate_day(prev_high, prev_low, minute_series, expiry, sigma, lot_size, lots, target, sl)
            if res is None:
                continue
            res["date"] = cur_date.isoformat()
            results[style].append(res)

    _report(results)


def _report(results: dict) -> None:
    for style, rows in results.items():
        rows = [r for r in rows if "skipped" not in r]
        if not rows:
            print(f"\n=== {style.upper()} — no trades ===")
            continue

        total_gross = sum(r["gross_pnl"] for r in rows)
        total_charges = sum(r["total_charges"] for r in rows)
        total_net = sum(r["net_pnl"] for r in rows)
        wins = sum(1 for r in rows if r["net_pnl"] > 0)

        print(f"\n=== {style.upper()} — {len(rows)} trading days ===")
        print(f"Gross: {total_gross:,.2f}  Charges: {total_charges:,.2f}  Net: {total_net:,.2f}")
        print(f"Win rate: {wins}/{len(rows)} ({100*wins/len(rows):.1f}%)")

        by_month = defaultdict(list)
        for r in rows:
            by_month[r["date"][:7]].append(r)
        print("Monthly net P&L:")
        for ym, month_rows in sorted(by_month.items()):
            m_net = sum(r["net_pnl"] for r in month_rows)
            print(f"  {ym}: {m_net:,.2f} ({len(month_rows)} days)")

    print("\nReminder: premiums are Black-Scholes estimates off real spot history with a flat IV")
    print("assumption — not real historical option prices. Use directionally, not as a guarantee.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD")
    p.add_argument("--to", dest="to_date", required=True, help="YYYY-MM-DD")
    p.add_argument("--sigma", type=float, default=0.13, help="flat IV assumption, default 0.13 (13%%)")
    p.add_argument("--lot-size", type=int, default=75)
    p.add_argument("--lots", type=int, default=1)
    p.add_argument("--target", type=float, default=3000.0)
    p.add_argument("--sl", type=float, default=3000.0)
    p.add_argument("--weekly-weekday", type=int, default=3, help="0=Mon..6=Sun, default 3=Thursday")
    p.add_argument("--monthly-weekday", type=int, default=3)
    args = p.parse_args()

    run_backtest(
        date.fromisoformat(args.from_date),
        date.fromisoformat(args.to_date),
        args.sigma,
        args.lot_size,
        args.lots,
        args.target,
        args.sl,
        args.weekly_weekday,
        args.monthly_weekday,
    )


if __name__ == "__main__":
    main()
