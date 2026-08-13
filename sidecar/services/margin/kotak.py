"""Kotak Neo margin checker."""
from __future__ import annotations

from typing import Any

from .base import (MarginQuote, MarginRequest, MarginUnavailable, pluck,
                   pluck_field, register, scrip_of)

_PRODUCT = {"NRML": "NRML", "MIS": "MIS"}


def _segment(req: MarginRequest) -> str:
    """Kotak's exchange segment for this contract.

    Derived from the underlying via the scrip master's own map rather than from
    `req.exchange`, which is Angel's vocabulary (NFO/BFO/MCX): the old
    {"NFO","BFO"} lookup silently defaulted MCX to `nse_fo`, so every commodity
    margin check asked the wrong segment and fell through to the estimate.
    """
    from services.feeds.kotak_scrip import OPT_SEGMENT
    return OPT_SEGMENT.get(req.underlying, "nse_fo")


def _token(session: Any, req: MarginRequest) -> str:
    """Kotak's numeric instrument token for this contract.

    `req.token` cannot be used: it comes from Angel's instrument master (see
    OrderManager._margin_request) and means nothing to Kotak.
    """
    from services.instruments import InstrumentKey

    key = InstrumentKey.option(req.underlying, req.expiry, req.strike, req.opt_type)
    scrip = scrip_of("kotak", session)
    return scrip.token_for(key) if scrip is not None else ""


def _available(session: Any) -> tuple[float, str]:
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
    cash, field = pluck_field(response, "Net", "net", "MarginAvailable",
                              "marginAvailable", "CollateralValue", "AdhocMargin")
    if cash is None:
        raise MarginUnavailable("Kotak limits() returned no recognisable "
                                "margin balance")
    return cash, f"kotak:limits.{field}"


def _required(session: Any, req: MarginRequest) -> tuple[float, bool, str]:
    token = _token(session, req)
    try:
        if not token:
            raise MarginUnavailable("Kotak instrument token unknown (scrip master "
                                    "not loaded)")
        response = session.margin_required(
            exchange_segment=_segment(req),
            price=str(req.reference_price),
            order_type="L" if req.order_type == "LIMIT" else "MKT",
            product=_PRODUCT.get(req.product, "NRML"),
            quantity=str(int(req.qty)),
            instrument_token=token,
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
    available, available_source = _available(session)
    required, estimated, source = _required(session, req)
    return MarginQuote(required=required, available=available, source=source,
                       estimated_requirement=estimated,
                       available_source=available_source)


register("kotak", check)
