"""Order routing for the sidecar — Paper vs Live, ported from the legacy
Tkinter app's single-branch model (app/order_manager.py:122).

Paper mode returns a synthetic fill at the supplied LTP and NEVER touches a
broker SDK. Live mode places a real order on every account the user has enabled
for execution on the Brokers page (BrokerManager.execution_sessions) — NOT on
every connected account, which is how a broker connected purely for market-data
redundancy used to receive a duplicate of every order.

Mode routing is TWO-KEY. Every order request must state the mode it was composed
under, and this module also holds the mode last confirmed by the client (POST
/trading-mode). Routing uses the mode ON THE REQUEST — never the stored one —
and only after the two agree. A disagreement is not resolved, it is REJECTED:
the two keys disagreeing means the UI the user acted on and the engine about to
act have different ideas about whether real money is at stake, and neither is
trustworthy enough to guess from. Previously the stored mode alone decided, so a
sidecar restart (which resets it to paper) or a dropped /trading-mode call left
the badge and the router silently disagreeing in either direction.

Scope note (C1): Angel live routing is fully ported; Kotak/Dhan live routing is
NOT yet ported and returns a clear error rather than silently mis-firing. The
tick-driven SL/Target/TSL/Rolling + automation engine (legacy
engines/trade_execution_engine.py) is a separate follow-up (C2).
"""
from __future__ import annotations

import threading
from typing import Any

import diagnostics
from bridge import events
from bridge.hub import hub
from services import expiry as expiry_filter
from services import market_session
from services.broker_limits import limit_resolver
from services.broker_manager import LABEL, manager
from services.live_book import live_book
from services.margin import MarginRequest, margin_engine
from services.order_sync import order_sync
from services.order_splitter import split_quantity
from services.paper_engine import paper_engine
from services.risk_engine import OrderContext, risk_engine

PAPER = "paper"
LIVE = "live"

# Angel exchange per index family (mirrors legacy exch filter).
_ANGEL_EXCH = {"NIFTY": "NFO", "BANKNIFTY": "NFO", "SENSEX": "BFO"}


