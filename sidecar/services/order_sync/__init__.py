"""Broker-independent order synchronization.

Importing this package registers every broker's order-book reader. A broker that
gains a push feed replaces its reader with one that calls
``order_sync.ingest(...)`` — the engine, the position book and the UI are
unaffected.
"""
from __future__ import annotations

from .base import (
    ACCEPTED,
    BrokerOrder,
    CANCELLED,
    EXPIRED,
    FILLED,
    NEW,
    PARTIAL,
    PENDING,
    REJECTED,
    SUBMITTED,
    TERMINAL,
    OrderSource,
    register,
    registered_brokers,
    source_for,
)
from .engine import OrderSyncEngine, TrackedOrder, order_sync

# Import for side effect: registers angel/dhan/kotak/icici readers.
from . import brokers  # noqa: E402,F401  (registration)

__all__ = [
    "BrokerOrder", "OrderSource", "OrderSyncEngine", "TrackedOrder",
    "order_sync", "register", "source_for", "registered_brokers",
    "NEW", "SUBMITTED", "ACCEPTED", "PENDING", "PARTIAL", "FILLED",
    "REJECTED", "CANCELLED", "EXPIRED", "TERMINAL",
]
