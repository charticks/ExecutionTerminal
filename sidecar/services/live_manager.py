"""Live trade management — Stop Loss, Target, Trail SL, Portfolio Trail Profit.

    Broker Confirms Fill
            ↓
    live_book (broker-confirmed position book)
            ↓
    LiveManager  ── SL / Target / Trail SL / Portfolio Trail / Square-Off
            ↓
    exit order → order_manager → broker → order_sync → book updated

Why this could not exist before
-------------------------------
Automation needs to know what is actually held. Until the Order Synchronization
Engine landed, a live "position" was created the moment a broker returned an
order id, so a stop could have fired against quantity that was never filled —
sending an exit for something the user did not own, or for the wrong size. Every
rule here reads `live_book`, which is written **only** from broker-confirmed
filled quantity.

Safety properties
-----------------
* **Confirmed quantity only.** Exits are sized from `LivePosition.qty`, which is
  cumulative confirmed fills. A partially filled entry is managed at the filled
  size, not the requested one.
* **One exit in flight per position.** `begin_exit` claims the quantity before
  the order is sent. Without that claim, a stop breached at 10:00:00 would fire
  again on every tick for the two seconds the broker takes to confirm.
* **A failed exit re-arms.** If the exit order is rejected or never confirms,
  the claim is released and the stop can fire again — a lost order id must not
  silently leave a position unprotected for the rest of the session.
* **Paper is untouched.** The paper engine manages its own book; this runs only
  in live mode.

Semantics (SL / Target / trail arithmetic) are shared with the paper engine via
`_compute_risk` / `_trail_of` / `_steps`, so the two engines cannot drift apart
in what a rule means.
"""
from __future__ import annotations

import threading
import time

import diagnostics
from services.broker_manager import manager
from services.instruments import instruments
from services.live_book import LivePosition, live_book
from services.paper_engine import MIN_PRICE, _steps

# An exit order that has not reached a terminal state within this long is
# assumed lost; the position's claim is released so its stop can fire again.
EXIT_CLAIM_TIMEOUT_S = 60.0

# Exit reasons, also used as the log's `reason` field.
SL_HIT = "stop-loss"
TARGET_HIT = "target"
PORTFOLIO_TRAIL = "portfolio-trail"
SQUARE_OFF = "square-off"


class LiveManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._enabled = True
        self._portfolio_trail = {"enabled": False, "activateAfter": 0.0,
                                 "trailDistance": 0.0}
        self._peak_pnl = 0.0
        self._pt_armed = False
        self._started = False

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self) -> None:
        """Subscribe to the shared option tick feed — the same feed the option
        chain and the paper engine use, so there is one price source."""
        with self._lock:
            if self._started:
                return
            self._started = True
        manager.add_option_tick_listener(self._on_tick)
        diagnostics.event("orders", "Live trade management", "started")

    def set_portfolio_trail(self, config: dict) -> dict:
        with self._lock:
            self._portfolio_trail = {
                "enabled": bool(config.get("enabled")),
                "activateAfter": float(config.get("activateAfter") or 0),
                "trailDistance": float(config.get("trailDistance") or 0),
            }
            self._reset_portfolio_trail()
        return {"ok": True, **self._portfolio_trail}

    def _reset_portfolio_trail(self) -> None:
        self._peak_pnl = 0.0
        self._pt_armed = False

    # ── tick handler ──────────────────────────────────────────────────────
    def _on_tick(self, key, ltp: float, _volume: int | None) -> None:
        """Mark to market, then evaluate. Never raises into the feed thread: an
        exception here would kill tick delivery for every other consumer."""
        try:
            from services.order_manager import order_manager, LIVE

            if order_manager.mode != LIVE:
                return  # paper engine owns the paper book
            token = instruments.token_for("angel", key)
            if token is None or not ltp or ltp <= 0:
                return
            live_book.update_quote(token, ltp)
            self.evaluate()
        except Exception as exc:
            diagnostics.exception("orders", "Live trade management tick failed",
                                  exc_info=exc)

    def evaluate(self) -> None:
        """One pass over every broker-confirmed position."""
        for key in live_book.release_stale_exits(EXIT_CLAIM_TIMEOUT_S):
            diagnostics.event(
                "orders", "Exit claim released", "expired", level="warn",
                position=key,
                reason=f"exit order did not confirm within "
                       f"{EXIT_CLAIM_TIMEOUT_S:.0f}s — protection re-armed")

        positions = live_book.open_positions()
        if not positions:
            with self._lock:
                self._reset_portfolio_trail()
            return

        for pos in positions:
            if pos.ltp <= 0 or pos.exitable_qty <= 0:
                continue
            # Trail first: a tick that both earns a trail step and breaches the
            # new stop must be handled in that order, or the position exits at
            # the old stop it had already outgrown.
            self._apply_trail(pos)
            self._check_stop_and_target(pos)

        self._check_portfolio_trail()

    # ── per-position rules ────────────────────────────────────────────────
    def _apply_trail(self, pos: LivePosition) -> None:
        """Port of PaperEngine._apply_trail for the confirmed book.

        Point mode: every `after` points of favourable premium movement earns
        one `step` of stop movement. Profit mode: once profit reaches `after`,
        lock in one step less than the level reached. Both require the position
        to HAVE a stop — trailing must never hand a stop to a trader who chose
        to trade without one.
        """
        trail = pos.trail
        if not trail or pos.sl is None or pos.sl_base is None:
            return
        direction = 1 if pos.side == "BUY" else -1
        after, step = float(trail.get("after", 0)), float(trail.get("step", 0))
        if after <= 0 or step <= 0:
            return

        if trail.get("mode") == "profit":
            profit = pos.pnl()
            if profit < after:
                return
            locked = (_steps(profit, step) * step) - step
            if locked <= 0 or pos.qty <= 0:
                return
            new_sl = pos.avg_entry + direction * (locked / pos.qty)
        else:
            move = (pos.ltp - pos.avg_entry) * direction
            steps = _steps(move, after)
            if steps < 1:
                return
            new_sl = pos.sl_base + direction * steps * step

        new_sl = round(max(MIN_PRICE, new_sl), 2)
        if live_book.move_stop(pos.key, new_sl):
            previous, pos.sl = pos.sl, new_sl
            diagnostics.event(
                "orders", "Trailing stop moved", "success", symbol=pos.key,
                side=pos.side, fromSl=previous, toSl=new_sl,
                ltp=pos.ltp, mode=trail.get("mode", "point"))

    def _check_stop_and_target(self, pos: LivePosition) -> None:
        """BUY exits when price falls to the SL or rises to the Target; a short
        premium is the mirror image. A leg the user switched off is absent and
        never triggers."""
        hit = None
        if pos.side == "BUY":
            if pos.sl is not None and pos.ltp <= pos.sl:
                hit = SL_HIT
            elif pos.target is not None and pos.ltp >= pos.target:
                hit = TARGET_HIT
        else:
            if pos.sl is not None and pos.ltp >= pos.sl:
                hit = SL_HIT
            elif pos.target is not None and pos.ltp <= pos.target:
                hit = TARGET_HIT
        if hit:
            self._exit(pos, pos.exitable_qty, hit,
                       trigger=pos.sl if hit == SL_HIT else pos.target)

    def _check_portfolio_trail(self) -> None:
        """Combined open P&L across every confirmed position: once it has
        peaked above `activateAfter`, giving back `trailDistance` squares the
        whole book off."""
        with self._lock:
            config = dict(self._portfolio_trail)
            if not config["enabled"] or config["trailDistance"] <= 0:
                return
            positions = live_book.open_positions()
            if not positions:
                self._reset_portfolio_trail()
                return
            net = round(sum(p.pnl() for p in positions), 2)
            if net > self._peak_pnl:
                self._peak_pnl = net
            if not self._pt_armed:
                if self._peak_pnl < config["activateAfter"]:
                    return
                self._pt_armed = True
                diagnostics.event("orders", "Portfolio trail armed", "success",
                                  peakPnl=self._peak_pnl,
                                  activateAfter=config["activateAfter"])
            if self._peak_pnl - net < config["trailDistance"]:
                return
            peak = self._peak_pnl
            self._reset_portfolio_trail()

        diagnostics.event(
            "orders", "Portfolio trail triggered", "success", level="warn",
            netPnl=net, peakPnl=peak, giveBack=round(peak - net, 2),
            positions=len(positions))
        for pos in positions:
            if pos.exitable_qty > 0:
                self._exit(pos, pos.exitable_qty, PORTFOLIO_TRAIL, trigger=net)

    # ── exit routing ──────────────────────────────────────────────────────
    def square_off_all(self, reason: str = SQUARE_OFF) -> dict:
        """Close every broker-confirmed position at market. Used by the manual
        Square Off All and available to any future automation."""
        positions = live_book.open_positions()
        fired = 0
        for pos in positions:
            if pos.exitable_qty > 0 and self._exit(pos, pos.exitable_qty, reason):
                fired += 1
        diagnostics.event("orders", "Square off all", "success", reason=reason,
                          positions=len(positions), exitsSent=fired)
        return {"ok": True, "positions": len(positions), "exitsSent": fired}

    def _exit(self, pos: LivePosition, qty: int, reason: str,
              trigger: float | None = None) -> bool:
        """Send a market exit for `qty` of a confirmed position.

        Claims the quantity BEFORE routing, so a stop that stays breached
        cannot fire a second order while the first is unconfirmed.
        """
        if qty <= 0:
            return False
        if not live_book.begin_exit(pos.key, qty, reason):
            return False  # already exiting — nothing to do

        exit_side = "SELL" if pos.side == "BUY" else "BUY"
        diagnostics.event(
            "orders", "Automated exit", "started", level="warn",
            symbol=pos.key, reason=reason, side=exit_side, qty=qty,
            ltp=pos.ltp, triggerLevel=trigger, entry=pos.avg_entry,
            pnl=round(pos.pnl(), 2))

        from services.order_manager import order_manager

        try:
            result = order_manager.place_exit(
                pos.underlying, pos.expiry, pos.strike, pos.opt_type,
                exit_side, qty, pos.lots or 1, reason=reason,
                position_key=pos.key)
        except Exception as exc:
            live_book.end_exit(pos.key)
            diagnostics.exception("orders", "Automated exit failed to route",
                                  exc_info=exc, symbol=pos.key, reason=reason)
            return False

        if not result.get("ok"):
            # Release immediately so the next tick can retry, rather than
            # leaving the position unprotected until the claim times out.
            live_book.end_exit(pos.key)
            diagnostics.event(
                "orders", "Automated exit", "rejected", symbol=pos.key,
                reason=reason, code=result.get("code"),
                detail=result.get("error"))
            return False
        return True


live_manager = LiveManager()
