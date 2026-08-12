"""Angel One (SmartAPI) margin checker."""
from __future__ import annotations

from typing import Any

from .base import MarginQuote, MarginRequest, MarginUnavailable, pluck, register

_PRODUCT = {"NRML": "CARRYFORWARD", "MIS": "INTRADAY"}


def _available(session: Any) -> float:
    """Free cash from rmsLimit(). No fallback: an unknown balance is never
    assumed, because assuming it is what this whole check exists to prevent."""
    try:
        response = session.rmsLimit()
    except Exception as exc:
        raise MarginUnavailable(f"Angel rmsLimit() failed: {exc}") from exc
    cash = pluck(response, "availablecash", "availableCash", "net",
                 "availableintradaypayin", "availablelimitmargin")
    if cash is None:
        raise MarginUnavailable("Angel rmsLimit() returned no recognisable "
                                "cash balance")
    return cash


def _required(session: Any, req: MarginRequest) -> tuple[float, bool, str]:
    """(requirement, was_estimated, source).

    Prefers Angel's margin calculator. A SHORT option falls back to nothing —
    SPAN + exposure cannot be derived locally — so an unavailable calculator
    rejects the order. A LONG option's requirement is the premium debit, which
    is arithmetic rather than a guess, so it is an acceptable answer.
    """
    params = {
        "positions": [{
            "exchange": req.exchange,
            "qty": int(req.qty),
            "price": req.reference_price,
            "productType": _PRODUCT.get(req.product, "CARRYFORWARD"),
            "token": req.token,
            "tradeType": req.side,
        }]
    }
    try:
        response = session.getMarginApi(params)
        quoted = pluck(response, "totalMarginRequired", "totalmarginrequired",
                       "marginRequired", "totalMargin")
        if quoted is not None and quoted > 0:
            return quoted, False, "angel:getMarginApi"
        reason = "Angel margin API returned no requirement"
    except Exception as exc:
        reason = f"Angel margin API failed: {exc}"

    debit = req.premium_debit()
    if debit is not None:
        return debit, True, "angel:premium-debit"
    raise MarginUnavailable(reason)


def check(session: Any, req: MarginRequest) -> MarginQuote:
    available = _available(session)
    required, estimated, source = _required(session, req)
    return MarginQuote(required=required, available=available, source=source,
                       estimated_requirement=estimated)


register("angel", check)
