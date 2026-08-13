"""Angel One (SmartAPI) margin checker."""
from __future__ import annotations

from typing import Any

from .base import (MarginQuote, MarginRequest, MarginUnavailable, pluck,
                   pluck_field, register)

_PRODUCT = {"NRML": "CARRYFORWARD", "MIS": "INTRADAY"}


def _available(session: Any) -> tuple[float, str]:
    """(free cash, which field it came from) from rmsLimit().

    No fallback: an unknown balance is never assumed, because assuming it is
    what this whole check exists to prevent.

    `availablecash` is asked for first and the order matters — `net` is the
    balance after utilised debits and can read 0 on an account that has cash but
    open positions. Note also that rmsLimit() reports the EQUITY segment: funds
    held only in the commodity ledger do not appear here, which is why the field
    name travels with the number into the log.
    """
    try:
        response = session.rmsLimit()
    except Exception as exc:
        raise MarginUnavailable(f"Angel rmsLimit() failed: {exc}") from exc
    cash, field = pluck_field(response, "availablecash", "availableCash",
                              "availableintradaypayin", "availablelimitmargin",
                              "net")
    if cash is None:
        raise MarginUnavailable("Angel rmsLimit() returned no recognisable "
                                "cash balance")
    return cash, f"angel:rmsLimit.{field}"


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
    available, available_source = _available(session)
    required, estimated, source = _required(session, req)
    return MarginQuote(required=required, available=available, source=source,
                       estimated_requirement=estimated,
                       available_source=available_source)


register("angel", check)
