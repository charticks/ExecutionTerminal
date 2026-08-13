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
    # Which balance field the broker's response was read from. Logged on every
    # rejection: "available=0" is unactionable on its own, because it cannot be
    # told apart from having read the wrong field of a response that did carry
    # the balance.
    available_source: str = ""

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


def scrip_of(broker: str, session: Any) -> Any:
    """A broker's loaded scrip master, or None — see FeedRouter.scrip_of.

    Imported lazily: services.broker_manager pulls in the whole feed layer, and
    this module is imported by every margin adapter at registration time.
    """
    from services.broker_manager import manager

    return manager.router.scrip_of(broker, session)


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


def _find(payload: Any, wanted: str) -> float | None:
    """Numeric value of one key, case-insensitive, searched recursively.

    Broker responses nest their real payload under "data", "Success" or similar,
    so a fixed path is not durable across SDK versions.
    """
    if not isinstance(payload, dict):
        return None
    for key, value in payload.items():
        if str(key).lower() == wanted:
            number = as_float(value)
            if number is not None:
                return number
    for value in payload.values():
        if isinstance(value, dict):
            found = _find(value, wanted)
            if found is not None:
                return found
        elif isinstance(value, list):
            for item in value:
                found = _find(item, wanted)
                if found is not None:
                    return found
    return None


def pluck_field(payload: Any, *keys: str) -> tuple[float | None, str]:
    """(value, field name) for the first of `keys` that is present and numeric.

    `keys` is a PRIORITY ORDER, and is honoured as one. This used to walk the
    response once and return whichever candidate name happened to appear first
    in the broker's own JSON — so for a balance asked for as
    ("availablecash", ..., "net", ...) an account whose `net` sat above
    `availablecash` in the payload was read as `net`. On an account with open
    positions or funds in another segment those two figures differ, and reading
    the wrong one silently understates the balance.

    The field name is returned so a rejection can say which number it used.
    """
    for key in keys:
        found = _find(payload, key.lower())
        if found is not None:
            return found, key
    return None, ""


def pluck(payload: Any, *keys: str) -> float | None:
    """`pluck_field` when only the value is wanted."""
    return pluck_field(payload, *keys)[0]
