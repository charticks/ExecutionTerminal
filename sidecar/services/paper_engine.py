"""Realistic paper-trading execution engine for the sidecar.

Unlike the old stub (which echoed the submitted price back as an instant fill),
this is a real, tick-driven order book that shares the SAME live market-data
feed as live trading — the only difference between Paper and Live is this
execution engine, not the pricing.

Execution rules (mirror a real exchange):
  * Market BUY  → fills at the best ASK
  * Market SELL → fills at the best BID
  * Buy Limit   → PENDING until ask <= limit, then fills
  * Sell Limit  → PENDING until bid >= limit, then fills
Pending orders are evaluated continuously on every option tick — never on
timers. When no depth is available (illiquid / far-expiry contracts) a synthetic
bid/ask spread is derived around the LTP so those fills are realistically worse
than the mid, and it widens for low-premium contracts.

Positions are marked-to-market on every tick and a full snapshot
(orders + trades + positions + net P&L) is published to the renderer via the
``paper_state`` event, so the Paper UI is driven by the engine exactly the way
the Live UI is driven by the broker book. Bid/ask stay INTERNAL to this engine —
the Option Chain UI continues to show only LTP.

State is in-memory for the sidecar session (a follow-up can persist it).
"""
from __future__ import annotations

import threading
import time
from typing import Any

import diagnostics
from bridge import events
from bridge.hub import hub
from services import expiry as expiry_filter
from services.broker_manager import manager
from services.instruments import instruments

# NSE/BSE index-option tick size (rupees). Prices must sit on this grid.
TICK_SIZE = 0.05
MIN_PRICE = 0.05
# A limit more than this fraction away from the current LTP is almost certainly
# a fat-finger; reject it rather than resting an order that can never fill sanely.
MAX_AWAY_PCT = 90.0
# Synthetic spread when the feed carries no depth: half-spread is the larger of
# one tick and this fraction of the LTP (wider for cheap/illiquid options).
SYNTH_HALF_PCT = 0.75  # % of LTP per side
# Duplicate-order guard: identical order within this window is dropped.
DUPLICATE_WINDOW_S = 0.6
# Statuses that count as "still in the book" — modifiable, cancellable, and the
# set the one-pending-order-per-instrument-and-side rule matches against.
WORKING_STATUSES = ("OPEN", "PENDING", "PARTIAL")


def _on_grid(price: float) -> bool:
    """True when price sits on the tick grid (within float tolerance)."""
    n = round(price / TICK_SIZE)
    return abs(n * TICK_SIZE - price) < 1e-6


def _compute_risk(entry: float, side: str, rule: dict | None) -> dict:
    """Port of charticks/src/lib/risk.ts computeRiskPrices — SL/Target as an
    offset (points or % of entry) from the fill price.

    A leg the user switched off in Trade Defaults (``slEnabled`` /
    ``targetEnabled`` false) produces no price at all, so the position simply
    carries no SL / no Target. Missing flags mean "on" — rules captured before
    the checkboxes existed always had both."""
    if not rule:
        return {}
    def off(mode: str, val: float) -> float:
        return (entry * val / 100.0) if mode == "percent" else val
    out: dict = {}
    if rule.get("slEnabled", True):
        sl_off = off(rule.get("slMode", "points"), float(rule.get("slVal", 0)))
        sl = entry - sl_off if side == "BUY" else entry + sl_off
        out["sl"] = round(max(MIN_PRICE, sl), 2)
    if rule.get("targetEnabled", True):
        tgt_off = off(rule.get("targetMode", "points"), float(rule.get("targetVal", 0)))
        target = entry + tgt_off if side == "BUY" else entry - tgt_off
        out["target"] = round(max(MIN_PRICE, target), 2)
    return out


def _steps(amount: float, size: float) -> int:
    """Whole `size` steps contained in `amount`, floored at 0.

    Both inputs are derived from float prices, so a value that should land
    exactly on a step boundary can arrive a hair short (₹6,999.9975 for a
    ₹7,000 profit). The tolerance keeps trailing on the boundary the user
    configured instead of silently skipping a step."""
    if size <= 0:
        return 0
    return max(0, int(amount / size + 1e-9))


def _trail_of(rule: dict | None) -> dict | None:
    """The Trail SL snapshot a new position inherits from its rule. Absent when
    Trail SL was disabled for the profile that opened the trade — a later
    settings change never retro-fits it onto a live position."""
    if not rule:
        return None
    # Trailing shifts an existing stop; with Stop Loss off there is nothing to
    # shift and the engine must never invent one. Backstop for the renderer,
    # which already refuses to emit a trail in that case.
    if not rule.get("slEnabled", True):
        return None
    t = rule.get("trail")
    if not isinstance(t, dict):
        return None
    after, step = float(t.get("after", 0)), float(t.get("step", 0))
    if after <= 0 or step <= 0:
        return None
    return {"mode": t.get("mode", "point"), "after": after, "step": step}


class PaperEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._orders: dict[str, dict] = {}
        self._trades: list[dict] = []
        self._positions: dict[str, dict] = {}
        # token -> set(order_ids) for O(1) tick routing of pending limit orders,
        # and token -> set(position_ids) for MTM.
        self._pending_by_token: dict[str, set[str]] = {}
        self._pos_by_token: dict[str, set[str]] = {}
        self._seq = 0
        self._net_pnl = 0.0
        # (token, side, qty, order_type) -> last placement ts, for the dup guard.
        self._recent: dict[tuple, float] = {}
        # Portfolio Trail Profit — one global config pushed from the renderer's
        # active profile, plus the running peak of combined open P&L.
        self._portfolio_trail: dict = {"enabled": False, "activateAfter": 0.0,
                                       "trailDistance": 0.0}
        self._peak_pnl = 0.0
        self._pt_armed = False
        manager.add_option_tick_listener(self._on_tick)

    # ── ids ─────────────────────────────────────────────────────────────
    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq}"

    # ── validation (Issue #7) ───────────────────────────────────────────
    def _validate(self, order_type: str, price: float, ltp: float | None) -> str | None:
        """Return an error string, or None if the order is acceptable.
        MARKET orders don't carry a price so only LIMITs are price-validated."""
        if order_type != "LIMIT":
            return None
        try:
            p = float(price)
        except (TypeError, ValueError):
            return "Invalid limit price."
        if p != p:  # NaN
            return "Invalid limit price."
        if p < MIN_PRICE:
            return f"Limit price must be at least ₹{MIN_PRICE:.2f}."
        if not _on_grid(p):
            return f"Limit price must be a multiple of the ₹{TICK_SIZE:.2f} tick size."
        if ltp and ltp > 0 and abs(p - ltp) / ltp * 100 > MAX_AWAY_PCT:
            return f"Limit price is too far from the market (LTP ₹{ltp:.2f})."
        return None

    # ── quote resolution + synthetic spread ─────────────────────────────
    def _fill_price(self, token: str, side: str) -> float | None:
        """Execution price for a market/marketable order: BUY→ask, SELL→bid.
        Falls back to a synthetic spread around the LTP when the feed has no
        depth (illiquid / far-expiry), so those fills are realistically worse
        than the LTP instead of always perfect."""
        ltp, bid, ask = manager.get_option_quote(token)
        if side == "BUY":
            if ask and ask > 0:
                return round(ask, 2)
        else:
            if bid and bid > 0:
                return round(bid, 2)
        if not ltp or ltp <= 0:
            return None
        half = max(TICK_SIZE, ltp * SYNTH_HALF_PCT / 100.0)
        px = ltp + half if side == "BUY" else ltp - half
        return round(max(MIN_PRICE, px), 2)

    # ── duplicate guard ─────────────────────────────────────────────────
    def _working_order(self, token: str, side: str) -> dict | None:
        """The existing not-yet-finished order on this instrument + side, if any.
        Caller must hold the lock. `token` identifies the exact contract, so a
        different strike, option type or expiry never collides."""
        for o in self._orders.values():
            if (o["token"] == token and o["side"] == side
                    and o["status"] in WORKING_STATUSES):
                return o
        return None

    # ── placement ───────────────────────────────────────────────────────
    def place(self, underlying: str, expiry: str, strike: float, opt_type: str,
              side: str, qty: int, lots: int, order_type: str, price: float,
              rule: dict | None = None, product: str = "NRML",
              validity: str = "DAY", allow_duplicate: bool = False) -> dict:
        underlying = (underlying or "").upper()
        opt_type = (opt_type or "").upper()
        side = (side or "").upper()
        order_type = (order_type or "MARKET").upper()
        product = (product or "NRML").upper()
        validity = (validity or "DAY").upper()

        if expiry_filter.is_expired(expiry):
            return {"ok": False, "code": "EXPIRED_CONTRACT",
                    "error": f"{underlying} {expiry} has expired — pick an active expiry."}

        tradingsymbol, token, _exch = manager.resolve_option(underlying, expiry, strike, opt_type)
        if not token:
            return {"ok": False, "error": f"Could not resolve {underlying} {expiry} "
                                          f"{int(strike)} {opt_type}."}
        ltp, _bid, _ask = manager.get_option_quote(token)

        err = self._validate(order_type, price, ltp)
        if err:
            return {"ok": False, "error": err}

        with self._lock:
            # Duplicate-order guard (Issue #6 backstop).
            now = time.time()
            key = (token, side, int(qty), order_type)
            last = self._recent.get(key)
            if last is not None and (now - last) < DUPLICATE_WINDOW_S:
                return {"ok": False, "error": "Duplicate order ignored.", "duplicate": True}

            # Only one working order per instrument + side may exist at a time.
            # The renderer catches this first and offers "modify the existing
            # order instead"; this is the authoritative backstop. Executed,
            # cancelled and rejected orders never match.
            if not allow_duplicate and self._working_order(token, side) is not None:
                return {"ok": False, "code": "DUPLICATE_PENDING",
                        "error": "A pending order already exists for this strike."}
            self._recent[key] = now

            oid = self._next("O")
            order = {
                "id": oid, "ts": int(now * 1000), "token": token,
                "underlying": underlying, "expiry": expiry, "strike": int(strike),
                "optType": opt_type, "side": side, "orderType": order_type,
                "lots": int(lots), "filledLots": 0, "qty": int(qty),
                "price": round(float(price), 2), "status": "OPEN",
                "avgFill": None, "rule": rule,
                "product": product, "validity": validity,
            }

            if order_type == "MARKET":
                fill = self._fill_price(token, side)
                if fill is None:
                    return {"ok": False, "error": "No market price available yet — "
                                                  "connect a broker so the feed is live."}
                self._orders[oid] = order
                self._execute(order, fill)
            else:
                self._orders[oid] = order
                self._pending_by_token.setdefault(token, set()).add(oid)

            self._publish()
        return {"ok": True, "orderId": oid, "status": self._orders[oid]["status"],
                "paper": True, "symbol": tradingsymbol}

    # ── fill / position open (lock held) ────────────────────────────────
    def _execute(self, order: dict, fill: float) -> None:
        order["status"] = "EXECUTED"
        order["filledLots"] = order["lots"]
        order["avgFill"] = fill
        self._pending_by_token.get(order["token"], set()).discard(order["id"])

        self._trades.append({
            "id": self._next("T"), "orderId": order["id"], "ts": int(time.time() * 1000),
            "underlying": order["underlying"], "strike": order["strike"],
            "optType": order["optType"], "side": order["side"],
            "qty": order["qty"], "price": fill,
        })

        entry = round(fill, 2)
        # Brokers keep a NET position book: one row per contract, never a
        # simultaneous long and short. Match on the token, which encodes
        # underlying + expiry + strike + option type exactly — any difference in
        # those is a different token and so a separate position.
        existing = self._open_position_any(order["token"])
        if existing is not None:
            if existing["side"] == order["side"]:
                # ADD: average the cost basis and grow the quantity so the book
                # never shows two rows for one strike (SL/Target stay as the
                # user set them on the existing position).
                total_qty = existing["qty"] + order["qty"]
                existing["avgEntry"] = round(
                    (existing["avgEntry"] * existing["qty"] + entry * order["qty"]) / total_qty, 2)
                existing["lots"] += order["lots"]
                existing["qty"] = total_qty
                existing["ltp"] = entry
                return
            # OPPOSITE side → net off against the existing position.
            remaining_lots = self._net_off(existing, order, entry)
            if remaining_lots <= 0:
                return
            # Reversal: the new order outsized the position it closed, so the
            # balance opens a fresh position on the new side.
            lot_sz = self._order_lot_size(order)
            order = dict(order)
            order["lots"] = remaining_lots
            order["qty"] = remaining_lots * lot_sz

        pid = self._next("p")
        risk = _compute_risk(entry, order["side"], order.get("rule"))
        pos = {
            "id": pid, "token": order["token"], "underlying": order["underlying"],
            "expiry": order["expiry"], "strike": order["strike"], "optType": order["optType"],
            "side": order["side"], "lots": order["lots"], "qty": order["qty"],
            "entry": entry, "avgEntry": entry, "ltp": entry, "status": "OPEN",
            "rule": order.get("rule"),
            # Trade-management settings are SNAPSHOTTED here and never re-read,
            # so later changes on Settings apply to future trades only.
            "trail": _trail_of(order.get("rule")),
            # The stop point trailing measures its steps from.
            "_slBase": risk.get("sl"),
            **risk,
        }
        self._positions[pid] = pos
        self._pos_by_token.setdefault(order["token"], set()).add(pid)

    def _open_position(self, token: str, side: str) -> dict | None:
        """The open position on this exact contract + side, if any. Caller must
        hold the lock."""
        for p in self._positions.values():
            if p["status"] == "OPEN" and p["token"] == token and p["side"] == side:
                return p
        return None

    def _open_position_any(self, token: str) -> dict | None:
        """The open position on this exact contract, whichever side it is on.
        Netting means at most one can exist. Caller must hold the lock."""
        for p in self._positions.values():
            if p["status"] == "OPEN" and p["token"] == token:
                return p
        return None

    def _order_lot_size(self, order: dict) -> int:
        return int(order["qty"] / order["lots"]) if order["lots"] else int(order["qty"])

    def _close_out(self, pos: dict, lots: int, exit_px: float) -> None:
        """Record `lots` of `pos` as squared off at `exit_px`.

        A full close flips the position itself to CLOSED (keeping its id, so the
        Positions tab row becomes the closed record). A partial close shrinks the
        live position and emits a SEPARATE closed record for the exited lots, so
        the realized P&L on them lands in Closed Trades immediately instead of
        vanishing into a smaller open row. Caller must hold the lock.
        """
        lot_sz = self._lot_size(pos)
        if lots >= pos["lots"]:
            pos["status"] = "CLOSED"
            pos["exit"] = round(exit_px, 2)
            pos["ltp"] = round(exit_px, 2)
            self._pos_by_token.get(pos["token"], set()).discard(pos["id"])
            return
        closed = dict(pos)
        closed["id"] = self._next("p")
        closed["lots"] = lots
        closed["qty"] = lots * lot_sz
        closed["status"] = "CLOSED"
        closed["exit"] = round(exit_px, 2)
        closed["ltp"] = round(exit_px, 2)
        self._positions[closed["id"]] = closed
        pos["lots"] -= lots
        pos["qty"] = pos["lots"] * lot_sz

    def _net_off(self, pos: dict, order: dict, fill: float) -> int:
        """Net an opposite-side fill against `pos`. Returns the lots left over
        once the existing position is exhausted (0 unless this is a reversal).
        Caller must hold the lock."""
        closing = min(pos["lots"], order["lots"])
        self._close_out(pos, closing, fill)
        return order["lots"] - closing

    def _lot_size(self, pos: dict) -> int:
        return int(pos["qty"] / pos["lots"]) if pos["lots"] else int(pos["qty"])

    # ── Trail SL (position level) ────────────────────────────────────────
    @staticmethod
    def _pnl(pos: dict) -> float:
        """Open P&L in rupees at the current mark."""
        dir_ = 1 if pos["side"] == "BUY" else -1
        return (pos["ltp"] - pos["avgEntry"]) * pos["qty"] * dir_

    def _apply_trail(self, pos: dict) -> bool:
        """Move this position's SL per ITS OWN trail snapshot. Returns True when
        the SL actually moved. Caller must hold the lock.

        Point based — every `after` points of favourable premium movement earns
        one `step` of SL movement in the same direction:
            entry 100, SL 80, after 10, step 10
            → 110 : SL 90    120 : SL 100    130 : SL 110

        Profit based — the SL stays put until profit reaches `after` (Start
        Trail), then locks in one `step` less than the profit reached:
            start ₹5,000, step ₹1,000
            → ₹5,000 : SL at ₹4,000 profit    ₹8,000 : SL at ₹7,000 profit

        The SL only ever tightens: a pullback never gives ground back. Both
        modes require the position to HAVE a stop — a trader who turned Stop
        Loss off chose to trade without one, and trailing must never silently
        hand them one back.
        """
        trail = pos.get("trail")
        if not trail or pos["status"] != "OPEN" or pos["qty"] <= 0:
            return False
        if pos.get("sl") is None or pos.get("_slBase") is None:
            return False
        dir_ = 1 if pos["side"] == "BUY" else -1
        after, step = float(trail["after"]), float(trail["step"])

        if trail.get("mode") == "profit":
            profit = self._pnl(pos)
            if profit < after:
                return False
            # Lock in whole steps of profit, one step behind the level reached.
            locked = (_steps(profit, step) * step) - step
            if locked <= 0:
                return False
            new_sl = pos["avgEntry"] + dir_ * (locked / pos["qty"])
        else:
            base = pos["_slBase"]
            move = (pos["ltp"] - pos["avgEntry"]) * dir_
            steps = _steps(move, after)
            if steps < 1:
                return False
            new_sl = base + dir_ * steps * step

        new_sl = round(max(MIN_PRICE, new_sl), 2)
        cur = pos.get("sl")
        # "Tighter" is upward for a long (stop rises toward price) and downward
        # for a short — in both cases dir_ * (new - cur) must be positive.
        if cur is not None and dir_ * (new_sl - cur) <= 0:
            return False
        pos["sl"] = new_sl
        return True

    def _check_exit(self, pos: dict) -> bool:
        """Square the position off if the mark has reached its Stop Loss or
        Target. Returns True when it exited. Caller must hold the lock.

        This is what makes paper trading behave like the live book: a stop that
        only ever *moves* protects nothing. Call it AFTER `_apply_trail`, so a
        tick that both earns a trail step and breaches the new stop is handled
        in the right order.

        BUY exits when price falls to the SL or rises to the Target; SELL — a
        short premium — is the mirror image. A leg the user switched off is
        simply absent and never triggers.
        """
        if pos["status"] != "OPEN":
            return False
        ltp, sl, target = pos["ltp"], pos.get("sl"), pos.get("target")
        long_ = pos["side"] == "BUY"
        if sl is not None and (ltp <= sl if long_ else ltp >= sl):
            reason = "SL"
        elif target is not None and (ltp >= target if long_ else ltp <= target):
            reason = "TARGET"
        else:
            return False
        # Exit at the price the other side of the book would actually pay,
        # exactly as a manual square-off does — a stop rarely fills at its
        # trigger. Falls back to the mark when there is no quote.
        exit_side = "SELL" if long_ else "BUY"
        fill = self._fill_price(pos["token"], exit_side) or ltp
        pos["status"] = "CLOSED"
        pos["exit"] = round(fill, 2)
        pos["exitReason"] = reason
        self._pos_by_token.get(pos["token"], set()).discard(pos["id"])
        return True

    # ── Portfolio Trail Profit (book level) ──────────────────────────────
    def set_portfolio_trail(self, cfg: dict) -> dict:
        """Replace the global Portfolio Trail Profit config (pushed from the
        renderer's active profile). Changing it re-arms from the current peak
        rather than acting on history the user has just redefined."""
        with self._lock:
            self._portfolio_trail = {
                "enabled": bool(cfg.get("enabled", False)),
                "activateAfter": float(cfg.get("activateAfter", 0) or 0),
                "trailDistance": float(cfg.get("trailDistance", 0) or 0),
            }
            self._reset_portfolio_trail()
        return {"ok": True}

    def _reset_portfolio_trail(self) -> None:
        """Disarm and forget the peak — used when the config changes and once
        the book is empty again. Caller must hold the lock."""
        self._peak_pnl = 0.0
        self._pt_armed = False

    def _check_portfolio_trail(self, net_pnl: float) -> bool:
        """Track the peak of combined open P&L and square everything off when it
        gives back `trailDistance` after arming. Returns True when it fired.
        Caller must hold the lock."""
        cfg = self._portfolio_trail
        if not cfg["enabled"] or cfg["trailDistance"] <= 0:
            return False
        if not any(p["status"] == "OPEN" for p in self._positions.values()):
            # Nothing at risk — start the next batch of trades from scratch.
            self._reset_portfolio_trail()
            return False
        if net_pnl > self._peak_pnl:
            self._peak_pnl = net_pnl
        if not self._pt_armed:
            if self._peak_pnl < cfg["activateAfter"]:
                return False
            self._pt_armed = True
        if self._peak_pnl - net_pnl < cfg["trailDistance"]:
            return False
        for p in self._positions.values():
            if p["status"] == "OPEN":
                p["status"] = "CLOSED"
                p["exit"] = p["ltp"]
                self._pos_by_token.get(p["token"], set()).discard(p["id"])
        self._reset_portfolio_trail()
        return True

    # ── tick handler — pending fills + MTM (Issues #1, #2) ───────────────
    def _on_tick(self, key, _ltp: float, _volume: int | None) -> None:
        # Ticks are delivered under a canonical InstrumentKey, but orders and
        # positions here are indexed by the Angel token resolve_option handed
        # back when they were placed. Translate at the boundary rather than
        # re-keying the live books, which would invalidate every in-flight
        # order; they move to keys when the feed router lands.
        token = instruments.token_for("angel", key)
        if token is None:
            return
        changed = False
        with self._lock:
            # 1) Evaluate pending limit orders on this token against bid/ask.
            pend = list(self._pending_by_token.get(token, set()))
            if pend:
                _l, bid, ask = manager.get_option_quote(token)
                for oid in pend:
                    o = self._orders.get(oid)
                    if not o or o["status"] != "OPEN":
                        continue
                    limit = o["price"]
                    hit = ((o["side"] == "BUY" and ask and ask <= limit) or
                           (o["side"] == "SELL" and bid and bid >= limit))
                    if hit:
                        fill = round(ask if o["side"] == "BUY" else bid, 2)
                        self._execute(o, fill)
                        changed = True
            # 2) Mark open positions on this token to market, trail each one's
            #    SL off the new mark, then act on SL / Target.
            ltp, _b, _a = manager.get_option_quote(token)
            if ltp and ltp > 0:
                for pid in list(self._pos_by_token.get(token, set())):
                    p = self._positions.get(pid)
                    if not p or p["status"] != "OPEN":
                        continue
                    if round(ltp, 2) != p["ltp"]:
                        p["ltp"] = round(ltp, 2)
                        changed = True
                    # Trail before the exit check: a tick can both earn a step
                    # and then breach the freshly moved stop.
                    if self._apply_trail(p):
                        changed = True
                    if self._check_exit(p):
                        changed = True
            if changed:
                self._publish()

    # ── modify / cancel (Issue #3) ───────────────────────────────────────
    def modify(self, order_id: str, price: float | None = None,
               qty: int | None = None, lots: int | None = None) -> dict:
        with self._lock:
            o = self._orders.get(order_id)
            if not o:
                return {"ok": False, "error": "Order not found."}
            if o["status"] not in WORKING_STATUSES:
                return {"ok": False, "error": "Only pending orders can be modified."}
            if price is not None:
                ltp, _b, _a = manager.get_option_quote(o["token"])
                err = self._validate("LIMIT", price, ltp)
                if err:
                    return {"ok": False, "error": err}
                o["price"] = round(float(price), 2)
            if lots is not None and int(lots) > 0:
                lot_sz = int(o["qty"] / o["lots"]) if o["lots"] else int(o["qty"])
                o["lots"] = int(lots)
                o["qty"] = int(lots) * lot_sz
            self._publish()
        return {"ok": True}

    def cancel(self, order_id: str) -> dict:
        with self._lock:
            o = self._orders.get(order_id)
            if not o:
                return {"ok": False, "error": "Order not found."}
            if o["status"] not in WORKING_STATUSES:
                return {"ok": False, "error": "Only pending orders can be cancelled."}
            o["status"] = "CANCELLED"
            self._pending_by_token.get(o["token"], set()).discard(order_id)
            self._publish()
        return {"ok": True}

    # ── snapshot publish ─────────────────────────────────────────────────
    def _net_open_pnl(self) -> float:
        """Combined P&L across every OPEN position — the single number
        Portfolio Trail Profit watches. Caller must hold the lock."""
        return sum(self._pnl(p) for p in self._positions.values() if p["status"] == "OPEN")

    def _publish(self) -> None:
        """Publish a full paper-book snapshot (lock held). Also recomputes net
        P&L across open positions and runs the portfolio trail check against it."""
        net = self._net_open_pnl()
        if self._check_portfolio_trail(net):
            # Everything was just squared off — republish the post-exit book.
            net = self._net_open_pnl()
        self._net_pnl = round(net, 2)
        # `token`, `rule` and the `_`-prefixed trail bookkeeping stay internal.
        positions = [
            {k: v for k, v in p.items()
             if k not in ("token", "rule") and not k.startswith("_")}
            for p in self._positions.values()
        ]
        orders = [{k: v for k, v in o.items() if k not in ("token", "rule")}
                  for o in self._orders.values()]
        orders.sort(key=lambda o: o["ts"], reverse=True)
        trades = sorted(self._trades, key=lambda t: t["ts"], reverse=True)
        hub.publish(events.paper_state(orders, trades, positions, self._net_pnl))

    def underlying_of(self, entity_id: str) -> str | None:
        """The underlying behind an order or position id, so session gating can
        use that instrument's real hours (MCX runs past the equity close).
        Returns None for an unknown id — callers then fall back to the default
        equity window, i.e. exactly the previous behaviour."""
        with self._lock:
            e = self._orders.get(entity_id) or self._positions.get(entity_id)
            return e["underlying"] if e else None

    # ── read model for risk validation ───────────────────────────────────
    # Same four methods as services.live_book.LiveBook, so risk validation asks
    # whichever book is authoritative for the mode the same questions.
    def open_count(self) -> int:
        with self._lock:
            return sum(1 for p in self._positions.values() if p["status"] == "OPEN")

    def held_lots(self, underlying: str, expiry: str, strike: float,
                  opt_type: str) -> int:
        """Lots currently open on one contract — what Max Position caps."""
        with self._lock:
            return sum(
                p.get("lots", 0) for p in self._positions.values()
                if p["status"] == "OPEN"
                and p["underlying"] == underlying
                and float(p.get("strike", 0)) == float(strike)
                and p.get("optType") == opt_type
                and (not p.get("expiry") or not expiry or p["expiry"] == expiry)
            )

    def _check_added_lots(self, pos: dict, delta: int) -> dict | None:
        """Apply the Max Position rule to lots added through the stepper, so
        growing a position outside the order ticket cannot exceed a limit the
        ticket enforces. Caller must hold the lock."""
        from services.risk_engine import risk_engine

        limit = risk_engine.config.max_positions
        if limit <= 0:
            return None
        resulting = pos["lots"] + delta
        if resulting <= limit:
            return None
        symbol = (f"{pos['underlying']} {pos.get('expiry', '')} "
                  f"{int(pos.get('strike', 0))} {pos.get('optType', '')}").strip()
        diagnostics.event(
            "risk", "Adjust lots", "rejected", symbol=symbol, limit=limit,
            held=pos["lots"], adding=delta, code="MAX_POSITION_LOTS")
        return {"ok": False, "code": "MAX_POSITION_LOTS",
                "error": f"Adding {delta} lot(s) would take {symbol} to "
                         f"{resulting} lots, over your Max Position limit ({limit}).",
                "limit": limit, "held": pos["lots"], "requested": delta}

    def working_order_id(self, underlying: str, expiry: str, strike: float,
                         opt_type: str, side: str) -> str | None:
        """Id of an existing working order on this contract + side, for the
        shared duplicate rule. Mirrors LiveBook.working_order_id."""
        token = manager.resolve_option(underlying, expiry, strike, opt_type)[1]
        if not token:
            return None
        with self._lock:
            existing = self._working_order(token, side)
            return existing["id"] if existing else None

    def orders_today(self) -> int:
        """Entries this session. Counts orders placed, not positions — the same
        thing the Home bar's Max Trades counter means."""
        with self._lock:
            return len(self._orders)

    def session_pnl(self) -> float:
        with self._lock:
            return self._net_open_pnl()

    def open_underlyings(self) -> list[str]:
        """Distinct underlyings with an open position — lets square-off-all be
        gated on any of their sessions rather than the equity window alone."""
        with self._lock:
            return sorted({p["underlying"] for p in self._positions.values()
                           if p["status"] == "OPEN"})

    def snapshot(self) -> dict:
        with self._lock:
            self._publish()
            return {"netPnl": self._net_pnl}

    def reset(self) -> None:
        """Clear the paper book (e.g. on switching to a fresh session)."""
        with self._lock:
            self._orders.clear()
            self._trades.clear()
            self._positions.clear()
            self._pending_by_token.clear()
            self._pos_by_token.clear()
            self._net_pnl = 0.0
            self._reset_portfolio_trail()
            self._publish()

    # ── position ops driven from the UI (mirror usePositionsStore) ───────
    def close_position(self, pos_id: str, fraction: float = 1.0) -> dict:
        # `fraction` arrives straight off the request body. It was never range
        # checked, and a negative value inverted the arithmetic below
        # (`lots -= exit_lots` with a negative exit_lots ADDS): a "close" call
        # with fraction=-1 doubled the position instead of closing it.
        try:
            fraction = float(fraction)
        except (TypeError, ValueError):
            fraction = float("nan")
        if not (fraction == fraction) or fraction <= 0 or fraction > 1:
            return {"ok": False, "code": "INVALID_EXIT_FRACTION",
                    "error": "Exit fraction must be greater than 0 and at most 1."}
        with self._lock:
            p = self._positions.get(pos_id)
            if not p or p["status"] != "OPEN":
                return {"ok": False, "error": "Position not open."}
            exit_lots = round(p["lots"] * fraction)
            if fraction >= 1 or exit_lots >= p["lots"] or (p["lots"] - exit_lots) < 1:
                p["status"] = "CLOSED"
                p["exit"] = p["ltp"]
                self._pos_by_token.get(p["token"], set()).discard(pos_id)
            else:
                p["lots"] -= exit_lots
                p["qty"] = p["lots"] * self._lot_size(p)
            self._publish()
        return {"ok": True}

    def adjust_lots(self, pos_id: str, delta: int) -> dict:
        # Adding lots here grows a position without going near place_order, so
        # it used to bypass every limit in the risk engine — `delta` was
        # unbounded and +9999 was accepted. Increases are now validated against
        # the same Max Position rule an equivalent order would face.
        try:
            delta = int(delta)
        except (TypeError, ValueError):
            return {"ok": False, "code": "INVALID_ADJUSTMENT",
                    "error": "Lot adjustment must be a whole number."}
        if delta == 0:
            return {"ok": True}
        with self._lock:
            p = self._positions.get(pos_id)
            if not p or p["status"] != "OPEN":
                return {"ok": False, "error": "Position not open."}
            if delta > 0:
                violation = self._check_added_lots(p, delta)
                if violation is not None:
                    return violation
            lot_sz = self._lot_size(p)
            if delta > 0:
                # Add lots — averages the cost basis at the current LTP.
                new_lots = p["lots"] + delta
                p["avgEntry"] = round((p["avgEntry"] * p["lots"] + p["ltp"] * delta) / new_lots, 2)
                p["lots"] = new_lots
            else:
                p["lots"] = max(1, p["lots"] + delta)
            p["qty"] = p["lots"] * lot_sz
            self._publish()
        return {"ok": True}

    def set_risk(self, pos_id: str, sl: float | None, target: float | None,
                 trail_after: float | None = None,
                 trail_step: float | None = None) -> dict:
        """Edit ONE position's risk settings. Never touches other positions or
        the profile defaults."""
        with self._lock:
            p = self._positions.get(pos_id)
            if not p or p["status"] != "OPEN":
                return {"ok": False, "error": "Position not open."}
            if sl is not None:
                p["sl"] = round(float(sl), 2)
                # A manual stop becomes the new baseline point trailing steps
                # from — otherwise the next tick would undo the user's edit.
                p["_slBase"] = p["sl"]
            if target is not None:
                p["target"] = round(float(target), 2)
            if trail_after is not None or trail_step is not None:
                trail = dict(p.get("trail") or {"mode": "point", "after": 0, "step": 0})
                if trail_after is not None:
                    trail["after"] = float(trail_after)
                if trail_step is not None:
                    trail["step"] = float(trail_step)
                p["trail"] = trail if trail["after"] > 0 and trail["step"] > 0 else None
            self._publish()
        return {"ok": True}

    def roll(self, pos_id: str, new_strike: int, new_entry: float) -> dict:
        # A roll closes one leg and opens another at a price the caller supplies,
        # so an unvalidated entry price would silently define the new position's
        # cost basis (and, through _compute_risk, its SL and target).
        try:
            new_strike = int(new_strike)
            entry = round(float(new_entry), 2)
        except (TypeError, ValueError):
            return {"ok": False, "code": "INVALID_ROLL",
                    "error": "Roll needs a whole strike and a numeric entry price."}
        if new_strike <= 0 or not (entry == entry) or entry < MIN_PRICE:
            return {"ok": False, "code": "INVALID_ROLL",
                    "error": f"Roll entry price must be at least ₹{MIN_PRICE:.2f} "
                             f"and the strike must be positive."}
        with self._lock:
            src = self._positions.get(pos_id)
            if not src or src["status"] != "OPEN":
                return {"ok": False, "error": "Position not open."}
            _sym, token, _exch = manager.resolve_option(
                src["underlying"], src["expiry"], new_strike, src["optType"])
            if not token:
                # Never roll into a contract we cannot resolve — the leg would
                # open with no token and never mark to market.
                return {"ok": False, "code": "INVALID_ROLL",
                        "error": f"Could not resolve {src['underlying']} "
                                 f"{src['expiry']} {new_strike} {src['optType']}."}
            # Close the source leg.
            src["status"] = "CLOSED"
            src["exit"] = src["ltp"]
            self._pos_by_token.get(src["token"], set()).discard(pos_id)
            # Open the rolled leg at the new strike, re-deriving risk from the rule.
            pid = self._next("p")
            risk = _compute_risk(entry, src["side"], src.get("rule"))
            rolled = {
                "id": pid, "token": token or src["token"], "underlying": src["underlying"],
                "expiry": src["expiry"], "strike": int(new_strike), "optType": src["optType"],
                "side": src["side"], "lots": src["lots"], "qty": src["qty"],
                "entry": entry, "avgEntry": entry, "ltp": entry, "status": "OPEN",
                "rule": src.get("rule"),
                # The roll carries the SOURCE position's trail settings across
                # (including any per-position edit), but restarts the trail from
                # the new leg's freshly derived stop.
                "trail": src.get("trail"),
                "_slBase": risk.get("sl"),
                **risk,
            }
            self._positions[pid] = rolled
            if token:
                self._pos_by_token.setdefault(token, set()).add(pid)
            self._publish()
        return {"ok": True}

    def square_off_all(self) -> dict:
        with self._lock:
            for p in self._positions.values():
                if p["status"] == "OPEN":
                    p["status"] = "CLOSED"
                    p["exit"] = p["ltp"]
                    self._pos_by_token.get(p["token"], set()).discard(p["id"])
            self._publish()
        return {"ok": True}


paper_engine = PaperEngine()
