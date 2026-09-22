"""NSE trading calendar.

Two bugs in the reference implementation this deliberately avoids:

  * it loaded only the current year's holiday file, once, at import — so a
    process running across New Year silently believed every day was a
    trading day;
  * its market-hours check looked at weekday and clock only, so it happily
    polled the broker all day on Republic Day.

Here holidays are loaded lazily per year and cached per year, so spanning a
year boundary just loads the next file. A missing file degrades to
"weekends only" with a warning rather than pretending to know better.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from app.config import DATA_DIR, ROOT_DIR
from app.storage import load_json
from app.timeutil import ist_now

logger = logging.getLogger("market_calendar")

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

_holiday_cache: dict[int, set[str]] = {}
_warned_years: set[int] = set()


def _holiday_file(year: int):
    # Reference data ships with the code; a user-supplied override in data/
    # wins so the calendar can be corrected without a code change.
    override = DATA_DIR / f"nse_holidays_{year}.json"
    if override.exists():
        return override
    return ROOT_DIR / "reference" / f"nse_holidays_{year}.json"


def holidays_for(year: int) -> set[str]:
    if year in _holiday_cache:
        return _holiday_cache[year]
    raw = load_json(_holiday_file(year), None)
    if not raw:
        if year not in _warned_years:
            logger.warning(
                "no NSE holiday file for %d — treating weekends only as non-trading days. "
                "Add data/nse_holidays_%d.json to fix.", year, year
            )
            _warned_years.add(year)
        _holiday_cache[year] = set()
        return _holiday_cache[year]
    days = {h["date"] if isinstance(h, dict) else str(h) for h in raw.get("holidays", [])}
    _holiday_cache[year] = days
    return days


def is_trading_day(d: date | None = None) -> bool:
    d = d or ist_now().date()
    if d.weekday() >= 5:
        return False
    return d.isoformat() not in holidays_for(d.year)


def prev_trading_day(d: date | None = None) -> date:
    d = d or ist_now().date()
    probe = d - timedelta(days=1)
    for _ in range(14):  # bounded: never loop forever on a bad calendar
        if is_trading_day(probe):
            return probe
        probe -= timedelta(days=1)
    return probe


def is_market_open(now: datetime | None = None) -> bool:
    now = now or ist_now()
    if not is_trading_day(now.date()):
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def session_info() -> dict:
    now = ist_now()
    return {
        "now_ist": now.isoformat(timespec="seconds"),
        "is_trading_day": is_trading_day(now.date()),
        "is_market_open": is_market_open(now),
        "prev_trading_day": prev_trading_day(now.date()).isoformat(),
        "holidays_known_for_year": len(holidays_for(now.year)),
    }
