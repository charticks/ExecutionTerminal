"""Per-broker order-book readers.

One function per broker, each returning normalized `BrokerOrder` rows. They are
short and structurally identical by design: the differences between brokers are
field names and response wrapping, and everything else — transitions, fills,
position updates, logging — lives in the engine.

Field-name candidates come from the SDK responses the legacy Tkinter app parsed
(`legacy/app/order_manager.py:poll_order_status`), widened so a renamed field in
a later SDK build degrades to "unrecognised" (which holds the last known state)
rather than to a wrong state.
"""
from __future__ import annotations

from typing import Any

from .base import (
    BrokerOrder,
    as_float,
    as_int,
    first,
    map_status,
    register,
    rows_of,
)


def _order(row: dict, id_keys: tuple[str, ...], status_keys: tuple[str, ...],
           filled_keys: tuple[str, ...], qty_keys: tuple[str, ...],
           price_keys: tuple[str, ...], reason_keys: tuple[str, ...]) -> BrokerOrder | None:
    order_id = first(row, *id_keys)
    if not order_id:
        return None
    raw = str(first(row, *status_keys) or "")
    filled = as_int(first(row, *filled_keys))
    total = as_int(first(row, *qty_keys))
    status = map_status(raw, filled, total)
    if status is None:
        return None  # unknown vocabulary — engine keeps the state it has
    return BrokerOrder(
        order_id=str(order_id),
        status=status,
        filled_qty=filled,
        avg_price=as_float(first(row, *price_keys)),
        raw_status=raw,
        reason=str(first(row, *reason_keys) or ""),
    )


def angel_orders(session: Any) -> list[BrokerOrder]:
    rows = rows_of(session.orderBook())
    out = []
    for row in rows:
        order = _order(
            row,
            id_keys=("orderid", "order_id"),
            status_keys=("orderstatus", "status"),
            filled_keys=("filledshares", "filledquantity"),
            qty_keys=("quantity", "orderquantity"),
            price_keys=("averageprice", "avgprice", "price"),
            reason_keys=("text", "rejectionreason", "message"),
        )
        if order:
            out.append(order)
    return out


def dhan_orders(session: Any) -> list[BrokerOrder]:
    rows = rows_of(session.get_order_list())
    out = []
    for row in rows:
        order = _order(
            row,
            id_keys=("orderId", "order_id"),
            status_keys=("orderStatus", "status"),
            filled_keys=("filledQty", "filled_qty", "tradedQuantity"),
            qty_keys=("quantity", "orderQuantity"),
            price_keys=("averageTradedPrice", "avgPrice", "price"),
            reason_keys=("omsErrorDescription", "reason", "message"),
        )
        if order:
            out.append(order)
    return out


def kotak_orders(session: Any) -> list[BrokerOrder]:
    rows = rows_of(session.order_report())
    out = []
    for row in rows:
        order = _order(
            row,
            id_keys=("nOrdNo", "orderId", "ordNo"),
            status_keys=("ordSt", "status", "stat"),
            filled_keys=("fldQty", "filledQty", "fldQuantity"),
            qty_keys=("qty", "quantity"),
            price_keys=("avgPrc", "avgPrice", "prc"),
            reason_keys=("rejRsn", "rejectionReason", "errMsg"),
        )
        if order:
            out.append(order)
    return out


def icici_orders(session: Any) -> list[BrokerOrder]:
    # Breeze wants an explicit exchange + date window; the order list is per
    # exchange, so both option venues are read and concatenated.
    from datetime import datetime, timedelta

    today = datetime.now()
    frm = (today - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00.000Z")
    to = today.strftime("%Y-%m-%dT23:59:59.000Z")
    out = []
    for exchange in ("NFO", "BFO"):
        try:
            response = session.get_order_list(exchange_code=exchange,
                                              from_date=frm, to_date=to)
        except Exception:
            # One dead venue must not hide the other's orders.
            continue
        for row in rows_of(response):
            order = _order(
                row,
                id_keys=("order_id", "orderId"),
                status_keys=("status", "order_status"),
                filled_keys=("quantity_filled", "executed_quantity", "filled_quantity"),
                qty_keys=("quantity",),
                price_keys=("average_price", "avg_price", "price"),
                reason_keys=("message", "reason", "Error"),
            )
            if order:
                out.append(order)
    return out


register("angel", angel_orders)
register("dhan", dhan_orders)
register("kotak", kotak_orders)
register("icici", icici_orders)
