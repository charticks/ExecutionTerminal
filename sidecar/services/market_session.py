"""Centralised market-session validation.

Single source of truth for "is trading allowed right now". Every trading action
— place / modify / cancel / square-off / partial exit / roll / adjust — asks this
module instead of doing its own clock arithmetic (the legacy Tkinter app scattered
`dt.time(9, 15) <= now <= dt.time(15, 30)` checks across several files).

The renderer mirrors this rule in charticks/src/lib/marketSession.ts so it can show
the dialog without a round trip, but THIS module is authoritative: a mis-routed or
stale client can never push an order through outside market hours.

Session: 09:15–15:30 IST, Monday–Friday for the equity segment. Instruments with
their own hours (MCX commodities) are listed in _SESSIONS and gated on those
instead — pass the underlying to is_market_open / require_open to get them. No
exchange holiday calendar yet — a holiday still resolves to "open" here and the
broker rejects the order.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Session window (module constants so they can be overridden for testing).
OPEN_TIME = dt.time(9, 15)
CLOSE_TIME = dt.time(15, 30)

MARKET_CLOSED_CODE = "MARKET_CLOSED"
MARKET_CLOSED_MESSAGE = (
    "Trading is currently unavailable because the market is closed. "
    "Please place orders during market hours."
)


def now_ist() -> dt.datetime:
    """Current IST time — independent of the host machine's timezone."""
    return dt.datetime.now(IST)


# MCX commodities run far later than the equity segment (09:00–23:30 IST), so
# gating Crude on the equity window would freeze its chain and block closing a
# Crude position all evening. Anything not listed here uses the equity window,
# which keeps every existing index on exactly the behaviour it had before.
_SESSIONS = {"CRUDEOIL": (dt.time(9, 0), dt.time(23, 30))}


def session_window(symbol: str | None = None) -> tuple[dt.time, dt.time]:
    """(open, close) IST times for an instrument. Defaults to the equity window."""
    return _SESSIONS.get((symbol or "").upper(), (OPEN_TIME, CLOSE_TIME))


def is_market_open(now: dt.datetime | None = None, symbol: str | None = None) -> bool:
    """True when the session for `symbol` is live (Mon–Fri). `symbol` defaults to
    the equity-derivatives session, 09:15–15:30 IST."""
    now = now or now_ist()
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)
    if now.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        return False
    open_time, close_time = session_window(symbol)
    return open_time <= now.time() <= close_time


def market_closed_error() -> dict:
    """The standard error payload returned to the renderer when trading is blocked."""
    return {"ok": False, "code": MARKET_CLOSED_CODE, "error": MARKET_CLOSED_MESSAGE}


def require_open_any(symbols: list[str]) -> dict | None:
    """Guard for actions spanning several instruments (square-off-all): allowed
    while ANY of them is in session, so an open MCX position is still closeable
    after the equity close. An empty list falls back to the equity window."""
    if not symbols:
        return require_open()
    return None if any(is_market_open(symbol=s) for s in symbols) else market_closed_error()


def require_open(symbol: str | None = None) -> dict | None:
    """Guard helper: `if (err := require_open()): return err` at the top of any
    trading action. Returns None when trading is allowed. Pass the underlying so
    instruments with their own session (MCX) are gated on their real hours."""
    return None if is_market_open(symbol=symbol) else market_closed_error()
