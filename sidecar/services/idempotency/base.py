"""Broker-independent idempotency contract.

The Order Engine asks one question before it sends anything — "has this exact
order already reached this broker?" — and one broker-shaped object answers it.
Adding a broker is a new `BrokerIdem` plus a `register()` call; the engine never
changes. Same shape as services/margin/ and services/order_sync/.

Three tiers of broker support, one interface
-------------------------------------------
* **native** — the broker accepts a client id AND can be queried by it. Dhan
  takes `tag` (sent as `correlationId`) and offers
  `get_order_by_correlationID`. This is the only tier that can answer with
  certainty, so it is preferred wherever it exists.
* **tag echo** — the broker accepts a client id and returns it on the order-book
  row, but has no lookup endpoint. Kotak takes `tag` (body field `ig`); ICICI
  takes `user_remark`. Reconciliation reads the order book and matches the tag.
* **attribute match** — the broker has no client-id concept at all. Angel's
  SmartAPI order params carry no tag and its order book has no client field, so
  a resent order can only be recognised by what it looks like: same symbol, side,
  quantity and a placement time inside the retry window.

The tiers differ only in confidence, and the caller is told which it got, so a
weaker tier is never silently presented as a guarantee.

Answering "no" is not the same as "don't know"
----------------------------------------------
`Resolution` has three values, not two, and UNKNOWN is not ABSENT. A broker we
could not reach, a response we could not parse, or a status vocabulary we do not
recognise all mean *we cannot tell whether the order exists* — and the safe
action there is to refuse to send, exactly as an unverifiable margin rejects the
order rather than passing it. Collapsing UNKNOWN into ABSENT is how an
idempotency layer causes the duplicate it exists to prevent.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

# Client ids ride in fields brokers built for short human tags, so they are kept
# short and strictly alphanumeric. Dhan documents 25 characters for
# correlationId; the others are undocumented, so the shortest useful id wins.
COID_PREFIX = "CH"
_DIGEST_CHARS = 11


class Tier(str, Enum):
    NATIVE = "native"          # broker can be queried by client id
    TAG_ECHO = "tag-echo"      # broker echoes the client id in its order book
    ATTRIBUTE = "attribute"    # no client id; matched on order attributes


class Resolution(str, Enum):
    FOUND = "found"        # the order is at the broker — adopt it, never resend
    ABSENT = "absent"      # definitively not there — safe to send
    UNKNOWN = "unknown"    # cannot tell — must NOT send


@dataclass(frozen=True)
class Placement:
    """One order about to be sent, in canonical terms.

    Used both to fingerprint the request and, on the attribute tier, to
    recognise it in a broker's order book. `leg` distinguishes the children of a
    split order: three legs of an order too big for one submission are three
    separate broker orders and must not collapse into one claim.
    """
    account_id: str
    broker: str
    underlying: str
    expiry: str
    strike: float
    opt_type: str
    side: str
    qty: int
    order_type: str
    price: float
    product: str
    validity: str
    leg: int = 0
    # Supplied by the caller when the client has its own request identity (a UI
    # that reissues the same id on retry). When present it replaces the derived
    # fingerprint, because the client knows better than we can infer.
    request_id: str = ""

    @property
    def symbol(self) -> str:
        return f"{self.underlying} {self.expiry} {int(self.strike)} {self.opt_type}"

    def fingerprint(self) -> str:
        """Stable digest of the order's intent.

        Deliberately excludes time: two clicks a second apart must produce the
        SAME fingerprint so the second is recognised as a duplicate. Separating
        a genuine second order from a double-click is the registry's job, via a
        retry window, not the fingerprint's — a time-bucketed hash would let a
        duplicate through whenever the two clicks straddled a bucket boundary.
        """
        if self.request_id:
            seed = f"req:{self.request_id}|{self.account_id}|{self.leg}"
        else:
            seed = "|".join((
                self.account_id, self.broker, self.underlying, self.expiry,
                f"{float(self.strike):.2f}", self.opt_type, self.side,
                str(int(self.qty)), self.order_type, f"{float(self.price):.2f}",
                self.product, self.validity, str(self.leg),
            ))
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def client_order_id(fingerprint: str, attempt: int = 0) -> str:
    """A broker-safe client order id for a fingerprint.

    `attempt` distinguishes deliberate later orders with identical parameters:
    once a claim has aged out of the retry window it is retired and the next
    identical order gets attempt+1, so "buy 1 lot again, ten minutes later"
    is a new order rather than a permanently blocked one.
    """
    return f"{COID_PREFIX}{fingerprint[:_DIGEST_CHARS]}{attempt % 100:02d}"


@dataclass(frozen=True)
class BrokerIdem:
    """One broker's participation in the framework.

    accepts_client_id — whether the broker takes a client id on placement at all.
                 Declarative: the SDK keyword itself belongs in the placer, with
                 the rest of that broker's vocabulary, so every placer receives
                 the same `client_order_id=` argument and a broker that cannot
                 use it simply ignores it.
    tag_keys   — order-book keys that echo it back, most likely first.
    rows       — (session) -> raw order-book rows. Deliberately raw rather than
                 the sync engine's normalized BrokerOrder: that mapper DROPS a
                 row whose status word it does not recognise, and a dropped row
                 would read as "order absent" and authorise a duplicate. Here an
                 unrecognised order must still count as an order.
    lookup     — (session, coid) -> broker order id | None, for brokers with a
                 real by-client-id endpoint. Raise to signal "could not tell";
                 return None only when the broker positively has no such order.
    id_keys    — order-book keys holding the broker's own order id.
    match_keys — order-book keys used on the attribute tier: symbol, side, qty.
    """
    broker: str
    tier: Tier
    rows: Callable[[Any], list[dict]]
    id_keys: tuple[str, ...]
    accepts_client_id: bool = False
    tag_keys: tuple[str, ...] = ()
    lookup: Callable[[Any, str], str | None] | None = None
    symbol_keys: tuple[str, ...] = ()
    side_keys: tuple[str, ...] = ()
    qty_keys: tuple[str, ...] = ()
    max_len: int = 25


_ADAPTERS: dict[str, BrokerIdem] = {}


def register(adapter: BrokerIdem) -> None:
    _ADAPTERS[adapter.broker.lower()] = adapter


def adapter_for(broker: str) -> BrokerIdem | None:
    return _ADAPTERS.get((broker or "").lower())


def registered_brokers() -> list[str]:
    return sorted(_ADAPTERS)
