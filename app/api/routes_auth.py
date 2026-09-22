from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()


@router.get("/api/auth/login")
async def login(request: Request):
    broker = request.app.state.broker
    return RedirectResponse(broker.login_url())


@router.get("/api/auth/callback")
async def callback(request: Request, code: str | None = None, error: str | None = None):
    if error:
        return HTMLResponse(f"<p>Upstox auth error: {error}</p>", status_code=400)
    if not code:
        return HTMLResponse("<p>Missing ?code from Upstox redirect</p>", status_code=400)
    broker = request.app.state.broker
    try:
        broker.exchange_code(code)
    except Exception as e:
        return HTMLResponse(f"<p>Token exchange failed: {e}</p>", status_code=500)
    return RedirectResponse("/")
