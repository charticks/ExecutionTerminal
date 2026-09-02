"""Charticks sidecar — FastAPI control plane + WebSocket data plane.

Localhost-only. Guarded by CHARTICKS_BRIDGE_TOKEN (set by the Electron main
process). In dev, the token defaults to "dev" to match the renderer fallback.

Run: python -m uvicorn server:app --host 127.0.0.1 --port 8787
"""
from __future__ import annotations

# FIRST, before anything expensive: this module's T0 is the origin every other
# sidecar phase is measured from. See startup_profile.py.
import startup_profile

import asyncio
import os

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import diagnostics
from bridge import events
from bridge.hub import hub
from services import market_session
from services import simulator
from services.broker_manager import manager as broker_manager
from services import market_data
from services.hedge import hedge_manager
from services.idempotency import store as idempotency_store
from services.instruments import instruments
from services.kill_switch import kill_switch
from services.live_book import live_book
from services.live_manager import live_manager
from services.live_store import live_store
from services.order_manager import order_manager
from services.order_sync import order_sync
from services.paper_engine import paper_engine
from services.paths import strategies_dir
from services.position_reconciler import reconciler
from services.risk_engine import risk_engine
from services.strategy_engine import discovery as strategy_discovery
from services.strategy_engine.manager import strategy_manager
import services.strategy_engine.strategies  # noqa: F401 — registers plugins
from services.subscriptions import option_subs

startup_profile.mark("imports-complete")

BRIDGE_TOKEN = os.environ.get("CHARTICKS_BRIDGE_TOKEN", "dev")

# At import, not in the startup event: anything that fails while the app object
# is being built happens before startup fires, and without handlers installed
# those records fall through to Python's last-resort stderr writer and are lost.
# install() is idempotent, so the startup event calling it again is harmless.
diagnostics.install()

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


