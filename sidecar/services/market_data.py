"""Live market-data adapters: the option chain and the positions poller.

The option-chain adapter is broker-agnostic. It asks ``services.instruments``
which expiries and strikes exist, builds the ATM window as canonical
``InstrumentKey``s, and hands that set to whichever feed is serving option
data — which then translates to its own tokens. It previously scanned Angel's
raw instrument-master rows to do the same job, which meant the chain was empty
unless Angel specifically was connected; it now works with any supported
broker, including a Dhan-only session.

Consequently this module no longer drives ``app.option_chain.OptionChainMixin``
(the legacy Tkinter mixin, headlessly, via stub tk vars). ATM selection lives
here in ``_STEP`` — the mixin's strike map had no MIDCPNIFTY entry and silently
fell back to 50, snapping the ATM to a strike that does not exist on a
25-point ladder.

The positions adapter polls each connected account's broker position book and
publishes per-account position_update plus an aggregate pnl_update; its
per-broker normalisers are necessarily broker-specific. All outputs go through
the same EventHub / REST contract the renderer already reads.
"""
from __future__ import annotations

import threading
import time
import traceback
from typing import Any

from logzero import logger as _file_log

from bridge import events
from bridge.hub import hub
from services.broker_manager import manager
from services import expiry as expiry_filter
from services import market_session
from services.instruments import InstrumentKey, instruments

_STEP = {"SENSEX": 100, "NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50,
         "MIDCPNIFTY": 25, "BANKEX": 100, "CRUDEOIL": 50}
_OPT_EXCH_TYPE = {"NFO": 2, "BFO": 4, "MCX": 5}  # Angel SmartWebSocket exchangeType codes

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
        the old option tokens."""
        self._clear_subscribed()

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
                self._count = max(1, min(int(count), 25))
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
        self._wake.set()

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
            # One route for every broker. The serving feed translates these
            # keys into its own tokens; the chain never sees either.
            manager.subscribe_option_keys(key_set)
            # Push immediately (new symbol/expiry/atm + empty rows) so the grid
            # repaints without waiting for the first option tick.
            self._refresh_snapshot()
            hub.publish(events.option_chain_update(self.snapshot()))

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
        # Clearing forces the next _rebuild_tokens to resolve and resubscribe.
        self._clear_subscribed()
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


class PositionsAdapter:
    """Polls each connected account's broker position book and publishes
    per-account position_update + an aggregate pnl_update."""

    def __init__(self) -> None:
        self._known_ids: set[str] = set()
        threading.Thread(target=self._run, name="positions-adapter", daemon=True).start()

    # ── per-broker position-book normalisers ──────────────────────────────
    @staticmethod
    def _f(v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def _angel(self, account_id: str, sess: Any) -> list[dict]:
        out: list[dict] = []
        resp = sess.position()
        data = resp.get("data") if isinstance(resp, dict) else None
        for p in data or []:
            qty = int(self._f(p.get("netqty")))
            if qty == 0:
                continue
            avg = self._f(p.get("totalbuyavgprice") or p.get("buyavgprice") or p.get("avgnetprice"))
            out.append({
                "id": f"{account_id}:{p.get('symboltoken') or p.get('tradingsymbol')}",
                "symbol": p.get("tradingsymbol", ""),
                "side": "BUY" if qty > 0 else "SELL",
                "qty": abs(qty),
                "entry": round(avg, 2),
                "ltp": round(self._f(p.get("ltp")), 2),
                "pnl": round(self._f(p.get("pnl")), 2),
                "account": account_id,
            })
        return out

    def _dhan(self, account_id: str, sess: Any) -> list[dict]:
        out: list[dict] = []
        resp = sess.get_positions()
        data = resp.get("data") if isinstance(resp, dict) else resp
        for p in data or []:
            qty = int(self._f(p.get("netQty")))
            if qty == 0:
                continue
            avg = self._f(p.get("buyAvg") if qty > 0 else p.get("sellAvg"))
            pnl = self._f(p.get("unrealizedProfit")) + self._f(p.get("realizedProfit"))
            out.append({
                "id": f"{account_id}:{p.get('securityId') or p.get('tradingSymbol')}",
                "symbol": p.get("tradingSymbol", ""),
                "side": "BUY" if qty > 0 else "SELL",
                "qty": abs(qty),
                "entry": round(avg, 2),
                "ltp": round(self._f(p.get("ltp") or p.get("lastTradedPrice")), 2),
                "pnl": round(pnl, 2),
                "account": account_id,
            })
        return out

    def _kotak(self, account_id: str, sess: Any) -> list[dict]:
        out: list[dict] = []
        resp = sess.positions()
        data = resp.get("data") if isinstance(resp, dict) else None
        for p in data or []:
            qty = int(self._f(p.get("flBuyQty")) - self._f(p.get("flSellQty")))
            if qty == 0:
                continue
            avg = self._f(p.get("buyAmt") if qty > 0 else p.get("sellAmt"))
            avg = round(avg / abs(qty), 2) if qty else 0.0
            out.append({
                "id": f"{account_id}:{p.get('tok') or p.get('trdSym')}",
                "symbol": p.get("trdSym", ""),
                "side": "BUY" if qty > 0 else "SELL",
                "qty": abs(qty),
                "entry": avg,
                "ltp": round(self._f(p.get("ltp")), 2),
                "pnl": round(self._f(p.get("urPnl") or p.get("rlPnl")), 2),
                "account": account_id,
            })
        return out

    def _poll_once(self) -> None:
        sessions = manager.connected_sessions()
        seen: set[str] = set()
        net_pnl = 0.0
        for account_id, broker, sess in sessions:
            try:
                if broker == "angel":
                    rows = self._angel(account_id, sess)
                elif broker == "dhan":
                    rows = self._dhan(account_id, sess)
                elif broker == "kotak":
                    rows = self._kotak(account_id, sess)
                else:
                    rows = []
            except Exception as e:
                classification = manager.session_manager.report_error(account_id, broker, e)
                if classification != "session_expired":
                    hub.publish(events.log_line("warn", f"[positions] {broker} poll failed ({classification}): {e}"))
                continue
            for r in rows:
                seen.add(r["id"])
                net_pnl += r["pnl"]
                hub.publish(events.position_update(r))
        # Positions that vanished (closed) since last poll → flag them flat/closed.
        for gone in self._known_ids - seen:
            hub.publish(events.position_update({"id": gone, "qty": 0, "pnl": 0, "closed": True}))
        self._known_ids = seen
        hub.publish(events.pnl_update(round(net_pnl, 2)))

    def _run(self) -> None:
        while True:
            if manager.connected_sessions():
                try:
                    self._poll_once()
                except Exception as e:
                    hub.publish(events.log_line("warn", f"[positions] {e}"))
            time.sleep(4.0)


option_chain = OptionChainAdapter()
positions = PositionsAdapter()
