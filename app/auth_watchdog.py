"""Keeps the broker session alive without manual intervention.

Polls the broker's auth state; the moment there's no valid token (fresh
start, or the ~03:30 IST daily expiry) it runs the automated login. Failures
back off exponentially so a broken login page or wrong credential doesn't
hammer Upstox — and the manual /api/auth/login flow always still works as
the fallback.
"""
from __future__ import annotations

import asyncio
import logging

from app import auto_login
from app.config import settings
from app.price_cache import cache

logger = logging.getLogger("auth_watchdog")

CHECK_INTERVAL_S = 15.0


class AuthWatchdog:
    def __init__(self, broker) -> None:
        self.broker = broker
        self.last_attempt_ts: float = 0.0
        self.consecutive_failures = 0
        self.last_result: str = "not_attempted"
        self.backoff_s: float = settings.auto_login_retry_s

    def status(self) -> dict:
        return {
            "configured": auto_login.is_configured(),
            "enabled": settings.auto_login_enabled,
            "last_result": self.last_result,
            "consecutive_failures": self.consecutive_failures,
            "next_retry_in_s": max(0, round(self.backoff_s - (asyncio.get_event_loop().time() - self.last_attempt_ts)))
            if self.last_attempt_ts
            else 0,
        }

    async def run_forever(self) -> None:
        if not settings.auto_login_enabled:
            logger.info("auto-login disabled (AUTO_LOGIN_ENABLED=false)")
            return
        if not auto_login.is_configured():
            logger.info("auto-login not configured — set UPSTOX_MOBILE / UPSTOX_PIN / UPSTOX_TOTP_SECRET in .env")
            self.last_result = "not_configured"
            return

        while True:
            try:
                await self._check_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                cache.log_error("auth_watchdog", f"{type(e).__name__}: {e}")
                logger.exception("auth watchdog error")
            await asyncio.sleep(CHECK_INTERVAL_S)

    async def _check_once(self) -> None:
        if self.broker.is_authenticated():
            if self.consecutive_failures:
                self.consecutive_failures = 0
                self.backoff_s = settings.auto_login_retry_s
            return

        loop = asyncio.get_event_loop()
        now = loop.time()
        if self.last_attempt_ts and (now - self.last_attempt_ts) < self.backoff_s:
            return

        self.last_attempt_ts = now
        logger.info("no valid token — starting automated login")
        try:
            ok = await auto_login.perform_login(self.broker)
        except auto_login.AutoLoginNotConfigured as e:
            self.last_result = "not_configured"
            logger.warning("%s", e)
            return
        except Exception as e:
            ok = False
            cache.log_error("auth_watchdog.login", f"{type(e).__name__}: {e}")
            logger.error("automated login raised: %s", e)

        if ok:
            self.last_result = "success"
            self.consecutive_failures = 0
            self.backoff_s = settings.auto_login_retry_s
            logger.info("automated login succeeded")
        else:
            self.last_result = "failed"
            self.consecutive_failures += 1
            self.backoff_s = min(self.backoff_s * 2, settings.auto_login_max_backoff_s)
            cache.log_error(
                "auth_watchdog",
                f"automated login failed (attempt {self.consecutive_failures}), retrying in {self.backoff_s:.0f}s "
                f"— see data/auto_login_debug/ or use /api/auth/login manually",
            )
