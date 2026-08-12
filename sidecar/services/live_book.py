"""Server-side record of the live positions Charticks itself opened.

Why this exists
---------------
Max Positions, Max Trades, Max Loss and Profit Target have to be enforced before
an order is routed, which means the sidecar needs to know the live book. It has
never had one: live positions were tracked only in the renderer, which is
exactly the state this whole exercise is moving server-side.

What it is — and is not
-----------------------
This book is built from the orders THIS sidecar placed and accepted, marked to
market off the shared option tick feed. That is enough to enforce the limits
above and it is honest about its own boundaries:

  * It does not see fills made in the broker's own app, on another machine, or
    before Charticks started.
  * A placement accepted by the broker is recorded as filled at the requested
    price. Acceptance is not a fill (see docs/LIVE-READINESS.md blocker #2) — a
    rejected-downstream or part-filled order will overstate the book.

Both go away when broker order-book / position polling lands, at which point
`reconcile()` is where broker-confirmed positions replace this record. Until
then an approximate book that enforces the loss limit beats no book at all,
which is what the alternative was.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import diagnostics
from bridge import events
from bridge.hub import hub

# How long after sending an order an identical one counts as a duplicate. Matches
# the paper engine's own window so both modes behave the same way.
DUPLICATE_WINDOW_S = 2.0


@dataclass
class LivePosition:
    key: str                  # underlying|expiry|strike|optType
    underlying: str
    expiry: str
    strike: float
    opt_type: str
    token: str
    side: str                 # net direction of the remaining quantity
    qty: int = 0
    lots: int = 0
    avg_entry: float = 0.0
    opened_ts: float = field(default_factory=time.time)
    # ── live trade management ────────────────────────────────────────────
    # The risk rule captured from the order that OPENED this position, and the
    # absolute prices derived from it. Snapshotted at entry and never re-read
    # from Settings, so changing defaults affects future trades only — same
    # contract as the paper engine.
    rule: dict | None = None
    sl: float | None = None
    target: float | None = None
    trail: dict | None = None
    sl_base: float | None = None     # SL as first set; trailing moves from here
    ltp: float = 0.0
    # An exit order for this position is currently at the broker. Blocks a
    # second exit from being fired on the next tick while the first is still
    # unconfirmed — the difference between one stop-loss exit and several.
    exit_pending_qty: int = 0
    exit_reason: str = ""
    exit_started_ts: float = 0.0

    @property
    def exitable_qty(self) -> int:
        """Quantity an automation may still act on: confirmed, minus whatever
        is already being exited."""
        return max(0, self.qty - self.exit_pending_qty)

    def pnl(self) -> float:
        if self.avg_entry <= 0 or self.ltp <= 0:
            return 0.0
        direction = 1 if self.side == "BUY" else -1
        return (self.ltp - self.avg_entry) * self.qty * direction


class LiveBook:
    """Thread-safe. Written from order placement, read by risk validation."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._positions: dict[str, LivePosition] = {}
        self._orders_today = 0
        self._realised = 0.0
        # contract|side -> time last submitted, for the duplicate window.
        self._recent: dict[str, float] = {}

    @staticmethod
    def key_for(underlying: str, expiry: str, strike: float, opt_type: str) -> str:
        return f"{underlying}|{expiry}|{int(strike)}|{opt_type}"

    # ── writes ─────────────────────────────────────────────────────────────
    def record_fill(self, underlying: str, expiry: str, strike: float,
                    opt_type: str, side: str, qty: int, lots: int,
                    price: float, token: str = "",
                    rule: dict | None = None) -> None:
        """Record an accepted live order. A same-side order averages in; an
        opposite-side one reduces (and closes at zero), booking realised P&L."""
        if qty <= 0:
            return
        # MARKET orders carry no price (0). Marking one in at 0 would value the
        # whole position as pure profit and could trip Profit Target on its own,
        # so fall back to the current quote, and leave it at 0 when even that is
        # unavailable — session_pnl() then excludes it rather than guessing.
        if price <= 0 and token:
            from services.broker_manager import manager
            ltp, _bid, _ask = manager.get_option_quote(token)
            price = ltp if ltp and ltp > 0 else 0.0
        key = self.key_for(underlying, expiry, strike, opt_type)
        # Set inside the lock, published outside it: hub.publish reaches the
        # WebSocket layer, and holding the book's lock across that would couple
        # position updates to network back-pressure.
        changed: LivePosition | None = None
        closed = False
        with self._lock:
            pos = self._positions.get(key)
            if pos is None:
                pos = LivePosition(
                    key=key, underlying=underlying, expiry=expiry, strike=strike,
                    opt_type=opt_type, token=token, side=side, qty=qty, lots=lots,
                    avg_entry=price, ltp=price, rule=rule)
                self._apply_rule(pos)
                self._positions[key] = pos
                changed = pos
            elif pos.side == side:
                total = pos.qty + qty
                pos.avg_entry = ((pos.avg_entry * pos.qty) + (price * qty)) / total
                pos.qty = total
                pos.lots += lots
                # Averaging in moves the cost basis, so SL/Target derived from
                # it must move too — otherwise a stop set for the first fill
                # sits at the wrong distance for the combined position.
                self._apply_rule(pos)
                changed = pos
            else:
                closing = min(pos.qty, qty)
                direction = 1 if pos.side == "BUY" else -1
                self._realised += (price - pos.avg_entry) * closing * direction
                pos.qty -= closing
                pos.lots = max(0, pos.lots - lots)
                # This fill satisfied part (or all) of any in-flight exit.
                pos.exit_pending_qty = max(0, pos.exit_pending_qty - closing)
                changed = pos
                if pos.qty <= 0:
                    self._positions.pop(key, None)
                    closed = True
                    diagnostics.event("orders", "Live position closed", "success",
                                      symbol=f"{underlying} {expiry} {int(strike)} {opt_type}",
                                      reason=pos.exit_reason or None,
                                      realised=round(self._realised, 2))
            if token and key in self._positions:
                self._positions[key].token = token
        if changed is not None:
            self._publish(changed, closed=closed)

    # ── live trade management state ───────────────────────────────────────
    @staticmethod
    def _apply_rule(pos: LivePosition) -> None:
        """Derive SL / Target / trail from the position's rule and cost basis.

        Shares `_compute_risk` with the paper engine so a live position's stop
        sits exactly where the same trade's paper stop would — the two engines
        must not disagree about what a rule means. Caller must hold the lock.
        """
        from services.paper_engine import _compute_risk, _trail_of

        if not pos.rule or pos.avg_entry <= 0:
            return
        risk = _compute_risk(pos.avg_entry, pos.side, pos.rule)
        pos.sl = risk.get("sl")
        pos.target = risk.get("target")
        pos.sl_base = pos.sl
        pos.trail = _trail_of(pos.rule)

    def update_quote(self, token: str, ltp: float) -> None:
        """Mark positions on this token to market. Called from the tick feed."""
        if not token or ltp <= 0:
            return
        touched = []
        with self._lock:
            for pos in self._positions.values():
                if pos.token == token:
                    pos.ltp = ltp
                    touched.append(pos)
        for pos in touched:
            self._publish(pos)

    def _publish(self, pos: LivePosition, closed: bool = False) -> None:
        """Push the position to the renderer.

        The Positions tab is driven by these events, which is what makes the
        UI requirement — a position appears only once the broker confirms a
        fill — true by construction: nothing publishes until record_fill runs.
        """
        hub.publish(events.position_update({
            "id": pos.key,
            "symbol": pos.key,
            "side": pos.side,
            "qty": 0 if closed else pos.qty,
            "entry": round(pos.avg_entry, 2),
            "ltp": round(pos.ltp, 2),
            "pnl": round(pos.pnl(), 2),
            "sl": pos.sl,
            "target": pos.target,
        }))

    def open_positions(self) -> list[LivePosition]:
        """Snapshot for the live manager. Copies, so evaluation never holds the
        lock while it decides — and never mutates the book by accident."""
        from copy import copy
        with self._lock:
            return [copy(p) for p in self._positions.values() if p.qty > 0]

    def get(self, key: str) -> LivePosition | None:
        from copy import copy
        with self._lock:
            pos = self._positions.get(key)
            return copy(pos) if pos else None

    def set_risk(self, key: str, sl: float | None = None,
                 target: float | None = None) -> bool:
        with self._lock:
            pos = self._positions.get(key)
            if pos is None:
                return False
            if sl is not None:
                pos.sl = sl
                pos.sl_base = sl
            if target is not None:
                pos.target = target
            return True

    def move_stop(self, key: str, new_sl: float) -> bool:
        """Trailing only ever tightens; a pullback never gives ground back."""
        with self._lock:
            pos = self._positions.get(key)
            if pos is None or pos.sl is None:
                return False
            better = new_sl > pos.sl if pos.side == "BUY" else new_sl < pos.sl
            if not better:
                return False
            pos.sl = new_sl
            return True

    def begin_exit(self, key: str, qty: int, reason: str) -> bool:
        """Claim `qty` for an automated exit. False when there is nothing left
        to claim — which is what stops a stop-loss firing again on every tick
        while the first exit order is still unconfirmed at the broker."""
        with self._lock:
            pos = self._positions.get(key)
            if pos is None or qty <= 0 or pos.exitable_qty < qty:
                return False
            pos.exit_pending_qty += qty
            pos.exit_reason = reason
            pos.exit_started_ts = time.time()
            return True

    def end_exit(self, key: str) -> None:
        """Release the claim — the exit order reached a terminal state. A
        rejected or cancelled exit must not leave the position permanently
        unprotected, so the claim is dropped and the stop can fire again."""
        with self._lock:
            pos = self._positions.get(key)
            if pos is not None:
                pos.exit_pending_qty = 0
                pos.exit_started_ts = 0.0

    def release_stale_exits(self, older_than_s: float) -> list[str]:
        """Drop exit claims whose order never reached a terminal state. Without
        this, one lost order id would silently disable that position's stop for
        the rest of the session."""
        cutoff = time.time() - older_than_s
        released = []
        with self._lock:
            for pos in self._positions.values():
                if pos.exit_pending_qty > 0 and 0 < pos.exit_started_ts < cutoff:
                    pos.exit_pending_qty = 0
                    pos.exit_started_ts = 0.0
                    released.append(pos.key)
        return released

    def reset(self) -> None:
        """Start a fresh session — clears counters and the book."""
        with self._lock:
            self._positions.clear()
            self._orders_today = 0
            self._realised = 0.0
            self._recent.clear()
        diagnostics.event("orders", "Live session reset", "success")

    # ── reads (used by risk validation) ────────────────────────────────────
    def open_count(self) -> int:
        with self._lock:
            return sum(1 for p in self._positions.values() if p.qty > 0)

    def held_lots(self, underlying: str, expiry: str, strike: float,
                  opt_type: str) -> int:
        with self._lock:
            pos = self._positions.get(self.key_for(underlying, expiry, strike, opt_type))
            return pos.lots if pos else 0

    def orders_today(self) -> int:
        with self._lock:
            return self._orders_today

    def working_order_id(self, underlying: str, expiry: str, strike: float,
                         opt_type: str, side: str) -> str | None:
        """Duplicate guard for LIVE, which had none at all — the one-working-
        order rule lived inside the paper engine only.

        There is no live order book to consult (blocker #2), so this is a
        short-window guard against the same order being fired twice: a
        double-click, a retry loop, or a re-submitted request. It is not a
        substitute for the broker's own working-order list.
        """
        key = f"{self.key_for(underlying, expiry, strike, opt_type)}|{side}"
        cutoff = time.time() - DUPLICATE_WINDOW_S
        with self._lock:
            sent = self._recent.get(key)
            return key if sent is not None and sent >= cutoff else None

    def note_submitted(self, underlying: str, expiry: str, strike: float,
                       opt_type: str, side: str) -> None:
        """Record that this contract + side was just sent, opening the duplicate
        window. Called immediately before routing, so a second request racing
        the first still sees it.

        Also where the Max Trades counter increments — once per order, not per
        fill. Counting in record_fill instead made a partially-filled order
        count twice (once for the partial, once for the remainder), inflating
        the session's trade count against the user's limit.
        """
        key = f"{self.key_for(underlying, expiry, strike, opt_type)}|{side}"
        with self._lock:
            self._recent[key] = time.time()
            self._orders_today += 1

    def session_pnl(self) -> float:
        """Realised + open mark-to-market, off the same tick feed the option
        chain uses. A position whose token has no quote contributes 0 rather
        than a stale number."""
        # Imported lazily: broker_manager imports order_manager's dependencies,
        # and a module-level import here would close the cycle.
        from services.broker_manager import manager

        with self._lock:
            positions = list(self._positions.values())
            total = self._realised
        for pos in positions:
            # avg_entry 0 means we never learned the fill price (market order,
            # no quote at the time). Its P&L is unknowable, so it contributes
            # nothing rather than its full mark as fictitious profit.
            if pos.qty <= 0 or not pos.token or pos.avg_entry <= 0:
                continue
            ltp, _bid, _ask = manager.get_option_quote(pos.token)
            if not ltp or ltp <= 0:
                continue
            direction = 1 if pos.side == "BUY" else -1
            total += (ltp - pos.avg_entry) * pos.qty * direction
        return round(total, 2)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "positions": [
                    {"symbol": f"{p.underlying} {p.expiry} {int(p.strike)} {p.opt_type}",
                     "side": p.side, "qty": p.qty, "lots": p.lots,
                     "avgEntry": round(p.avg_entry, 2)}
                    for p in self._positions.values() if p.qty > 0
                ],
                "ordersToday": self._orders_today,
                "realised": round(self._realised, 2),
            }


live_book = LiveBook()
