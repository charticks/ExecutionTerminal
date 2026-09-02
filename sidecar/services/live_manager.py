"""Live trade management — Stop Loss, Target, Trail SL, Portfolio Trail Profit.

    Broker Confirms Fill / Broker Position Book
            ↓
    live_book (canonical, persisted, reconciled)
            ↓
    LiveManager  ── SL / Target / Trail SL / Portfolio Trail / Square-Off
            ↓
    exit order → order_manager → broker → order_sync → book updated

Broker independence
-------------------
Positions are addressed by canonical ``InstrumentKey`` — underlying, expiry,
strike, option type — and never by a broker's token. This engine previously
translated every tick through ``instruments.token_for("angel", key)``: with any
other broker connected the translation returned None, the handler returned
early, and **every stop loss silently stopped evaluating** while the Positions
tab carried on looking normal. Nothing here knows a broker's name any more.

Two evaluation paths, on purpose
--------------------------------
* **Tick-driven** — immediate reaction, the moment a price arrives.
* **Periodic** (``EVAL_INTERVAL_S``) — the safety net. Ticks stop for reasons
  that have nothing to do with the market: a socket dies, a subscription is
  dropped, a feed fails over. A stop loss that only runs when prices happen to
  arrive is not a stop loss, so the timer re-reads the quote cache, re-asserts
  the subscription every position needs, and evaluates regardless.

Monitoring, not just management
-------------------------------
The engine continuously answers "is this position actually being protected right
now?" for every position, and says so out loud (``services.live_book`` monitor
states + the ``monitor_alarm`` event). Automation that quietly stops is the
failure mode this whole module is shaped around: the trader must never see a
normal-looking position that nothing is watching.

Safety properties
-----------------
* **Confirmed quantity only.** Exits are sized from ``LivePosition.qty``, which
  is cumulative confirmed fills, reconciled against the broker's own book.
* **One exit in flight per position.** ``begin_exit`` claims the quantity before
  the order is sent.
* **A failed exit re-arms.** The claim is released and the stop can fire again.
* **Unmanaged positions are never auto-exited.** A position Charticks did not
  open (or was told to stop managing) is displayed and counted, never acted on.
* **Restored positions arm only after the broker confirms them.** Nothing is
  exited against a position read from disk and not yet re-verified.
* **Paper is untouched.** The paper engine manages its own book.

Semantics (SL / Target / trail arithmetic) are shared with the paper engine via
``_compute_risk`` / ``_trail_of`` / ``_steps``, so the two engines cannot drift
apart in what a rule means.
"""
from __future__ import annotations

import threading
import time

import diagnostics
from bridge import events
from bridge.hub import hub
from services import market_session
from services.broker_manager import manager
from services.instruments import InstrumentKey, instruments
from services.live_book import (
    ALARM_STATES,
    EXITING,
    FEED_LOST,
    NO_RULE,
    PAUSED,
    PROTECTED,
    RESTORING,
    UNMANAGED,
    LivePosition,
    live_book,
)
from services.paper_engine import MIN_PRICE, _steps, paper_engine
from services.subscriptions import LIVE as SUB_LIVE, PAPER as SUB_PAPER, option_subs

# An exit order that has not reached a terminal state within this long is
# assumed lost; the position's claim is released so its stop can fire again.
EXIT_CLAIM_TIMEOUT_S = 60.0

# The periodic safety-net cycle. Fast enough that a dead feed is caught within a
# second or so, slow enough to be free: one pass is a dict lookup per position.
EVAL_INTERVAL_S = 0.75

# During market hours, a managed position whose last quote is older than this is
# treated as having no market data at all. Deliberately generous — a deep OTM
# strike can genuinely go a minute without a trade — because the subscription
# and feed-connectivity checks below catch a broken feed far sooner than this
# does; the staleness rule is the last line, not the first.
QUOTE_STALE_S = 90.0

# Exit reasons, also used as the log's `reason` field.
SL_HIT = "stop-loss"
TARGET_HIT = "target"
PORTFOLIO_TRAIL = "portfolio-trail"
SQUARE_OFF = "square-off"
ROLL = "roll"