@app.get("/startup-profile")
async def startup_profile_state(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Where this process spent its start-up, in ms from its own T0.

    Folded into the cross-process timeline by the Electron main process, which
    knows when it spawned us. See charticks/electron/startup.ts.
    """
    _check_bearer(authorization)
    return JSONResponse({"phases": startup_profile.phases(),
                         "uptimeMs": round(startup_profile.elapsed_ms(), 1)})


@app.on_event("startup")
async def _startup() -> None:
    startup_profile.mark("asgi-startup-begin")
    diagnostics.install()  # no-op if import-time install already ran
    hub.bind_loop(asyncio.get_running_loop())
    simulator.start()
    # Restore the persisted live position book BEFORE the manager starts, so
    # the first evaluation cycle already knows what is held. Restored positions
    # stay disarmed until the reconciler has matched them against the broker's
    # own book — a stop must never fire against a position that was closed
    # while Charticks was not running.
    restored = reconciler.restore_and_start()
    if restored.get("restored"):
        hub.publish(events.log_line(
            "warn", f"[positions] restored {restored['restored']} live position(s) "
                    f"from disk — confirming with your broker before resuming "
                    f"stop loss / target management"))
    # Live trade management subscribes to the shared tick feed AND runs its own
    # periodic evaluation cycle, so stops keep evaluating when ticks do not.
    live_manager.start()
    # Recreate every configured strategy instance and start whichever were
    # auto-starting, AFTER live trade management is up: a strategy's entry
    # order rides the exact same order_manager/live_manager pipeline a manual
    # trade does, so that pipeline must already be ready. A strategy's own
    # OPEN positions need no special recovery — they are ordinary
    # SRC_CHARTICKS positions the reconciliation flow above already restores;
    # this only reattaches which instance manages which.
    # restore() already isolates every per-entry and per-instance failure
    # internally; this is the outermost backstop, because a strategy engine
    # that cannot come back up must never be the reason THE WHOLE SIDECAR
    # fails to start — Live trade management above is already running by
    # this point regardless of what happens here.
    try:
        strategy_restored = strategy_manager.restore()
    except Exception as exc:
        diagnostics.exception("strategy", "Strategy roster restore failed at startup",
                              exc_info=exc)
        strategy_restored = {}
    if strategy_restored.get("restored"):
        hub.publish(events.log_line(
            "info", f"[strategies] restored {strategy_restored['restored']} "
                    f"configured strategy instance(s), "
                    f"{strategy_restored['started']} auto-started"))
    # AFTER restore(): the roster it just recreated already contains every
    # previously-discovered instance, so discover() here only ever adds
    # instances for preset files that are genuinely new since the last run
    # — never a duplicate of one restore() just brought back.
    try:
        discovered = strategy_discovery.discover(strategy_manager, strategies_dir())
    except Exception as exc:
        diagnostics.exception("strategy", "Preset discovery failed at startup",
                              exc_info=exc)
        discovered = {}
    if discovered.get("created"):
        hub.publish(events.log_line(
            "info", f"[strategies] discovered {discovered['created']} new preset(s) "
                    f"from strategies/"))
    strategy_manager.start()
    # asyncio swallows exceptions from tasks nobody awaits; route them to
    # exceptions.log rather than the default stderr print that goes nowhere in
    # a packaged build.
    asyncio.get_running_loop().set_exception_handler(
        lambda _loop, ctx: diagnostics.exception(
            "app", f"asyncio: {ctx.get('message', 'error')}",
            exc_info=ctx.get("exception") or False))
    startup_profile.mark("ready")


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Write the live book down before the process goes.

    Persistence is debounced onto a background thread, so up to half a second of
    bookkeeping — most importantly a stop that has just trailed — is in memory
    at any moment. On a clean exit there is no reason to lose it.
    """
    # Strategies stop BEFORE live trade management, so nothing tries to place
    # a new order while the pipeline underneath it is on its way down.
    strategy_manager.stop_all()
    strategy_manager.stop()
    strategy_manager.flush_state()
    live_manager.stop()
    order_sync.stop()
    live_store.flush()
    diagnostics.event("app", "Sidecar shutdown", "success",
                      openPositions=len(live_book.open_positions()))


@app.get("/shutdown-check")
async def shutdown_check(authorization: str | None = Header(default=None)) -> JSONResponse:
    """What the user would be walking away from if they quit right now.

    Every live stop loss, target and trail lives in this process, so quitting
    while a managed position is open removes the only thing protecting it — and
    the broker keeps the position. The main process asks this before closing and
    makes the user confirm; it is not something to discover afterwards.
    """
    _check_bearer(authorization)
    positions = live_book.open_positions()
    managed = [p for p in positions if p.managed]
    return JSONResponse({
        "live": order_manager.mode == "live",
        "openPositions": len(positions),
        "managedPositions": len(managed),
        "workingOrders": order_sync.snapshot().get("open", 0),
        "symbols": [p.symbol for p in managed][:10],
    })


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Any exception escaping a handler. Without this it became a bare 500 with
    the trace printed to a stdout nobody reads — the class of failure that made
    "it just stopped working" unreconstructable after the fact."""
    diagnostics.exception("app", "Unhandled request error", exc_info=exc,
                          path=request.url.path, method=request.method)
    return JSONResponse(status_code=500,
                        content={"ok": False, "code": "INTERNAL_ERROR",
                                 "error": "Charticks hit an unexpected error. "
                                          "The details are in logs/exceptions.log."})


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
    # What is subscribed and WHO asked for it. The chain is only one source:
    # open live and paper positions declare their own contracts, and the union
    # is what actually goes on the wire.
    status["subscriptions"] = option_subs.status()
    # Per-feed detail. The top-level fields above stay as they were (the market
    # feed's own status) so existing readers are unaffected; this is additive,
    # and becomes the interesting half once a second broker streams.
    status["feeds"] = broker_manager.router.status()
    status["adapterError"] = market_data.option_chain.last_error()
    return JSONResponse(status)


@app.get("/order-feed")
async def order_feed(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Firstock's order-update WebSocket, per connected account — separate from
    /market-feed because it carries order events, not ticks, and from
    /order-sync because this is the transport, not the tracked orders
    themselves. Empty until a Firstock account is connected; no other broker
    has one yet."""
    _check_bearer(authorization)
    return JSONResponse({"feeds": broker_manager.order_feed_status()})


@app.get("/option-chain")
async def option_chain(authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    # Live snapshot from the reused OptionChainEngine. Empty rows until an Angel
    # session is connected and streaming (honest — no mock fallback).
    return JSONResponse(market_data.option_chain.snapshot())


@app.get("/market-session")
async def market_session_state(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Whether the market is open, and the holiday calendar behind that answer.

    The renderer mirrors the session rule so it can raise its dialog without a
    round trip, but it cannot mirror a calendar it has never seen. Serving the
    dates means the UI says "closed today — Republic Day" at the moment the user
    clicks, instead of composing an order for the engine to reject.
    """
    _check_bearer(authorization)
    today = market_session.now_ist().date()
    return JSONResponse({
        "openEquity": market_session.is_market_open(),
        "openCommodity": market_session.is_market_open(symbol="CRUDEOIL"),
        "holidayEquity": market_session.holiday_for(today),
        "holidayCommodity": market_session.holiday_for(today, "CRUDEOIL"),
        "date": today.isoformat(),
        "serverTimeIst": market_session.now_ist().strftime("%H:%M:%S"),
    })


@app.get("/contract-specs")
async def contract_specs(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Lot size and strike step per underlying, from the instrument master.

    The renderer used to carry its own hard-coded table (`lib/indices.ts`) and
    compute every order's quantity as `lots x that number`. Exchanges revise lot
    sizes, and when one drifted the renderer sent a quantity that disagreed with
    the contract: the sidecar's own lot-size rule then rejected every order for
    that index with LOT_QTY_MISMATCH, and the only fix was a new build. The
    master is the authority, so it is served from here and the table becomes a
    fallback for indices the master has not loaded yet.
    """
    _check_bearer(authorization)
    return JSONResponse({"specs": broker_manager.contract_specs(),
                         "steps": market_data.strike_steps()})


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


@app.post("/risk-config")
async def risk_config(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    """Push the active profile's limits + the Home session bar. Authoritative for
    order validation; until it arrives, LIVE orders are refused. The renderer
    re-pushes on every change and every reconnect (see useRiskSync)."""
    _check_bearer(authorization)
    return JSONResponse(risk_engine.set_config(body or {}))


@app.post("/hedge-config")
async def hedge_config(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    """Push the active profile's Auto Hedge config.

    Enforced in the sidecar, on broker-confirmed fills — the renderer used to own
    this end to end, so a hedge went in on "order accepted" (not on a fill) and
    only while a window happened to be open. Re-sent on every change and every
    reconnect, exactly like the risk config.
    """
    _check_bearer(authorization)
    return JSONResponse(hedge_manager.set_config(body or {}))


@app.post("/kill-switch")
async def kill_switch_set(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    """Engage / release the emergency halt. Sticky and server-owned: nothing
    clears it automatically, so it survives a renderer reload and cannot be
    undone by a reconnect. Blocks new entries only — exits stay available."""
    _check_bearer(authorization)
    if bool(body.get("halted")):
        return JSONResponse(kill_switch.engage(body.get("reason")))
    return JSONResponse(kill_switch.release())


@app.get("/kill-switch")
async def kill_switch_state(authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    return JSONResponse(kill_switch.state())


@app.get("/orders/sync")
async def orders_sync_state(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Live orders as the Order Synchronization Engine sees them — the source of
    truth for live order state, and what the position book is driven from."""
    _check_bearer(authorization)
    return JSONResponse(order_sync.snapshot())


@app.get("/orders/idempotency")
async def orders_idempotency(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Client-order-id claims, including any whose outcome is still unknown.

    An unresolved claim means Charticks sent an order and never learned whether
    the broker took it — the one state that will refuse a later identical order,
    so it has to be inspectable without reading a log file."""
    _check_bearer(authorization)
    return JSONResponse(idempotency_store.snapshot())


@app.get("/live-book")
async def live_book_state(authorization: str | None = Header(default=None)) -> JSONResponse:
    """The sidecar's own record of live positions, for diagnostics — this is
    what Max Positions / Max Loss are actually evaluated against."""
    _check_bearer(authorization)
    return JSONResponse(live_book.snapshot())


@app.get("/positions/monitor")
async def positions_monitor(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Is every live position actually being watched right now?

    The one place that answers it end to end: whether the management engine is
    running, when it last completed a cycle, which positions are in an alarm
    state and why, what is subscribed for market data, and how reconciliation
    against the broker's book is going.
    """
    _check_bearer(authorization)
    return JSONResponse({
        "monitor": live_manager.monitor_status(),
        "reconciliation": reconciler.status(),
        "book": live_book.snapshot(),
    })


@app.post("/positions/adopt")
async def positions_adopt(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    """Take over management of a position Charticks did not open.

    The rule is applied against the broker's cost basis, so an adopted position
    is managed on exactly the same terms as a native one. Explicit by design:
    Charticks never assumes a position found at the broker is its own.
    """
    _check_bearer(authorization)
    position_id = str(body.get("id", ""))
    if not position_id:
        raise HTTPException(status_code=400, detail="id is required")
    return JSONResponse(live_book.adopt(position_id, body.get("rule")))


@app.post("/positions/ignore")
async def positions_ignore(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    """Stop managing a position without closing it. It stays visible and is
    plainly marked unmanaged — never hidden, because it is still real
    exposure."""
    _check_bearer(authorization)
    position_id = str(body.get("id", ""))
    if not position_id:
        raise HTTPException(status_code=400, detail="id is required")
    return JSONResponse(live_book.release(position_id))


@app.post("/positions/clear-history")
async def positions_clear_history(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Forget this session's completed trades. Open positions are untouched."""
    _check_bearer(authorization)
    return JSONResponse(live_book.clear_history())


@app.post("/positions/hedge-decision")
async def positions_hedge_decision(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    """Answer the "your hedge is now on its own" question.

    `action` is "close" or "keep". Charticks does neither by itself: closing is
    an exit the user did not ask for, keeping is a position they did not choose
    to hold alone. See services/hedge.py.
    """
    _check_bearer(authorization)
    hedge_id = str(body.get("hedgeId", ""))
    action = str(body.get("action", "")).lower()
    if not hedge_id or action not in ("close", "keep"):
        raise HTTPException(status_code=400,
                            detail="hedgeId and action ('close'|'keep') are required")
    if action == "close":
        pos = live_book.get(hedge_id)
        if closed := market_session.require_open(pos.underlying if pos else None):
            return JSONResponse(closed)
    return JSONResponse(await run_in_threadpool(
        hedge_manager.resolve_orphan, hedge_id, action))


@app.post("/positions/reconcile")
async def positions_reconcile(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Force an immediate read of every broker's position book."""
    _check_bearer(authorization)
    return JSONResponse(await run_in_threadpool(reconciler.reconcile_once))


@app.post("/orders/place")
async def orders_place(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    # Live placement blocks on the broker SDK — keep it off the event loop.
    result = await run_in_threadpool(
        order_manager.place_order,
        # Mode travels ON the request and is validated against the confirmed
        # one — the sidecar never infers it. See OrderManager._resolve_mode.
        body.get("mode"),
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
        # Explicit, logged breach of the Max Position limit ("Always Override",
        # or the user answering the Ask Me prompt). Never inferred.
        bool(body.get("overrideMaxPos", False)),
        # Idempotency: a client that reissues the SAME id when retrying gives the
        # strongest possible duplicate signal, because it knows the two requests
        # are one intent. Optional — when absent the order's own parameters are
        # fingerprinted instead, which still catches double-clicks and restarts.
        # See services/idempotency/.
        str(body.get("clientRequestId") or ""),
        # The user was shown "an identical order was placed N seconds ago" and
        # chose to send this one anyway. Scoped to this request and never
        # inferred, exactly like overrideMaxPos — a confirmation must not be able
        # to leave duplicate protection switched off.
        bool(body.get("overrideDuplicate", False)),
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
        body.get("mode"),
        str(body.get("id", "")),
        float(price) if price is not None else None,
        int(qty) if qty is not None else None,
        int(lots) if lots is not None else None,
    )
    return JSONResponse(result)


@app.post("/orders/cancel")
async def orders_cancel(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    result = await run_in_threadpool(
        order_manager.cancel_order, body.get("mode"), str(body.get("id", "")))
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
    position_id = str(body.get("id", ""))
    fraction = float(body.get("fraction", 1.0))
    # Live positions are closed by the live manager (a real broker exit); paper
    # positions by the paper engine. Routing everything to the paper engine, as
    # this did, meant a close on a live position silently did nothing.
    if order_manager.mode == "live":
        pos = live_book.get(position_id)
        if closed := market_session.require_open(pos.underlying if pos else None):
            return JSONResponse(closed)
        return JSONResponse(await run_in_threadpool(
            live_manager.close_position, position_id, fraction))
    if closed := market_session.require_open(paper_engine.underlying_of(position_id)):
        return JSONResponse(closed)
    result = await run_in_threadpool(paper_engine.close_position, position_id, fraction)
    return JSONResponse(result)


@app.post("/positions/adjust")
async def positions_adjust(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    position_id = str(body.get("id", ""))
    delta = int(body.get("delta", 0))
    if order_manager.mode == "live":
        # Real orders now: adding places an entry for the extra lots, reducing is
        # a partial exit of exactly that many. This used to route to the paper
        # engine regardless of mode, so on a live position it looked up an id
        # that book had never held and returned quietly.
        pos = live_book.get(position_id)
        if closed := market_session.require_open(pos.underlying if pos else None):
            return JSONResponse(closed)
        return JSONResponse(await run_in_threadpool(
            live_manager.adjust_lots, position_id, delta))
    if closed := market_session.require_open(paper_engine.underlying_of(position_id)):
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
    position_id = str(body.get("id", ""))
    args = (
        float(sl) if sl is not None else None,
        float(target) if target is not None else None,
        float(trail_after) if trail_after is not None else None,
        float(trail_step) if trail_step is not None else None,
    )
    # Whichever book owns the position owns its risk state. Live edits used to
    # be routed to the paper engine, which simply did not have the position —
    # so the edit silently did nothing while the UI showed the new number.
    if order_manager.mode == "live":
        ok = await run_in_threadpool(live_book.set_risk, position_id, *args)
        if not ok:
            return JSONResponse({"ok": False, "code": "NO_POSITION",
                                 "error": "That live position is no longer open."})
        return JSONResponse({"ok": True})
    result = await run_in_threadpool(paper_engine.set_risk, position_id, *args)
    return JSONResponse(result)


@app.post("/portfolio-trail")
async def portfolio_trail(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    """Push the active profile's Portfolio Trail Profit config. One global
    config for the whole book — never per-instrument or per-position. The
    renderer re-sends this whenever the setting or the active profile changes."""
    _check_bearer(authorization)
    # Both engines get the config: whichever mode is active, the one that owns
    # that book acts on it.
    live_manager.set_portfolio_trail(body or {})
    return JSONResponse(await run_in_threadpool(paper_engine.set_portfolio_trail, body))


@app.post("/positions/roll")
async def positions_roll(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    """Move a position to another strike on the same series.

    Live rolls are REAL orders, sequenced by the live manager: close the current
    leg, and open the new one only once the broker confirms the close. This used
    to be routed to the paper engine regardless of mode, so a live position's
    roll found nothing to act on — while the renderer separately faked the
    result in its own store and showed a rolled position that did not exist.
    """
    _check_bearer(authorization)
    position_id = str(body.get("id", ""))
    if order_manager.mode == "live":
        pos = live_book.get(position_id)
        if closed := market_session.require_open(pos.underlying if pos else None):
            return JSONResponse(closed)
        return JSONResponse(await run_in_threadpool(
            live_manager.roll_position, position_id, int(body.get("newStrike", 0))))
    if closed := market_session.require_open(paper_engine.underlying_of(position_id)):
        return JSONResponse(closed)
    result = await run_in_threadpool(
        paper_engine.roll, position_id,
        int(body.get("newStrike", 0)), float(body.get("newEntry", 0)))
    return JSONResponse(result)


@app.post("/positions/square-off")
async def positions_square_off(authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    # In live mode this closes BROKER-CONFIRMED positions only; the paper
    # engine owns the paper book. Never both.
    if order_manager.mode == "live":
        underlyings = sorted({p.underlying for p in live_book.open_positions()})
        if closed := market_session.require_open_any(underlyings):
            return JSONResponse(closed)
        return JSONResponse(await run_in_threadpool(live_manager.square_off_all))
    if closed := market_session.require_open_any(paper_engine.open_underlyings()):
        return JSONResponse(closed)
    result = await run_in_threadpool(paper_engine.square_off_all)
    return JSONResponse(result)


@app.post("/paper/reset")
async def paper_reset(authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    await run_in_threadpool(paper_engine.reset)
    return JSONResponse({"ok": True})


# ---- Strategies (control plane) ----
# Every handler here is a thin pass-through to strategy_manager, exactly like
# every other section in this file — no business logic lives in a handler.
# Instance start/stop can subscribe candles and place a real order (Phase
# 2/3), so both go through run_in_threadpool the same way orders/place does.
@app.get("/strategies")
async def strategies_list(authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    from services.strategy_engine.registry import all_specs

    return JSONResponse({
        "specs": [s.to_dict() for s in all_specs()],
        "instances": strategy_manager.list_instances(),
    })


@app.get("/strategies/{instance_id}")
async def strategies_detail(instance_id: str,
                            authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    detail = strategy_manager.instance_detail(instance_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="no such strategy instance")
    return JSONResponse(detail)


@app.post("/strategies")
async def strategies_create(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    spec_name = body.get("strategy")
    if not spec_name:
        raise HTTPException(status_code=400, detail="strategy is required")
    result = strategy_manager.create_instance(
        spec_name, body.get("params") or {}, bool(body.get("autoStart")))
    return JSONResponse(result)


@app.post("/strategies/rescan")
async def strategies_rescan(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Re-scan the project's strategies/ folder on demand — the explicit
    counterpart to the automatic scan at startup, for a preset file added
    or edited while the app is already running (see discovery.py's own
    docstring for why an edited file has no effect on an already-discovered
    instance)."""
    _check_bearer(authorization)
    result = await run_in_threadpool(
        strategy_discovery.discover, strategy_manager, strategies_dir())
    return JSONResponse(result)


@app.post("/strategies/{instance_id}/update")
async def strategies_update(instance_id: str, body: dict,
                            authorization: str | None = Header(default=None)) -> JSONResponse:
    """Replace a stopped instance's configured params — the "Edit" action.
    Refuses (STILL_RUNNING) while the instance is running, same as
    /remove — see StrategyManager.update_params."""
    _check_bearer(authorization)
    result = strategy_manager.update_params(instance_id, body.get("params") or {})
    return JSONResponse(result)


@app.post("/strategies/{instance_id}/start")
async def strategies_start(instance_id: str,
                           authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    result = await run_in_threadpool(strategy_manager.start_instance, instance_id)
    return JSONResponse(result)


@app.post("/strategies/{instance_id}/stop")
async def strategies_stop(instance_id: str,
                          authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    result = await run_in_threadpool(strategy_manager.stop_instance, instance_id)
    return JSONResponse(result)


@app.post("/strategies/{instance_id}/remove")
async def strategies_remove(instance_id: str,
                            authorization: str | None = Header(default=None)) -> JSONResponse:
    # POST, not DELETE: every other action endpoint in this file is POST
    # (see /brokers/disconnect, /positions/close, /paper/reset, ...) — one
    # lone DELETE verb would be the only exception to that convention.
    _check_bearer(authorization)
    result = strategy_manager.remove_instance(instance_id)
    return JSONResponse(result)


# ---- Broker connectivity (control plane) ----
# Accounts + encrypted credentials are owned by the Electron main process. The
# renderer forwards decrypted credentials here per account; the sidecar keeps
# sessions in memory keyed by account_id and never persists secrets.
@app.get("/brokers")
async def brokers(authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_bearer(authorization)
    return JSONResponse({"status": broker_manager.status_map()})


@app.post("/brokers/execute")
async def brokers_execute(body: dict, authorization: str | None = Header(default=None)) -> JSONResponse:
    """Set which accounts may receive LIVE orders. Owned by the Brokers page and
    persisted with the broker config; the renderer re-pushes this whenever it
    changes and on every reconnect, because the sidecar starts with an empty set
    and refuses live placement until told otherwise."""
    _check_bearer(authorization)
    account_ids = body.get("accountIds")
    if not isinstance(account_ids, list):
        raise HTTPException(status_code=400, detail="accountIds must be a list")
    return JSONResponse(broker_manager.set_execution_accounts(account_ids))


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
        # Everything here is real: broker health, the kill-switch state, and the
        # live order book. The simulator contributes nothing unless it has been
        # explicitly enabled (see services/simulator.py).
        for event in broker_manager.snapshot_events():
            await ws.send_json(event)
        # A halt must be visible immediately to a reloaded window, not only to
        # whoever was connected when it was engaged.
        for event in kill_switch.snapshot_events():
            await ws.send_json(event)
        for event in simulator.snapshot_events():
            await ws.send_json(event)
        # Position events are DELTAS, so a reloaded window would show an empty
        # book until the next tick — indistinguishable from holding nothing.
        # Republishing on connect is what makes a restored position visible
        # immediately after a restart, warning badge and all. The alarm is
        # replayed for the same reason: it is published on transitions, and a
        # window that reloads mid-alarm must not come back looking calm.
        live_book.republish()
        live_manager.republish_alarm()
        # Same reasoning, for strategy instances: status is published on
        # transitions only, and a just-opened Strategies page must show every
        # configured instance (and its recent logs) immediately, not wait for
        # the next state change to happen to occur after it connected.
        for event in strategy_manager.snapshot_events():
            await ws.send_json(event)
        while True:
            event = await q.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        hub.unregister(q)
