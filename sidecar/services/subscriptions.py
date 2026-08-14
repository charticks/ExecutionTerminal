"""One desired option-subscription set, pooled from every source that needs one.

Why this exists
---------------
A feed's option subscription is a *replace*, not an add: handing it a set of
contracts unsubscribes everything else. The option chain was the only caller, so
switching the chain from CRUDEOIL to BANKNIFTY silently unsubscribed the very
contract an open CRUDEOIL position was being managed on. No ticks then reached
the live manager for it, and its stop loss stopped evaluating — with the
position still displayed, still apparently protected, while Crude fell.

So no component may hand the feed a subscription directly any more. Each one
declares what IT needs under its own source name and this hub sends the union:

    option chain window ─┐
    live positions      ─┼─→ union ─→ feed.subscribe_keys(...)
    paper positions     ─┘

Removing a contract from one source therefore cannot unsubscribe it while
another source still wants it, which is the property that was missing.

`reassert()` re-sends the current union even when it has not changed — used
after a reconnect, where the feed has forgotten what it was carrying but our
idea of the desired set is unchanged.
"""
from __future__ import annotations

import threading
import time

import diagnostics
from services.instruments import InstrumentKey

# Sources. Named constants because a typo in a source name would silently
# create a second, permanently empty source rather than update an existing one.
CHAIN = "chain"
LIVE = "live"
PAPER = "paper"


class OptionSubscriptionHub:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sources: dict[str, set[InstrumentKey]] = {}
        self._sent: set[InstrumentKey] = set()
        self._sent_ts = 0.0

    # ── declaration ───────────────────────────────────────────────────────
    def set(self, source: str, keys) -> None:
        """Replace `source`'s desired contracts and reconcile the union."""
        wanted = {k for k in keys if isinstance(k, InstrumentKey)}
        with self._lock:
            if self._sources.get(source) == wanted:
                return
            self._sources[source] = wanted
        self._reconcile()

    def add(self, source: str, key: InstrumentKey) -> None:
        with self._lock:
            current = self._sources.get(source, set())
            if key in current:
                return
            self._sources[source] = current | {key}
        self._reconcile()

    def union(self) -> set[InstrumentKey]:
        with self._lock:
            out: set[InstrumentKey] = set()
            for keys in self._sources.values():
                out |= keys
            return out

    def subscribed(self) -> set[InstrumentKey]:
        """What was last actually sent to a feed."""
        with self._lock:
            return set(self._sent)

    def covers(self, key: InstrumentKey) -> bool:
        """Whether `key` is in the set currently sent to the feed. The live
        manager asks this per position: a position outside it cannot receive
        ticks, which is a monitoring failure and not a market condition."""
        with self._lock:
            return key in self._sent

    # ── delivery ──────────────────────────────────────────────────────────
    def reassert(self) -> None:
        """Re-send the union even if unchanged (post-reconnect)."""
        self._reconcile(force=True)

    def _reconcile(self, force: bool = False) -> None:
        wanted = self.union()
        with self._lock:
            if not force and wanted == self._sent:
                return
            previous = set(self._sent)
            self._sent = wanted
            self._sent_ts = time.time()
        # Outside the lock: this reaches a broker socket, and holding the hub's
        # lock across network I/O would stall every declaring thread.
        try:
            from services.broker_manager import manager
            manager.subscribe_option_keys(wanted)
        except Exception as exc:
            with self._lock:
                self._sent = previous  # not delivered — do not claim it was
            diagnostics.exception("websocket", "Option subscription failed",
                                  exc_info=exc, contracts=len(wanted))
            return
        added = len(wanted - previous)
        dropped = len(previous - wanted)
        if added or dropped or force:
            diagnostics.event(
                "websocket", "Option subscription", "sent",
                contracts=len(wanted), added=added, dropped=dropped,
                sources=",".join(f"{s}:{len(k)}" for s, k in
                                 sorted(self._snapshot_sources().items())))

    def _snapshot_sources(self) -> dict[str, set[InstrumentKey]]:
        with self._lock:
            return dict(self._sources)

    # ── diagnostics ───────────────────────────────────────────────────────
    def status(self) -> dict:
        with self._lock:
            return {
                "subscribed": len(self._sent),
                "sentTs": self._sent_ts,
                "sources": {s: sorted(str(k) for k in keys)
                            for s, keys in self._sources.items()},
            }


option_subs = OptionSubscriptionHub()
