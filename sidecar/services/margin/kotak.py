"""Kotak Neo margin checker."""
from __future__ import annotations

from typing import Any

from .base import MarginQuote, MarginRequest, MarginUnavailable, pluck, register

_PRODUCT = {"NRML": "NRML", "MIS": "MIS"}
_SEGMENT = {"NFO": "nse_fo", "BFO": "bse_fo"}


def _available(session: Any) -> float:
    try:
        response = session.limits(segment="ALL", exchange="ALL", product="ALL")
    except TypeError:
        # Older neo-api-client builds take no keyword arguments.
        try:
            response = session.limits()
        except Exception as exc:
            raise MarginUnavailable(f"Kotak limits() failed: {exc}") from exc
    except Exception as exc:
        raise MarginUnavailable(f"Kotak limits() failed: {exc}") from exc
    cash = pluck(response, "Net", "net", "MarginAvailable", "marginAvailable",
                 "CollateralValue", "AdhocMargin")
    if cash is None:
        raise MarginUnavailable("Kotak limits() returned no recognisable "
                                "margin balance")
    return cash


def _required(session: Any, req: MarginRequest) -> tuple[float, bool, str]:
    try:
        response = session.margin_required(
            exchange_segment=_SEGMENT.get(req.exchange, "nse_fo"),
            price=str(req.reference_price),
            order_type="L" if req.order_type == "LIMIT" else "MKT",
            product=_PRODUCT.get(req.product, "NRML"),
            quantity=str(int(req.qty)),
            instrument_token=req.token,
            transaction_type="B" if req.side == "BUY" else "S",
        )
        quoted = pluck(response, "totalMarginRequired", "margin", "marginUsed",
                       "insufficientMargin", "totMrgn")
        if quoted is not None and quoted > 0:
            return quoted, False, "kotak:margin_required"
        reason = "Kotak margin_required() returned no requirement"
    except Exception as exc:
        reason = f"Kotak margin_required() failed: {exc}"

    debit = req.premium_debit()
    if debit is not None:
        return debit, True, "kotak:premium-debit"
    raise MarginUnavailable(reason)


def check(session: Any, req: MarginRequest) -> MarginQuote:
    available = _available(session)
    required, estimated, source = _required(session, req)
    return MarginQuote(required=required, available=available, source=source,
                       estimated_requirement=estimated)


register("kotak", check)
