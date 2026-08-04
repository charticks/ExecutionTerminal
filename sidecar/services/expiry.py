"""Charticks' own expiry-validation layer.

Brokers keep returning yesterday's contracts for a while after the open (the
instrument master is refreshed on their schedule, not ours), so nothing in the
app may trust the master's expiry list directly. Every place that surfaces or
resolves an option contract — the chain, strike selection, order placement,
rolls — funnels through here, so the whole app works off one validated set of
active contracts.

A contract is active until its expiry session ends: it stays tradable all day
on its own expiry date and becomes expired once that day's close passes. The
renderer mirrors this in charticks/src/lib/expiry.ts for the dropdown, but THIS
module is authoritative — a stale client can never resolve an expired token.
"""
from __future__ import annotations

import datetime as dt

from services.market_session import CLOSE_TIME, IST, now_ist

# Formats seen across broker instrument masters (Angel: "23JUL2026").
_FORMATS = ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d", "%d%m%Y")


def parse_expiry(expiry: str) -> dt.date | None:
    """Parse a broker expiry string to a date, or None when unrecognisable."""
    if not expiry:
        return None
    raw = expiry.strip().upper()
    for fmt in _FORMATS:
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def is_expired(expiry: str, now: dt.datetime | None = None) -> bool:
    """True when `expiry` has already passed in IST.

    Unparseable expiries are treated as NOT expired — dropping a contract we
    simply failed to parse would silently hide tradable strikes, which is worse
    than showing one stale row.
    """
    d = parse_expiry(expiry)
    if d is None:
        return False
    now = (now or now_ist())
    now = now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)
    if d < now.date():
        return True
    # Expiry day itself: tradable until the session closes.
    return d == now.date() and now.time() > CLOSE_TIME


def sort_key(expiry: str) -> dt.date:
    """Chronological sort key; unparseable expiries sort last."""
    return parse_expiry(expiry) or dt.date.max


def active_expiries(expiries: list[str], now: dt.datetime | None = None) -> list[str]:
    """Filter to the still-tradable expiries, chronologically ascending."""
    return sorted(
        (e for e in expiries if e and not is_expired(e, now)),
        key=sort_key,
    )
