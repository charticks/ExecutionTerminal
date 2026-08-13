"""ICICI Direct (Breeze) margin checker."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import (MarginQuote, MarginRequest, MarginUnavailable, pluck,
                   pluck_field, register)

_EXCHANGE = {"BFO": "BFO"}


def _exchange_code(req: MarginRequest) -> str:
    return _EXCHANGE.get(req.exchange, "NFO")


def _available(session: Any) -> tuple[float, str]:
    try:
        response = session.get_funds()
    except Exception as exc:
        raise MarginUnavailable(f"ICICI get_funds() failed: {exc}") from exc
    # F&O allocation first: an options order draws on that, and allocated_equity
    # can be non-zero while nothing is allocated to derivatives.
    cash, field = pluck_field(response, "allocated_fno", "allocated_equity",
                              "unallocated_balance", "total_bank_balance")
    if cash is None:
        raise MarginUnavailable("ICICI get_funds() returned no recognisable "
                                "balance")
    return cash, f"icici:getFunds.{field}"


def _required(session: Any, req: MarginRequest, stock_code: str) -> tuple[float, bool, str]:
    try:
        expiry_iso = datetime.strptime(req.expiry, "%d%b%Y").strftime(
            "%Y-%m-%dT06:00:00.000Z")
    except ValueError as exc:
        raise MarginUnavailable(f"ICICI: unparseable expiry '{req.expiry}'") from exc
    try:
        response = session.margin_calculator([{
            "strike_price": str(int(req.strike)),
            "quantity": str(int(req.qty)),
            "right": "call" if req.opt_type == "CE" else "put",
            "product": "options",
            "action": req.side.lower(),
            "price": str(req.reference_price),
            "expiry_date": expiry_iso,
            "stock_code": stock_code,
            "cover_order_flow": "N",
            "fresh_order_type": "N",
            "cover_limit_rate": "0",
            "cover_sltp_price": "0",
            "fresh_limit_rate": "0",
            "open_quantity": "0",
        }], _exchange_code(req))
        quoted = pluck(response, "span_margin_required", "total_requirement",
                       "non_span_margin_required", "total_margin")
        if quoted is not None and quoted > 0:
            return quoted, False, "icici:margin_calculator"
        reason = "ICICI margin calculator returned no requirement"
    except Exception as exc:
        reason = f"ICICI margin calculator failed: {exc}"

    debit = req.premium_debit()
    if debit is not None:
        return debit, True, "icici:premium-debit"
    raise MarginUnavailable(reason)


def check(session: Any, req: MarginRequest) -> MarginQuote:
    # Breeze addresses a contract by stock_code, not token, and the code comes
    # from the ICICI scrip master the feed loaded — the same resolution the
    # order placer uses. Without it there is nothing to price.
    from services.broker_manager import manager

    stock_code = ""
    for account_id, broker, sess in manager.execution_sessions():
        if broker == "icici" and sess is session:
            feed = manager.router.feed_for(account_id)
            stock_code = (feed.scrip.stock_code_for(req.underlying)
                          if feed is not None else "") or ""
            break
    if not stock_code:
        raise MarginUnavailable(
            f"ICICI stock code for {req.underlying} is unknown (scrip master "
            f"not loaded) — cannot price the margin requirement")

    available, available_source = _available(session)
    required, estimated, source = _required(session, req, stock_code)
    return MarginQuote(required=required, available=available, source=source,
                       estimated_requirement=estimated,
                       available_source=available_source)


register("icici", check)
