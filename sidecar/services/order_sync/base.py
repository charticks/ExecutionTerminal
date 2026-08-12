"""Broker-independent order-state contract.

The synchronization engine knows only the canonical lifecycle below. Each broker
maps its own vocabulary onto it in its own module, so the engine, the position
book and the UI never learn a broker's status strings.

Polling is the first implementation, not the contract. A broker with an order
WebSocket (Kotak's `subscribe_to_orderfeed`, for example) implements the same
`OrderSource` and pushes snapshots instead of being polled — nothing above this
layer changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

# ── canonical lifecycle ────────────────────────────────────────────────────
NEW = "NEW"                # created by Charticks, not yet sent
SUBMITTED = "SUBMITTED"    # sent, broker returned an id
ACCEPTED = "ACCEPTED"      # broker acknowledged / validated it
PENDING = "PENDING"        # live at the exchange, unfilled (resting limit)
PARTIAL = "PARTIAL"        # partially filled
FILLED = "FILLED"
REJECTED = "REJECTED"
CANCELLED = "CANCELLED"
EXPIRED = "EXPIRED"

TERMINAL = frozenset({FILLED, REJECTED, CANCELLED, EXPIRED})

# Monotonic ordering, so a poll that arrives out of order can never walk a
# FILLED order back to PENDING. Terminal states share the top rank; a terminal
# state is never left.
_RANK = {
    NEW: 0, SUBMITTED: 1, ACCEPTED: 2, PENDING: 3, PARTIAL: 4,
    FILLED: 5, REJECTED: 5, CANCELLED: 5, EXPIRED: 5,
}


def advances(current: str, incoming: str) -> bool:
    """True when `incoming` is a legitimate forward transition.

    Broker order books are eventually consistent and can return a stale row
    after a fresher one; without this a filled order could flicker back to
    pending and re-emit fills.
    """
    if current == incoming:
        return False
    if current in TERMINAL:
        return False
    return _RANK.get(incoming, -1) >= _RANK.get(current, 0)


@dataclass(frozen=True)
class BrokerOrder:
    """One row of a broker's order book, normalized.

    `raw_status` is kept verbatim for the log: when a mapping turns out to be
    wrong, the only way to see it after the fact is the broker's own word.
    """
    order_id: str
    status: str                 # canonical, from the mapper below
    filled_qty: int = 0
    avg_price: float = 0.0
    raw_status: str = ""
    reason: str = ""            # broker's rejection text, when any


# (session) -> rows of that account's order book. Raising is fine; the engine
# logs it and retries on the next cycle.
OrderSource = Callable[[Any], list[BrokerOrder]]

_SOURCES: dict[str, OrderSource] = {}


def register(broker: str, source: OrderSource) -> None:
    _SOURCES[broker.lower()] = source


def source_for(broker: str) -> OrderSource | None:
    return _SOURCES.get((broker or "").lower())


def registered_brokers() -> list[str]:
    return sorted(_SOURCES)


# ── shared mapping helpers ────────────────────────────────────────────────
# Every broker spells these differently and inconsistently between endpoints.
# Substring matching against the lowercased status is more durable than exact
# tables, and unknown values map to None so the engine holds the last known
# state rather than inventing one.

_FILLED_WORDS = ("complete", "traded", "filled", "executed")
_REJECTED_WORDS = ("reject", "failed", "invalid")
_CANCELLED_WORDS = ("cancel",)
_EXPIRED_WORDS = ("expire", "lapsed")
_PARTIAL_WORDS = ("partial",)
_PENDING_WORDS = ("open", "pending", "trigger pending", "put order req received",
                  "modify", "validation pending")
_ACCEPTED_WORDS = ("accept", "confirm", "ack")


def map_status(raw: str, filled_qty: int = 0, total_qty: int = 0) -> str | None:
    """Canonical status for a broker's own word, or None when unrecognised.

    Quantities are consulted because several brokers report a part-filled order
    as plain "open": a row with some quantity filled is PARTIAL regardless of
    what it calls itself.
    """
    text = (raw or "").strip().lower()
    if not text:
        return None
    if any(w in text for w in _REJECTED_WORDS):
        return REJECTED
    if any(w in text for w in _CANCELLED_WORDS):
        return CANCELLED
    if any(w in text for w in _EXPIRED_WORDS):
        return EXPIRED
    if any(w in text for w in _PARTIAL_WORDS):
        return PARTIAL
    if any(w in text for w in _FILLED_WORDS):
        # Trust the quantities over the word when they disagree — a "complete"
        # row that is short of the requested size is a partial fill.
        if total_qty and 0 < filled_qty < total_qty:
            return PARTIAL
        return FILLED
    if 0 < filled_qty < (total_qty or filled_qty + 1):
        return PARTIAL
    if any(w in text for w in _PENDING_WORDS):
        return PENDING
    if any(w in text for w in _ACCEPTED_WORDS):
        return ACCEPTED
    return None


def as_int(value: Any) -> int:
    try:
        return int(float(str(value).replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        return 0


def as_float(value: Any) -> float:
    try:
        return float(str(value).replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def rows_of(response: Any) -> list[dict]:
    """The list of order rows inside a broker response, whatever it wrapped
    them in ("data", "Success", a bare list, …)."""
    if isinstance(response, list):
        return [r for r in response if isinstance(r, dict)]
    if not isinstance(response, dict):
        return []
    for key in ("data", "Success", "success", "orders", "OrderBookDetail"):
        value = response.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
        if isinstance(value, dict):
            return [value]
    return []


def first(row: dict, *keys: str) -> Any:
    """First present, non-empty value among `keys`, case-insensitively."""
    lowered = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, "", []):
            return value
    return None
