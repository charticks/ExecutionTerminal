"""Live market data for the option chain.

The option-chain adapter is broker-agnostic. It asks ``services.instruments``
which expiries and strikes exist, builds the ATM window as canonical
``InstrumentKey``s, and declares that set to the shared subscription hub — which
sends the union of every source's contracts to whichever feed is serving option
data. It previously scanned Angel's raw instrument-master rows to do the same
job, which meant the chain was empty unless Angel specifically was connected; it
now works with any supported broker, including a Dhan-only session.

It also no longer subscribes the feed DIRECTLY. Doing so replaced the whole
option subscription, so switching the chain to another index unsubscribed the
contracts open positions were being managed on and their stops stopped
evaluating (see services.subscriptions).

Consequently this module no longer drives ``app.option_chain.OptionChainMixin``
(the legacy Tkinter mixin, headlessly, via stub tk vars). ATM selection lives
here in ``_STEP`` — the mixin's strike map had no MIDCPNIFTY entry and silently
fell back to 50, snapping the ATM to a strike that does not exist on a
25-point ladder.

The broker position book is no longer polled here: reading it, matching it to
Charticks' own book and deciding what is managed is one job, and it lives in
``services.position_reconciler``. This module used to publish its own
``position_update`` rows keyed by broker token alongside the live book's,
so the same contract appeared twice and neither row could say whether anything
was protecting it.
"""
from __future__ import annotations

import threading
import time
import traceback

from logzero import logger as _file_log

from bridge import events
from bridge.hub import hub
from services.broker_manager import manager
from services import expiry as expiry_filter
from services import market_session
from services.instruments import InstrumentKey, instruments
from services.subscriptions import CHAIN, option_subs

_STEP = {"SENSEX": 100, "NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50,
         "MIDCPNIFTY": 25, "BANKEX": 100, "CRUDEOIL": 50}


def strike_steps() -> dict[str, int]:
    """The strike ladder per underlying — this module's own source of truth, so
    the renderer's Roll picker and ATM window step by the same amount the chain
    is built from rather than a second copy that can drift."""
    return dict(_STEP)

# Option-subscription watchdog: silence longer than this on a connected feed
# during market hours means the subscription didn't take, so re-assert it.
OPTION_SILENCE_S = 20.0
RESUBSCRIBE_COOLDOWN_S = 30.0


def _oi_of(tick: dict) -> int:
    """Open interest for a contract, falling back to traded volume only when
    the feed genuinely did not supply OI. The two are different quantities —
    volume is today's turnover, OI is positions still held — so the fallback is
    a last resort, not an equivalence."""
    oi = tick.get("oi")
    if oi is None:
        return tick.get("volume") or 0
    return oi


