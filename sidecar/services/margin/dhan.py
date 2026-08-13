"""Dhan HQ margin checker."""
from __future__ import annotations

from typing import Any

from .base import (MarginQuote, MarginRequest, MarginUnavailable, pluck,
                   pluck_field, register, scrip_of)

_PRODUCT = {"NRML": "CARRYFORWARD", "MIS": "INTRADAY"}


def _contract(session: Any, req: MarginRequest) -> tuple[str, str]:
    """(securityId, REST exchange segment) as DHAN names them, or ("", "").

    `req.token` cannot be used: it is Angel's token (see
    OrderManager._margin_request) and means nothing to Dhan. And the segment has
    to come from Dhan's own scrip master rather than a {"NFO","BFO"} lookup on
    `req.exchange` — that lookup defaulted MCX to NSE_FNO, so every commodity
    margin check asked about the wrong exchange and fell through to the estimate.
    """
    from services.instruments import InstrumentKey, instruments

    key = InstrumentKey.option(req.underlying, req.expiry, req.strike, req.opt_type)
    security_id = instruments.token_for("dhan", key) or ""
    scrip = scrip_of("dhan", session)
    segment = scrip.rest_segment_for(key) if scrip is not None else ""
    return security_id, segment


def _available(session: Any) -> tuple[float, str]:
    try:
        response = session.get_fund_limits()
    except Exception as exc:
        raise MarginUnavailable(f"Dhan get_fund_limits() failed: {exc}") from exc
    # "availabelBalance" is Dhan's own spelling in the v2 response; both are
    # accepted so a corrected SDK does not silently stop matching.
    cash, field = pluck_field(response, "availabelBalance", "availableBalance",
                              "withdrawableBalance", "sodLimit")
    if cash is None:
        raise MarginUnavailable("Dhan get_fund_limits() returned no "
                                "recognisable balance")
    return cash, f"dhan:fundLimits.{field}"


def _required(session: Any, req: MarginRequest) -> tuple[float, bool, str]:
    security_id, segment = _contract(session, req)
    try:
        if not security_id or not segment:
            raise MarginUnavailable("Dhan security id / exchange segment unknown "
                                    "(scrip master not loaded)")
        response = session.margin_calculator(
            security_id=str(security_id),
            exchange_segment=segment,
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
    available, available_source = _available(session)
    required, estimated, source = _required(session, req)
    return MarginQuote(required=required, available=available, source=source,
                       estimated_requirement=estimated,
                       available_source=available_source)


register("dhan", check)
