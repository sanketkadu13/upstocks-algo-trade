"""Pre-market readiness check.

Answers one question before the session starts: is there anything that would
stop this app trading correctly today? Each check returns ok/warn/fail with a
human-readable line, so the answer is actionable rather than a stack trace at
09:33.
"""
from __future__ import annotations

import logging
import time
from datetime import date

from app import instruments, market_calendar, notify
from app.config import settings
from app.price_cache import cache
from app.timeutil import ist_now

logger = logging.getLogger("health_check")

OK, WARN, FAIL = "ok", "warn", "fail"

_last_result: dict | None = None


def _check(name: str, level: str, detail: str) -> dict:
    return {"name": name, "level": level, "detail": detail}


def run(broker, engine, gateway) -> dict:
    checks: list[dict] = []
    now = ist_now()

    # --- session ---------------------------------------------------------
    if market_calendar.is_trading_day(now.date()):
        checks.append(_check("Trading day", OK, now.strftime("%A %d %b %Y")))
    else:
        checks.append(_check("Trading day", WARN, f"{now:%A %d %b} is a holiday or weekend — no trading today"))

    known = market_calendar.holidays_for(now.year)
    if known:
        checks.append(_check("Holiday calendar", OK, f"{len(known)} holidays loaded for {now.year}"))
    else:
        checks.append(_check("Holiday calendar", WARN,
                             f"no holiday file for {now.year} — weekends only. Add data/nse_holidays_{now.year}.json"))

    # --- broker session --------------------------------------------------
    if broker.is_authenticated():
        expiry = getattr(broker, "token_expiry_ist", lambda: None)()
        if expiry:
            hours = (expiry - now).total_seconds() / 3600
            level = OK if hours > 6 else WARN
            checks.append(_check("Broker token", level, f"valid until {expiry:%d %b %H:%M} IST ({hours:.1f}h left)"))
        else:
            checks.append(_check("Broker token", OK, "authenticated"))
    else:
        checks.append(_check("Broker token", FAIL, "not authenticated — log in at /api/auth/login"))

    try:
        profile = broker.profile()
        checks.append(_check("Account", OK, f"{profile.get('user_name') or profile.get('user_id') or 'connected'}"))
    except Exception as e:
        checks.append(_check("Account", WARN, f"profile lookup failed: {e}"))

    # --- funds -----------------------------------------------------------
    try:
        funds = broker.funds()
        equity = funds.get("equity") or {}
        available = equity.get("available_margin")
        if available is None:
            # Upstox shapes vary by account; surface whatever we got.
            checks.append(_check("Funds", WARN, f"could not read available margin from {list(funds)[:3]}"))
        else:
            need = _estimated_margin(engine)
            level = OK if available >= need else WARN
            checks.append(
                _check("Funds", level,
                       f"₹{available:,.0f} available; roughly ₹{need:,.0f} needed for armed strategies")
            )
    except Exception as e:
        checks.append(_check("Funds", WARN, f"funds lookup failed: {e}"))

    # --- market data -----------------------------------------------------
    spot = cache.get(settings.index_key)
    if spot and not spot["stale"]:
        checks.append(_check("NIFTY feed", OK, f"{spot['ltp']:.2f} ({spot['source']}, {spot['age_s']:.0f}s old)"))
    elif spot:
        checks.append(_check("NIFTY feed", WARN, f"last tick {spot['age_s']:.0f}s old — stale"))
    else:
        checks.append(_check("NIFTY feed", FAIL, "no NIFTY price at all"))

    subscribed = cache.subscribed_keys()
    stale = cache.stale_or_missing(subscribed, settings.tick_stale_after_s)
    if not stale:
        checks.append(_check("Subscriptions", OK, f"{len(subscribed)} instruments, all fresh"))
    else:
        checks.append(_check("Subscriptions", WARN, f"{len(stale)}/{len(subscribed)} instruments stale or unpriced"))

    # --- instrument master ------------------------------------------------
    try:
        master = instruments.load()
        age_h = (time.time() - master.get("downloaded_at", 0)) / 3600
        level = OK if age_h < 24 else WARN
        checks.append(_check("Instrument master", level,
                             f"{len(master.get('nifty_options', []))} option contracts, "
                             f"{len(master.get('equities', []))} equities, {age_h:.1f}h old"))
    except Exception as e:
        checks.append(_check("Instrument master", FAIL, f"unavailable: {e}"))

    # --- strategy readiness -----------------------------------------------
    pr = engine.ensure_prev_range()
    if pr.get("high") is not None:
        checks.append(_check("Previous-day range", OK,
                             f"{pr['date']}: H {pr['high']:.2f} / L {pr['low']:.2f} ({pr['source']})"))
    else:
        checks.append(_check("Previous-day range", FAIL, "unavailable — strikes cannot be computed"))

    armed = [s for s in engine.strategies.values()
             if s.get("auto_entry_enabled") and s.get("auto_entry_last_fired") != date.today().isoformat()]
    if armed:
        detail = ", ".join(f"{s['name']} at {s['auto_entry_time']} ({s['mode']})" for s in armed)
        level = FAIL if gateway.killed else OK
        checks.append(_check("Auto-entry armed", level,
                             detail + (" — BUT THE KILL SWITCH IS ARMED" if gateway.killed else "")))
    else:
        checks.append(_check("Auto-entry armed", OK, "nothing scheduled to fire today"))

    live = [sid for sid, rt in engine.runtimes.items() if rt.status == "live" and rt.legs]
    if live:
        checks.append(_check("Open positions", WARN, f"already holding: {', '.join(live)}"))

    if gateway.killed:
        checks.append(_check("Kill switch", WARN, "ARMED — no orders can be placed"))
    else:
        checks.append(_check("Kill switch", OK, "released — orders allowed"))

    live_mode = [s["name"] for s in engine.strategies.values() if s.get("mode") == "live"]
    if live_mode:
        checks.append(_check("Live-money strategies", WARN, ", ".join(live_mode)))

    # --- alerting ---------------------------------------------------------
    if notify.is_configured():
        checks.append(_check("Telegram alerts", OK, "configured"))
    else:
        checks.append(_check("Telegram alerts", WARN, "not configured — you will not be alerted on entry/exit"))

    worst = FAIL if any(c["level"] == FAIL for c in checks) else (
        WARN if any(c["level"] == WARN for c in checks) else OK)

    result = {
        "ran_at": now.isoformat(timespec="seconds"),
        "overall": worst,
        "checks": checks,
        "problems": [c for c in checks if c["level"] != OK],
    }

    global _last_result
    _last_result = result
    return result


def _estimated_margin(engine) -> float:
    """Rough SPAN+exposure per short option leg. Deliberately approximate —
    it exists to catch 'nowhere near enough funds', not to be exact."""
    per_leg = 80_000.0
    armed = [s for s in engine.strategies.values() if s.get("auto_entry_enabled")]
    return sum(per_leg * 2 * int(s.get("lots", 1)) for s in armed)


def last_result() -> dict | None:
    return _last_result


def format_for_telegram(result: dict) -> str:
    icon = {OK: "✅", WARN: "⚠️", FAIL: "❌"}
    lines = [f"{icon[result['overall']]} *Pre-market check* — {result['ran_at'][11:16]} IST"]
    for c in result["checks"]:
        lines.append(f"{icon[c['level']]} {c['name']}: {c['detail']}")
    return "\n".join(lines)
