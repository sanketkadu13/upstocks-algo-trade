"""Persistence and shape for the multi-strategy model.

Replaces the single hardcoded strangle config: the app now holds N named
strategies, each with its own legs, targets, trailing stop and auto-entry
schedule. Only declared config fields are persisted — live runtime values
(MTM, peaks, positions) stay in memory and are rebuilt from the broker, so a
stale file can never resurrect a position that isn't really there.
"""
from __future__ import annotations

import threading
from typing import Any

from app.config import DATA_DIR
from app.storage import atomic_json_dump, load_json
from app.timeutil import ist_now

STRATEGIES_FILE = DATA_DIR / "strategies.json"

# Exit triggers, in the order they're evaluated.
EXIT_REASONS = (
    "PROFIT_TARGET",
    "LOSS_LIMIT",
    "TRAILING_SL",
    "PROFIT_LOCK",
    "EOD_SQUAREOFF",
    "MANUAL",
    "EXTERNAL",
)

STRATEGY_KINDS = ("strangle_prev_day_range", "manual_legs")

DEFAULTS: dict[str, Any] = {
    "name": "10 AM Strangle",
    "kind": "strangle_prev_day_range",
    "order": 0,
    "enabled": True,
    # paper   — simulated fills, no broker orders
    # live    — real orders (requires the request IP to be whitelisted with Upstox)
    # monitor — track a position you opened yourself; alerts on triggers but
    #           never places an order. The honest mode when order placement
    #           isn't possible from where this is running.
    "mode": "paper",
    "lots": 1,
    # Targets. Each side can be disabled independently, and each can be judged
    # on LTP or on the realistic exit price (see basis note below).
    "profit_target": 3000.0,
    "profit_target_enabled": True,
    "profit_target_basis": "exit",
    "loss_limit": 3000.0,
    "loss_limit_enabled": True,
    "loss_limit_basis": "exit",
    # Trailing stop: arms once P&L reaches activate_at, then follows the peak
    # down by trail_by.
    "trail_enabled": False,
    "trail_activate_at": 1500.0,
    "trail_by": 750.0,
    # Profit lock: a one-way floor. Once P&L touches the trigger, the floor is
    # pinned and never lowered again.
    "lock_profit_enabled": False,
    "lock_profit_trigger": 2000.0,
    "lock_profit_lock_at": 1000.0,
    # Auto entry
    "auto_entry_enabled": False,
    "auto_entry_time": "09:33",
    "auto_entry_expiry": "weekly_current",
    "auto_entry_grace_minutes": 10,
    # Skip rules
    "skip_expiry_day": True,
    "vix_max": 20.0,
    # Intraday square-off. Upstox squares intraday positions off on its own
    # schedule; exiting before that keeps price control in our hands.
    "eod_squareoff_enabled": True,
    "eod_squareoff_time": "15:15",
}

# Anything not in here is runtime-only and deliberately not written to disk.
_PERSISTED_FIELDS = set(DEFAULTS) | {"id", "created_at", "auto_entry_last_fired", "legs"}

_lock = threading.RLock()


def new_strategy(sid: str, **overrides) -> dict:
    s = dict(DEFAULTS)
    s.update(
        {
            "id": sid,
            "created_at": ist_now().isoformat(timespec="seconds"),
            "auto_entry_last_fired": None,
            "legs": [],  # resolved instrument legs, set on entry
        }
    )
    s.update({k: v for k, v in overrides.items() if k in _PERSISTED_FIELDS})
    return s


def load_all() -> dict[str, dict]:
    with _lock:
        raw = load_json(STRATEGIES_FILE, {}) or {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict] = {}
        for sid, data in raw.items():
            if not isinstance(data, dict):
                continue
            # Overlay stored values on current defaults so adding a new config
            # field never breaks an existing install.
            merged = dict(DEFAULTS)
            merged.update({k: v for k, v in data.items() if k in _PERSISTED_FIELDS})
            merged["id"] = sid
            merged.setdefault("legs", [])
            out[sid] = merged
        return out


def save_all(strategies: dict[str, dict]) -> None:
    with _lock:
        to_write = {
            sid: {k: v for k, v in s.items() if k in _PERSISTED_FIELDS}
            for sid, s in strategies.items()
        }
        atomic_json_dump(STRATEGIES_FILE, to_write)


def next_sid(existing: dict[str, dict]) -> str:
    n = 1
    while f"s{n}" in existing:
        n += 1
    return f"s{n}"


def normalize_time(value: Any) -> str | None:
    """Accept what a browser time input may send and return canonical HH:MM.

    <input type="time"> yields "HH:MM" normally but "HH:MM:SS" once a step is
    involved, and browsers differ. Rejecting the seconds form silently dropped
    the field, so the save appeared to work and the time snapped back.
    """
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def validate_patch(patch: dict) -> tuple[dict, dict[str, str]]:
    """Whitelist + coerce.

    Returns (clean, rejected). Rejected entries are reported rather than
    dropped in silence — a setting that vanishes on save with no explanation
    is worse than an error, because you believe the new value took effect.
    """
    out: dict[str, Any] = {}
    rejected: dict[str, str] = {}

    for key, value in patch.items():
        if key not in DEFAULTS:
            rejected[key] = "unknown setting"
            continue
        default = DEFAULTS[key]
        try:
            if isinstance(default, bool):
                out[key] = bool(value)
            elif isinstance(default, int) and not isinstance(default, bool):
                if str(value).strip() == "":
                    raise ValueError("empty")
                out[key] = int(float(value))     # "2.0" from a number input
            elif isinstance(default, float):
                if str(value).strip() == "":
                    raise ValueError("empty")
                out[key] = float(str(value).replace(",", ""))   # "1,800"
            else:
                out[key] = str(value).strip()
        except (TypeError, ValueError):
            rejected[key] = f"{value!r} is not a valid {type(default).__name__}"

    if "mode" in out and out["mode"] not in ("paper", "live", "monitor"):
        rejected["mode"] = f"{out.pop('mode')!r} must be paper, live or monitor"
    if "kind" in out and out["kind"] not in STRATEGY_KINDS:
        rejected["kind"] = f"{out.pop('kind')!r} is not a known strategy kind"
    for basis_key in ("profit_target_basis", "loss_limit_basis"):
        if basis_key in out and out[basis_key] not in ("ltp", "exit"):
            rejected[basis_key] = f"{out.pop(basis_key)!r} must be ltp or exit"

    for time_key in ("auto_entry_time", "eod_squareoff_time"):
        if time_key in out:
            canonical = normalize_time(out[time_key])
            if canonical is None:
                rejected[time_key] = f"{out.pop(time_key)!r} is not a valid HH:MM time"
            else:
                out[time_key] = canonical

    for positive_key in ("lots", "profit_target", "loss_limit"):
        if positive_key in out and out[positive_key] <= 0:
            rejected[positive_key] = f"{out.pop(positive_key)} must be greater than zero"

    return out, rejected
