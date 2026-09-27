from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import audit, strategies_store
from app.broker import AuthRequired
from app.order_gateway import OrderBlocked

router = APIRouter()


class ConfigPatch(BaseModel):
    model_config = {"extra": "allow"}


class KillPatch(BaseModel):
    enabled: bool
    source: str = "dashboard"


class CreatePatch(BaseModel):
    name: str | None = None
    kind: str | None = None


def _engine(request: Request):
    return request.app.state.engine


def _require(engine, sid: str):
    if sid not in engine.strategies:
        raise HTTPException(404, f"no strategy {sid}")


@router.get("/api/state")
async def state(request: Request):
    return _engine(request).full_state()


@router.get("/api/active")
async def active(request: Request):
    engine = _engine(request)
    items = engine.active_items()
    return {
        "items": items,
        "summary": {
            "total": len(items),
            "real_money": sum(1 for i in items if i["real_money"]),
            "live": sum(1 for i in items if i["state"] == "live"),
        },
    }


@router.get("/api/activity")
async def activity(limit: int = 50):
    return {"events": audit.read_events(limit=limit)}


@router.get("/api/kill-switch")
async def get_kill(request: Request):
    return request.app.state.gateway.kill_state()


@router.post("/api/kill-switch")
async def set_kill(body: KillPatch, request: Request):
    return request.app.state.gateway.set_kill(body.enabled, source=body.source)


@router.post("/api/strategies")
async def create_strategy(body: CreatePatch, request: Request):
    engine = _engine(request)
    sid = strategies_store.next_sid(engine.strategies)
    overrides = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    engine.strategies[sid] = strategies_store.new_strategy(sid, **overrides)
    engine.strategies[sid]["order"] = len(engine.strategies)
    strategies_store.save_all(engine.strategies)
    engine.reload_strategies()
    audit.log_event("strategy_created", sid=sid, name=engine.strategies[sid]["name"])
    return engine.strategy_view(sid)


@router.delete("/api/strategies/{sid}")
async def delete_strategy(sid: str, request: Request):
    engine = _engine(request)
    _require(engine, sid)
    rt = engine.runtimes.get(sid)
    if rt and rt.status == "live" and rt.legs:
        raise HTTPException(409, "cannot delete a strategy holding an open position")
    engine.strategies.pop(sid, None)
    engine.runtimes.pop(sid, None)
    strategies_store.save_all(engine.strategies)
    audit.log_event("strategy_deleted", sid=sid)
    return {"ok": True}


@router.post("/api/strategies/{sid}/config")
async def update_config(sid: str, patch: ConfigPatch, request: Request):
    engine = _engine(request)
    _require(engine, sid)
    clean, rejected = strategies_store.validate_patch(patch.model_dump())
    if not clean:
        detail = "; ".join(f"{k}: {v}" for k, v in rejected.items()) or "no valid fields in patch"
        raise HTTPException(400, detail)

    if clean.get("mode") == "live" and not request.app.state.broker.is_authenticated():
        raise HTTPException(409, "cannot switch to LIVE: broker not authenticated")

    engine.strategies[sid].update(clean)
    strategies_store.save_all(engine.strategies)
    audit.log_event("strategy_config_changed", sid=sid, changes=clean, rejected=rejected or None)

    view = engine.strategy_view(sid)
    # Tell the caller what did NOT get saved, so a silently ignored field
    # can't masquerade as a successful save.
    view["rejected"] = rejected
    return view


@router.get("/api/strategies/{sid}/plan")
async def plan(sid: str, request: Request):
    engine = _engine(request)
    _require(engine, sid)
    return engine.plan_entry(sid)


@router.post("/api/strategies/{sid}/enter")
async def enter(sid: str, request: Request):
    engine = _engine(request)
    _require(engine, sid)
    try:
        result = engine.enter(sid, source="manual")
    except OrderBlocked as e:
        raise HTTPException(423, str(e))
    except AuthRequired as e:
        raise HTTPException(401, f"broker re-auth required: {e.login_url}")
    if not result.get("ok") and result.get("error"):
        raise HTTPException(400, result["error"])
    return result


class AdoptBody(BaseModel):
    instrument_keys: list[str] | None = None


@router.get("/api/positions")
async def broker_positions(request: Request):
    """Open positions at the broker, whether or not this app opened them."""
    try:
        return {"positions": _engine(request).broker_positions()}
    except Exception as e:
        raise HTTPException(502, str(e))


@router.post("/api/strategies/{sid}/adopt")
async def adopt(sid: str, body: AdoptBody, request: Request):
    """Track a position that was opened outside this app."""
    engine = _engine(request)
    _require(engine, sid)
    result = engine.adopt_positions(sid, body.instrument_keys)
    if not result.get("ok"):
        raise HTTPException(400, result["error"])
    return result


@router.post("/api/strategies/{sid}/exit")
async def exit_strategy(sid: str, request: Request):
    engine = _engine(request)
    _require(engine, sid)
    try:
        result = engine.exit(sid, reason="MANUAL", detail="manual exit from dashboard")
    except AuthRequired as e:
        raise HTTPException(401, f"broker re-auth required: {e.login_url}")
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "exit failed"))
    return result


@router.post("/api/enter-all")
async def enter_all(request: Request):
    engine = _engine(request)
    results = {}
    for sid in sorted(engine.strategies):
        if engine.strategies[sid].get("enabled", True):
            results[sid] = engine.enter(sid, source="manual_all")
    return {"results": results}


@router.post("/api/exit-all")
async def exit_all(request: Request):
    engine = _engine(request)
    results = {}
    for sid in sorted(engine.strategies):
        rt = engine.runtimes.get(sid)
        if rt and rt.status == "live" and rt.legs:
            results[sid] = engine.exit(sid, reason="MANUAL", detail="exit all")
    return {"results": results}
