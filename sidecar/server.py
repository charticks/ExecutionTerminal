"""Charticks sidecar — FastAPI control plane + WebSocket data plane.

Localhost-only. Guarded by CHARTICKS_BRIDGE_TOKEN (set by the Electron main
process). In dev, the token defaults to "dev" to match the renderer fallback.

Run: python -m uvicorn server:app --host 127.0.0.1 --port 8787
"""
from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from bridge.hub import hub
from services import market_session
from services import simulator
from services.broker_manager import manager as broker_manager
from services import market_data
from services.instruments import instruments
from services.order_manager import order_manager
from services.paper_engine import paper_engine

BRIDGE_TOKEN = os.environ.get("CHARTICKS_BRIDGE_TOKEN", "dev")

app = FastAPI(title="Charticks Sidecar", version="0.1.0")

# The renderer runs on a different origin than this localhost sidecar (vite dev
# server in dev, file:// in the packaged Electron app), so browser fetches send a
# CORS preflight OPTIONS first. Without this middleware that preflight 405s and
# every POST /brokers/*/connect fails before it reaches the handler. Auth is via
# the bearer token (not cookies), so a permissive localhost policy is safe here.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(https?://(localhost|127\.0\.0\.1)(:\d+)?|file://.*|null)$",
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)


@app.on_event("startup")
async def _startup() -> None:
    hub.bind_loop(asyncio.get_running_loop())
    simulator.start()


def _check_bearer(authorization: str | None) -> None:
    expected = f"Bearer {BRIDGE_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


# ---- Control plane (REST) ----
@app.get("/health")
async def health() -> dict:
    return {"ok": True, "service": "charticks-sidecar", "version": "0.1.0"}


