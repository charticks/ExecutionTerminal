"""Dhan HQ margin checker."""
from __future__ import annotations

from typing import Any

from .base import MarginQuote, MarginRequest, MarginUnavailable, pluck, register

_PRODUCT = {"NRML": "CARRYFORWARD", "MIS": "INTRADAY"}
_EXCHANGE = {"NFO": "NSE_FNO", "BFO": "BSE_FNO"}


def _available(session: Any) -> float:
    try:
        response = session.get_fund_limits()
    except Exception as exc:
        raise MarginUnavailable(f"Dhan get_fund_limits() failed: {exc}") from exc
    # "availabelBalance" is Dhan's own spelling in the v2 response; both are
    # accepted so a corrected SDK does not silently stop matching.
    cash = pluck(response, "availabelBalance", "availableBalance",
                 "withdrawableBalance", "sodLimit")
    if cash is None:
        raise MarginUnavailable("Dhan get_fund_limits() returned no "
                                "recognisable balance")
    return cash


def _required(session: Any, req: MarginRequest) -> tuple[float, bool, str]:
    try:
        response = session.margin_calculator(
            security_id=req.token,
            exchange_segment=_EXCHANGE.get(req.exchange, "NSE_FNO"),
            transaction_type=req.side,
            quantity=int(req.qty),
            product_type=_PRODUCT.get(req.product, "CARRYFORWARD"),
            price=req.reference_price,
        )
        quoted = pluck(response, "totalMargin", "total_margin",
                       "insufficientBalance", "spanMargin")
        if quoted is not None and quoted > 0:
            return quoted, False, "dhan:margin_calculator"
        reason = "Dhan margin calculator returned no requirement"
    except Exception as exc:
        reason = f"Dhan margin calculator failed: {exc}"

    debit = req.premium_debit()
    if debit is not None:
        return debit, True, "dhan:premium-debit"
    raise MarginUnavailable(reason)


def check(session: Any, req: MarginRequest) -> MarginQuote:
    available = _available(session)
    required, estimated, source = _required(session, req)
    return MarginQuote(required=required, available=available, source=source,
                       estimated_requirement=estimated)


register("dhan", check)
