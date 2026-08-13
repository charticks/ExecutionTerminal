"""Order Synchronization Engine — the source of truth for live order state.

    Order Engine
          |
          v
    OrderSyncEngine        <- tracks every live order to a terminal state
          |
     +----+------+--------+--------+
     |           |        |        |
   angel      dhan      kotak    icici      (services/order_sync/brokers.py)

What changed
------------
Charticks previously treated "the broker returned an order id" as "the order
filled": `place_order` reported COMPLETE and the renderer marked the row
EXECUTED. An order id means the request was *accepted for routing* — it can
still be rejected by the exchange, rest unfilled, or fill in parts. The terminal
therefore showed positions that did not exist, at prices never traded.

Now placement registers the order here at SUBMITTED, a background poller walks
it through the broker's real lifecycle, and **a position is only booked when the
broker confirms filled quantity**. Partial fills book exactly what filled.

Polling is an implementation detail of the source, not of this engine: a broker
with an order WebSocket implements the same `OrderSource` and calls `ingest()`
directly. Nothing above this layer changes.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import diagnostics
from bridge import events
from bridge.hub import hub

from .base import (
    BrokerOrder,
    FILLED,
    PARTIAL,
    REJECTED,
    SUBMITTED,
    TERMINAL,
    advances,
    source_for,
)

# How often to read each broker's order book while anything is open. Fast enough
# that a fill shows up promptly, slow enough not to hammer a rate-limited API.
POLL_INTERVAL_S = 2.0
# Stop chasing an order that never reaches a terminal state — a broker that
# dropped it, or an id we can no longer find in the book. It stays visible in
# its last known state rather than being polled forever.
GIVE_UP_AFTER_S = 15 * 60.0


@dataclass
class TrackedOrder:
    """One live order, from submission to a terminal state."""
    order_id: str
    account_id: str
    broker: str
    underlying: str
    expiry: str
    strike: float
    opt_type: str
    side: str
    qty: int
    lot_size: int
    price: float
    token: str = ""
    # How the order was composed. Carried because a broker's modify API wants
    # the whole order restated, not just the fields that changed — Angel and
    # Kotak both require product, order type and validity on a modify — and
    # re-deriving them from defaults would silently rewrite an MIS order as
    # NRML. See OrderManager._modify_* .
    product: str = "NRML"
    order_type: str = "LIMIT"
    validity: str = "DAY"
    # Set on a child of a split order, to the synthetic parent id the UI knows
    # it by, so a cancel aimed at the parent can find every child.
    parent_id: str = ""
    # The idempotency claim this order was placed under. Carried so that when the
    # broker confirms a terminal state, the claim can be closed — an unresolved
    # claim blocks later identical orders, so leaving one open after the broker
    # has plainly answered would obstruct legitimate trading.
    client_order_id: str = ""
    status: str = SUBMITTED
    filled_qty: int = 0        # what we have already BOOKED into the position book
    avg_price: float = 0.0
    reason: str = ""
    # Risk rule from the order that opened the position, handed to the position
    # book on the first confirmed fill so live trade management knows where
    # this trade's stop and target sit.
    rule: dict | None = None
    # Set when this order CLOSES a position, to the key of that position. On a
    # terminal state the position's exit claim is released, so a rejected exit
    # re-arms the stop instead of leaving the position unprotected.
    exit_for: str = ""
    created_ts: float = field(default_factory=time.time)
    updated_ts: float = field(default_factory=time.time)

    @property
    def symbol(self) -> str:
        return f"{self.underlying} {self.expiry} {int(self.strike)} {self.opt_type}"

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def as_dict(self) -> dict:
        return {
            "orderId": self.order_id, "account": self.account_id,
            "broker": self.broker, "symbol": self.symbol, "side": self.side,
            "qty": self.qty, "filledQty": self.filled_qty,
            "avgPrice": round(self.avg_price, 2), "status": self.status,
            "reason": self.reason, "ts": int(self.updated_ts * 1000),
            "clientOrderId": self.client_order_id, "parentId": self.parent_id,
            # The contract in STRUCTURED form as well as the display `symbol`.
            # The renderer repaints its live order book from this snapshot, and
            # re-deriving a strike or an expiry by parsing "NIFTY 02SEP2026 25000
            # CE" back apart would break the moment a broker or an underlying
            # spelled it differently.
            "underlying": self.underlying, "expiry": self.expiry,
            "strike": self.strike, "optType": self.opt_type,
            "lotSize": self.lot_size, "price": self.price,
            "orderType": self.order_type, "product": self.product,
            "validity": self.validity,
        }


class OrderSyncEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._orders: dict[str, TrackedOrder] = {}   # key: broker|order_id
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ── registration (called by the Order Engine right after placement) ────
    def track(self, order_id: str, account_id: str, broker: str, underlying: str,
              expiry: str, strike: float, opt_type: str, side: str, qty: int,
              lot_size: int, price: float, token: str = "",
              rule: dict | None = None, exit_for: str = "",
              product: str = "NRML", order_type: str = "LIMIT",
              validity: str = "DAY", parent_id: str = "",
              client_order_id: str = "") -> TrackedOrder:
        order = TrackedOrder(
            order_id=str(order_id), account_id=account_id, broker=broker,
            underlying=underlying, expiry=expiry, strike=strike,
            opt_type=opt_type, side=side, qty=int(qty),
            lot_size=max(1, int(lot_size)), price=float(price), token=token,
            rule=rule, exit_for=exit_for, product=product,
            order_type=order_type, validity=validity, parent_id=parent_id,
            client_order_id=client_order_id)
        with self._lock:
            self._orders[self._key(broker, order_id)] = order
        diagnostics.event("orders", "Order state", SUBMITTED, broker=broker,
                          account=account_id, symbol=order.symbol,
                          side=side, qty=qty, orderId=order_id)
        self._publish(order)
        self._ensure_running()
        return order

    @staticmethod
    def _key(broker: str, order_id: str) -> str:
        return f"{broker}|{order_id}"

    # ── read model ────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            orders = [o.as_dict() for o in self._orders.values()]
        return {"orders": orders, "open": sum(
            1 for o in orders if o["status"] not in TERMINAL)}

    def open_orders(self) -> list[TrackedOrder]:
        with self._lock:
            return [o for o in self._orders.values() if not o.terminal]

    def resolve(self, order_id: str) -> list[TrackedOrder]:
        """Every live order the UI's `order_id` refers to.

        Usually one. A split order is a LIST: the UI knows it by the synthetic
        parent id, and modifying or cancelling it has to reach each child that
        actually exists at the broker. Returning a list rather than one order is
        what stops a "cancel" on a split order from silently cancelling only
        part of it.

        Terminal orders are excluded — there is nothing left to act on, and
        including them would turn "already filled" into an API error.
        """
        wanted = str(order_id)
        with self._lock:
            exact = [o for o in self._orders.values()
                     if o.order_id == wanted and not o.terminal]
            if exact:
                return exact
            return [o for o in self._orders.values()
                    if o.parent_id == wanted and not o.terminal]

    def known(self, order_id: str) -> bool:
        """Whether this id belongs to a live order at all, terminal or not —
        used to tell "already filled/cancelled" apart from "never heard of it"."""
        wanted = str(order_id)
        with self._lock:
            return any(o.order_id == wanted or o.parent_id == wanted
                       for o in self._orders.values())

    def reset(self) -> None:
        with self._lock:
            self._orders.clear()

    # ── polling loop ──────────────────────────────────────────────────────
    def _ensure_running(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="order-sync")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def poll_soon(self) -> None:
        """Run one pass now, off the caller's thread.

        Used after a modify or cancel is accepted: the broker's book is the only
        authority on what the order became, and waiting out the poll interval
        would leave the UI showing the pre-amend order. Never raises and never
        blocks the caller — an amend that worked must not be reported as failed
        because the follow-up read did.
        """
        def run() -> None:
            try:
                self.poll_once()
            except Exception as exc:
                diagnostics.exception("orders", "Post-amend order sync failed",
                                      exc_info=exc)

        threading.Thread(target=run, daemon=True, name="order-sync-now").start()

    def _run(self) -> None:
        # Runs only while something is open, so an idle sidecar makes no broker
        # calls at all.
        while not self._stop.wait(POLL_INTERVAL_S):
            try:
                if not self.open_orders():
                    return
                self.poll_once()
            except Exception as exc:
                # The loop must outlive any single failure, or one bad response
                # silently ends all order tracking.
                diagnostics.exception("orders", "Order sync cycle failed",
                                      exc_info=exc)

    def poll_once(self) -> None:
        """One pass over every account that has an open order."""
        from services.broker_manager import manager

        pending = self.open_orders()
        if not pending:
            return
        self._expire_stale(pending)

        accounts = {(o.account_id, o.broker) for o in pending if not o.terminal}
        sessions = {aid: sess for aid, _b, sess in manager.connected_sessions()}
        for account_id, broker in accounts:
            session = sessions.get(account_id)
            if session is None:
                # Disconnected mid-flight. Not an error and not a state change:
                # the order is still live at the broker and will be re-read when
                # the account reconnects.
                continue
            source = source_for(broker)
            if source is None:
                continue
            try:
                rows = source(session)
            except Exception as exc:
                diagnostics.event("orders", "Order book poll", "failed",
                                  level="warn", broker=broker,
                                  account=account_id, reason=str(exc))
                continue
            self.ingest(broker, account_id, rows)

    def _expire_stale(self, pending: list[TrackedOrder]) -> None:
        cutoff = time.time() - GIVE_UP_AFTER_S
        for order in pending:
            if order.created_ts < cutoff:
                diagnostics.event(
                    "orders", "Order state", "abandoned", level="warn",
                    broker=order.broker, account=order.account_id,
                    symbol=order.symbol, orderId=order.order_id,
                    lastStatus=order.status,
                    reason=f"no terminal state after {GIVE_UP_AFTER_S / 60:.0f} minutes")
                with self._lock:
                    order.status = order.status  # keep last known
                    order.created_ts = time.time()  # stop re-logging
                    self._orders.pop(self._key(order.broker, order.order_id), None)

    # ── state application ─────────────────────────────────────────────────
    def ingest(self, broker: str, account_id: str, rows: list[BrokerOrder]) -> None:
        """Apply broker rows to tracked orders. Also the entry point for a
        push-based (WebSocket) source — it need not poll to use this."""
        by_id = {r.order_id: r for r in rows}
        with self._lock:
            tracked = [o for o in self._orders.values()
                       if o.broker == broker and o.account_id == account_id
                       and not o.terminal]
        for order in tracked:
            row = by_id.get(order.order_id)
            if row is not None:
                self._apply(order, row)

    def _apply(self, order: TrackedOrder, row: BrokerOrder) -> None:
        newly_filled = 0
        transitioned = False
        with self._lock:
            # Book only the INCREMENT since the last poll, so a repeated row or
            # a series of partials can never double-count a fill.
            if row.filled_qty > order.filled_qty:
                newly_filled = row.filled_qty - order.filled_qty
                order.filled_qty = row.filled_qty
            if row.avg_price > 0:
                order.avg_price = row.avg_price
            if row.reason:
                order.reason = row.reason
            if advances(order.status, row.status):
                order.status = row.status
                transitioned = True
            order.updated_ts = time.time()

        if newly_filled > 0:
            self._book_fill(order, newly_filled)
        if order.terminal and order.client_order_id:
            # The broker has given a final answer, so the idempotency claim is
            # settled. Closing it here — rather than only at placement time — is
            # what stops an order whose acknowledgement was lost from blocking
            # later identical orders forever: the poller sees the fill, the claim
            # resolves, and the retry window governs from then on.
            from services.idempotency import guard as idempotency_guard
            idempotency_guard.note_terminal(order.client_order_id, order.order_id)
        if order.terminal and order.exit_for:
            # Rejected, cancelled or fully filled — either way this exit is
            # over. Releasing re-arms the position's stop; leaving the claim in
            # place after a REJECTED exit would disable it silently.
            from services.live_book import live_book
            live_book.end_exit(order.exit_for)
        if transitioned or newly_filled > 0:
            level = "error" if order.status == REJECTED else "info"
            diagnostics.event(
                "orders", "Order state", order.status, level=level,
                broker=order.broker, account=order.account_id,
                symbol=order.symbol, side=order.side, orderId=order.order_id,
                filledQty=order.filled_qty, requestedQty=order.qty,
                avgPrice=round(order.avg_price, 2) or None,
                brokerStatus=row.raw_status, reason=order.reason or None)
            self._publish(order)

    def _book_fill(self, order: TrackedOrder, qty: int) -> None:
        """Create/extend the position — the ONLY path that does so for live.

        Deliberately driven by confirmed filled quantity rather than by
        placement, which is what made positions appear for orders that were
        never actually executed.
        """
        from services.live_book import live_book

        price = order.avg_price if order.avg_price > 0 else order.price
        live_book.record_fill(
            order.underlying, order.expiry, order.strike, order.opt_type,
            order.side, qty, max(1, qty // order.lot_size), price,
            token=order.token, rule=order.rule)
        diagnostics.event(
            "orders", "Fill booked",
            "success" if order.status == FILLED else PARTIAL,
            broker=order.broker, account=order.account_id, symbol=order.symbol,
            side=order.side, filledQty=qty, cumulativeQty=order.filled_qty,
            requestedQty=order.qty, price=round(price, 2))

    def _publish(self, order: TrackedOrder) -> None:
        hub.publish(events.order_update(
            order.order_id, order.symbol, order.side,
            order.filled_qty or order.qty, order.avg_price or order.price,
            order.status))


order_sync = OrderSyncEngine()