class OrderManager:
    def __init__(self) -> None:
        self._mode = PAPER
        self._lock = threading.Lock()
        self._parent_seq = 0

    def _next_parent_id(self) -> str:
        with self._lock:
            self._parent_seq += 1
            return f"P{self._parent_seq}"

    # ── mode (confirmed by the client; one half of the two-key check) ─────
    def set_mode(self, mode: str) -> dict:
        mode = LIVE if str(mode).lower() == LIVE else PAPER
        with self._lock:
            self._mode = mode
        self._log("info", f"[order] trading mode → {mode}")
        return {"ok": True, "mode": mode}

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def _resolve_mode(self, requested: str | None, action: str) -> tuple[str | None, dict | None]:
        """Validate the mode carried by a request against the confirmed one.

        Returns (mode, None) when routing may proceed on `mode`, or
        (None, error) when it may not. Callers must route on the returned mode
        and never on self.mode.
        """
        value = str(requested or "").strip().lower()
        if value not in (PAPER, LIVE):
            # An order with no mode is a client that predates this contract (or
            # a hand-rolled request). Defaulting either way would be a guess.
            return None, {
                "ok": False, "code": "MODE_REQUIRED",
                "error": f"This {action} request did not specify a trading mode. "
                         f"Charticks cannot route it without one.",
            }
        current = self.mode
        if value != current:
            return None, {
                "ok": False, "code": "MODE_MISMATCH",
                "requestedMode": value, "activeMode": current,
                "error": f"Trading mode mismatch — this {action} was sent as "
                         f"{value.upper()} but Charticks is currently set to "
                         f"{current.upper()}. Nothing was sent to any broker. "
                         f"Re-select your trading mode, then try again.",
            }
        return value, None

    def _log(self, level: str, msg: str) -> None:
        diagnostics.emit("orders", level, msg, publish=True)

    def _margin_request(self, underlying: str, expiry: str, strike: float,
                        opt_type: str, side: str, qty: int, lots: int,
                        order_type: str, price: float,
                        product: str) -> MarginRequest:
        """Describe the order for the margin checkers, without broker vocabulary."""
        tradingsymbol, token, exch = "", "", _ANGEL_EXCH.get(underlying, "NFO")
        try:
            tradingsymbol, resolved, exch = manager.resolve_option(
                underlying, expiry, strike, opt_type)
            token = resolved or ""
        except Exception:
            pass
        ltp = None
        if token:
            ltp, _bid, _ask = manager.get_option_quote(token)
        return MarginRequest(
            underlying=underlying, expiry=expiry, strike=strike,
            opt_type=opt_type, side=side, qty=qty, lots=lots,
            order_type=order_type, price=price, product=product,
            exchange=exch, tradingsymbol=tradingsymbol, token=token, ltp=ltp)

    @staticmethod
    def _token_for(underlying: str, expiry: str, strike: float, opt_type: str) -> str:
        """Feed token for a contract, used to mark the live book to market.
        Never fatal — an unresolvable token just means that position shows no
        open P&L until the instrument master catches up."""
        try:
            _sym, token, _exch = manager.resolve_option(underlying, expiry, strike, opt_type)
            return token or ""
        except Exception:
            return ""

    def _risk_context(self, route_mode: str, underlying: str, expiry: str,
                      strike: float, opt_type: str, side: str, qty: int,
                      lots: int, order_type: str, price: float, product: str,
                      validity: str, override_max_pos: bool,
                      allow_duplicate: bool) -> OrderContext:
        """Snapshot the runtime state the rules need, from whichever book is
        authoritative for this mode. Resolving the quote here (rather than in a
        rule) keeps the rules pure and means one lookup covers all of them."""
        ltp = None
        meta: dict | None = None
        try:
            _sym, token, _exch = manager.resolve_option(underlying, expiry, strike, opt_type)
            if token:
                ltp, _bid, _ask = manager.get_option_quote(token)
            meta = manager.option_meta(underlying, expiry, strike, opt_type)
        except Exception as exc:
            # A missing quote weakens the away-from-market check but must not
            # block the order — the rule treats ltp=None as "cannot compare".
            diagnostics.event("risk", "Quote lookup for validation", "failed",
                              level="warn", symbol=f"{underlying} {expiry} "
                              f"{int(strike)} {opt_type}", reason=str(exc))

        book = live_book if route_mode == LIVE else paper_engine
        cap, splits, unsupported = self._broker_constraints(
            route_mode, underlying, product, meta)
        return OrderContext(
            mode=route_mode, underlying=underlying, expiry=expiry, strike=strike,
            opt_type=opt_type, side=side, qty=qty, lots=lots,
            order_type=order_type, price=price, product=product, validity=validity,
            config=risk_engine.config, ltp=ltp,
            open_positions=book.open_count(),
            held_lots=book.held_lots(underlying, expiry, strike, opt_type),
            orders_today=book.orders_today(),
            session_pnl=book.session_pnl(),
            lot_size=(meta or {}).get("lotSize"),
            tick_size=(meta or {}).get("tickSize"),
            spot=manager.index_ltp.get(underlying),
            feed_stale=manager.feed_stale(),
            broker_qty_cap=cap,
            supports_splitting=splits,
            unsupported_product=unsupported,
            duplicate_of=book.working_order_id(underlying, expiry, strike, opt_type, side),
            override_max_pos=override_max_pos,
            allow_duplicate=allow_duplicate,
        )

    # Products each broker genuinely supports for options. ICICI's Breeze
    # exposes a single "options" product with no intraday variant, so an MIS
    # order there used to be logged as a warning and placed as carry-forward —
    # silently turning an intraday trade into a positional one.
    _BROKER_PRODUCTS = {"icici": ("NRML",)}

    def _broker_constraints(self, route_mode: str, underlying: str, product: str,
                            meta: dict | None) -> tuple[int | None, bool, str | None]:
        """Tightest per-order quantity cap across the target brokers, whether
        they can all split, and the first broker that cannot honour `product`."""
        if route_mode != LIVE:
            return None, True, None
        caps: list[int] = []
        splits = True
        unsupported: str | None = None
        lot_size = (meta or {}).get("lotSize") or 0
        for account_id, broker, sess in manager.execution_sessions():
            allowed = self._BROKER_PRODUCTS.get(broker)
            if allowed and product not in allowed and unsupported is None:
                unsupported = LABEL.get(broker, broker)
            try:
                limits = limit_resolver.resolve(account_id, broker, sess, underlying)
            except Exception:
                continue
            # hard_cap_qty, not cap_qty: we want the freeze limit itself even
            # when this broker cannot split, which is exactly the case the
            # freeze-quantity rule exists to reject.
            cap = limits.hard_cap_qty(lot_size) if lot_size else None
            if cap:
                caps.append(int(cap))
            if not getattr(limits, "supports_splitting", True):
                splits = False
        return (min(caps) if caps else None), splits, unsupported

    # ── entry point ────────────────────────────────────────────────────────
    def place_order(self, mode: str | None, underlying: str, expiry: str,
                    strike: float, opt_type: str, side: str, qty: int,
                    order_type: str, price: float, lots: int = 0,
                    rule: dict | None = None,
                    product: str = "NRML", validity: str = "DAY",
                    allow_duplicate: bool = False,
                    override_max_pos: bool = False) -> dict:
        underlying = (underlying or "").upper()
        opt_type = (opt_type or "").upper()
        side = (side or "").upper()
        order_type = (order_type or "MARKET").upper()
        product = (product or "NRML").upper()
        validity = (validity or "DAY").upper()
        symbol_hint = f"{underlying}{expiry}{int(strike)}{opt_type}"

        # Mode gate FIRST: resolve which engine this order was composed for
        # before doing anything else, so a mismatched request is rejected before
        # it can touch a session, an engine, or the duplicate-order book.
        route_mode, mode_error = self._resolve_mode(mode, "order")
        if mode_error:
            self._log("warn", f"[order] ⛔ {side} {qty} {symbol_hint} rejected — "
                              f"{mode_error['code']}")
            return mode_error

        # Market-session gate — ahead of the paper/live fork so ONE check covers
        # both engines and no request can reach a broker outside market hours.
        closed = market_session.require_open(underlying)
        if closed:
            self._log("warn", f"[order] ⛔ {side} {qty} {symbol_hint} rejected — market closed")
            return closed

        # Expiry gate — likewise ahead of the fork, so an expired contract the
        # broker is still listing can never be traded by either engine.
        if expiry_filter.is_expired(expiry):
            self._log("warn", f"[order] ⛔ {side} {qty} {symbol_hint} rejected — contract expired")
            return {"ok": False, "code": "EXPIRED_CONTRACT",
                    "error": f"{underlying} {expiry} has expired — pick an active expiry."}

        # Configured trading rules — Max Quantity / Max Price / tick grid /
        # Max Positions / Max Trades / Max Loss / Profit Target. Server-side and
        # ahead of the fork, so no order of either kind can slip past a limit
        # the user has set. See services/risk_engine.py.
        violation = risk_engine.validate(
            self._risk_context(route_mode, underlying, expiry, strike, opt_type,
                               side, qty, lots or qty, order_type, price, product,
                               validity, override_max_pos, allow_duplicate))
        if violation is not None:
            self._log("warn", f"[order] ⛔ {side} {qty} {symbol_hint} rejected — "
                              f"{violation.code}")
            return violation.as_response()

        # PAPER: route to the tick-driven paper execution engine (realistic
        # market/limit fills against internal bid/ask, MTM, validation) — no SDK.
        if route_mode != LIVE:
            res = paper_engine.place(underlying, expiry, strike, opt_type, side,
                                     qty, lots or qty, order_type, price, rule,
                                     product, validity, allow_duplicate)
            lvl = "info" if res.get("ok") else "warn"
            msg = (f"📝 Paper {order_type} {side} {qty} {symbol_hint} → "
                   f"{res.get('status') or res.get('error')}")
            self._log(lvl, f"[order] {msg}")
            return res

        # LIVE: fan out to the accounts the user opted in to on the Brokers page
        # — NOT to every connected broker. Connectivity buys market data and
        # account services; execution is a separate, explicit choice, so adding
        # a second broker for feed redundancy can't silently double a position.
        if not manager.execution_accounts():
            self._log("warn", f"[order] ⛔ {side} {qty} {symbol_hint} rejected — "
                              f"no execution broker selected")
            return {"ok": False, "code": "NO_EXECUTION_BROKER",
                    "error": "No execution broker selected. Please enable Execute "
                             "on at least one connected broker."}
        sessions = manager.execution_sessions()
        if not sessions:
            # Opted in somewhere, but none of those accounts is connected — a
            # different problem from "nothing selected", so say so.
            self._log("warn", f"[order] ⛔ {side} {qty} {symbol_hint} rejected — "
                              f"no execution broker is connected")
            return {"ok": False, "code": "EXECUTION_BROKER_DISCONNECTED",
                    "error": "No execution broker is connected. Connect a broker "
                             "that has Execute enabled, or enable Execute on one "
                             "that is already connected."}

        # Pre-trade margin — AFTER every risk rule, BEFORE any broker API call.
        # Fail-safe and all-or-nothing: an unverifiable margin at any single
        # execution broker rejects the whole order rather than fanning out
        # partially. See services/margin/.
        rejection = margin_engine.validate(
            sessions,
            self._margin_request(underlying, expiry, strike, opt_type, side,
                                 qty, lots or qty, order_type, price, product))
        if rejection is not None:
            self._log("warn", f"[order] ⛔ {side} {qty} {symbol_hint} rejected — "
                              f"{rejection.code}")
            return rejection.as_response()

        # Open the duplicate window BEFORE routing, so a second request racing
        # this one is rejected rather than both reaching the broker.
        live_book.note_submitted(underlying, expiry, strike, opt_type, side)
        diagnostics.event("orders", "Place Order", "started", mode=LIVE,
                          symbol=symbol_hint, side=side, qty=qty,
                          orderType=order_type, price=price, product=product,
                          validity=validity,
                          accounts=len(sessions))

        results: list[dict] = []
        for account_id, broker, sess in sessions:
            res = self._place_with_splitting(account_id, broker, sess, underlying,
                                             expiry, strike, opt_type, side, qty,
                                             order_type, price, lots or qty,
                                             product, validity, symbol_hint)
            res["account"] = account_id
            results.append(res)
            submitted = int(res.get("executedQty") or (qty if res.get("ok") else 0))
            diagnostics.event(
                "orders", "Place Order",
                "submitted" if res.get("ok") else "rejected",
                broker=LABEL.get(broker, broker), account=account_id,
                symbol=symbol_hint, side=side, requestedQty=qty,
                submittedQty=submitted, orderId=res.get("orderId"),
                code=res.get("code"), reason=res.get("error"))
            # Hand the order to the synchronization engine rather than booking a
            # position here. An order id means ACCEPTED FOR ROUTING, not filled:
            # booking it immediately is what made the terminal show positions
            # that did not exist. The engine polls the broker's own order book
            # and books the position only on confirmed filled quantity.
            if res.get("ok") and res.get("orderId"):
                requested_lots = lots or qty
                lot_size = max(1, qty // requested_lots) if requested_lots else 1
                order_sync.track(
                    str(res["orderId"]), account_id, broker, underlying, expiry,
                    strike, opt_type, side, submitted or qty, lot_size, price,
                    token=self._token_for(underlying, expiry, strike, opt_type),
                    rule=rule)
            else:
                # Nothing to track — report the rejection once, here.
                hub.publish(events.order_update(
                    str(res.get("orderId") or f"{broker}-{symbol_hint}"),
                    symbol_hint, side, qty, price, "REJECTED"))

        ok = any(r.get("ok") for r in results)
        out = {"ok": ok, "results": results, "symbol": symbol_hint}
        # Surface a partial execution to the renderer so it can offer a retry of
        # the remaining quantity (one logical trade, part-filled).
        partial = next((r for r in results if r.get("code") == "PARTIAL_FILL"), None)
        if partial and not ok:
            out.update({k: partial[k] for k in
                        ("code", "error", "executedQty", "remainingQty") if k in partial})
        return out

    # ── exits (automation + manual square-off) ─────────────────────────────
    def place_exit(self, underlying: str, expiry: str, strike: float,
                   opt_type: str, side: str, qty: int, lots: int,
                   reason: str = "exit", position_key: str = "") -> dict:
        """Close broker-confirmed exposure at market.

        Separate entry point from place_order because an exit is a different
        kind of request: it reduces risk rather than adding it, so entry-only
        rules (kill switch, session locks, position caps, notional, duplicate)
        stand down and no margin check is performed — closing a long needs no
        margin, and closing a short releases it. Structural and price checks
        still apply; a malformed exit is still malformed.

        Quantity is supplied by the caller from the CONFIRMED position book, so
        this can only ever close what the broker has actually filled.
        """
        underlying, opt_type, side = underlying.upper(), opt_type.upper(), side.upper()
        symbol_hint = f"{underlying}{expiry}{int(strike)}{opt_type}"

        if self.mode != LIVE:
            return {"ok": False, "code": "NOT_LIVE",
                    "error": "Live exits are only routed in live mode."}
        closed = market_session.require_open(underlying)
        if closed:
            # Exits are still session-gated: the exchange will not accept one
            # outside hours either, and pretending otherwise hides the real
            # reason a stop could not act.
            self._log("warn", f"[order] ⛔ exit {side} {qty} {symbol_hint} — market closed")
            return closed

        sessions = manager.execution_sessions()
        if not sessions:
            self._log("error", f"[order] ⛔ exit {side} {qty} {symbol_hint} — "
                               f"no execution broker connected")
            return {"ok": False, "code": "EXECUTION_BROKER_DISCONNECTED",
                    "error": "No execution broker is connected to close this position."}

        ctx = self._risk_context(LIVE, underlying, expiry, strike, opt_type, side,
                                 qty, lots or qty, "MARKET", 0.0, "NRML", "DAY",
                                 override_max_pos=True, allow_duplicate=True)
        ctx.is_exit = True
        violation = risk_engine.validate(ctx)
        if violation is not None:
            self._log("error", f"[order] ⛔ exit {side} {qty} {symbol_hint} rejected — "
                               f"{violation.code}")
            return violation.as_response()

        results = []
        for account_id, broker, sess in sessions:
            res = self._place_with_splitting(account_id, broker, sess, underlying,
                                             expiry, strike, opt_type, side, qty,
                                             "MARKET", 0.0, lots or qty,
                                             "NRML", "DAY", symbol_hint)
            res["account"] = account_id
            results.append(res)
            diagnostics.event(
                "orders", "Exit order", "submitted" if res.get("ok") else "rejected",
                level="warn", broker=LABEL.get(broker, broker), account=account_id,
                symbol=symbol_hint, side=side, qty=qty, reason=reason,
                orderId=res.get("orderId"), detail=res.get("error"))
            if res.get("ok") and res.get("orderId"):
                order_sync.track(
                    str(res["orderId"]), account_id, broker, underlying, expiry,
                    strike, opt_type, side, qty,
                    max(1, qty // max(1, lots or qty)), 0.0,
                    token=self._token_for(underlying, expiry, strike, opt_type),
                    exit_for=position_key)
        ok = any(r.get("ok") for r in results)
        return {"ok": ok, "results": results, "symbol": symbol_hint,
                "error": None if ok else "; ".join(
                    str(r.get("error")) for r in results if r.get("error"))}

    # ── splitting-aware live submission for a single account ───────────────
    def _place_with_splitting(self, account_id: str, broker: str, sess: Any,
                              underlying: str, expiry: str, strike: float,
                              opt_type: str, side: str, qty: int, order_type: str,
                              price: float, lots: int, product: str, validity: str,
                              symbol_hint: str) -> dict:
        """Submit `qty` at `broker`, transparently splitting into the minimum
        number of child orders when it exceeds the broker's per-order limit.

        On the first child failure we STOP — the remaining quantity is reported
        back rather than fired blindly, so the user decides whether to retry."""
        placer = self._live_placer(broker)
        if placer is None:
            return {"ok": False, "broker": broker,
                    "error": f"Live order routing for {broker} is not available yet."}

        limits = limit_resolver.resolve(account_id, broker, sess, underlying)
        chunks = split_quantity(qty, lots, limits)

        # Fast path — within the broker limit: identical to the pre-splitting flow.
        if len(chunks) == 1:
            try:
                res = placer(account_id, sess, underlying, expiry, strike, opt_type,
                             side, qty, order_type, price, product, validity)
            except Exception as e:  # defensive — never let one account kill the rest
                res = {"ok": False, "broker": broker, "error": str(e)}
            lvl = "info" if res.get("ok") else "error"
            self._log(lvl, f"[order] {'✅' if res.get('ok') else '❌'} Live {broker} "
                           f"{side} {qty} {symbol_hint}: "
                           f"{res.get('orderId') or res.get('error')}")
            if res.get("ok"):
                res["executedQty"] = qty
            return res

        parent_id = self._next_parent_id()
        self._log("info", f"[order] ✂ Live {broker} {side} {qty} {symbol_hint} exceeds "
                          f"the per-order limit — splitting into {len(chunks)} orders "
                          f"(parent {parent_id})")

        child_ids: list[str] = []
        executed = 0
        for idx, (chunk_qty, _chunk_lots) in enumerate(chunks, start=1):
            try:
                res = placer(account_id, sess, underlying, expiry, strike, opt_type,
                             side, chunk_qty, order_type, price,
                             product, validity)
            except Exception as e:
                res = {"ok": False, "broker": broker, "error": str(e)}
            if not res.get("ok"):
                remaining = qty - executed
                self._log("error", f"[order] ❌ Live {broker} {symbol_hint} child "
                                   f"{idx}/{len(chunks)} failed: {res.get('error')} — "
                                   f"stopping. Executed {executed}, remaining {remaining}.")
                return {"ok": False, "broker": broker, "code": "PARTIAL_FILL",
                        "orderId": parent_id, "parentId": parent_id,
                        "childOrderIds": child_ids, "requestedQty": qty,
                        "executedQty": executed, "remainingQty": remaining,
                        "error": (f"Only part of the requested quantity was executed "
                                  f"({executed} of {qty}). {remaining} remaining.")}
            child_ids.append(str(res.get("orderId")))
            executed += chunk_qty
            self._log("info", f"[order] ✅ Live {broker} {side} {chunk_qty} {symbol_hint} "
                              f"({idx}/{len(chunks)}, parent {parent_id}): "
                              f"{res.get('orderId')}")

        return {"ok": True, "broker": broker, "orderId": parent_id,
                "parentId": parent_id, "childOrderIds": child_ids,
                "requestedQty": qty, "executedQty": executed, "split": len(chunks)}

    # ── live placement dispatch ─────────────────────────────────────────────
    # Broker → placement method name. Every placer shares one signature:
    # (account_id, sess, underlying, expiry, strike, opt_type, side, qty,
    #  order_type, price, product, validity) -> dict. A broker absent here gets
    # the "not available yet" gate in _place_with_splitting — adding live
    # routing for a broker is one method plus one entry.
    _LIVE_PLACERS = {"angel": "_place_angel", "icici": "_place_icici"}

    def _live_placer(self, broker: str):
        name = self._LIVE_PLACERS.get((broker or "").lower())
        return getattr(self, name) if name else None

    # ── Angel live placement (port of app/order_manager.py:184-197) ─────────
    def _place_angel(self, _account_id: str, smart: Any, underlying: str,
                     expiry: str, strike: float,
                     opt_type: str, side: str, qty: int, order_type: str,
                     price: float, product: str = "NRML", validity: str = "DAY") -> dict:
        exch = _ANGEL_EXCH.get(underlying, "NFO")
        tradingsymbol, token, exch = manager.resolve_option(underlying, expiry, strike, opt_type)
        if not token:
            return {"ok": False, "broker": "angel",
                    "error": f"Could not resolve Angel token for {underlying} {expiry} "
                             f"{int(strike)} {opt_type}"}
        # Map profile Product/Validity to Angel SmartAPI params.
        producttype = "CARRYFORWARD" if product == "NRML" else "INTRADAY"
        duration = "IOC" if validity == "IOC" else "DAY"
        params = {
            "variety": "NORMAL",
            "tradingsymbol": tradingsymbol,
            "symboltoken": token,
            "transactiontype": side,           # BUY / SELL
            "exchange": exch,                    # NFO / BFO
            "ordertype": order_type,             # MARKET / LIMIT
            "producttype": producttype,          # CARRYFORWARD (NRML) / INTRADAY (MIS)
            "duration": duration,                # DAY / IOC
            "price": str(price) if order_type == "LIMIT" else "0",
            "quantity": str(int(qty)),
        }
        resp = smart.placeOrder(params)
        # SmartConnect.placeOrder returns the order id (str) or a dict per SDK version.
        order_id = resp.get("data", {}).get("orderid") if isinstance(resp, dict) else resp
        return {"ok": True, "broker": "angel", "orderId": order_id, "tradingsymbol": tradingsymbol}

    # ── ICICI Direct (Breeze) live placement ───────────────────────────────
    # Breeze addresses a contract by (stock_code, exchange_code, expiry, right,
    # strike) — there is no token — so `stock_code` comes from the ICICI scrip
    # master's ShortName column, resolved by the feed's loaded master.
    _ICICI_EXCH = {"SENSEX": "BFO", "BANKEX": "BFO"}

    @staticmethod
    def _icici_expiry(expiry: str) -> str:
        """'02SEP2026' -> Breeze's ISO-with-time form."""
        from datetime import datetime
        return datetime.strptime(expiry, "%d%b%Y").strftime("%Y-%m-%dT06:00:00.000Z")

    def _place_icici(self, account_id: str, breeze: Any, underlying: str,
                     expiry: str, strike: float, opt_type: str, side: str,
                     qty: int, order_type: str, price: float,
                     product: str = "NRML", validity: str = "DAY") -> dict:
        feed = manager.router.feed_for(account_id)
        stock_code = feed.scrip.stock_code_for(underlying) if feed is not None else None
        if not stock_code:
            # Never guess a stock_code — a wrong one is a wrong instrument.
            return {"ok": False, "broker": "icici",
                    "error": f"ICICI stock code for {underlying} is unknown "
                             f"(scrip master not loaded yet) — reconnect the account"}
        try:
            expiry_iso = self._icici_expiry(expiry)
        except ValueError:
            return {"ok": False, "broker": "icici",
                    "error": f"Unparseable expiry '{expiry}' for ICICI"}

        # A non-NRML product never reaches here: rule_broker_capability rejects
        # it up front. Placing an MIS order as carry-forward silently changed
        # what the user asked for, so it is now a rejection, not a downgrade.
        params = {
            "stock_code": stock_code,
            "exchange_code": self._ICICI_EXCH.get(underlying, "NFO"),
            "product": "options",
            "action": side.lower(),                       # buy / sell
            "order_type": order_type.lower(),             # market / limit
            "quantity": str(int(qty)),
            "price": str(price) if order_type == "LIMIT" else "",
            "validity": "ioc" if validity == "IOC" else "day",
            "expiry_date": expiry_iso,
            "right": "call" if opt_type == "CE" else "put",
            "strike_price": str(int(strike)),
            "stoploss": "",
        }
        try:
            resp = breeze.place_order(**params)
        except Exception as e:
            # An auth-shaped failure here must latch the account the same way a
            # feed error does — the day-token dies for orders and data alike.
            manager.session_manager.report_error(account_id, "icici", e)
            raise

        status = resp.get("Status") if isinstance(resp, dict) else None
        err = resp.get("Error") if isinstance(resp, dict) else None
        success = resp.get("Success") if isinstance(resp, dict) else None
        if status != 200 or err or not success:
            detail = err or f"ICICI rejected the order (status {status})"
            manager.session_manager.report_error(account_id, "icici", detail)
            return {"ok": False, "broker": "icici", "error": detail}
        order_id = success.get("order_id") if isinstance(success, dict) else None
        return {"ok": True, "broker": "icici", "orderId": order_id,
                "tradingsymbol": f"{stock_code} {expiry} {int(strike)} {opt_type}"}

    # ── modify / cancel (paper → engine; live routing not yet ported) ──────
    # Same two-key mode contract as place_order: a cancel composed against the
    # paper book must never be applied by a live-mode engine, or vice versa.
    def modify_order(self, mode: str | None, order_id: str, price: float | None,
                     qty: int | None, lots: int | None) -> dict:
        route_mode, mode_error = self._resolve_mode(mode, "modify")
        if mode_error:
            return mode_error
        closed = market_session.require_open(paper_engine.underlying_of(order_id))
        if closed:
            return closed
        if route_mode != LIVE:
            return paper_engine.modify(order_id, price, qty, lots)
        return {"ok": False, "error": "Live order modification is not available yet."}

    def cancel_order(self, mode: str | None, order_id: str) -> dict:
        route_mode, mode_error = self._resolve_mode(mode, "cancel")
        if mode_error:
            return mode_error
        closed = market_session.require_open(paper_engine.underlying_of(order_id))
        if closed:
            return closed
        if route_mode != LIVE:
            return paper_engine.cancel(order_id)
        return {"ok": False, "error": "Live order cancellation is not available yet."}


order_manager = OrderManager()
