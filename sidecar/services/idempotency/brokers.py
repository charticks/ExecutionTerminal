"""Per-broker idempotency adapters — one `BrokerIdem` each.

Short and structurally identical by design, exactly like the order-book readers
next door in services/order_sync/brokers.py: the only per-broker facts are which
call returns the order book, which keyword carries a client id, and which field
echoes it back. Every decision made from those facts lives in guard.py.

The order-book calls are the SAME endpoints the Order Synchronization Engine
polls, so a broker that can be synchronized can be reconciled; adding a broker
to one and not the other is not a state this can get into.
"""
from __future__ import annotations

from typing import Any

from services.order_sync.base import rows_of

from .base import BrokerIdem, Tier, register


def _dhan_lookup(session: Any, coid: str) -> str | None:
    """Dhan's real by-client-id endpoint: GET /orders/external/{correlationId}.

    Returns None only for a definite "no such order". A transport or shape
    failure raises, which the guard reads as "cannot tell" — the distinction is
    the whole value of having a native lookup.
    """
    response = session.get_order_by_correlationID(coid)
    if not isinstance(response, dict):
        raise ValueError(f"unexpected response {response!r}")
    status = str(response.get("status", "")).lower()
    if status and status != "success":
        remarks = response.get("remarks")
        text = ""
        if isinstance(remarks, dict):
            text = str(remarks.get("error_message") or remarks.get("error_code") or "")
        else:
            text = str(remarks or "")
        # Dhan answers "no order with that correlation id" as a not-found error.
        # Anything else is a failure we must not read as absence.
        if "not found" in text.lower() or "no data" in text.lower():
            return None
        raise ValueError(text or f"lookup failed ({response})")
    rows = rows_of(response)
    for row in rows:
        order_id = row.get("orderId") or row.get("order_id")
        if order_id:
            return str(order_id)
    return None


# Dhan — full native support: a client id on the way in, a lookup on the way out.
register(BrokerIdem(
    broker="dhan",
    tier=Tier.NATIVE,
    accepts_client_id=True,                # dhanhq maps tag -> correlationId
    tag_keys=("correlationId", "correlation_id", "tag"),
    lookup=_dhan_lookup,
    rows=lambda s: rows_of(s.get_order_list()),
    id_keys=("orderId", "order_id"),
    symbol_keys=("tradingSymbol", "trading_symbol", "customSymbol"),
    side_keys=("transactionType", "transaction_type"),
    qty_keys=("quantity", "orderQuantity"),
    max_len=25,                            # Dhan documents 25 for correlationId
))

# Kotak Neo — accepts a client id (place_order's `tag`, sent as body field `ig`)
# and echoes it on the order-book row, but has no lookup endpoint.
register(BrokerIdem(
    broker="kotak",
    tier=Tier.TAG_ECHO,
    accepts_client_id=True,
    tag_keys=("GuiOrdId", "guiOrdId", "ig", "tag", "orderTag"),
    rows=lambda s: rows_of(s.order_report()),
    id_keys=("nOrdNo", "orderId", "ordNo"),
    symbol_keys=("trdSym", "tradingSymbol", "sym"),
    side_keys=("trnsTp", "transactionType"),
    qty_keys=("qty", "quantity"),
    max_len=20,
))


def _icici_rows(session: Any) -> list[dict]:
    """Breeze's order list is per-exchange and per-date-window.

    Both option venues are read and concatenated, and a failure on either RAISES
    rather than being swallowed: a half-read book that came back short would look
    like "our order is absent" and authorise a duplicate. The sync engine can
    afford to skip a dead venue; this cannot.
    """
    from datetime import datetime, timedelta

    today = datetime.now()
    frm = (today - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00.000Z")
    to = today.strftime("%Y-%m-%dT23:59:59.000Z")
    rows: list[dict] = []
    for exchange in ("NFO", "BFO"):
        rows.extend(rows_of(session.get_order_list(
            exchange_code=exchange, from_date=frm, to_date=to)))
    return rows


# ICICI Direct — `user_remark` goes out with the order and comes back on the row.
register(BrokerIdem(
    broker="icici",
    tier=Tier.TAG_ECHO,
    accepts_client_id=True,   # Breeze: user_remark
    tag_keys=("user_remark", "userRemark", "remarks"),
    rows=_icici_rows,
    id_keys=("order_id", "orderId"),
    symbol_keys=("stock_code", "tradingSymbol", "symbol"),
    side_keys=("action", "transaction_type"),
    qty_keys=("quantity",),
    max_len=20,
))

# Angel One — SmartAPI's order params carry no client id and its order book has
# no client field, so this is the attribute tier: a resent order is recognised
# by contract, side and quantity. Weaker than the others by the broker's design,
# not by ours, and reported as such so it is never mistaken for a guarantee.
register(BrokerIdem(
    broker="angel",
    tier=Tier.ATTRIBUTE,
    accepts_client_id=False,
    rows=lambda s: rows_of(s.orderBook()),
    id_keys=("orderid", "order_id"),
    symbol_keys=("tradingsymbol", "trading_symbol"),
    side_keys=("transactiontype", "transaction_type"),
    qty_keys=("quantity", "orderquantity"),
))
