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
instead — pass the underlying to is_market_open / require_open to get them.

Trading holidays come from ``config/market_holidays.json``. Before that file
existed a holiday resolved to "open": the order was composed, validated, routed
and rejected by the broker, and the user was shown whatever opaque SDK error came
back instead of "the market is closed today". The calendar is data, not code, so
next year's list is a file edit rather than a release — and a missing or
unreadable file degrades to the old behaviour rather than blocking trading,
because refusing to trade on the strength of a file we could not read would be a
far worse failure than the one it prevents.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

_HOLIDAY_PATH = Path(__file__).resolve().parent.parent / "config" / "market_holidays.json"

# Session window (module constants so they can be overridden for testing).
OPEN_TIME = dt.time(9, 15)
CLOSE_TIME = dt.time(15, 30)

MARKET_CLOSED_CODE = "MARKET_CLOSED"
MARKET_CLOSED_MESSAGE = (
    "Trading is currently unavailable because the market is closed. "
    "Please place orders during market hours."
)


# ── holiday calendar ───────────────────────────────────────────────────────
# Segment -> {ISO date: description}. "equity" covers NSE/BSE derivatives;
# "commodity" covers MCX, which trades on some equity holidays (and in an
# evening-only session on others, which this deliberately does NOT model — a
# half-open day is treated as open and the broker remains the final authority
# on the exact minute).
_holiday_lock = threading.Lock()
_holidays: dict[str, dict[str, str]] | None = None

# Which calendar an underlying is gated on.
_SEGMENT = {"CRUDEOIL": "commodity"}


def _load_holidays() -> dict[str, dict[str, str]]:
    global _holidays
    with _holiday_lock:
        if _holidays is not None:
            return _holidays
        loaded: dict[str, dict[str, str]] = {}
        try:
            raw = json.loads(_HOLIDAY_PATH.read_text(encoding="utf-8"))
            for segment, days in (raw or {}).items():
                if segment.startswith("_") or not isinstance(days, dict):
                    continue
                loaded[segment] = {str(k): str(v) for k, v in days.items()}
        except Exception:
            # No calendar, or an unreadable one. Fall back to "every weekday is a
            # trading day" — the behaviour before this existed. Never fail closed
            # here: that would stop trading on a perfectly ordinary day.
            loaded = {}
        _holidays = loaded
        return loaded


def reload_holidays() -> int:
    """Re-read the calendar from disk. Returns the number of dates loaded."""
    global _holidays
    with _holiday_lock:
        _holidays = None
    calendar = _load_holidays()
    return sum(len(v) for v in calendar.values())


def holiday_for(day: dt.date, symbol: str | None = None) -> str | None:
    """The holiday's name when `day` is one for this instrument's segment."""
    segment = _SEGMENT.get((symbol or "").upper(), "equity")
    return _load_holidays().get(segment, {}).get(day.isoformat())


def holiday_error(name: str) -> dict:
    return {"ok": False, "code": MARKET_CLOSED_CODE,
            "error": f"The market is closed today — {name}. "
                     f"No orders can be placed or modified.",
            "holiday": name}


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
    if holiday_for(now.date(), symbol) is not None:
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
    instruments with their own session (MCX) are gated on their real hours.

    A holiday gets its own message naming the holiday. "The market is closed"
    on a Tuesday morning reads as a bug in the app; "closed today — Republic
    Day" does not, and it is the difference between the user retrying for an
    hour and the user going away.
    """
    if is_market_open(symbol=symbol):
        return None
    name = holiday_for(now_ist().date(), symbol)
    return holiday_error(name) if name else market_closed_error()
