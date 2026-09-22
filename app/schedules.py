"""Daily scheduled jobs: pre-market health check and end-of-day summary.

Each job records the date it last ran and fires once per trading day when the
clock is past its time. That "past its time" rather than "exactly at its
time" matters: the reference implementation only fired inside a narrow
window, so a process started at 09:06 silently skipped that day's 09:00
check with no trace.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date

from app import health_check, market_calendar, notify
from app.config import DATA_DIR
from app.storage import atomic_json_dump, load_json
from app.strategy import ledger
from app.timeutil import ist_now, parse_hhmm

logger = logging.getLogger("schedules")

STATE_FILE = DATA_DIR / "schedule_state.json"

HEALTH_CHECK_TIME = "09:00"
EOD_SUMMARY_TIME = "15:35"
CHECK_INTERVAL_S = 60.0


def _state() -> dict:
    return load_json(STATE_FILE, {}) or {}


def _mark_ran(job: str) -> None:
    s = _state()
    s[job] = date.today().isoformat()
    atomic_json_dump(STATE_FILE, s)


def _already_ran_today(job: str) -> bool:
    return _state().get(job) == date.today().isoformat()


def _past(hhmm: str) -> bool:
    h, m = parse_hhmm(hhmm)
    now = ist_now()
    return (now.hour, now.minute) >= (h, m)


def run_health_check(broker, engine, gateway, announce: bool = True) -> dict:
    result = health_check.run(broker, engine, gateway)
    if announce and notify.is_configured():
        notify.send_async(health_check.format_for_telegram(result))
    logger.info("pre-market check: %s (%d problems)", result["overall"], len(result["problems"]))
    return result


def build_eod_summary() -> str:
    rows = ledger.read_today()
    totals = ledger.totals(rows)
    if not rows:
        return "📊 *EOD* — no trades today."
    lines = [
        f"📊 *EOD summary* — {date.today():%d %b %Y}",
        f"{totals['count']} trade(s)",
    ]
    for r in rows:
        icon = "✅" if r.get("net_pnl", 0) >= 0 else "🔻"
        lines.append(
            f"{icon} {r.get('strategy_name', r.get('strategy_id'))} "
            f"({r.get('exit_reason')}) net ₹{r.get('net_pnl', 0):,.0f}"
        )
    lines.append(f"\ngross ₹{totals['gross_pnl']:,.0f} · charges ₹{totals['total_charges']:,.0f}")
    lines.append(f"*net ₹{totals['net_pnl']:,.0f}*")
    return "\n".join(lines)


async def run_forever(broker, engine, gateway) -> None:
    while True:
        try:
            if market_calendar.is_trading_day():
                if not _already_ran_today("health_check") and _past(HEALTH_CHECK_TIME):
                    _mark_ran("health_check")
                    await asyncio.to_thread(run_health_check, broker, engine, gateway, True)

                if not _already_ran_today("eod_summary") and _past(EOD_SUMMARY_TIME):
                    _mark_ran("eod_summary")
                    notify.send_async(build_eod_summary())
                    logger.info("EOD summary sent")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduled job failed")
        await asyncio.sleep(CHECK_INTERVAL_S)
