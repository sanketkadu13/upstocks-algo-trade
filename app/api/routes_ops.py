from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import health_check, market_calendar, notify, schedules

router = APIRouter()

STREAM_INTERVAL_S = 2.0
HEARTBEAT_EVERY = 15  # ticks without a change before we prod the connection


class TelegramConfig(BaseModel):
    token: str | None = None
    chat_id: str | None = None
    clear: bool = False


@router.get("/api/stream")
async def stream(request: Request):
    """Server-sent events carrying the same payload as /api/state.

    Lets the dashboard stay current without every client polling on its own
    timer; the browser reconnects automatically if the stream drops.
    """
    engine = request.app.state.engine

    async def gen():
        last_payload = None
        idle = 0
        while True:
            if await request.is_disconnected():
                break
            try:
                payload = engine.full_state()
                serialized = json.dumps(payload, default=str)
                if serialized != last_payload:
                    last_payload = serialized
                    idle = 0
                    yield f"data: {serialized}\n\n"
                else:
                    idle += 1
                    if idle >= HEARTBEAT_EVERY:
                        idle = 0
                        yield ": heartbeat\n\n"
            except Exception as e:  # never let one bad tick kill the stream
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            await asyncio.sleep(STREAM_INTERVAL_S)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx must not buffer SSE
        },
    )


@router.get("/api/health-check")
async def get_health_check():
    result = health_check.last_result()
    return result or {"ran_at": None, "overall": None, "checks": [], "problems": []}


@router.post("/api/health-check")
async def run_health_check(request: Request, announce: bool = False):
    return schedules.run_health_check(
        request.app.state.broker, request.app.state.engine, request.app.state.gateway, announce
    )


@router.get("/api/market")
async def market():
    return market_calendar.session_info()


@router.get("/api/telegram")
async def telegram_status():
    return notify.status()


@router.post("/api/telegram")
async def telegram_config(body: TelegramConfig):
    return notify.save_config(body.token, body.chat_id, clear=body.clear)


@router.post("/api/telegram/test")
async def telegram_test():
    if not notify.is_configured():
        raise HTTPException(400, "Telegram is not configured")
    ok = notify.send("✅ Test message from the NIFTY options engine.")
    if not ok:
        raise HTTPException(502, notify.status().get("last_error") or "send failed")
    return {"ok": True}


@router.get("/api/eod-summary")
async def eod_summary():
    return {"text": schedules.build_eod_summary()}
