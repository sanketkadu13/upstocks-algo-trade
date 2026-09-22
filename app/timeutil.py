from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST_OFFSET = timedelta(hours=5, minutes=30)


def ist_now() -> datetime:
    """Wall-clock IST as a naive datetime.

    Naive on purpose: it is compared against naive market times throughout
    (09:15, 15:15...) and mixing the two raises. The UTC read is explicit
    rather than via the deprecated utcnow().
    """
    return datetime.now(timezone.utc).replace(tzinfo=None) + IST_OFFSET


def parse_hhmm(hhmm: str) -> tuple[int, int]:
    h, m = hhmm.split(":")
    return int(h), int(m)
