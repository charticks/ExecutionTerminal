"""Broker-independent margin contract.

The Order Engine knows only what is in this module. Each broker implements
``MarginChecker`` in its own file and registers it here, so adding a broker never
touches order routing.

Fail-safe by construction: a checker either returns a MarginQuote it stands
behind, or raises MarginUnavailable. There is no third outcome — "we could not
tell" is a rejection, never a silent pass.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class MarginUnavailable(Exception):
    """The broker could not tell us the margin position for this order.

    Raised for a timeout, an API error, an unparseable response, or a missing
    capability. Always results in the order being rejected — an unverifiable
    margin is treated exactly like an insufficient one.
    """


@dataclass(frozen=True)
class MarginRequest:
    """The order, described without reference to any broker's field names."""
    underlying: str
    expiry: str
    strike: float
    opt_type: str            # CE | PE
    side: str                # BUY | SELL
    qty: int
    lots: int
    order_type: str          # MARKET | LIMIT
    price: float             # limit price, 0 for MARKET
    product: str             # NRML | MIS
    exchange: str            # NFO | BFO
    tradingsymbol: str = ""
    token: str = ""
    ltp: float | None = None

    @property
    def symbol(self) -> str:
        return f"{self.underlying} {self.expiry} {int(self.strike)} {self.opt_type}"

    @property
    def reference_price(self) -> float:
        """Price to value the order at: the limit if it has one, else the last
        trade. Used for the deterministic debit below."""
        if self.order_type == "LIMIT" and self.price > 0:
            return self.price
        return self.ltp or 0.0

    def premium_debit(self) -> float | None:
        """Cash a long option costs outright — premium x quantity.

        For a BUY this is the margin requirement, as arithmetic rather than an
        estimate, so it is a legitimate answer when a broker exposes no margin
        API. A SHORT option's requirement is SPAN + exposure, which genuinely
        cannot be derived here; those must come from the broker or be rejected.
        """
        price = self.reference_price
        if self.side != "BUY" or price <= 0:
            return None
        return round(price * self.qty, 2)


@dataclass(frozen=True)
class MarginQuote:
    """A broker's answer. Both figures are in rupees."""
    required: float
    available: float
    source: str                 # how it was obtained — goes into the log
    estimated_requirement: bool = False  # derived, not quoted by the broker

    @property
    def sufficient(self) -> bool:
        return self.available >= self.required

    @property
    def shortfall(self) -> float:
        return max(0.0, round(self.required - self.available, 2))


# (session, request) -> MarginQuote. Must raise MarginUnavailable rather than
# return a guess. The session is whatever the broker's SDK login produced.
MarginChecker = Callable[[Any, MarginRequest], MarginQuote]

_CHECKERS: dict[str, MarginChecker] = {}


def register(broker: str, checker: MarginChecker) -> None:
    _CHECKERS[broker.lower()] = checker


def checker_for(broker: str) -> MarginChecker | None:
    return _CHECKERS.get((broker or "").lower())


def registered_brokers() -> list[str]:
    return sorted(_CHECKERS)


# ── shared parsing helpers ────────────────────────────────────────────────
# Every Indian broker SDK returns loosely-typed JSON with its own casing and
# its own spelling (Dhan really does ship "availabelBalance"). These keep that
# mess in one place instead of in four adapters.

def as_float(value: Any) -> float | None:
    """Tolerant numeric parse: handles strings, commas, None and blanks."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            value = value.replace(",", "").strip()
            if not value:
                return None
        return float(value)
    except (TypeError, ValueError):
        return None


def pluck(payload: Any, *keys: str) -> float | None:
    """First parseable numeric value among `keys`, searched case-insensitively
    and recursively through nested dicts.

    Broker responses nest their real payload under "data", "Success" or
    similar, and rename fields between SDK versions; matching on a set of
    candidate names is more durable than a fixed path.
    """
    if not isinstance(payload, dict):
        return None
    wanted = {k.lower() for k in keys}
    for key, value in payload.items():
        if str(key).lower() in wanted:
            number = as_float(value)
            if number is not None:
                return number
    for value in payload.values():
        if isinstance(value, dict):
            found = pluck(value, *keys)
            if found is not None:
                return found
        elif isinstance(value, list):
            for item in value:
                found = pluck(item, *keys)
                if found is not None:
                    return found
    return None
