"""Order idempotency: one live order per intent, whatever the network does.

    Order Engine -> guard.claim() -> BrokerIdem (per broker) -> broker order book
                                  -> ClaimStore (durable journal)

The Order Engine calls `guard.claim()` before submitting and one of
`guard.placed / failed / unresolved` after. It never learns which broker supports
what: a broker joins the framework by registering a `BrokerIdem` in brokers.py,
and one that has not is refused reconciliation rather than silently trusted.

Read guard.py for the decision logic and store.py for why the journal is on disk.
"""
from .base import (BrokerIdem, Placement, Resolution, Tier, adapter_for,
                   client_order_id, register, registered_brokers)
from .guard import ATTRIBUTE_WINDOW_S, RETRY_WINDOW_S, Decision, guard
from .store import CLAIMED, FAILED, PLACED, Claim, store

# Registers every adapter. Imported for the side effect, exactly as the margin
# and order-sync packages do it, so `import services.idempotency` is enough.
from . import brokers  # noqa: E402,F401  (side-effecting registration)

__all__ = [
    "ATTRIBUTE_WINDOW_S", "BrokerIdem", "CLAIMED", "Claim", "Decision", "FAILED",
    "PLACED", "Placement", "RETRY_WINDOW_S", "Resolution", "Tier", "adapter_for",
    "client_order_id", "guard", "register", "registered_brokers", "store",
]
