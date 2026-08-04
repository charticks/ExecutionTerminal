"""The contract every broker market-data feed implements.

A feed owns one broker's streaming connection and nothing else: its own socket,
its own token vocabulary, its own wire format. It converts what arrives into
canonical `InstrumentKey`-addressed ticks and hands them to a `FeedHost`, which
is the only thing it is allowed to know about the rest of the sidecar.

That direction matters. Feeds depend on the host; the host never imports a
feed (see feeds.registry). Adding a broker is then a new module plus a registry
entry, with no edit to any existing feed.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol

from services.instruments import InstrumentKey

# Capabilities a feed can advertise. The router uses these to decide which feed
# serves what, so a broker that streams options but not spot can still be used
# for the half it supports rather than being all-or-nothing.
CAP_INDEX = "index"
CAP_OPTION = "option"
CAP_DEPTH = "depth"


class FeedHost(Protocol):
    """What a feed is allowed to call back into.

    Deliberately narrow: a feed can log, look up reference data, and emit
    ticks. It cannot reach broker sessions, publish hub events directly, or
    touch the option chain.
    """

    def log(self, level: str, msg: str) -> None: ...

    def instrument_master(self) -> list[dict]:
        """Reference data rows for symbol/token resolution."""

    def option_exchange(self, underlying: str) -> str | None:
        """Charticks' exchange segment for an index's options, or None if the
        index is not one we trade."""

    # Each callback takes the emitting feed itself, not a broker name: two
    # accounts can be connected on the SAME broker, and the router has to know
    # which one a tick came from to apply its primary/failover policy.
    def on_index_tick(self, feed: "MarketFeed", symbol: str, ltp: float,
                      change_pct: float) -> None: ...

    def on_option_tick(self, feed: "MarketFeed", key: InstrumentKey, ltp: float,
                       volume: int | None, bid: float | None, ask: float | None,
                       oi: int | None = None) -> None:
        """`oi` is true open interest where the feed carries it (Angel
        SNAP_QUOTE, Dhan Full). None means genuinely unavailable — the option
        chain then falls back to its volume figure rather than showing zero."""

    def on_unmapped_tick(self, feed: "MarketFeed", token: str) -> None:
        """A tick arrived for a token this feed could not map to a key."""

    def report_feed_error(self, feed: "MarketFeed", err: Any) -> str:
        """Classify a transport error (session_expired / network / unknown) so
        feed-surfaced auth failures take the same recovery path as REST ones."""


class MarketFeed(ABC):
    """One broker's live market-data connection."""

    broker: str = ""

    def __init__(self, account_id: str, host: FeedHost) -> None:
        self.account_id = account_id
        self.host = host

    @abstractmethod
    def start(self) -> None:
        """Bring the connection up. Must be idempotent — callers reconcile with
        this rather than tracking whether they already started it, because
        'started once' is not the same as 'currently carrying data'."""

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def reconnect(self) -> None:
        """Force a fresh connection now, e.g. after re-auth changed the tokens."""

    @abstractmethod
    def capabilities(self) -> set[str]: ...

    @abstractmethod
    def status(self) -> dict:
        """Transport diagnostics — same shape as WebSocketManager.status(), so
        the health monitor and /market-feed have one shape to read."""

    @property
    @abstractmethod
    def connected(self) -> bool: ...
