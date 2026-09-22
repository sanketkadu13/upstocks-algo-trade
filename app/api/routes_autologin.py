from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import auto_login
from app.config import settings

router = APIRouter()


class EnableBody(BaseModel):
    enabled: bool


@router.get("/api/auto-login")
async def status(request: Request):
    broker = request.app.state.broker
    watchdog = getattr(request.app.state, "auth_watchdog", None)
    return {
        "enabled": settings.auto_login_enabled,
        "configured": auto_login.is_configured(),
        "missing": auto_login.missing_fields(),
        "watchdog": watchdog.status() if watchdog else None,
        "broker_authenticated": broker.is_authenticated(),
        "redirect_uri": settings.upstox_redirect_uri,
        "login_url": broker.login_url(),
    }


@router.get("/api/auto-login/totp")
async def current_totp():
    """Current code plus seconds remaining, so it can be compared against an
    authenticator app without spending a login attempt.

    Upstox locks the login after a handful of bad codes, so verifying the
    secret this way first is much cheaper than discovering it's wrong by
    failing real logins.
    """
    if not settings.upstox_totp_secret:
        raise HTTPException(400, "UPSTOX_TOTP_SECRET is not set in .env")
    try:
        code = auto_login._totp_now()
    except Exception as e:
        raise HTTPException(400, f"secret is not valid base32: {e}")
    return {"code": code, "seconds_left": 30 - int(time.time() % 30)}


@router.post("/api/auto-login/test")
async def test_login(request: Request):
    """Run the full automated login once, now, and report what happened."""
    broker = request.app.state.broker
    if not auto_login.is_configured():
        raise HTTPException(400, f"auto-login not configured — missing: {auto_login.missing_fields()}")
    try:
        ok = await auto_login.perform_login(broker, headless=True)
    except auto_login.AutoLoginNotConfigured as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")

    if not ok:
        raise HTTPException(
            502,
            "automated login failed — a screenshot of where it stopped was written to "
            "data/auto_login_debug/. Check that the TOTP code matches your authenticator.",
        )
    return {"ok": True, "authenticated": broker.is_authenticated()}


@router.post("/api/auto-login/enable")
async def set_enabled(body: EnableBody):
    """Runtime toggle. This does not rewrite .env — AUTO_LOGIN_ENABLED there
    remains the value applied on restart."""
    settings.auto_login_enabled = bool(body.enabled)
    return {"enabled": settings.auto_login_enabled, "note": "runtime only; set AUTO_LOGIN_ENABLED in .env to persist"}
