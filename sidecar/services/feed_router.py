"""Owns every live broker feed and the one tick cache they all write into.

This is the piece that makes "connect a broker → its WebSocket comes up" true:
`attach()` is called from the broker connect path and `detach()` from
disconnect, so feed lifetime follows account lifetime instead of being wired by
hand at one call site.

It is also where the multi-feed question gets answered. Two brokers will both
happily stream NIFTY, and blindly merging them yields an LTP that flickers
between two sources with different latency. So each capability has exactly one
*primary* feed at a time; non-primary ticks are dropped rather than published.
When a primary disconnects, the next attached feed that advertises the
capability is promoted. Deterministic and inspectable — which matters far more
here than squeezing out the few milliseconds a first-tick-wins merge might save.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from bridge import events
from bridge.hub import hub

from services.instruments import InstrumentKey
from services.feeds.base import MarketFeed
from services.feeds.registry import feed_class
from services.reliability.subscription_registry import SubscriptionRegistry


class FeedRouter:
    def __init__(
        self,
        subscriptions: SubscriptionRegistry,
        instrument_master: Callable[[], list[dict]],
        option_exchange: Callable[[str], str | None],
        report_error: Callable[[str, Any], str],
        log: Callable[[str, str], None],
    ) -> None:
        # Injected rather than imported: the router must not depend on
        # BrokerManager, or the import graph cycles (BrokerManager builds it).
        self._subscriptions = subscriptions
        self._instrument_master = instrument_master
        self._option_exchange = option_exchange
        self._report_error = report_error
        self._log = log

        self._lock = threading.RLock()
        self._feeds: dict[str, MarketFeed] = {}       # account_id -> feed
        self._primary: dict[str, str] = {}            # capability -> account_id

        # Canonical market-data caches, shared across every feed. Keyed by
        # InstrumentKey so the same contract occupies one entry no matter which
        # broker carried the tick (see services.instruments).
        self.tick_lock = threading.Lock()
        self.option_ticks: dict[InstrumentKey, dict] = {}
        self.index_ltp: dict[str, float] = {}

        # Ticks the registry could not map to an InstrumentKey — should stay 0.
        # A rising count means a token was subscribed without being bound, i.e.
        # the chain will show blank prices for a contract that IS streaming.
        self.unmapped_ticks: int = 0
        self.last_unmapped_token: str | None = None

        self._option_tick_listeners: list[Any] = []
        self._index_tick_listeners: list[Any] = []

    # ── lifecycle, driven by broker connect/disconnect ────────────────────
    def attach(self, account_id: str, broker: str) -> MarketFeed | None:
        """Ensure `account_id` has a running feed. Idempotent: reconnecting an
        already-attached account reuses its feed and reconciles the connection
        rather than building a second socket."""
        cls = feed_class(broker)
        if cls is None:
            return None  # broker trades but does not stream (Kotak, Dhan today)
        with self._lock:
            feed = self._feeds.get(account_id)
            if feed is None:
                feed = self._build(cls, account_id)
                self._feeds[account_id] = feed
                self._claim_capabilities(account_id, feed)
        return feed

    def _build(self, cls: type[MarketFeed], account_id: str) -> MarketFeed:
        # AngelFeed needs the shared subscription registry; a feed that doesn't
        # take one is constructed with the plain (account_id, host) signature.
        try:
            return cls(account_id, self, self._subscriptions)  # type: ignore[call-arg]
        except TypeError:
            return cls(account_id, self)

    def detach(self, account_id: str) -> None:
        with self._lock:
            feed = self._feeds.pop(account_id, None)
            if feed is None:
                return
            self._release_capabilities(account_id)
        feed.stop()
        self._promote_all()

    def feed_for(self, account_id: str) -> MarketFeed | None:
        with self._lock:
            return self._feeds.get(account_id)

    def feeds(self) -> list[MarketFeed]:
        with self._lock:
            return list(self._feeds.values())

    def scrip_of(self, broker: str, session=None):
        """A broker's loaded scrip master, or None.

        For callers that hold a SESSION rather than an account id — margin
        checkers and the order router's modify path both do, because a broker's
        API is addressed through its session. Where the feed holds that same
        client object (Kotak shares one session between feed and orders) identity
        picks the exact feed; otherwise the first feed of that broker answers,
        which is sound because a scrip master is a broker-wide instrument list,
        not per-account data — two accounts at one broker resolve a contract
        identically.
        """
        candidates = [f for f in self.feeds() if getattr(f, "broker", "") == broker]
        if session is not None:
            for feed in candidates:
                if getattr(feed, "client", None) is session:
                    return getattr(feed, "scrip", None)
        return getattr(candidates[0], "scrip", None) if candidates else None

    def primary_feed(self, capability: str) -> MarketFeed | None:
        with self._lock:
            aid = self._primary.get(capability)
            return self._feeds.get(aid) if aid else None

    # ── primary selection ─────────────────────────────────────────────────
    def _claim_capabilities(self, account_id: str, feed: MarketFeed) -> None:
        """Caller holds the lock. An unclaimed capability goes to the first
        feed that advertises it — first connected wins, which keeps startup
        order the only thing that decides, rather than tick timing."""
        for cap in feed.capabilities():
            if cap not in self._primary:
                self._primary[cap] = account_id

    def _release_capabilities(self, account_id: str) -> None:
        """Caller holds the lock."""
        for cap in [c for c, aid in self._primary.items() if aid == account_id]:
            del self._primary[cap]

    def _promote_all(self) -> None:
        """Give every orphaned capability to some attached feed that can serve
        it. Called when a feed detaches so the data plane self-heals instead of
        going quiet until the user reconnects something."""
        with self._lock:
            for aid, feed in self._feeds.items():
                for cap in feed.capabilities():
                    if cap not in self._primary:
                        self._primary[cap] = aid
                        self._log("info", f"[feed] {feed.broker} ({aid}) is now primary for {cap}")

    def _is_primary(self, capability: str, account_id: str) -> bool:
        with self._lock:
            claimed = self._primary.get(capability)
            # No claimant (e.g. a feed emitting during teardown) → let it
            # through rather than silently dropping live data.
            return claimed is None or claimed == account_id

    # ── FeedHost implementation (what feeds call back into) ───────────────
    def log(self, level: str, msg: str) -> None:
        self._log(level, msg)

    def instrument_master(self) -> list[dict]:
        return self._instrument_master()

    def option_exchange(self, underlying: str) -> str | None:
        return self._option_exchange(underlying)

    def report_feed_error(self, feed: MarketFeed, err: Any) -> str:
        return self._report_error(feed.account_id, err)

    def on_index_tick(self, feed: MarketFeed, symbol: str, ltp: float,
                      change_pct: float) -> None:
        if not self._is_primary("index", feed.account_id):
            return
        self.index_ltp[symbol] = ltp
        hub.publish(events.index_quote(symbol, round(ltp, 2), round(change_pct, 2)))
        for fn in self._index_tick_listeners:
            try:
                fn(symbol, ltp)
            except Exception as e:
                self._log("warn", f"[index] tick listener error: {e}")

    def on_option_tick(self, feed: MarketFeed, key: InstrumentKey, ltp: float,
                       volume: int | None, bid: float | None, ask: float | None,
                       oi: int | None = None) -> None:
        if not self._is_primary("option", feed.account_id):
            return
        with self.tick_lock:
            prev = self.option_ticks.get(key)
            # Carry the last known bid/ask forward if this tick lacks depth.
            if bid is None and prev:
                bid = prev.get("bid")
            if ask is None and prev:
                ask = prev.get("ask")
            # Same carry-forward for OI: it is a slow-moving figure and some
            # packets omit it, so a gap must not blank a value we already have.
            if oi is None and prev:
                oi = prev.get("oi")
            self.option_ticks[key] = {
                "ltp": ltp, "volume": volume, "bid": bid, "ask": ask,
                "oi": oi, "ts": time.time(), "source": feed.broker}
        for fn in self._option_tick_listeners:
            try:
                fn(key, ltp, volume)
            except Exception as e:
                self._log("warn", f"[option-chain] tick listener error: {e}")

    def on_unmapped_tick(self, feed: MarketFeed, token: str) -> None:
        self.unmapped_ticks += 1
        self.last_unmapped_token = token

    # ── read model ────────────────────────────────────────────────────────
    def add_option_tick_listener(self, fn: Any) -> None:
        self._option_tick_listeners.append(fn)

    def add_index_tick_listener(self, fn: Any) -> None:
        """fn(symbol: str, ltp: float) — same shape and guarantees as
        add_option_tick_listener, for consumers that need the underlying
        index rather than an option contract (e.g. a strategy's spot-based
        candles)."""
        self._index_tick_listeners.append(fn)

    def subscribe_option_keys(self, keys: set) -> None:
        """Hand the desired option set, as canonical keys, to whichever feed
        currently serves option data. A feed that subscribes by broker token
        (Angel) keeps its own path and ignores this."""
        feed = self.primary_feed("option")
        if feed is not None and hasattr(feed, "subscribe_keys"):
            feed.subscribe_keys(keys)

    def ws_managers(self) -> list:
        """The underlying reconnecting-socket objects, for the health monitor's
        stale/zombie watchdog. A feed without one (a future non-WebSocket feed)
        is simply skipped rather than special-cased there."""
        return [f.ws for f in self.feeds() if getattr(f, "ws", None) is not None]

    def status(self) -> list[dict]:
        """Per-feed transport diagnostics for /market-feed."""
        with self._lock:
            primary = dict(self._primary)
            feeds = list(self._feeds.items())
        out = []
        for aid, feed in feeds:
            s = feed.status()
            s["primaryFor"] = sorted(c for c, a in primary.items() if a == aid)
            out.append(s)
        return out
