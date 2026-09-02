"""Which brokers have a market-data feed.

A broker being *supported for trading* and a broker *having a live feed* are
different things, and conflating them is how you end up with a green "connected"
badge over a dead data plane. A broker absent from this map trades over REST and
simply has no feed; the router treats a missing entry as "no feed for this
broker" rather than an error.

Adding a broker's feed = one module + one line here.
"""
from __future__ import annotations

from .angel_feed import AngelFeed
from .base import MarketFeed
from .dhan_feed import DhanFeed
from .firstock_feed import FirstockFeed
from .icici_feed import ICICIFeed
from .kotak_feed import KotakFeed

FEEDS: dict[str, type[MarketFeed]] = {
    "angel": AngelFeed,
    "dhan": DhanFeed,
    "kotak": KotakFeed,
    "icici": ICICIFeed,
    "firstock": FirstockFeed,
}


def has_feed(broker: str) -> bool:
    return (broker or "").lower() in FEEDS


def feed_class(broker: str) -> type[MarketFeed] | None:
    return FEEDS.get((broker or "").lower())
