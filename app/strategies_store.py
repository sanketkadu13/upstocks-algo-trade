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
    "mode": "paper",  # paper | live
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


def validate_patch(patch: dict) -> dict:
    """Whitelist + coerce. Unknown keys are dropped rather than persisted."""
    out: dict[str, Any] = {}
    for key, value in patch.items():
        if key not in DEFAULTS:
            continue
        default = DEFAULTS[key]
        try:
            if isinstance(default, bool):
                out[key] = bool(value)
            elif isinstance(default, int) and not isinstance(default, bool):
                out[key] = int(value)
            elif isinstance(default, float):
                out[key] = float(value)
            else:
                out[key] = str(value).strip()
        except (TypeError, ValueError):
            continue

    if "mode" in out and out["mode"] not in ("paper", "live"):
        out.pop("mode")
    if "kind" in out and out["kind"] not in STRATEGY_KINDS:
        out.pop("kind")
    for basis_key in ("profit_target_basis", "loss_limit_basis"):
        if basis_key in out and out[basis_key] not in ("ltp", "exit"):
            out.pop(basis_key)
    for time_key in ("auto_entry_time", "eod_squareoff_time"):
        if time_key in out:
            try:
                h, m = out[time_key].split(":")
                if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                    raise ValueError
            except Exception:
                out.pop(time_key)
    return out
