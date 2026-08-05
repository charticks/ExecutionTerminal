"""Order routing for the sidecar — Paper vs Live, ported from the legacy
Tkinter app's single-branch model (app/order_manager.py:122).

Paper mode returns a synthetic fill at the supplied LTP and NEVER touches a
broker SDK. Live mode places a real order on every connected broker session
(mirroring the legacy multi-broker fan-out). The mode is held authoritatively
here — set via POST /trading-mode — so even a mis-routed request can't reach a
broker while in Paper.

Scope note (C1): Angel live routing is fully ported; Kotak/Dhan live routing is
NOT yet ported and returns a clear error rather than silently mis-firing. The
tick-driven SL/Target/TSL/Rolling + automation engine (legacy
engines/trade_execution_engine.py) is a separate follow-up (C2).
"""
from __future__ import annotations

import threading
from typing import Any

from bridge import events
from bridge.hub import hub
from services import expiry as expiry_filter
from services import market_session
from services.broker_limits import limit_resolver
from services.broker_manager import manager
from services.order_splitter import split_quantity
from services.paper_engine import paper_engine

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

    # ── mode (authoritative, backstops the client) ────────────────────────
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

    def _log(self, level: str, msg: str) -> None:
        hub.publish(events.log_line(level, msg))

    # ── entry point ────────────────────────────────────────────────────────
    def place_order(self, underlying: str, expiry: str, strike: float,
                    opt_type: str, side: str, qty: int, order_type: str,
                    price: float, lots: int = 0, rule: dict | None = None,
                    product: str = "NRML", validity: str = "DAY",
                    allow_duplicate: bool = False) -> dict:
        underlying = (underlying or "").upper()
        opt_type = (opt_type or "").upper()
        side = (side or "").upper()
        order_type = (order_type or "MARKET").upper()
        product = (product or "NRML").upper()
        validity = (validity or "DAY").upper()
        symbol_hint = f"{underlying}{expiry}{int(strike)}{opt_type}"

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

        # PAPER: route to the tick-driven paper execution engine (realistic
        # market/limit fills against internal bid/ask, MTM, validation) — no SDK.
        if self.mode != LIVE:
            res = paper_engine.place(underlying, expiry, strike, opt_type, side,
                                     qty, lots or qty, order_type, price, rule,
                                     product, validity, allow_duplicate)
            lvl = "info" if res.get("ok") else "warn"
            msg = (f"📝 Paper {order_type} {side} {qty} {symbol_hint} → "
                   f"{res.get('status') or res.get('error')}")
            self._log(lvl, f"[order] {msg}")
            return res

        # LIVE: fan out to every connected session (legacy multi-broker model).
        sessions = manager.connected_sessions()
        if not sessions:
            return {"ok": False, "error": "No connected broker to place a live order."}

        results: list[dict] = []
        for account_id, broker, sess in sessions:
            res = self._place_with_splitting(account_id, broker, sess, underlying,
                                             expiry, strike, opt_type, side, qty,
                                             order_type, price, lots or qty,
                                             product, validity, symbol_hint)
            res["account"] = account_id
            results.append(res)
            # ONE order_update per account (the parent), never one per child —
            # splitting must stay invisible in the UI.
            status = "COMPLETE" if res.get("ok") else "REJECTED"
            hub.publish(events.order_update(
                str(res.get("orderId") or f"{broker}-{symbol_hint}"),
                symbol_hint, side, int(res.get("executedQty") or qty), price, status))

        ok = any(r.get("ok") for r in results)
        out = {"ok": ok, "results": results, "symbol": symbol_hint}
        # Surface a partial execution to the renderer so it can offer a retry of
        # the remaining quantity (one logical trade, part-filled).
        partial = next((r for r in results if r.get("code") == "PARTIAL_FILL"), None)
        if partial and not ok:
            out.update({k: partial[k] for k in
                        ("code", "error", "executedQty", "remainingQty") if k in partial})
        return out

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

        if product != "NRML":
            # Breeze exposes one options product; record the downgrade rather
            # than silently routing an MIS order as carry-forward.
            self._log("warn", f"[order] ICICI has no separate {product} product for "
                              f"options — placing as 'options'")
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
    def modify_order(self, order_id: str, price: float | None,
                     qty: int | None, lots: int | None) -> dict:
        closed = market_session.require_open(paper_engine.underlying_of(order_id))
        if closed:
            return closed
        if self.mode != LIVE:
            return paper_engine.modify(order_id, price, qty, lots)
        return {"ok": False, "error": "Live order modification is not available yet."}

    def cancel_order(self, order_id: str) -> dict:
        closed = market_session.require_open(paper_engine.underlying_of(order_id))
        if closed:
            return closed
        if self.mode != LIVE:
            return paper_engine.cancel(order_id)
        return {"ok": False, "error": "Live order cancellation is not available yet."}


order_manager = OrderManager()
