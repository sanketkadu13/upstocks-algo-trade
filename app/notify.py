"""Telegram alerting.

Credentials are read from the environment on every send so they can be
changed without a restart. Sending is always best-effort: an alerting failure
must never propagate into the trading path.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import httpx

from app.config import DATA_DIR
from app.storage import atomic_json_dump, load_json

logger = logging.getLogger("notify")

CONFIG_FILE = DATA_DIR / "telegram.json"
API = "https://api.telegram.org/bot{token}/{method}"

_lock = threading.Lock()
_last_error: str | None = None
_sent_count = 0


def _creds() -> tuple[str | None, str | None]:
    """File config wins over env, so the dashboard can set it at runtime."""
    cfg = load_json(CONFIG_FILE, {}) or {}
    token = cfg.get("token") or os.getenv("TELEGRAM_TOKEN") or None
    chat_id = cfg.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID") or None
    return token, chat_id


def is_configured() -> bool:
    token, chat_id = _creds()
    return bool(token and chat_id)


def save_config(token: str | None, chat_id: str | None, clear: bool = False) -> dict:
    if clear:
        atomic_json_dump(CONFIG_FILE, {})
        return {"configured": False}
    current = load_json(CONFIG_FILE, {}) or {}
    # A blank token means "keep the existing one" so the UI can show a mask
    # without ever round-tripping the secret through the browser.
    if token:
        current["token"] = token.strip()
    if chat_id:
        current["chat_id"] = str(chat_id).strip()
    atomic_json_dump(CONFIG_FILE, current)
    return {"configured": is_configured()}


def status() -> dict:
    token, chat_id = _creds()
    return {
        "configured": bool(token and chat_id),
        "chat_id": chat_id,
        "token_hint": (token[:6] + "…" + token[-4:]) if token else None,
        "sent": _sent_count,
        "last_error": _last_error,
    }


def send(text: str, markdown: bool = True) -> bool:
    """Returns True if Telegram accepted the message."""
    global _last_error, _sent_count
    token, chat_id = _creds()
    if not token or not chat_id:
        return False

    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if markdown:
        payload["parse_mode"] = "Markdown"

    try:
        resp = httpx.post(API.format(token=token, method="sendMessage"), json=payload, timeout=10.0)
        if resp.status_code == 400 and markdown:
            # Almost always an unescaped _ or * in an instrument symbol.
            # Losing the alert matters more than losing the formatting.
            return send(text, markdown=False)
        resp.raise_for_status()
        with _lock:
            _sent_count += 1
            _last_error = None
        return True
    except Exception as e:
        with _lock:
            _last_error = f"{type(e).__name__}: {e}"
        logger.warning("telegram send failed: %s", e)
        return False


def send_async(text: str) -> None:
    """Fire-and-forget from inside trading code paths."""
    threading.Thread(target=send, args=(text,), daemon=True).start()


# -- event helpers ----------------------------------------------------------

def notify_entry(strategy_name: str, mode: str, legs: list[dict], expiry: str) -> None:
    lines = [f"*ENTRY* — {strategy_name} ({mode.upper()})", f"expiry {expiry}"]
    credit = 0.0
    for leg in legs:
        lines.append(f"  SELL {leg['symbol']} x{abs(leg['qty'])} @ {leg['avg_entry']:.2f}")
        credit += leg["avg_entry"] * abs(leg["qty"])
    lines.append(f"credit ₹{credit:,.0f}")
    send_async("\n".join(lines))


def notify_exit(strategy_name: str, mode: str, reason: str, gross: float, charges: float, net: float) -> None:
    emoji = "✅" if net >= 0 else "🔻"
    send_async(
        f"{emoji} *EXIT* — {strategy_name} ({mode.upper()})\n"
        f"reason: {reason}\n"
        f"gross ₹{gross:,.0f} · charges ₹{charges:,.0f}\n"
        f"*net ₹{net:,.0f}*"
    )


def notify_problem(where: str, detail: str) -> None:
    send_async(f"⚠️ *{where}*\n{detail}")


def notify_critical(where: str, detail: str) -> None:
    send_async(f"🚨 *{where}*\n{detail}\n\n_Needs manual attention._")
