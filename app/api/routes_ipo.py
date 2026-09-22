from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.ipo import listings, scanner, watchlist

router = APIRouter()


class AddBody(BaseModel):
    symbol: str
    buy_amount_inr: float = watchlist.DEFAULT_BUY_INR
    force: bool = False


class AmountBody(BaseModel):
    amount: float


class StatusBody(BaseModel):
    status: str


class ScanBody(BaseModel):
    max_days_since_listing: int = 400


@router.get("/api/ipo/watchlist")
async def get_watchlist():
    rows = watchlist.view()
    return {
        "entries": rows,
        "summary": {
            "total": len(rows),
            "armed": sum(1 for r in rows if r.get("status") == "armed"),
            "triggered": sum(1 for r in rows if r.get("status") == "triggered"),
            "near": sum(1 for r in rows if (r.get("distance_high_pct") or -99) >= -1
                        and (r.get("distance_high_pct") or 1) <= 0),
        },
    }


@router.post("/api/ipo/watchlist")
async def add_to_watchlist(body: AddBody, request: Request):
    result = watchlist.add(
        request.app.state.broker, body.symbol, body.buy_amount_inr, force=body.force
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "could not add"))
    return result


@router.delete("/api/ipo/watchlist/{symbol}")
async def remove_from_watchlist(symbol: str):
    result = watchlist.remove(symbol)
    if not result.get("ok"):
        raise HTTPException(404, result["error"])
    return result


@router.post("/api/ipo/watchlist/{symbol}/status")
async def change_status(symbol: str, body: StatusBody):
    result = watchlist.set_status(symbol, body.status)
    if not result.get("ok"):
        raise HTTPException(400, result["error"])
    return result


@router.post("/api/ipo/watchlist/{symbol}/amount")
async def change_amount(symbol: str, body: AmountBody):
    result = watchlist.set_amount(symbol, body.amount)
    if not result.get("ok"):
        raise HTTPException(404, result["error"])
    return result


@router.post("/api/ipo/watchlist/{symbol}/refresh")
async def refresh_levels(symbol: str, request: Request):
    result = watchlist.refresh_levels(request.app.state.broker, symbol)
    if not result.get("ok"):
        raise HTTPException(400, result["error"])
    return result


@router.get("/api/ipo/analyze/{symbol}")
async def analyze(symbol: str, request: Request):
    result = listings.analyze(request.app.state.broker, symbol)
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "analysis failed"))
    return result


@router.get("/api/ipo/ledger")
async def ipo_ledger(limit: int = 100):
    return {"records": watchlist.ledger(limit=limit)}


@router.get("/api/ipo/outcomes")
async def ipo_outcomes(request: Request):
    return {"outcomes": watchlist.outcomes(request.app.state.broker)}


@router.get("/api/ipo/scan")
async def scan_status():
    return {"status": scanner.status(), "results": scanner.results()}


@router.post("/api/ipo/scan")
async def scan_start(body: ScanBody, request: Request):
    result = scanner.start(request.app.state.broker, body.max_days_since_listing)
    if not result.get("ok"):
        raise HTTPException(409, result["error"])
    return result


@router.post("/api/ipo/scan/cancel")
async def scan_cancel():
    return scanner.cancel()
