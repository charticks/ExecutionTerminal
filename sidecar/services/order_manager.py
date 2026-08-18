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

Live routing covers all four connected brokers — Angel, Kotak, ICICI and Dhan —
for placement, modification and cancellation, through three dispatch tables
(_LIVE_PLACERS / _LIVE_MODIFIERS / _LIVE_CANCELLERS). A broker absent from one
gets a clear "not available yet" rather than silently mis-firing.

Every live submission goes through ONE seam, `_submit_once`, which is where
idempotency is enforced: a client order id is claimed and journalled before the
SDK call and resolved after it, so a timeout, a restart or a double-click cannot
produce two live orders for one intent. See services/idempotency/.
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
from services.idempotency import Placement
from services.idempotency import guard as idempotency_guard
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
                    override_max_pos: bool = False,
                    request_id: str = "",
                    override_duplicate: bool = False) -> dict:
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
                                             product, validity, symbol_hint,
                                             request_id=request_id,
                                             override_duplicate=override_duplicate)
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
                token = self._token_for(underlying, expiry, strike, opt_type)
                # A split order is tracked as its CHILDREN, never as its parent:
                # the parent id is ours, not the broker's, so polling it found
                # nothing and no fill was ever booked. The parent id is recorded
                # on each child so the UI can still amend or cancel the whole
                # trade by the id it was given.
                parent = str(res.get("parentId") or "")
                legs = res.get("children") or [
                    {"orderId": str(res["orderId"]), "qty": submitted or qty}]
                for leg in legs:
                    order_sync.track(
                        str(leg["orderId"]), account_id, broker, underlying, expiry,
                        strike, opt_type, side, int(leg["qty"]), lot_size, price,
                        token=token, rule=rule, product=product,
                        order_type=order_type, validity=validity,
                        parent_id=parent if len(legs) > 1 else "",
                        client_order_id=str(res.get("clientOrderId") or ""))
            else:
                # Nothing to track — report the rejection once, here. It still
                # carries the full contract: a rejected order belongs in the
                # Order Book as much as a filled one, and with only a symbol
                # string the renderer could not build the row.
                hub.publish(events.order_update(
                    str(res.get("orderId") or f"{broker}-{symbol_hint}"),
                    f"{underlying} {expiry} {int(strike)} {opt_type}",
                    side, qty, price, "REJECTED",
                    underlying=underlying, expiry=expiry, strike=strike,
                    optType=opt_type, requestedQty=qty, filledQty=0,
                    limitPrice=price, orderType=order_type, product=product,
                    validity=validity, account=account_id, broker=broker,
                    reason=res.get("error")))

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
                                             "NRML", "DAY", symbol_hint,
                                             request_id=f"exit:{position_key}")
            res["account"] = account_id
            results.append(res)
            diagnostics.event(
                "orders", "Exit order", "submitted" if res.get("ok") else "rejected",
                level="warn", broker=LABEL.get(broker, broker), account=account_id,
                symbol=symbol_hint, side=side, qty=qty, reason=reason,
                orderId=res.get("orderId"), detail=res.get("error"))
            if res.get("ok") and res.get("orderId"):
                lot_size = max(1, qty // max(1, lots or qty))
                token = self._token_for(underlying, expiry, strike, opt_type)
                parent = str(res.get("parentId") or "")
                legs = res.get("children") or [
                    {"orderId": str(res["orderId"]), "qty": qty}]
                for leg in legs:
                    order_sync.track(
                        str(leg["orderId"]), account_id, broker, underlying, expiry,
                        strike, opt_type, side, int(leg["qty"]), lot_size, 0.0,
                        token=token, exit_for=position_key,
                        order_type="MARKET",
                        parent_id=parent if len(legs) > 1 else "",
                        client_order_id=str(res.get("clientOrderId") or ""))
        ok = any(r.get("ok") for r in results)
        return {"ok": ok, "results": results, "symbol": symbol_hint,
                "error": None if ok else "; ".join(
                    str(r.get("error")) for r in results if r.get("error"))}

    # ── splitting-aware live submission for a single account ───────────────
    # Errors that mean "the broker may or may not have received this". A timeout
    # or a dropped connection is the one failure mode that must NOT be recorded
    # as a rejection: doing so authorises a retry that can double the position.
    _UNRESOLVED_MARKERS = ("timed out", "timeout", "read timeout", "connection reset",
                           "connection aborted", "connection broken", "remote end closed",
                           "broken pipe", "econnreset", "temporarily unavailable",
                           "bad gateway", "gateway timeout", "service unavailable",
                           "502", "503", "504")

    @classmethod
    def _is_unresolved(cls, error: Any) -> bool:
        text = str(error).lower()
        return any(marker in text for marker in cls._UNRESOLVED_MARKERS)

    def _submit_once(self, placer, account_id: str, broker: str, sess: Any,
                     underlying: str, expiry: str, strike: float, opt_type: str,
                     side: str, qty: int, order_type: str, price: float,
                     product: str, validity: str, request_id: str = "",
                     leg: int = 0, override_duplicate: bool = False) -> dict:
        """Send exactly one broker order, at most once.

        The only place in the engine that touches a live placement API, and
        therefore the only place idempotency has to be enforced. Everything
        broker-specific is behind the registered adapter (services/idempotency/),
        so a new broker joins by registering one — this method does not change.
        """
        placement = Placement(
            account_id=account_id, broker=broker, underlying=underlying,
            expiry=expiry, strike=strike, opt_type=opt_type, side=side,
            qty=int(qty), order_type=order_type, price=float(price),
            product=product, validity=validity, leg=leg, request_id=request_id or "")
        decision = idempotency_guard.claim(placement, override=override_duplicate)

        if decision.duplicate:
            # This order is already at the broker. Report it as the success it is,
            # carrying the existing order id, so the caller books nothing new and
            # the UI shows one order rather than two.
            self._log("warn", f"[order] ⛔ Live {broker} {side} {qty} {underlying} "
                              f"{int(strike)} {opt_type} suppressed as a duplicate "
                              f"of {decision.order_id or decision.coid} "
                              f"({decision.detail})")
            return {"ok": True, "broker": broker, "orderId": decision.order_id,
                    "duplicate": True, "clientOrderId": decision.coid,
                    "detail": f"Already placed — {decision.detail}"}
        if decision.blocked:
            self._log("warn", f"[order] ⛔ Live {broker} {side} {qty} {underlying} "
                              f"{int(strike)} {opt_type} held — {decision.code}: "
                              f"{decision.detail}")
            return decision.as_response()

        try:
            res = placer(account_id, sess, underlying, expiry, strike, opt_type,
                         side, qty, order_type, price, product, validity,
                         client_order_id=decision.coid)
        except Exception as e:  # defensive — never let one account kill the rest
            if self._is_unresolved(e):
                idempotency_guard.unresolved(decision, str(e))
                return {"ok": False, "broker": broker,
                        "code": "IDEMPOTENCY_UNRESOLVED",
                        "clientOrderId": decision.coid,
                        "error": (f"The connection to {LABEL.get(broker, broker)} "
                                  f"failed before it answered ({e}), so Charticks "
                                  f"cannot tell whether the order was accepted. It "
                                  f"will check the broker before sending again — "
                                  f"do not retry from another window.")}
            idempotency_guard.failed(decision, str(e))
            return {"ok": False, "broker": broker, "error": str(e),
                    "clientOrderId": decision.coid}

        res.setdefault("clientOrderId", decision.coid)
        if res.get("ok") and res.get("orderId"):
            idempotency_guard.placed(decision, str(res["orderId"]))
        elif self._is_unresolved(res.get("error")):
            idempotency_guard.unresolved(decision, str(res.get("error")))
            res["code"] = "IDEMPOTENCY_UNRESOLVED"
        else:
            idempotency_guard.failed(decision, str(res.get("error") or "rejected"))
        return res

    def _place_with_splitting(self, account_id: str, broker: str, sess: Any,
                              underlying: str, expiry: str, strike: float,
                              opt_type: str, side: str, qty: int, order_type: str,
                              price: float, lots: int, product: str, validity: str,
                              symbol_hint: str, request_id: str = "",
                              override_duplicate: bool = False) -> dict:
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
            res = self._submit_once(placer, account_id, broker, sess, underlying,
                                    expiry, strike, opt_type, side, qty, order_type,
                                    price, product, validity, request_id, leg=0,
                                    override_duplicate=override_duplicate)
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
        children: list[dict] = []
        executed = 0
        for idx, (chunk_qty, _chunk_lots) in enumerate(chunks, start=1):
            # `leg=idx`: each child is its own broker order and needs its own
            # client id, or three identical legs would collapse into one claim
            # and two of them would be suppressed as duplicates of the first.
            res = self._submit_once(placer, account_id, broker, sess, underlying,
                                    expiry, strike, opt_type, side, chunk_qty,
                                    order_type, price, product, validity,
                                    request_id, leg=idx,
                                    override_duplicate=override_duplicate)
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
            children.append({"orderId": str(res.get("orderId")), "qty": chunk_qty})
            executed += chunk_qty
            self._log("info", f"[order] ✅ Live {broker} {side} {chunk_qty} {symbol_hint} "
                              f"({idx}/{len(chunks)}, parent {parent_id}): "
                              f"{res.get('orderId')}")

        return {"ok": True, "broker": broker, "orderId": parent_id,
                "parentId": parent_id, "childOrderIds": child_ids,
                # Per-child quantities, because each child is a real order at the
                # broker and has to be tracked as one. Tracking the synthetic
                # parent instead meant the broker's order book never contained the
                # id being polled: fills were never booked and the order sat
                # pending until it timed out. See the tracking block in place_order.
                "children": children,
                "requestedQty": qty, "executedQty": executed, "split": len(chunks)}

    # ── live placement dispatch ─────────────────────────────────────────────
    # Broker → placement method name. Every placer shares one signature:
    # (account_id, sess, underlying, expiry, strike, opt_type, side, qty,
    #  order_type, price, product, validity) -> dict. A broker absent here gets
    # the "not available yet" gate in _place_with_splitting — adding live
    # routing for a broker is one method plus one entry.
    _LIVE_PLACERS = {"angel": "_place_angel", "icici": "_place_icici",
                     "kotak": "_place_kotak", "dhan": "_place_dhan"}

    def _live_placer(self, broker: str):
        name = self._LIVE_PLACERS.get((broker or "").lower())
        return getattr(self, name) if name else None

    # Modify / cancel dispatch, same shape as placement. Both take the TrackedOrder
    # the synchronization engine holds — a broker's modify API wants the whole
    # order restated (product, order type, validity, symbol, token), not a diff,
    # so the tracked record is the only place all of it exists after placement.
    #   modifier(account_id, sess, order, price, qty) -> dict
    #   canceller(account_id, sess, order)            -> dict
    _LIVE_MODIFIERS = {"angel": "_modify_angel", "icici": "_modify_icici",
                       "kotak": "_modify_kotak", "dhan": "_modify_dhan"}
    _LIVE_CANCELLERS = {"angel": "_cancel_angel", "icici": "_cancel_icici",
                        "kotak": "_cancel_kotak", "dhan": "_cancel_dhan"}

    def _live_modifier(self, broker: str):
        name = self._LIVE_MODIFIERS.get((broker or "").lower())
        return getattr(self, name) if name else None

    def _live_canceller(self, broker: str):
        name = self._LIVE_CANCELLERS.get((broker or "").lower())
        return getattr(self, name) if name else None

    # ── Angel live placement (port of app/order_manager.py:184-197) ─────────
    def _place_angel(self, _account_id: str, smart: Any, underlying: str,
                     expiry: str, strike: float,
                     opt_type: str, side: str, qty: int, order_type: str,
                     price: float, product: str = "NRML", validity: str = "DAY",
                     client_order_id: str = "") -> dict:
        # client_order_id is accepted and NOT sent: SmartAPI's order params carry
        # no client-id field. Angel is reconciled on order attributes instead —
        # see services/idempotency/brokers.py. Accepting the argument anyway keeps
        # one placer signature for every broker.
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

    # ── Dhan HQ live placement ─────────────────────────────────────────────
    # Dhan addresses a contract by (securityId, exchangeSegment). The securityId
    # is what the instruments registry holds for the "dhan" namespace; the
    # segment comes from the scrip master's own classification, because Dhan's
    # ORDER api takes a string ("MCX_COMM") where its FEED takes an int (5).
    _DHAN_PRODUCT = {"NRML": "MARGIN", "MIS": "INTRADAY"}

    @staticmethod
    def _dhan_response(resp: Any) -> tuple[str, str]:
        """(order id, error) from a dhanhq response.

        dhanhq never raises for a rejected order: every call comes back as
        {"status": "success"|"failure", "remarks": …, "data": …}, so a caller
        that only catches exceptions treats a rejection as a fill.
        """
        if not isinstance(resp, dict):
            return "", f"Dhan returned an unexpected response: {resp!r}"
        if str(resp.get("status", "")).lower() != "success":
            remarks = resp.get("remarks")
            if isinstance(remarks, dict):
                detail = (remarks.get("error_message") or remarks.get("error_type")
                          or remarks.get("error_code"))
            else:
                detail = remarks
            return "", str(detail or f"Dhan rejected the request ({resp})")
        data = resp.get("data")
        if isinstance(data, dict):
            order_id = data.get("orderId") or data.get("order_id") or ""
            # Dhan echoes an orderStatus alongside the id; REJECTED there is a
            # rejection even though the HTTP call succeeded.
            status = str(data.get("orderStatus") or "").upper()
            if status == "REJECTED":
                return "", str(data.get("omsErrorDescription")
                               or data.get("remarks") or "Dhan rejected the order")
            return str(order_id), "" if order_id else "Dhan returned no order id"
        return "", f"Dhan returned no order data ({resp})"

    def _place_dhan(self, account_id: str, dhan: Any, underlying: str,
                    expiry: str, strike: float, opt_type: str, side: str,
                    qty: int, order_type: str, price: float,
                    product: str = "NRML", validity: str = "DAY",
                    client_order_id: str = "") -> dict:
        from services.instruments import InstrumentKey, instruments

        key = InstrumentKey.option(underlying, expiry, strike, opt_type)
        security_id = instruments.token_for("dhan", key)
        feed = manager.router.feed_for(account_id)
        segment = feed.scrip.rest_segment_for(key) if feed is not None else ""
        if not security_id or not segment:
            # Never guess either half — a wrong security id or the wrong exchange
            # is a wrong instrument, not a near miss.
            return {"ok": False, "broker": "dhan",
                    "error": f"Dhan security id for {underlying} {expiry} "
                             f"{int(strike)} {opt_type} is unknown (scrip master not "
                             f"loaded) — reconnect the Dhan account"}
        try:
            resp = dhan.place_order(
                security_id=str(security_id),
                exchange_segment=segment,
                transaction_type=side,                 # BUY / SELL
                quantity=int(qty),
                order_type=order_type,                 # MARKET / LIMIT
                product_type=self._DHAN_PRODUCT.get(product, "MARGIN"),
                price=float(price) if order_type == "LIMIT" else 0.0,
                validity="IOC" if validity == "IOC" else "DAY",
                # dhanhq sends `tag` as correlationId, which is Dhan's native
                # client order id and is queryable via get_order_by_correlationID.
                tag=client_order_id or None,
            )
        except Exception as e:
            manager.session_manager.report_error(account_id, "dhan", e)
            raise

        order_id, error = self._dhan_response(resp)
        if error:
            manager.session_manager.report_error(account_id, "dhan", error)
            return {"ok": False, "broker": "dhan", "error": error}
        return {"ok": True, "broker": "dhan", "orderId": order_id,
                "tradingsymbol": f"{underlying} {expiry} {int(strike)} {opt_type}"}

    # ── Kotak Neo live placement ───────────────────────────────────────────
    # NeoAPI addresses a contract by (exchange_segment, trading_symbol) — the
    # pTrdSymbol from the scrip master, which is also what the feed subscribes
    # with, so a symbol that streams is a symbol that can be traded.
    #
    # Its vocabulary differs from every other broker's and it validates strictly
    # (see neo_api_client/req_data_validation.place_order_validation): side is
    # B/S not BUY/SELL, order type is MKT/L not MARKET/LIMIT, and price and
    # quantity must be STRINGS or the SDK raises ApiValueError. Those mappings
    # are all here so no caller has to know them.
    _KOTAK_SIDE = {"BUY": "B", "SELL": "S"}
    _KOTAK_ORDER_TYPE = {"MARKET": "MKT", "LIMIT": "L"}

    @staticmethod
    def _kotak_feed(account_id: str):
        """This account's Kotak feed, which owns the instrument list."""
        try:
            return manager.router.feed_for(account_id)
        except Exception:
            return None

    def _kotak_reload_symbol(self, account_id: str, key) -> str:
        """Re-read Kotak's instrument list once, then look the contract up again.

        The instrument list is loaded at login. If that download failed, every
        order was rejected for the rest of the session with an instruction to
        reconnect — a repair the engine can attempt itself, and the moment a
        user is trying to trade is exactly when it is worth attempting.
        """
        feed = self._kotak_feed(account_id)
        if feed is None or not hasattr(feed, "reload_instruments"):
            return ""
        self._log("warn", f"[order] Kotak does not list {key} — reloading its "
                          f"instrument master before rejecting the order")
        try:
            if not feed.reload_instruments():
                return ""
        except Exception as exc:
            diagnostics.exception("orders", "Kotak instrument reload failed",
                                  exc_info=exc, symbol=str(key))
            return ""
        from services.instruments import instruments as registry
        return registry.token_for("kotak", key) or ""

    def _kotak_unknown_symbol(self, account_id: str, underlying: str, expiry: str,
                              strike: float, opt_type: str) -> str:
        """Say WHY the contract could not be resolved.

        Three quite different situations produced one message that fitted none
        of them: the list never loaded, the list loaded but does not contain
        this contract, or Kotak does not trade this underlying at all. Only the
        first is fixed by reconnecting.
        """
        contract = f"{underlying} {expiry} {int(strike)} {opt_type}"
        feed = self._kotak_feed(account_id)
        scrip = getattr(feed, "scrip", None) if feed is not None else None
        loaded = int(getattr(scrip, "row_count", 0) or 0)
        options = int(getattr(scrip, "option_count", 0) or 0)
        reason = getattr(scrip, "last_error", None) if scrip is not None else None

        if feed is None:
            return (f"Kotak has no market-data feed attached to this account, so "
                    f"{contract} cannot be resolved. Reconnect the Kotak account.")
        # Counted in OPTIONS. A list holding only index spots is not a list that
        # can trade, and reporting its size ("4 instruments") sent the reader
        # looking for a wrong expiry instead of a failed download.
        if options == 0:
            return (f"Kotak's instrument list has no tradable options in it"
                    + (f" — {reason}" if reason else "")
                    + f", so {contract} cannot be traded"
                    + (f" (the list did load {loaded:,} non-option entries, which is "
                       f"why this is not simply an empty download)" if loaded else "")
                    + f". Charticks retries this automatically; if it keeps failing, "
                      f"reconnect the Kotak account and send broker.log.")
        from services.feeds.kotak_scrip import OPT_SEGMENT
        if underlying.upper() not in OPT_SEGMENT:
            return (f"Charticks does not have a Kotak exchange segment mapped for "
                    f"{underlying}, so it cannot route {contract} there.")

        # Say what the master DOES hold for this underlying, expiry and strike.
        # The previous message asserted absence and blamed the expiry, which was
        # wrong often enough to send people re-checking a correct expiry.
        from services.instruments import InstrumentKey
        key = InstrumentKey.option(underlying, expiry, strike, opt_type)
        detail = ""
        try:
            if scrip is not None and hasattr(scrip, "explain_miss"):
                detail = scrip.explain_miss(key)
        except Exception as exc:      # diagnosis must never mask the rejection
            diagnostics.exception("orders", "Kotak miss diagnosis failed",
                                  exc_info=exc, symbol=str(key))
        diagnostics.emit("orders", "warn", "Kotak contract not resolved",
                         requested=key.position_id, underlying=underlying,
                         expiry=expiry, strike=int(strike), optType=opt_type,
                         optionsLoaded=options, instrumentsLoaded=loaded,
                         diagnosis=detail or "(unavailable)")
        return (f"Kotak's instrument list ({options:,} options) does not contain "
                f"{contract}"
                + (f" — {detail}" if detail else "")
                + ". Its master is refreshed daily, so a contract added today may "
                  "need the account reconnected.")

    def _place_kotak(self, account_id: str, client: Any, underlying: str,
                     expiry: str, strike: float, opt_type: str, side: str,
                     qty: int, order_type: str, price: float,
                     product: str = "NRML", validity: str = "DAY",
                     client_order_id: str = "") -> dict:
        from services.feeds.kotak_scrip import OPT_SEGMENT
        from services.instruments import InstrumentKey, instruments

        key = InstrumentKey.option(underlying, expiry, strike, opt_type)
        trading_symbol = instruments.token_for("kotak", key)
        if not trading_symbol:
            # Never guess a trading symbol — a wrong one is a wrong instrument.
            #
            # But an unresolved contract is worth ONE repair attempt before
            # refusing: the most common cause is an instrument list that failed
            # to download at login, and asking the user to reconnect made them
            # fix by hand something the engine can fix by asking again.
            trading_symbol = self._kotak_reload_symbol(account_id, key)
        if not trading_symbol:
            return {"ok": False, "broker": "kotak",
                    "error": self._kotak_unknown_symbol(
                        account_id, underlying, expiry, strike, opt_type)}
        segment = OPT_SEGMENT.get(underlying)
        if segment is None:
            return {"ok": False, "broker": "kotak",
                    "error": f"Kotak does not have an exchange segment mapped for "
                             f"{underlying}"}
        tt = self._KOTAK_SIDE.get(side)
        pt = self._KOTAK_ORDER_TYPE.get(order_type)
        if tt is None or pt is None:
            return {"ok": False, "broker": "kotak",
                    "error": f"Kotak cannot place a {side} {order_type} order"}

        try:
            resp = client.place_order(
                exchange_segment=segment,
                product="MIS" if product == "MIS" else "NRML",
                price=str(price) if order_type == "LIMIT" else "0",
                order_type=pt,
                quantity=str(int(qty)),
                validity="IOC" if validity == "IOC" else "DAY",
                trading_symbol=trading_symbol,
                transaction_type=tt,
                # NeoAPI sends `tag` as the body field `ig`, echoed on the order
                # book row as GuiOrdId.
                tag=client_order_id or None,
            )
        except Exception as e:
            manager.session_manager.report_error(account_id, "kotak", e)
            raise

        # NeoAPI returns failure rather than raising it — both an SDK-level
        # {"Error": ...} and an API-level {"stat": "Not_Ok"}. Treating a
        # non-exception as success is how a rejected order gets booked as a
        # position that does not exist.
        if not isinstance(resp, dict):
            return {"ok": False, "broker": "kotak",
                    "error": f"Kotak returned an unexpected response: {resp!r}"}
        detail = resp.get("Error") or resp.get("error") or resp.get("Error Message")
        if detail:
            manager.session_manager.report_error(account_id, "kotak", detail)
            return {"ok": False, "broker": "kotak", "error": str(detail)}
        order_id = resp.get("nOrdNo") or resp.get("orderId") or resp.get("ordNo")
        if str(resp.get("stat", "Ok")).lower() not in ("ok", "200") or not order_id:
            detail = (resp.get("errMsg") or resp.get("emsg")
                      or f"Kotak rejected the order ({resp})")
            manager.session_manager.report_error(account_id, "kotak", detail)
            return {"ok": False, "broker": "kotak", "error": str(detail)}
        return {"ok": True, "broker": "kotak", "orderId": str(order_id),
                "tradingsymbol": trading_symbol}

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
                     product: str = "NRML", validity: str = "DAY",
                     client_order_id: str = "") -> dict:
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
            # Breeze echoes user_remark on the order row, which is what makes a
            # timed-out ICICI placement reconcilable.
            "user_remark": client_order_id,
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

    # ── live modify, per broker ─────────────────────────────────────────────
    # Every broker's modify API restates the ORDER, not the change, and each
    # wants a different subset under different names. `order` is the tracked
    # record, so an unchanged field is resubmitted as it was placed rather than
    # defaulted — a modify that silently turns MIS into NRML, or a LIMIT into a
    # MARKET, is a different order than the user asked for.

    def _modify_angel(self, account_id: str, smart: Any, order: Any,
                      price: float, qty: int) -> dict:
        tradingsymbol, token, exch = manager.resolve_option(
            order.underlying, order.expiry, order.strike, order.opt_type)
        if not token:
            return {"ok": False, "broker": "angel",
                    "error": f"Could not resolve Angel token for {order.symbol}"}
        params = {
            "variety": "NORMAL",
            "orderid": str(order.order_id),
            "tradingsymbol": tradingsymbol,
            "symboltoken": token,
            "exchange": exch,
            # A price is only meaningful on a LIMIT order, and a modify that
            # carries one implies the user wants a limit — so an amended price on
            # a MARKET order converts it, rather than being silently dropped.
            "ordertype": "LIMIT" if price > 0 else order.order_type,
            "producttype": "CARRYFORWARD" if order.product == "NRML" else "INTRADAY",
            "duration": "IOC" if order.validity == "IOC" else "DAY",
            "price": str(price if price > 0 else order.price),
            "quantity": str(int(qty)),
        }
        try:
            resp = smart.modifyOrder(params)
        except Exception as e:
            manager.session_manager.report_error(account_id, "angel", e)
            raise
        return self._angel_ack("angel", resp, order.order_id)

    @staticmethod
    def _angel_ack(broker: str, resp: Any, order_id: str) -> dict:
        """SmartAPI reports failure in the body (`status: false`), not by raising."""
        if isinstance(resp, dict) and resp.get("status") is False:
            return {"ok": False, "broker": broker,
                    "error": str(resp.get("message") or resp.get("errorcode")
                                 or f"Angel rejected the request ({resp})")}
        returned = ""
        if isinstance(resp, dict):
            data = resp.get("data")
            if isinstance(data, dict):
                returned = str(data.get("orderid") or "")
        return {"ok": True, "broker": broker, "orderId": returned or str(order_id)}

    def _modify_kotak(self, account_id: str, client: Any, order: Any,
                      price: float, qty: int) -> dict:
        from services.feeds.kotak_scrip import OPT_SEGMENT
        from services.instruments import InstrumentKey, instruments

        key = InstrumentKey.option(order.underlying, order.expiry, order.strike,
                                   order.opt_type)
        trading_symbol = instruments.token_for("kotak", key)
        scrip = manager.router.scrip_of("kotak", client)
        token = scrip.token_for(key) if scrip is not None else ""
        segment = OPT_SEGMENT.get(order.underlying)
        if not (trading_symbol and token and segment):
            return {"ok": False, "broker": "kotak",
                    "error": f"Kotak cannot identify {order.symbol} (scrip master "
                             f"not loaded) — reconnect the Kotak account"}
        try:
            # Pass the full contract, which selects the SDK's "quick" path. The
            # order-id-only path re-reads the order book and, in this SDK version,
            # applies none of the vocabulary mapping the full path does.
            resp = client.modify_order(
                order_id=str(order.order_id),
                price=str(price if price > 0 else order.price),
                order_type="L" if price > 0 or order.order_type == "LIMIT" else "MKT",
                quantity=str(int(qty)),
                validity="IOC" if order.validity == "IOC" else "DAY",
                instrument_token=str(token),
                exchange_segment=segment,
                product="MIS" if order.product == "MIS" else "NRML",
                trading_symbol=trading_symbol,
                transaction_type=self._KOTAK_SIDE.get(order.side, "B"),
                filled_quantity=str(int(order.filled_qty)),
            )
        except Exception as e:
            manager.session_manager.report_error(account_id, "kotak", e)
            raise
        return self._kotak_ack(account_id, resp, order.order_id)

    def _kotak_ack(self, account_id: str, resp: Any, order_id: str) -> dict:
        if not isinstance(resp, dict):
            return {"ok": False, "broker": "kotak",
                    "error": f"Kotak returned an unexpected response: {resp!r}"}
        detail = resp.get("Error") or resp.get("error") or resp.get("Error Message")
        if not detail and str(resp.get("stat", "Ok")).lower() not in ("ok", "200"):
            detail = resp.get("errMsg") or resp.get("emsg") or f"Kotak refused ({resp})"
        if detail:
            manager.session_manager.report_error(account_id, "kotak", detail)
            return {"ok": False, "broker": "kotak", "error": str(detail)}
        returned = resp.get("nOrdNo") or resp.get("orderId") or resp.get("ordNo")
        return {"ok": True, "broker": "kotak", "orderId": str(returned or order_id)}

    def _modify_dhan(self, account_id: str, dhan: Any, order: Any,
                     price: float, qty: int) -> dict:
        try:
            resp = dhan.modify_order(
                order_id=str(order.order_id),
                order_type="LIMIT" if price > 0 or order.order_type == "LIMIT" else "MARKET",
                # "NA" is Dhan's own value for a plain order: legName applies to
                # bracket/cover legs, and an empty string is rejected.
                leg_name="NA",
                quantity=int(qty),
                price=float(price if price > 0 else order.price),
                trigger_price=0,
                disclosed_quantity=0,
                validity="IOC" if order.validity == "IOC" else "DAY",
            )
        except Exception as e:
            manager.session_manager.report_error(account_id, "dhan", e)
            raise
        returned, error = self._dhan_response(resp)
        if error:
            manager.session_manager.report_error(account_id, "dhan", error)
            return {"ok": False, "broker": "dhan", "error": error}
        return {"ok": True, "broker": "dhan", "orderId": returned or str(order.order_id)}

    def _modify_icici(self, account_id: str, breeze: Any, order: Any,
                      price: float, qty: int) -> dict:
        try:
            resp = breeze.modify_order(
                order_id=str(order.order_id),
                exchange_code=self._ICICI_EXCH.get(order.underlying, "NFO"),
                order_type="limit" if price > 0 or order.order_type == "LIMIT" else "market",
                stoploss="",
                quantity=str(int(qty)),
                price=str(price if price > 0 else order.price),
                validity="ioc" if order.validity == "IOC" else "day",
                disclosed_quantity="0",
                validity_date="",
            )
        except Exception as e:
            manager.session_manager.report_error(account_id, "icici", e)
            raise
        return self._icici_ack(account_id, resp, order.order_id)

    def _icici_ack(self, account_id: str, resp: Any, order_id: str) -> dict:
        if not isinstance(resp, dict):
            return {"ok": False, "broker": "icici",
                    "error": f"ICICI returned an unexpected response: {resp!r}"}
        err = resp.get("Error")
        if err or resp.get("Status") != 200:
            detail = err or f"ICICI refused the request (status {resp.get('Status')})"
            manager.session_manager.report_error(account_id, "icici", detail)
            return {"ok": False, "broker": "icici", "error": str(detail)}
        success = resp.get("Success")
        returned = success.get("order_id") if isinstance(success, dict) else None
        return {"ok": True, "broker": "icici", "orderId": str(returned or order_id)}

    # ── live cancel, per broker ─────────────────────────────────────────────

    def _cancel_angel(self, account_id: str, smart: Any, order: Any) -> dict:
        try:
            resp = smart.cancelOrder(str(order.order_id), "NORMAL")
        except Exception as e:
            manager.session_manager.report_error(account_id, "angel", e)
            raise
        return self._angel_ack("angel", resp, order.order_id)

    def _cancel_kotak(self, account_id: str, client: Any, order: Any) -> dict:
        try:
            # isVerify=False deliberately: the SDK's verify path re-reads the
            # order book and returns {"Error": "The Given Order Status is …"} for
            # an order that has since filled, which reads as a failure when the
            # honest answer is "too late". The sync engine already holds status.
            resp = client.cancel_order(order_id=str(order.order_id), amo="NO")
        except Exception as e:
            manager.session_manager.report_error(account_id, "kotak", e)
            raise
        return self._kotak_ack(account_id, resp, order.order_id)

    def _cancel_dhan(self, account_id: str, dhan: Any, order: Any) -> dict:
        try:
            resp = dhan.cancel_order(str(order.order_id))
        except Exception as e:
            manager.session_manager.report_error(account_id, "dhan", e)
            raise
        returned, error = self._dhan_response(resp)
        if error:
            manager.session_manager.report_error(account_id, "dhan", error)
            return {"ok": False, "broker": "dhan", "error": error}
        return {"ok": True, "broker": "dhan", "orderId": returned or str(order.order_id)}

    def _cancel_icici(self, account_id: str, breeze: Any, order: Any) -> dict:
        try:
            resp = breeze.cancel_order(
                exchange_code=self._ICICI_EXCH.get(order.underlying, "NFO"),
                order_id=str(order.order_id))
        except Exception as e:
            manager.session_manager.report_error(account_id, "icici", e)
            raise
        return self._icici_ack(account_id, resp, order.order_id)

    # ── modify / cancel ─────────────────────────────────────────────────────
    # Same two-key mode contract as place_order: a cancel composed against the
    # paper book must never be applied by a live-mode engine, or vice versa.
    def modify_order(self, mode: str | None, order_id: str, price: float | None,
                     qty: int | None, lots: int | None) -> dict:
        route_mode, mode_error = self._resolve_mode(mode, "modify")
        if mode_error:
            return mode_error
        if route_mode != LIVE:
            closed = market_session.require_open(paper_engine.underlying_of(order_id))
            if closed:
                return closed
            return paper_engine.modify(order_id, price, qty, lots)
        return self._amend_live("modify", order_id, price, qty, lots)

    def cancel_order(self, mode: str | None, order_id: str) -> dict:
        route_mode, mode_error = self._resolve_mode(mode, "cancel")
        if mode_error:
            return mode_error
        if route_mode != LIVE:
            closed = market_session.require_open(paper_engine.underlying_of(order_id))
            if closed:
                return closed
            return paper_engine.cancel(order_id)
        return self._amend_live("cancel", order_id, None, None, None)

    def _amend_live(self, action: str, order_id: str, price: float | None,
                    qty: int | None, lots: int | None) -> dict:
        """Modify or cancel a working LIVE order.

        One body for both because everything except the final SDK call is shared:
        resolve the id to the orders that actually exist at a broker, find each
        one's session, apply, and report. A split order resolves to several
        children and every one is acted on — a "cancel" that pulled one leg of
        three would leave a position nobody asked for.
        """
        orders = order_sync.resolve(order_id)
        if not orders:
            # Known-but-terminal and never-seen are different problems.
            if order_sync.known(order_id):
                return {"ok": False, "code": "ORDER_NOT_WORKING",
                        "error": "That order is no longer working — it has already "
                                 "filled, been cancelled or been rejected."}
            return {"ok": False, "code": "UNKNOWN_ORDER",
                    "error": f"Charticks is not tracking a live order {order_id}. "
                             f"Only orders placed from Charticks can be amended."}

        # Session gating uses the instrument's own hours, from the tracked order
        # rather than the paper book — MCX runs hours past the equity close, and
        # looking a live id up in the paper book always missed.
        closed = market_session.require_open(orders[0].underlying)
        if closed:
            return closed
        if action == "modify" and (price is None or price <= 0) and qty is None and lots is None:
            return {"ok": False, "code": "NOTHING_TO_MODIFY",
                    "error": "Give a new price or a new quantity to modify."}
        if action == "modify" and len(orders) > 1 and (qty is not None or lots is not None):
            # A split order's quantity is spread across its legs, so one new
            # quantity has no single correct meaning: applied per leg it
            # multiplies the position by the number of legs, and redistributing
            # it would silently resize legs the user never saw. Price amends
            # every leg unambiguously, so those are allowed.
            return {"ok": False, "code": "SPLIT_QTY_NOT_MODIFIABLE",
                    "error": (f"This order was split across {len(orders)} broker "
                              f"orders, so its quantity cannot be changed as one. "
                              f"Cancel it and place the quantity you want, or amend "
                              f"the price only.")}

        results: list[dict] = []
        for order in orders:
            results.append(self._amend_one(action, order, price, qty, lots))

        ok = all(r.get("ok") for r in results)
        failures = [r for r in results if not r.get("ok")]
        out: dict[str, Any] = {"ok": ok, "orderId": order_id, "results": results}
        if failures:
            out["error"] = failures[0].get("error")
            if len(orders) > 1:
                # Partial outcome on a split order is the dangerous case: say so
                # explicitly rather than reporting the first error as the whole
                # story, because some legs may now be amended and others not.
                done = len(results) - len(failures)
                out["code"] = "PARTIAL_AMEND"
                out["error"] = (f"{action.title()} succeeded on {done} of "
                                f"{len(results)} split legs. {failures[0].get('error')}")
        return out

    def _amend_one(self, action: str, order: Any, price: float | None,
                   qty: int | None, lots: int | None) -> dict:
        """Apply one modify/cancel to one real broker order."""
        session = manager.live_session(order.account_id)
        if session is None:
            return {"ok": False, "orderId": order.order_id,
                    "error": f"The {LABEL.get(order.broker, order.broker)} account "
                             f"holding order {order.order_id} is not connected."}
        broker, sess = session
        handler = (self._live_modifier(broker) if action == "modify"
                   else self._live_canceller(broker))
        if handler is None:
            return {"ok": False, "orderId": order.order_id,
                    "error": f"Live order {action} for {broker} is not available yet."}

        if action == "modify":
            # Quantity in LOTS when given that way, because that is how the user
            # thinks and how the broker's limit is expressed.
            new_qty = order.qty
            if lots is not None and lots > 0:
                new_qty = int(lots) * max(1, order.lot_size)
            elif qty is not None and qty > 0:
                new_qty = int(qty)
            args = (order, float(price or 0.0), int(new_qty))
        else:
            args = (order,)

        diagnostics.event("orders", f"{action.title()} Order", "started",
                          broker=LABEL.get(broker, broker), account=order.account_id,
                          symbol=order.symbol, orderId=order.order_id,
                          newPrice=price, newQty=qty, newLots=lots)
        try:
            res = handler(order.account_id, sess, *args)
        except Exception as e:
            # Defensive, exactly as placement is: one broker's SDK blowing up
            # must not take down a multi-leg amend.
            res = {"ok": False, "error": str(e)}
        res.setdefault("orderId", order.order_id)
        diagnostics.event("orders", f"{action.title()} Order",
                          "success" if res.get("ok") else "failed",
                          broker=LABEL.get(broker, broker), account=order.account_id,
                          symbol=order.symbol, orderId=order.order_id,
                          reason=res.get("error"))
        self._log("info" if res.get("ok") else "error",
                  f"[order] {'✅' if res.get('ok') else '❌'} Live {broker} {action} "
                  f"{order.order_id} {order.symbol}: "
                  f"{'accepted' if res.get('ok') else res.get('error')}")
        if res.get("ok"):
            # Do not rewrite the order's state here — the synchronization engine
            # is the only thing allowed to say what a broker order IS. Poll now
            # so the UI reflects the change in a second rather than at the next
            # scheduled sweep.
            order_sync.poll_soon()
        return res


order_manager = OrderManager()