# How long a roll waits for its exit leg to be confirmed closed before giving up
# and leaving the user flat rather than opening a second leg against a position
# that may still be open. Deliberately generous — a market exit normally
# confirms in a second or two, and the cost of waiting is nothing while the cost
# of guessing is a doubled position.
ROLL_TIMEOUT_S = 90.0


class LiveManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._portfolio_trail = {"enabled": False, "activateAfter": 0.0,
                                 "trailDistance": 0.0}
        self._peak_pnl = 0.0
        self._pt_armed = False
        self._started = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Positions currently in an alarm state, so the alarm event is published
        # on TRANSITIONS rather than every cycle.
        self._alarming: dict[str, str] = {}
        self._last_cycle_ts = 0.0
        self._cycles = 0
        # Rolls whose exit leg has been sent and whose entry leg is waiting on
        # the broker confirming that exit. Keyed by the SOURCE position key.
        self._pending_rolls: dict[str, dict] = {}

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self) -> None:
        """Subscribe to the shared option tick feed and start the periodic
        evaluation cycle. The feed gives immediacy; the cycle guarantees the
        engine runs whether or not the feed does."""
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="live-manager")
        manager.add_option_tick_listener(self._on_tick)
        self._thread.start()
        diagnostics.event("orders", "Live trade management", "started",
                          evaluationIntervalMs=int(EVAL_INTERVAL_S * 1000))

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._started = False

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self._started and self._thread and self._thread.is_alive())

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

    # ── periodic cycle ────────────────────────────────────────────────────
    def _run(self) -> None:
        """Safety-net loop. Never exits on error: an exception here would end
        all live position management for the session, which is precisely the
        silent failure this engine exists to prevent."""
        while not self._stop.wait(EVAL_INTERVAL_S):
            try:
                self.cycle()
            except Exception as exc:
                diagnostics.exception("orders", "Live management cycle failed",
                                      exc_info=exc)

    def cycle(self) -> None:
        """One pass: keep subscriptions honest, re-read quotes, evaluate rules,
        then publish what is and is not being monitored."""
        self._sync_subscriptions()
        self._refresh_quotes()
        if self._is_live():
            self.evaluate()
            self._advance_rolls()
        # Paper's stops need the same safety net, and the paper engine owns that
        # book — this loop only drives it, exactly as the tick feed does. Run in
        # BOTH modes, because the paper book does not empty when the user
        # switches to live, and its tick handler is not mode-gated either.
        paper_engine.sweep()
        self._update_monitoring()
        with self._lock:
            self._last_cycle_ts = time.time()
            self._cycles += 1

    @staticmethod
    def _is_live() -> bool:
        from services.order_manager import order_manager, LIVE
        return order_manager.mode == LIVE

    def _sync_subscriptions(self) -> None:
        """Every open position stays subscribed for market data, whatever the
        user is looking at. The option chain declares its own window separately;
        the hub sends the union, so switching index or expiry can no longer
        unsubscribe the contract a live position is being managed on.

        Paper is declared from here rather than from inside the paper engine:
        a declaration reaches a broker socket, and the paper book's lock must
        never be held across network I/O.
        """
        option_subs.set(SUB_LIVE, live_book.subscription_keys())
        option_subs.set(SUB_PAPER, paper_engine.subscription_keys())

    def _refresh_quotes(self) -> None:
        """Pull the latest cached quote for every position.

        The tick listener already pushes prices, but it only fires for ticks
        that actually arrive on the primary feed while this process is
        listening. A position opened between ticks, a feed failover, or a
        listener that missed a burst would otherwise mark to a stale price —
        and a stop evaluated against a stale price is worse than none.
        """
        for pos in live_book.open_positions():
            tick = manager.get_option_tick(pos.ikey)
            ltp = tick.get("ltp")
            if ltp and ltp > 0 and abs(ltp - pos.ltp) > 1e-9:
                live_book.update_quote(pos.ikey, float(ltp))

    # ── tick handler ──────────────────────────────────────────────────────
    def _on_tick(self, key: InstrumentKey, ltp: float, _volume: int | None) -> None:
        """Mark to market, then evaluate. Never raises into the feed thread: an
        exception here would kill tick delivery for every other consumer.

        `key` is canonical — the same identity whichever broker's feed carried
        the tick — so no broker-specific translation happens here at all.
        """
        try:
            if not ltp or ltp <= 0 or not self._is_live():
                return
            if live_book.update_quote(key, ltp) is None:
                return  # tick for a contract we hold no position in
            self.evaluate()
        except Exception as exc:
            diagnostics.exception("orders", "Live trade management tick failed",
                                  exc_info=exc)

    def evaluate(self) -> None:
        """One pass over every position automation may act on."""
        for key in live_book.release_stale_exits(EXIT_CLAIM_TIMEOUT_S):
            diagnostics.event(
                "orders", "Exit claim released", "expired", level="warn",
                position=key,
                reason=f"exit order did not confirm within "
                       f"{EXIT_CLAIM_TIMEOUT_S:.0f}s — protection re-armed")

        # `verified_ts` is the arming condition, and the ONLY one: it means a
        # broker has confirmed this position exists. A confirmed fill sets it;
        # so does reconciliation. A position restored from disk has it cleared,
        # so nothing acts on one that may have been closed while Charticks was
        # not running — exiting a position that is gone opens a new one in the
        # opposite direction.
        #
        # Not gated on the monitor state: that is computed once per cycle, and
        # gating on it would leave a just-filled position unarmed until the next
        # pass — a window in which its stop does not exist.
        positions = [p for p in live_book.managed_positions() if p.verified_ts > 0]
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
        """Combined open P&L across every MANAGED position: once it has peaked
        above `activateAfter`, giving back `trailDistance` squares those
        positions off. Positions Charticks does not manage are excluded from
        both the P&L and the exit — a book-level rule must not close a trade the
        user is running elsewhere."""
        with self._lock:
            config = dict(self._portfolio_trail)
            if not config["enabled"] or config["trailDistance"] <= 0:
                return
            positions = [p for p in live_book.managed_positions() if p.verified_ts > 0]
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

    # ── monitoring / alarm ────────────────────────────────────────────────
    def _monitor_state(self, pos: LivePosition, live_mode: bool) -> tuple[str, str]:
        """What is (or is not) protecting this position right now.

        Ordered by severity so the state the trader sees is the most urgent one
        that applies, and never a reassuring one that happens to be true too.
        """
        if not pos.managed:
            return UNMANAGED, ("opened outside Charticks — no stop loss, target "
                               "or trail is being applied")
        if pos.verified_ts <= 0:
            return RESTORING, ("restored after a restart — waiting for the broker "
                               "to confirm this position is still open")
        if not live_mode:
            return PAUSED, ("Charticks is in Paper mode — live automation is not "
                            "running for this position")
        if not self.running:
            return PAUSED, "the live management engine is not running"
        if pos.exit_pending_qty > 0:
            return EXITING, f"exit in progress ({pos.exit_reason or 'exit'})"
        if not pos.has_rule:
            return NO_RULE, "no stop loss or target was set for this position"

        # Market data. Checked in the order the failures actually happen, so the
        # message names the real cause rather than its last symptom.
        #
        # Resolvability is asked of the broker that is actually SERVING option
        # data, not of any broker at all: a contract only Dhan lists is not
        # quotable while Angel is the serving feed, and "some broker knows this
        # contract" would report that as healthy right up until the stop failed
        # to evaluate.
        feed = manager.router.primary_feed("option")
        serving = getattr(feed, "broker", "") if feed is not None else ""
        if serving:
            if instruments.token_for(serving, pos.ikey) is None:
                return FEED_LOST, (f"the {serving} market-data feed does not list "
                                   f"this contract, so its price cannot be tracked")
        elif instruments.any_token(pos.ikey) is None:
            return FEED_LOST, ("no connected broker lists this contract, so its "
                               "price cannot be tracked")
        if not option_subs.covers(pos.ikey):
            return FEED_LOST, "this contract is not subscribed for market data"
        if not manager.option_feed_connected:
            return FEED_LOST, "the market data feed is disconnected"
        tick = manager.get_option_tick(pos.ikey)
        if not tick.get("ltp"):
            return FEED_LOST, "no market price has been received for this contract"
        # Outside market hours no ticks are expected and none are needed: the
        # rule is armed and will evaluate on the first tick of the next session.
        if market_session.is_market_open(symbol=pos.underlying):
            age = time.time() - max(float(tick.get("ts") or 0), pos.last_tick_ts)
            if age > QUOTE_STALE_S:
                return FEED_LOST, f"no price update for {int(age)}s"
        return PROTECTED, ""

    def _update_monitoring(self) -> None:
        live_mode = self._is_live()
        alarming: dict[str, str] = {}
        details: dict[str, str] = {}
        for pos in live_book.open_positions():
            state, detail = self._monitor_state(pos, live_mode)
            if live_book.set_monitor(pos.key, state, detail):
                diagnostics.event(
                    "orders", "Position monitoring", state,
                    level="warn" if state in ALARM_STATES else "info",
                    symbol=pos.symbol, qty=pos.qty, managed=pos.managed,
                    reason=detail or None)
            if state in ALARM_STATES:
                alarming[pos.key] = state
                details[pos.key] = detail
        with self._lock:
            changed = alarming != self._alarming
            self._alarming = alarming
        if changed:
            hub.publish(events.monitor_alarm(
                active=bool(alarming),
                positions=[{"id": k, "state": s, "detail": details.get(k, "")}
                           for k, s in sorted(alarming.items())]))

    def republish_alarm(self) -> None:
        """Re-send the current alarm. The event is published on TRANSITIONS, so
        a renderer that connects (or reloads) mid-alarm would otherwise show no
        banner while a position sits unprotected."""
        with self._lock:
            alarming = dict(self._alarming)
        details = {p.key: p.monitor_detail for p in live_book.open_positions()}
        hub.publish(events.monitor_alarm(
            active=bool(alarming),
            positions=[{"id": k, "state": s, "detail": details.get(k, "")}
                       for k, s in sorted(alarming.items())]))

    def monitor_status(self) -> dict:
        """Diagnostics read model — what the engine believes it is watching."""
        positions = live_book.open_positions()
        with self._lock:
            alarming = dict(self._alarming)
            cycles, last = self._cycles, self._last_cycle_ts
        return {
            "running": self.running,
            "liveMode": self._is_live(),
            "cycles": cycles,
            "lastCycleTs": last,
            "evaluationIntervalMs": int(EVAL_INTERVAL_S * 1000),
            "positions": len(positions),
            "managed": sum(1 for p in positions if p.managed),
            "alarm": {"active": bool(alarming), "positions": alarming},
            "subscriptions": option_subs.status(),
        }

    # ── exit routing ──────────────────────────────────────────────────────
    def square_off_all(self, reason: str = SQUARE_OFF,
                       include_unmanaged: bool = True) -> dict:
        """Close every open position at market.

        Unmanaged positions are included: Square Off All is an explicit user
        action on real exposure, and leaving the user's other positions open
        after they asked for everything to be closed would be the more
        surprising behaviour. Automated rules never touch them.
        """
        positions = [p for p in live_book.open_positions()
                     if include_unmanaged or p.managed]
        fired = 0
        for pos in positions:
            if pos.exitable_qty > 0 and self._exit(pos, pos.exitable_qty, reason):
                fired += 1
        diagnostics.event("orders", "Square off all", "success", reason=reason,
                          positions=len(positions), exitsSent=fired,
                          unmanagedIncluded=include_unmanaged)
        return {"ok": True, "positions": len(positions), "exitsSent": fired}

    def close_position(self, position_id: str, fraction: float = 1.0) -> dict:
        """Close all or part of ONE live position at market.

        The live counterpart of PaperEngine.close_position. Both the partial-
        exit buttons and the Positions grid's close action reach live positions
        through here; before this existed they were routed to the paper engine,
        which has never held a live position, so they silently did nothing.
        """
        try:
            fraction = float(fraction)
        except (TypeError, ValueError):
            fraction = float("nan")
        if not (fraction == fraction) or fraction <= 0 or fraction > 1:
            return {"ok": False, "code": "INVALID_EXIT_FRACTION",
                    "error": "Exit fraction must be greater than 0 and at most 1."}
        pos = live_book.get(position_id)
        if pos is None or pos.qty <= 0:
            return {"ok": False, "code": "NO_POSITION",
                    "error": "That position is no longer open."}
        if pos.exitable_qty <= 0:
            return {"ok": False, "code": "EXIT_IN_FLIGHT",
                    "error": "An exit for this position is already at the broker."}
        qty = pos.exitable_qty if fraction >= 1 else int(pos.exitable_qty * fraction)
        if qty <= 0:
            return {"ok": False, "code": "INVALID_EXIT_FRACTION",
                    "error": "That fraction rounds down to zero quantity."}
        # Round a partial exit down to whole lots: exchanges reject a part-lot
        # order, and a rejected exit is a stop that did not happen.
        lot_size = int(pos.qty / pos.lots) if pos.lots else 0
        if lot_size > 0 and qty % lot_size:
            qty = max(lot_size, (qty // lot_size) * lot_size)
        if not self._exit(pos, qty, "manual-exit"):
            return {"ok": False, "code": "EXIT_FAILED",
                    "error": "The exit order was not accepted — see the Orders "
                             "screen and logs/orders.log for the broker's reason."}
        return {"ok": True, "qty": qty}

    # ── adjust size ───────────────────────────────────────────────────────
    def adjust_lots(self, position_id: str, delta: int) -> dict:
        """Grow or shrink a live position by `delta` lots, with real orders.

        The live counterpart of PaperEngine.adjust_lots. It did not exist: the
        endpoint routed to the paper engine whatever the mode, so on a live
        position it looked up an id the paper book had never held and returned
        quietly — a control that appeared to work and did nothing.

        Adding places an ordinary entry order, so it passes every risk rule and
        margin check an equivalent ticket would. Reducing is a partial exit of
        exactly `delta` lots, sized from confirmed quantity like every other exit.
        """
        try:
            delta = int(delta)
        except (TypeError, ValueError):
            return {"ok": False, "code": "INVALID_ADJUSTMENT",
                    "error": "Lot adjustment must be a whole number."}
        if delta == 0:
            return {"ok": True, "delta": 0}

        pos = live_book.get(position_id)
        if pos is None or pos.qty <= 0:
            return {"ok": False, "code": "NO_POSITION",
                    "error": "That position is no longer open."}
        if not pos.managed:
            return {"ok": False, "code": "UNMANAGED_POSITION",
                    "error": f"{pos.symbol} was not opened by Charticks. Adjust it "
                             f"in your broker's own terminal, or take it over with "
                             f"Manage first."}
        if pos.exit_pending_qty > 0:
            return {"ok": False, "code": "EXIT_IN_FLIGHT",
                    "error": "An exit for this position is already at the broker. "
                             "Wait for it to confirm before resizing."}

        lot_size = int(pos.qty / pos.lots) if pos.lots else 0
        if lot_size <= 0:
            return {"ok": False, "code": "UNKNOWN_LOT_SIZE",
                    "error": f"Charticks does not know {pos.underlying}'s lot size "
                             f"for this position, so it cannot resize it by lots. "
                             f"Use the partial-exit buttons instead."}

        if delta > 0:
            from services.order_manager import LIVE, order_manager

            result = order_manager.place_order(
                LIVE, pos.underlying, pos.expiry, pos.strike, pos.opt_type,
                pos.side, delta * lot_size, "MARKET", 0.0, lots=delta,
                # The position's OWN rule, so the added quantity is managed on
                # the same terms — record_fill re-derives SL/Target from the new
                # averaged cost basis, exactly as the paper engine does.
                rule=pos.rule, product=pos.product, validity="DAY",
                # Adding to a position the user already holds is not a new
                # position, and `allow_duplicate` because this is a deliberate
                # second order on a contract they are already in.
                allow_duplicate=True,
                request_id=f"adjust:{pos.key}:+{delta}")
            if not result.get("ok"):
                return {"ok": False, "code": result.get("code") or "ADJUST_FAILED",
                        "error": result.get("error")
                                 or "The broker did not accept the added lots."}
            diagnostics.event("orders", "Adjust lots", "added", symbol=pos.key,
                              lots=delta, qty=delta * lot_size, side=pos.side)
            return {"ok": True, "delta": delta}

        # Reduce: a partial exit of exactly this many lots.
        reduce_lots = min(-delta, pos.lots)
        qty = reduce_lots * lot_size
        if qty >= pos.exitable_qty:
            qty = pos.exitable_qty
        if qty <= 0:
            return {"ok": False, "code": "NOTHING_TO_REDUCE",
                    "error": "There is no quantity left to reduce."}
        if not self._exit(pos, qty, "adjust-lots"):
            return {"ok": False, "code": "EXIT_FAILED",
                    "error": "The reducing order was not accepted — see the Orders "
                             "screen for the broker's reason."}
        diagnostics.event("orders", "Adjust lots", "reduced", symbol=pos.key,
                          lots=reduce_lots, qty=qty)
        return {"ok": True, "delta": -reduce_lots}

    # ── roll ──────────────────────────────────────────────────────────────
    def roll_position(self, position_id: str, new_strike: int) -> dict:
        """Move a live position to another strike on the same series.

        Two legs, strictly sequenced: the current leg is closed at market FIRST,
        and the new leg is opened only once the broker has confirmed the close
        (see ``_advance_rolls``). Never both at once.

        This used to be a renderer-only illusion: the Roll dialog deleted the row
        from the local store and inserted a synthetic one, so the user saw a
        rolled position while the original was untouched at the broker and the
        new strike was never bought. Nothing was sent anywhere.

        Sequencing matters more than latency here. Sending both legs together
        would double the exposure whenever the exit failed, and opening the new
        leg on "exit accepted" rather than "exit filled" would do the same
        whenever the exit was rejected at the exchange.
        """
        pos = live_book.get(position_id)
        if pos is None or pos.qty <= 0:
            return {"ok": False, "code": "NO_POSITION",
                    "error": "That position is no longer open."}
        if not pos.managed:
            return {"ok": False, "code": "UNMANAGED_POSITION",
                    "error": f"{pos.symbol} was not opened by Charticks. Manage it "
                             f"first, or roll it in your broker's own terminal."}
        try:
            strike = int(new_strike)
        except (TypeError, ValueError):
            strike = 0
        if strike <= 0:
            return {"ok": False, "code": "INVALID_ROLL",
                    "error": "Roll needs a whole, positive strike."}
        if strike == int(pos.strike):
            return {"ok": False, "code": "INVALID_ROLL",
                    "error": "That is the strike the position is already on."}
        target = InstrumentKey.option(pos.underlying, pos.expiry, strike, pos.opt_type)
        if not instruments.has(target):
            # Never roll into a contract no connected broker lists: the entry leg
            # would be routed against an instrument nothing can quote or trade.
            return {"ok": False, "code": "INVALID_ROLL",
                    "error": f"No connected broker lists {pos.underlying} "
                             f"{pos.expiry} {strike} {pos.opt_type}."}
        if pos.exitable_qty <= 0:
            return {"ok": False, "code": "EXIT_IN_FLIGHT",
                    "error": "An exit for this position is already at the broker."}
        with self._lock:
            if pos.key in self._pending_rolls:
                return {"ok": False, "code": "ROLL_IN_PROGRESS",
                        "error": f"A roll of {pos.symbol} is already under way."}
            self._pending_rolls[pos.key] = {
                "strike": strike, "underlying": pos.underlying,
                "expiry": pos.expiry, "opt_type": pos.opt_type, "side": pos.side,
                "qty": int(pos.exitable_qty), "lots": int(pos.lots),
                "rule": pos.rule, "product": pos.product,
                "from_strike": int(pos.strike), "started_ts": time.time(),
            }
        diagnostics.event(
            "orders", "Roll", "started", level="warn", symbol=pos.symbol,
            fromStrike=int(pos.strike), toStrike=strike, side=pos.side,
            qty=pos.exitable_qty, product=pos.product)

        if not self._exit(pos, pos.exitable_qty, ROLL):
            with self._lock:
                self._pending_rolls.pop(pos.key, None)
            diagnostics.event("orders", "Roll", "failed", level="error",
                              symbol=pos.symbol, toStrike=strike,
                              reason="the closing leg was not accepted — nothing "
                                     "was rolled and the position is unchanged")
            return {"ok": False, "code": "ROLL_EXIT_FAILED",
                    "error": "The closing leg was not accepted, so nothing was "
                             "rolled. Your position is unchanged — see the Orders "
                             "screen for the broker's reason."}
        return {"ok": True, "stage": "closing", "fromStrike": int(pos.strike),
                "toStrike": strike, "qty": pos.exitable_qty}

    def _advance_rolls(self) -> None:
        """Open the second leg of any roll whose first leg has now closed.

        Runs on the periodic cycle rather than off the fill callback so a roll
        cannot be stranded by a missed event: every pass re-reads the book and
        asks the only question that matters — is the old leg gone yet?
        """
        with self._lock:
            pending = list(self._pending_rolls.items())
        for key, roll in pending:
            source = live_book.get(key)
            if source is not None and source.qty > 0:
                if (time.time() - roll["started_ts"]) < ROLL_TIMEOUT_S:
                    continue
                # The exit never confirmed. Leaving the roll pending would open
                # the new leg later against a position that is still open, so it
                # is abandoned instead: the user keeps the position they had.
                with self._lock:
                    self._pending_rolls.pop(key, None)
                diagnostics.event(
                    "orders", "Roll", "abandoned", level="error",
                    symbol=source.symbol, toStrike=roll["strike"],
                    reason=f"the closing leg did not confirm within "
                           f"{ROLL_TIMEOUT_S:.0f}s — the new leg was NOT opened. "
                           f"Check your broker's order book.")
                continue
            with self._lock:
                if self._pending_rolls.pop(key, None) is None:
                    continue
            self._open_rolled_leg(roll)

    def _open_rolled_leg(self, roll: dict) -> None:
        """Place the roll's entry leg, once the old one is confirmed closed."""
        from services.order_manager import LIVE, order_manager

        result = order_manager.place_order(
            LIVE, roll["underlying"], roll["expiry"], float(roll["strike"]),
            roll["opt_type"], roll["side"], int(roll["qty"]), "MARKET", 0.0,
            lots=int(roll["lots"]), rule=roll["rule"], product=roll["product"],
            validity="DAY",
            # This is the same exposure the user already held and has just
            # closed, not a new position: a position cap that was satisfied a
            # second ago must not be the reason a roll leaves them flat.
            override_max_pos=True, allow_duplicate=True,
            request_id=f"roll:{roll['underlying']}{roll['expiry']}"
                       f"{roll['from_strike']}->{roll['strike']}{roll['opt_type']}")
        if result.get("ok"):
            diagnostics.event(
                "orders", "Roll", "success", symbol=result.get("symbol"),
                fromStrike=roll["from_strike"], toStrike=roll["strike"],
                side=roll["side"], qty=roll["qty"])
            return
        # The old leg is closed and the new one was refused. Say so loudly and
        # precisely: the user is now FLAT, which is a safe state but not the one
        # they asked for, and they have to know without reading a log file.
        detail = str(result.get("error") or result.get("code") or "rejected")
        diagnostics.event(
            "orders", "Roll", "failed", level="error",
            symbol=f"{roll['underlying']} {roll['expiry']} {roll['strike']} "
                   f"{roll['opt_type']}",
            fromStrike=roll["from_strike"], toStrike=roll["strike"],
            reason=f"the old leg closed but the new leg was refused ({detail}) — "
                   f"you are now FLAT on this contract")
        hub.publish(events.log_line(
            "error", f"[roll] {roll['underlying']} {roll['from_strike']} "
                     f"{roll['opt_type']} was closed but the {roll['strike']} leg "
                     f"was refused ({detail}). You are FLAT — place the new leg "
                     f"manually if you still want it."))

    def _exit(self, pos: LivePosition, qty: int, reason: str,
              trigger: float | None = None) -> bool:
        """Send a market exit for `qty` of a position.

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
            source=pos.source, pnl=round(pos.pnl(), 2))

        from services.order_manager import order_manager

        # Lots actually being exited, not the position's TOTAL lots: `qty`
        # here can be less than `pos.qty` (a partial exit, an adjust-lots
        # reduce), and passing the total told risk_engine.rule_lot_size this
        # order was `pos.lots` lots of a different, larger quantity than the
        # one actually being sent — an internally-inconsistent order that
        # rule rejects. Falls back to `pos.lots` when the lot size is unknown
        # (a position whose contract no loaded master lists), preserving the
        # original behaviour for that case: raw-quantity splitting rather
        # than claiming the whole position is one lot.
        lot_size = int(pos.qty / pos.lots) if pos.lots else 0
        lots = qty // lot_size if lot_size > 0 else pos.lots
        try:
            result = order_manager.place_exit(
                pos.underlying, pos.expiry, pos.strike, pos.opt_type,
                exit_side, qty, lots, reason=reason,
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