@app.get("/market-feed")
async def market_feed(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Market-data plane health, separate from broker login state. A broker can
    be authenticated (REST fine) while the tick feed is dead — that combination
    silently emptied the option chain, so it is now explicitly observable."""
    _check_bearer(authorization)
    status = broker_manager.market_ws.status()
    status["indexLtp"] = dict(broker_manager.index_ltp)
    # Compared as canonical keys, not broker tokens, so this stays meaningful
    # once a second feed streams the same contracts under different ids.
    subscribed = market_data.option_chain.subscribed_keys()
    with broker_manager._tick_lock:
        ticked = set(broker_manager.option_ticks)
    status["optionFeed"] = {
        "subscribeState": broker_manager.option_sub_state,
        "subscribeTs": broker_manager.option_sub_ts,
        "subscribedTokens": len(subscribed),
        "tokensWithTicks": len(subscribed & ticked),
        "totalTokensTicked": len(ticked),
        # Human-readable ("NIFTY 26AUG2026 24000 CE") rather than a bare token.
        "sampleSilent": sorted(str(k) for k in (subscribed - ticked))[:5],
        "unmappedTicks": broker_manager.unmapped_ticks,
        "lastUnmappedToken": broker_manager.last_unmapped_token,
    }
    status["instrumentBindings"] = instruments.stats()
    # Per-feed detail. The top-level fields above stay as they were (the market
    # feed's own status) so existing readers are unaffected; this is additive,
    # and becomes the interesting half once a second broker streams.
    status["feeds"] = broker_manager.router.status()
    status["adapterError"] = market_data.option_chain.last_error()
    return JSONResponse(status)


@app.get("/option-chain")
async def option_chain(authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    # Live snapshot from the reused OptionChainEngine. Empty rows until an Angel
    # session is connected and streaming (honest — no mock fallback).
    return JSONResponse(market_data.option_chain.snapshot())


@app.post("/option-chain/select")
async def option_chain_select(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    market_data.option_chain.select(body.get("symbol"), body.get("count"), body.get("expiry"))
    return JSONResponse({"ok": True})


@app.post("/option-chain/watch")
async def option_chain_watch(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    """Track specific contracts (index + expiry + option type + strikes) so the
    Roll picker gets each strike's own live premium. Empty strikes clears it."""
    _check_bearer(authorization)
    market_data.option_chain.set_watch(
        body.get("symbol"), body.get("expiry"), body.get("optType"), body.get("strikes"))
    return JSONResponse({"ok": True})


# ---- Order routing + trading mode (control plane) ----
@app.post("/trading-mode")
async def trading_mode(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    return JSONResponse(order_manager.set_mode(body.get("mode", "paper")))


@app.post("/orders/place")
async def orders_place(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    # Live placement blocks on the broker SDK — keep it off the event loop.
    result = await run_in_threadpool(
        order_manager.place_order,
        body.get("underlying", ""),
        body.get("expiry", ""),
        float(body.get("strike", 0)),
        body.get("optType", ""),
        body.get("side", ""),
        int(body.get("qty", 0)),
        body.get("orderType", "MARKET"),
        float(body.get("price", 0)),
        int(body.get("lots", 0)),
        body.get("rule"),
        body.get("product", "NRML"),
        body.get("validity", "DAY"),
        bool(body.get("allowDuplicate", False)),
    )
    return JSONResponse(result)


@app.post("/orders/modify")
async def orders_modify(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    price = body.get("price")
    qty = body.get("qty")
    lots = body.get("lots")
    result = await run_in_threadpool(
        order_manager.modify_order,
        str(body.get("id", "")),
        float(price) if price is not None else None,
        int(qty) if qty is not None else None,
        int(lots) if lots is not None else None,
    )
    return JSONResponse(result)


@app.post("/orders/cancel")
async def orders_cancel(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    result = await run_in_threadpool(order_manager.cancel_order, str(body.get("id", "")))
    return JSONResponse(result)


# ---- Paper position ops (paper engine is authoritative for the paper book) ----
# Every position op that moves quantity (close / partial exit / adjust / roll /
# square off all) is a trading action and goes through the same market-session
# gate as order placement. /positions/risk is deliberately NOT gated — editing an
# SL or target changes local risk state and sends nothing to any engine.
@app.get("/paper/state")
async def paper_state(authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    # Re-publishes the full snapshot over the WS so a reloaded renderer repaints.
    return JSONResponse(paper_engine.snapshot())


@app.post("/positions/close")
async def positions_close(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    if closed := market_session.require_open(paper_engine.underlying_of(str(body.get("id", "")))):
        return JSONResponse(closed)
    result = await run_in_threadpool(
        paper_engine.close_position, str(body.get("id", "")), float(body.get("fraction", 1.0)))
    return JSONResponse(result)


@app.post("/positions/adjust")
async def positions_adjust(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    if closed := market_session.require_open(paper_engine.underlying_of(str(body.get("id", "")))):
        return JSONResponse(closed)
    result = await run_in_threadpool(
        paper_engine.adjust_lots, str(body.get("id", "")), int(body.get("delta", 0)))
    return JSONResponse(result)


@app.post("/positions/risk")
async def positions_risk(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    sl = body.get("sl")
    target = body.get("target")
    trail_after = body.get("trailAfter")
    trail_step = body.get("trailStep")
    result = await run_in_threadpool(
        paper_engine.set_risk, str(body.get("id", "")),
        float(sl) if sl is not None else None,
        float(target) if target is not None else None,
        float(trail_after) if trail_after is not None else None,
        float(trail_step) if trail_step is not None else None)
    return JSONResponse(result)


@app.post("/portfolio-trail")
async def portfolio_trail(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    """Push the active profile's Portfolio Trail Profit config. One global
    config for the whole book — never per-instrument or per-position. The
    renderer re-sends this whenever the setting or the active profile changes."""
    _check_bearer(authorization)
    return JSONResponse(await run_in_threadpool(paper_engine.set_portfolio_trail, body))


@app.post("/positions/roll")
async def positions_roll(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    if closed := market_session.require_open(paper_engine.underlying_of(str(body.get("id", "")))):
        return JSONResponse(closed)
    result = await run_in_threadpool(
        paper_engine.roll, str(body.get("id", "")),
        int(body.get("newStrike", 0)), float(body.get("newEntry", 0)))
    return JSONResponse(result)


@app.post("/positions/square-off")
async def positions_square_off(authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    if closed := market_session.require_open_any(paper_engine.open_underlyings()):
        return JSONResponse(closed)
    result = await run_in_threadpool(paper_engine.square_off_all)
    return JSONResponse(result)


@app.post("/paper/reset")
async def paper_reset(authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    await run_in_threadpool(paper_engine.reset)
    return JSONResponse({"ok": True})


# ---- Broker connectivity (control plane) ----
# Accounts + encrypted credentials are owned by the Electron main process. The
# renderer forwards decrypted credentials here per account; the sidecar keeps
# sessions in memory keyed by account_id and never persists secrets.
@app.get("/brokers")
async def brokers(authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    return JSONResponse({"status": broker_manager.status_map()})


@app.post("/brokers/reconnect")
async def broker_reconnect(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Force an immediate feed reconnect — called by the renderer on network-up
    (window 'online') so recovery doesn't wait out the backoff timer."""
    _check_bearer(authorization)
    await run_in_threadpool(broker_manager.health_monitor.force_reconnect_all)
    return JSONResponse({"ok": True})


@app.post("/brokers/connect")
async def broker_connect(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    account_id = body.get("accountId")
    broker = body.get("broker")
    credentials = body.get("credentials") or {}
    if not account_id or not broker:
        raise HTTPException(status_code=400, detail="accountId and broker are required")
    # The connect sequence is blocking (SDK auth + master download); run it off
    # the event loop so the WebSocket data plane keeps flowing.
    result = await run_in_threadpool(broker_manager.connect, account_id, broker, credentials)
    return JSONResponse(result)


@app.post("/brokers/disconnect")
async def broker_disconnect(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    account_id = body.get("accountId")
    if not account_id:
        raise HTTPException(status_code=400, detail="accountId is required")
    result = await run_in_threadpool(broker_manager.disconnect, account_id)
    return JSONResponse(result)


# ---- Data plane (WebSocket) ----
@app.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    token = ws.query_params.get("token")
    if token != BRIDGE_TOKEN:
        await ws.close(code=4401)
        return

    await ws.accept()
    q = hub.register()
    try:
        # Replay current state so a late-joining client is immediately correct.
        # Real broker health comes from the broker manager; market data (indices,
        # positions, pnl) still comes from the simulator in this phase.
        for event in broker_manager.snapshot_events():
            await ws.send_json(event)
        for event in simulator.snapshot_events():
            await ws.send_json(event)
        while True:
            event = await q.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        hub.unregister(q)
