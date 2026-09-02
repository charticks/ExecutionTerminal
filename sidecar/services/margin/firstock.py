"""Firstock margin checker.

The only broker of the five that answers both halves of the question in one
call: ``orderMargin`` returns ``marginOnNewOrder`` (what this order needs) and
``availableMargin`` (what the account has) together, so there is no window in
which the two figures were read at different moments.

``limits`` is kept as the fallback for the balance alone, because a margin call
that fails for a contract-shaped reason should still be able to say what the
account holds.
"""
from __future__ import annotations

from typing import Any

from .base import (MarginQuote, MarginRequest, MarginUnavailable, pluck,
                   pluck_field, register, scrip_of)


def _contract(session: Any, req: MarginRequest) -> dict | None:
    """Firstock's own (exchange, tradingSymbol) for the contract, or None.

    `req.token` cannot be used: it is ANGEL's token (see
    OrderManager._margin_request) and means nothing to Firstock. Firstock also
    addresses a contract by trading symbol rather than by token, and its symbol
    has three incompatible encodings across NFO and BFO — so the master is the
    only honest source.
    """
    from services.instruments import InstrumentKey

    key = InstrumentKey.option(req.underlying, req.expiry, req.strike, req.opt_type)
    scrip = scrip_of("firstock", session)
    return scrip.contract_for(key) if scrip is not None else None


def _available_from_limits(session: Any) -> tuple[float, str]:
    try:
        response = session.limits()
    except Exception as exc:
        raise MarginUnavailable(f"Firstock limit() failed: {exc}") from exc
    cash, field = pluck_field(response, "availableMargin", "cash", "totalMargin")
    if cash is None:
        raise MarginUnavailable("Firstock limit() returned no recognisable balance")
    return cash, f"firstock:limit.{field}"


def check(session: Any, req: MarginRequest) -> MarginQuote:
    contract = _contract(session, req)
    if contract is None:
        # Not a margin answer at all. Raising is what rejects the order, which
        # is correct: an unverifiable margin is treated exactly like an
        # insufficient one.
        raise MarginUnavailable(
            f"Firstock does not list {req.symbol} (its instrument master has no "
            f"such contract), so its margin cannot be checked")

    try:
        response = session.order_margin(
            exchange=contract["exchange"],
            trading_symbol=contract["tradingSymbol"],
            side=req.side, qty=int(req.qty), order_type=req.order_type,
            price=req.reference_price, product=req.product)
    except Exception as exc:
        # Fall back to the arithmetic answer for a LONG option, whose cost is
        # simply the premium — that is a figure we can stand behind rather than
        # an estimate. A short's requirement is SPAN plus exposure and genuinely
        # cannot be derived here, so it is refused.
        debit = req.premium_debit()
        if debit is None:
            raise MarginUnavailable(
                f"Firstock orderMargin() failed and the requirement cannot be "
                f"derived for a {req.side}: {exc}") from exc
        available, available_source = _available_from_limits(session)
        return MarginQuote(required=debit, available=available,
                           source="firstock:premium-debit", estimated_requirement=True,
                           available_source=available_source)

    required = pluck(response, "marginOnNewOrder")
    if required is None:
        raise MarginUnavailable("Firstock orderMargin() returned no requirement")

    available, available_source = pluck_field(response, "availableMargin", "cash")
    if available is None:
        # The margin call answered the requirement but not the balance — ask the
        # funds endpoint rather than assuming zero, which would reject every
        # order on a funded account.
        available, available_source = _available_from_limits(session)
    else:
        available_source = f"firstock:orderMargin.{available_source}"

    return MarginQuote(required=required, available=available,
                       source="firstock:orderMargin",
                       available_source=available_source)


register("firstock", check)
