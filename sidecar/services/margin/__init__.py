"""Broker-independent pre-trade margin validation.

    Order Engine
          |
          v
    MarginEngine  (engine.py)  <- the only thing the Order Engine imports
          |
     +----+------+--------+--------+
     |           |        |        |
   angel.py   dhan.py  kotak.py  icici.py

Importing this package registers every broker checker. A new broker is a new
module here plus `register("<broker>", check)` — no change to order routing.
"""
from __future__ import annotations

from .base import (
    MarginChecker,
    MarginQuote,
    MarginRequest,
    MarginUnavailable,
    checker_for,
    register,
    registered_brokers,
)
from .engine import MarginEngine, MarginRejection, margin_engine

# Import for side effect: each module registers its checker on import. Listed
# explicitly (rather than discovered) so an accidental deletion is a visible
# import error, not a broker that silently stops being margin-checked.
from . import angel, dhan, firstock, kotak, icici  # noqa: E402,F401  (registration)

__all__ = [
    "MarginChecker", "MarginQuote", "MarginRequest", "MarginUnavailable",
    "MarginEngine", "MarginRejection", "margin_engine",
    "checker_for", "register", "registered_brokers",
]
