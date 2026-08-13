"""Pre-trade margin validation, orchestrated across the execution brokers.

Sits between server-side risk validation and broker placement:

    Order Request -> Risk Validation -> MARGIN VALIDATION -> Placement -> Sync

The Order Engine calls ``margin_engine.validate(...)`` and knows nothing about
any broker: each one implements ``MarginChecker`` in its own module (see
services/margin/angel.py and friends) and registers itself. Adding a broker is a
new file plus a register() call.

Two policies, both deliberate:

* **Fail-safe.** A timeout, an API error, an unparseable response or a missing
  checker all reject the order. An unverifiable margin is treated exactly like
  an insufficient one, because "we could not check" and "you cannot afford it"
  have the same correct outcome.
* **All-or-nothing across brokers.** Every enabled execution broker must pass.
  One failure rejects the whole order rather than filling on the brokers that
  happened to pass — a partial fan-out leaves an unbalanced position nobody
  asked for.
"""
from __future__ import annotations

import threading
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any

import diagnostics

from .base import MarginQuote, MarginRequest, MarginUnavailable, checker_for

# A margin call is a network round-trip on the order path, so it is bounded
# tightly: a slow broker must not hold an order open indefinitely, and the
# fail-safe policy means a timeout is a rejection rather than a delay.
TIMEOUT_S = 6.0


@dataclass(frozen=True)
class MarginRejection:
    code: str
    error: str
    details: dict[str, Any]

    def as_response(self) -> dict:
        return {"ok": False, "code": self.code, "error": self.error, **self.details}


class MarginEngine:
    def __init__(self) -> None:
        # One worker per call, created per validation: broker SDKs are not
        # reliably thread-safe across concurrent calls on one session, and the
        # executor exists only to impose a timeout on a blocking call.
        self._timeout = TIMEOUT_S

    def validate(self, sessions: list[tuple[str, str, Any]],
                 req: MarginRequest) -> MarginRejection | None:
        """Check every execution broker. Returns None to proceed."""
        if not sessions:
            # The caller checks this first; belt and braces so this can never
            # silently pass an order with nothing actually validated.
            return MarginRejection(
                "NO_EXECUTION_BROKER",
                "No execution broker to validate margin against.", {})

        for account_id, broker, session in sessions:
            rejection = self._validate_one(account_id, broker, session, req)
            if rejection is not None:
                return rejection
        return None

    def _validate_one(self, account_id: str, broker: str, session: Any,
                      req: MarginRequest) -> MarginRejection | None:
        label = broker.title()
        checker = checker_for(broker)
        if checker is None:
            return self._reject(
                account_id, broker, req, "MARGIN_CHECK_UNAVAILABLE",
                f"Charticks cannot verify margin with {label} yet, so the order "
                f"was not sent. Disable Execute on that broker to trade "
                f"elsewhere.",
                reason=f"no margin checker registered for '{broker}'")

        try:
            quote = self._call_with_timeout(checker, session, req)
        except FutureTimeout:
            return self._reject(
                account_id, broker, req, "MARGIN_CHECK_TIMEOUT",
                f"{label} did not respond to the margin check within "
                f"{self._timeout:.0f}s, so the order was not sent.",
                reason=f"margin check timed out after {self._timeout:.0f}s")
        except MarginUnavailable as exc:
            return self._reject(
                account_id, broker, req, "MARGIN_CHECK_UNAVAILABLE",
                f"Charticks could not verify your {label} margin, so the order "
                f"was not sent. Try again in a moment.",
                reason=str(exc))
        except Exception as exc:
            # A bug in a checker must not become an open door.
            diagnostics.exception("risk", "Margin check crashed", exc_info=exc,
                                  broker=label,
                                  account=diagnostics.mask_account(account_id),
                                  symbol=req.symbol)
            return self._reject(
                account_id, broker, req, "MARGIN_CHECK_UNAVAILABLE",
                f"The {label} margin check failed unexpectedly, so the order "
                f"was not sent. See logs/exceptions.log.",
                reason=f"{type(exc).__name__}: {exc}")

        if not quote.sufficient:
            return self._reject(
                account_id, broker, req, "INSUFFICIENT_MARGIN",
                f"Insufficient margin at {label}: ₹{quote.required:,.0f} needed, "
                f"₹{quote.available:,.0f} available "
                f"(short by ₹{quote.shortfall:,.0f}).",
                reason="required exceeds available", quote=quote)

        diagnostics.event(
            "risk", "Margin validation", "success", broker=label,
            account=account_id, symbol=req.symbol, side=req.side, qty=req.qty,
            requiredMargin=round(quote.required, 2),
            availableMargin=round(quote.available, 2),
            headroom=round(quote.available - quote.required, 2),
            source=quote.source,
            requirement="estimated" if quote.estimated_requirement else "broker")
        return None

    def _call_with_timeout(self, checker, session: Any,
                           req: MarginRequest) -> MarginQuote:
        """Run a blocking SDK call with a hard wall-clock bound.

        A daemon thread rather than a ThreadPoolExecutor: the executor's
        context manager joins its workers on exit, so a hung broker call made
        the timeout meaningless — the check correctly reported a timeout but
        only after the call itself finished (30s against a 1s bound). A daemon
        thread can simply be abandoned, and never delays interpreter shutdown.
        """
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["quote"] = checker(session, req)
            except BaseException as exc:  # re-raised on the calling thread
                box["error"] = exc

        worker = threading.Thread(target=run, daemon=True,
                                  name="margin-check")
        worker.start()
        worker.join(self._timeout)
        if worker.is_alive():
            raise FutureTimeout()
        if "error" in box:
            raise box["error"]
        quote = box.get("quote")
        if not isinstance(quote, MarginQuote):
            raise MarginUnavailable(
                f"margin checker returned {type(quote).__name__}, not a MarginQuote")
        return quote

    def _reject(self, account_id: str, broker: str, req: MarginRequest,
                code: str, message: str, reason: str,
                quote: MarginQuote | None = None) -> MarginRejection:
        details: dict[str, Any] = {"broker": broker}
        if quote is not None:
            details.update({
                "requiredMargin": round(quote.required, 2),
                "availableMargin": round(quote.available, 2),
                "shortfall": quote.shortfall,
            })
        diagnostics.event(
            "risk", "Margin validation", "rejected", broker=broker.title(),
            account=account_id, symbol=req.symbol, side=req.side, qty=req.qty,
            code=code, reason=reason,
            requiredMargin=details.get("requiredMargin"),
            availableMargin=details.get("availableMargin"),
            shortfall=details.get("shortfall"),
            source=quote.source if quote else None,
            availableFrom=quote.available_source if quote else None)
        return MarginRejection(code, message, details)


margin_engine = MarginEngine()
