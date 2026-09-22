"""Append-only audit trail of everything consequential the app does.

Separate from the python logger on purpose: this is queryable structured
history the dashboard reads back, and it must survive restarts. Every order
placed, blocked, or failed lands here, as does every kill-switch flip.
"""
from __future__ import annotations

import logging

from app.config import DATA_DIR
from app.storage import append_jsonl, read_jsonl
from app.timeutil import ist_now

logger = logging.getLogger("audit")

ACTIVITY_FILE = DATA_DIR / "activity.jsonl"


def log_event(event: str, **fields) -> None:
    """Best-effort — auditing must never break the thing it's auditing."""
    record = {"ts": ist_now().isoformat(timespec="seconds"), "event": event, **fields}
    try:
        append_jsonl(ACTIVITY_FILE, record)
    except Exception as e:
        logger.warning("failed writing audit event %s: %s", event, e)


def read_events(limit: int = 50) -> list[dict]:
    """Newest first."""
    events = read_jsonl(ACTIVITY_FILE, limit=max(limit * 2, 200))
    return list(reversed(events))[:limit]
