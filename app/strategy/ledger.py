"""Append-only trade ledger. Every closed trade recorded with full charge
breakdown -> gross, charges, net. Read helpers reconstruct today's view."""
from __future__ import annotations

import json
import threading
from datetime import date

from app.config import TAP_LEDGER_FILE

_lock = threading.Lock()


def append(record: dict) -> None:
    with _lock:
        with open(TAP_LEDGER_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


def read_all() -> list[dict]:
    if not TAP_LEDGER_FILE.exists():
        return []
    with _lock:
        lines = TAP_LEDGER_FILE.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def read_today() -> list[dict]:
    today = date.today().isoformat()
    return [r for r in read_all() if r.get("date") == today]


def totals(records: list[dict]) -> dict:
    gross = sum(r.get("gross_pnl", 0.0) for r in records)
    charges = sum(r.get("total_charges", 0.0) for r in records)
    net = sum(r.get("net_pnl", 0.0) for r in records)
    return {
        "count": len(records),
        "gross_pnl": round(gross, 2),
        "total_charges": round(charges, 2),
        "net_pnl": round(net, 2),
    }