class OptionChainAdapter:
    """Maintains a live option-chain snapshot for the active index using the
    mixin's ATM/token mapping; ticks arrive over broker_manager's shared
    market WebSocket (see BrokerManager.subscribe_option_tokens)."""

    def __init__(self) -> None:
        # RLock, not Lock: the subscription state is now a token set AND a key
        # set that must change together, so the mutations are factored into
        # _set_subscribed / _clear_subscribed — and several callers already hold
        # the lock when they invoke them. Matches BrokerManager and
        # SubscriptionRegistry, which are both RLock for the same reason.
        self._lock = threading.RLock()
        self._symbol = "NIFTY"
        self._count = 10
        # None → follow the nearest expiry; else a user-picked expiry string.
        self._expiry: str | None = None
        self._expiries: list[str] = []
        # The contracts we last asked to be subscribed, as canonical keys —
        # no broker tokens anywhere in this adapter any more.
        self._subscribed_keys: set[InstrumentKey] = set()
        # Active chain window: strike -> {"CE": key, "PE": key}.
        self._strike_keys: dict[int, dict[str, InstrumentKey]] = {}
        # Ad-hoc contracts a UI surface needs quotes for outside the chain window
        # (currently the Roll Up/Down picker). Subscribed alongside the chain on
        # the same feed and published as `watch` in the snapshot, so those LTPs
        # are the real contract's and tick in real time.
        self._watch: dict | None = None
        self._watch_keys: dict[int, InstrumentKey] = {}
        self._snapshot: dict = {"symbol": self._symbol, "expiry": "", "expiries": [],
                                "atm": 0, "rows": [], "watch": []}
        # Set by select() so an index/expiry change rebuilds immediately
        # instead of waiting out the 1s idle poll — keeps switch latency minimal.
        self._wake = threading.Event()
        # Registered once so a post-recovery reconnect (fresh tokens after
        # re-auth invalidated the old option tokens) forces a full rebuild.
        manager.subscriptions.set_replayer("option_chain", lambda _specs: self.resubscribe())
        manager.subscriptions.register("option_chain", "active", {"symbol": self._symbol, "count": self._count})
        # Push a fresh snapshot the instant a relevant tick lands instead of
        # waiting on the 1s REST poll cycle — eliminates the ~1-2s combined
        # backend-refresh + frontend-poll delay.
        self._last_push_ts = 0.0
        self._push_min_interval = 0.1  # throttle: avoid flooding on tick bursts
        # Subscription watchdog bookkeeping (see _watchdog).
        self._last_option_tick_ts = 0.0
        self._subscribed_at = 0.0
        self._last_resub_ts = 0.0
        # Last traceback from the run loop (see _run) — surfaced via /market-feed.
        self._last_error: str | None = None
        self._last_error_ts = 0.0
        manager.add_option_tick_listener(self._on_option_tick)
        threading.Thread(target=self._run, name="oc-adapter", daemon=True).start()

    def subscribed_keys(self) -> set[InstrumentKey]:
        with self._lock:
            return set(self._subscribed_keys)

    def _set_subscribed(self, keys: set[InstrumentKey]) -> None:
        with self._lock:
            self._subscribed_keys = set(keys)

    def _clear_subscribed(self) -> None:
        with self._lock:
            self._subscribed_keys = set()

    def last_error(self) -> dict | None:
        """Most recent run-loop traceback, for /market-feed."""
        if not self._last_error:
            return None
        return {"ts": self._last_error_ts, "traceback": self._last_error}

    def _on_option_tick(self, key: InstrumentKey, _ltp: float, _volume: int | None) -> None:
        with self._lock:
            relevant = key in self._subscribed_keys
            if relevant:
                self._last_option_tick_ts = time.time()
            now = time.time()
            throttled = (now - self._last_push_ts) < self._push_min_interval
        if not relevant or throttled:
            return
        with self._lock:
            self._last_push_ts = now
        self._refresh_snapshot()
        hub.publish(events.option_chain_update(self.snapshot()))

    def resubscribe(self) -> None:
        """Force a full token rebuild + resubscribe on the shared market feed —
        called by the reliability layer after a session re-auth invalidates
        the old option tokens.

        `reassert` as well as the rebuild: after a reconnect the DESIRED set is
        usually identical to the one already declared, so the hub would treat a
        re-declaration as a no-op — while the feed, which has forgotten
        everything, carries nothing at all.
        """
        self._clear_subscribed()
        option_subs.reassert()

    def select(self, symbol: str | None, count: int | None, expiry: str | None = None) -> None:
        with self._lock:
            if symbol:
                new_symbol = symbol.upper()
                if new_symbol != self._symbol:
                    # Different index → its expiry list differs; revert to
                    # nearest unless the caller pinned one in the same call.
                    self._expiry = None
                    self._expiries = []
                self._symbol = new_symbol
            if count:
                # Cap matched to the renderer's "All Strikes" (ALL_RANGE). It was
                # 25, which is BELOW the explicit "30" option, so picking "All"
                # after "30" silently narrowed the chain. 50 either side is 202
                # contracts — comfortably inside every feed's subscription
                # budget, and the grid now repaints per strike rather than
                # wholesale, so the width costs nothing to render.
                self._count = max(1, min(int(count), 50))
            if expiry is not None:
                self._expiry = expiry or None
            # Force a resubscribe on next rebuild by clearing the token set.
            self._clear_subscribed()
        manager.subscriptions.register("option_chain", "active", {"symbol": self._symbol, "count": self._count})
        # Wake the run loop so the switch takes effect right away.
        self._wake.set()

    def set_watch(self, symbol: str | None, expiry: str | None, opt_type: str | None,
                  strikes: list | None) -> None:
        """Track an explicit set of contracts (one index + expiry + option type,
        many strikes) in addition to the chain window. Pass empty strikes to
        clear. Used by the Roll picker so each offered strike shows ITS OWN live
        premium instead of one derived from another contract."""
        with self._lock:
            if not symbol or not opt_type or not strikes:
                self._watch = None
                self._watch_keys = {}
            else:
                self._watch = {
                    "symbol": symbol.upper(),
                    "expiry": expiry or "",
                    "optType": opt_type.upper(),
                    "strikes": sorted({int(s) for s in strikes}),
                }
            # Force the next rebuild to resubscribe with the new token set.
            self._clear_subscribed()
        # Answer from the cache NOW, before waking the rebuild.
        #
        # The Roll picker opens on this call and used to render "—" against every
        # strike until a rebuild had resolved the contracts, subscribed them and
        # a first tick had arrived — a second or more of an empty dialog for
        # contracts whose price the sidecar very often already had. Roll
        # candidates sit close to the position, so they are usually inside the
        # chain window and already ticking. Publishing the cached quotes first
        # means the dialog is populated the moment it appears, and the
        # subscription below only has to fill in whatever was genuinely missing.
        self._publish_watch_from_cache()
        self._wake.set()

    def _publish_watch_from_cache(self) -> None:
        """Fill the snapshot's `watch` block from quotes already in memory."""
        watch_keys = self._resolve_watch_keys()
        with self._lock:
            self._watch_keys = watch_keys
            base = dict(self._snapshot)
        base["watch"] = [
            {"strike": strike,
             "ltp": round(ltp, 2) if (ltp := manager.get_option_ltp(key)) is not None else None}
            for strike, key in sorted(watch_keys.items())
        ]
        with self._lock:
            self._snapshot = base
        hub.publish(events.option_chain_update(self.snapshot()))

    def _resolve_watch_keys(self) -> dict[int, InstrumentKey]:
        """strike -> key for the current watch list. Strikes no connected
        broker lists are simply absent, so the UI shows no price rather than a
        wrong one."""
        with self._lock:
            watch = dict(self._watch) if self._watch else None
        if not watch:
            return {}
        expiry = watch["expiry"]
        if not expiry or expiry_filter.is_expired(expiry):
            return {}
        out: dict[int, InstrumentKey] = {}
        for strike in watch["strikes"]:
            key = InstrumentKey.option(watch["symbol"], expiry, int(strike),
                                       watch["optType"])
            if instruments.has(key):
                out[int(strike)] = key
        return out

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._snapshot)

    def _rebuild_tokens(self) -> None:
        """Resolve the ATM window to canonical contracts and (re)subscribe.

        Entirely broker-agnostic: expiries and strikes come from the shared
        instrument registry, which every feed populates from its OWN scrip
        master. Whichever broker is connected, the chain builds the same way —
        it no longer reads Angel's instrument-master rows, which is what used
        to leave it empty in a Dhan-only session.
        """
        symbol = self._symbol

        # Expiries need no spot price, so the dropdown populates the instant
        # any broker's catalogue is bound.
        expiries = instruments.expiries(symbol)
        if not expiries:
            return  # no catalogue yet — no broker connected, or none listing this index
        with self._lock:
            selected_expiry = self._expiry
            if selected_expiry not in expiries:
                # Stale/unset pick (e.g. just switched index) → nearest.
                selected_expiry = None
                self._expiry = None
            self._expiries = expiries
        expiry = selected_expiry or expiries[0]

        # Watch contracts (Roll picker) are independent of the chain window and
        # may sit on a different index — resolved and subscribed alongside it.
        watch_keys = self._resolve_watch_keys()

        spot = manager.index_ltp.get(symbol)
        if not spot:
            # No spot yet — still surface the expiry list + empty rows so the
            # UI can render the dropdown while we wait for the first index tick.
            with self._lock:
                self._watch_keys = watch_keys
                self._snapshot = {"symbol": symbol, "expiry": expiry,
                                  "expiries": expiries, "atm": 0, "rows": [],
                                  "watch": []}
            hub.publish(events.option_chain_update(self.snapshot()))
            return

        # Strike interval comes from _STEP, not the legacy mixin: that map has
        # no MIDCPNIFTY entry and silently falls back to 50, which would snap
        # the ATM to a strike that does not exist on a 25-point ladder (and so
        # resolve nothing at all). _STEP is the sidecar's own source of truth.
        step = _STEP.get(symbol, 50)
        atm = int(round(spot / step) * step)
        strike_keys = self._window_keys(symbol, expiry, atm, step)

        key_set = {k for legs in strike_keys.values() for k in legs.values()}
        key_set |= set(watch_keys.values())
        with self._lock:
            self._strike_keys = strike_keys
            self._watch_keys = watch_keys
            # Preserve rows only if still on the same symbol+expiry, else blank
            # them so a switch doesn't briefly show the previous chain's prices.
            same = (self._snapshot.get("symbol") == symbol
                    and self._snapshot.get("expiry") == expiry)
            self._snapshot = {"symbol": symbol, "expiry": expiry,
                              "expiries": expiries, "atm": atm,
                              "rows": self._snapshot.get("rows", []) if same else [],
                              "watch": []}

        if key_set and key_set != self._subscribed_keys:
            self._set_subscribed(key_set)
            self._subscribed_at = time.time()
            # PUBLISH FIRST, SUBSCRIBE SECOND. Deliberate, and the order matters.
            #
            # Declaring the subscription reaches a broker socket and can take
            # hundreds of milliseconds — for a wide window it is a couple of
            # hundred contracts across several batched messages. Publishing after
            # it meant that widening the chain (5 strikes -> All) left the grid
            # showing the OLD, narrower window for the whole of that round trip,
            # which reads as the application having frozen.
            #
            # Every strike in the new window is known before any of it is
            # subscribed, and prices we already hold are filled in from the tick
            # cache, so the grid can paint its final shape immediately and let
            # the remaining prices arrive as ticks. A strike with no quote yet
            # renders blank rather than absent.
            self._refresh_snapshot()
            hub.publish(events.option_chain_update(self.snapshot()))
            # DECLARE, don't subscribe: the hub adds open positions' contracts
            # and sends the union, so changing index or expiry can never
            # unsubscribe a contract something else still needs. The serving
            # feed translates the keys into its own tokens; the chain never
            # sees either.
            option_subs.set(CHAIN, key_set)

    def _window_keys(self, symbol: str, expiry: str, atm: int,
                     step: int) -> dict[int, dict[str, InstrumentKey]]:
        """strike -> {"CE": key, "PE": key} for the active window, keeping only
        contracts some connected broker actually lists."""
        out: dict[int, dict[str, InstrumentKey]] = {}
        for k in range(-self._count, self._count + 1):
            strike = atm + k * step
            for opt in ("CE", "PE"):
                key = InstrumentKey.option(symbol, expiry, strike, opt)
                if instruments.has(key):
                    out.setdefault(int(strike), {})[opt] = key
        return out

    def _refresh_snapshot(self) -> None:
        if not self._subscribed_keys:
            return
        with self._lock:
            strike_keys = dict(self._strike_keys)
            watch_keys = dict(self._watch_keys)
            base = dict(self._snapshot)
        rows = []
        for strike in sorted(strike_keys):
            legs = strike_keys[strike]
            ce_tick = manager.get_option_tick(legs["CE"]) if legs.get("CE") else {}
            pe_tick = manager.get_option_tick(legs["PE"]) if legs.get("PE") else {}
            ce = ce_tick.get("ltp")
            pe = pe_tick.get("ltp")
            rows.append({
                "strike": strike,
                "ce": round(ce, 2) if ce is not None else None,
                "pe": round(pe, 2) if pe is not None else None,
                # Real open interest where the feed carries it (Angel
                # SNAP_QUOTE, Dhan Full). Only when OI is genuinely absent does
                # this fall back to traded volume, which is what these columns
                # used to show unconditionally despite being labelled OI.
                "ceoi": _oi_of(ce_tick),
                "peoi": _oi_of(pe_tick),
            })
        base["rows"] = rows
        # Each watched strike carries its OWN contract's live LTP.
        watch = []
        for strike, key in sorted(watch_keys.items()):
            ltp = manager.get_option_ltp(key)
            watch.append({"strike": strike, "ltp": round(ltp, 2) if ltp is not None else None})
        base["watch"] = watch
        with self._lock:
            self._snapshot = base

    def _watchdog(self) -> None:
        """Re-assert the option subscription when we hold tokens but no option
        tick has arrived for them.

        `_subscribed_keys` records that we SENT a subscribe, not that data is
        flowing — the same "started once" trap that left the market feed dead.
        If the subscribe was lost (sent on a socket that was mid-reconnect, or
        dropped by the server), nothing else would ever retry it and the chain
        would show strikes with blank prices indefinitely.
        """
        if not self._subscribed_keys or not manager.option_feed_connected:
            return
        # Only meaningful while the session is live; outside hours silence is
        # expected and re-subscribing would just churn.
        if not market_session.is_market_open(symbol=self._symbol):
            return
        now = time.time()
        last_data = max(self._last_option_tick_ts, self._subscribed_at)
        if (now - last_data) < OPTION_SILENCE_S:
            return
        if (now - self._last_resub_ts) < RESUBSCRIBE_COOLDOWN_S:
            return
        self._last_resub_ts = now
        hub.publish(events.log_line(
            "warn", f"[option-chain] no option ticks for {now - last_data:.0f}s "
                    f"on {len(self._subscribed_keys)} subscribed contracts — resubscribing"))
        # Clearing forces the next _rebuild_tokens to resolve and re-declare;
        # the reassert is what actually puts the request back on the wire when
        # the desired set has not changed.
        self._clear_subscribed()
        option_subs.reassert()
        self._wake.set()

    def _run(self) -> None:
        while True:
            try:
                self._rebuild_tokens()
                self._refresh_snapshot()
                self._watchdog()
            except Exception as e:
                # Keep the hub message (unchanged), but also retain the full
                # traceback and write it to the log file — this loop's only
                # error channel was the hub, which is never persisted, so a
                # repeating failure here was completely invisible.
                self._last_error = traceback.format_exc()
                self._last_error_ts = time.time()
                _file_log.error("[option-chain] loop error: %s", self._last_error)
                hub.publish(events.log_line("warn", f"[option-chain] {e}"))
            # Wake early if select() fired (index/expiry switch); otherwise
            # tick every 1s as a rebuild/reconcile heartbeat (ATM drift etc.).
            self._wake.wait(timeout=1.0)
            self._wake.clear()


option_chain = OptionChainAdapter()
