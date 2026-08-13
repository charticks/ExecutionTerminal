"""The decision: may this order be sent, and under what client id?

One call before the SDK, one call after. Everything broker-specific is behind a
`BrokerIdem` (see base.py), so the Order Engine holds no broker knowledge and a
new broker joins by registering an adapter.

    decision = guard.claim(placement)
    if decision.duplicate:  -> adopt decision.order_id, send NOTHING
    if decision.blocked:    -> refuse, decision.error says why
    ...place with decision.tag_kwargs()...
    guard.placed(decision, order_id)  |  guard.failed(decision, reason)
                                      |  guard.unresolved(decision, reason)

What each hazard maps to
------------------------
* **Double-click / duplicate request in flight** — same fingerprint, existing
  claim inside the retry window. Blocked without touching the broker.
* **Network timeout** — the caller reports `unresolved`. The claim stays CLAIMED,
  and the NEXT attempt at that order reconciles against the broker before
  anything is sent.
* **Application restart** — the claim is on disk (see store.py), so the next
  attempt takes exactly the same reconciliation path as an in-session retry.
* **Transient failure with an explicit rejection** — `failed`. A rejection is a
  final answer, so a retry is legitimate and is allowed.

The asymmetry that makes this safe
----------------------------------
A PLACED claim only blocks inside the retry window: past it, an identical order
is a new order the user can see in their book and legitimately wants again. An
UNRESOLVED claim blocks regardless of age until it is reconciled, because
"probably didn't happen" is not a basis for sending a live order.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import diagnostics
from services.broker_manager import LABEL

from .base import (BrokerIdem, Placement, Resolution, Tier, adapter_for,
                   client_order_id)
from .store import CLAIMED, FAILED, PLACED, Claim, store

# Prefix for naming the already-placed order in a duplicate message. A broker
# order id reads as noise without it.
LABEL_ORDER = "order "

# How long an identical, successfully-placed order is treated as a duplicate.
# Long enough to cover a double-click, a slow SDK call and a user retrying by
# hand; short enough that deliberately buying the same contract twice in a
# session is not obstructed.
RETRY_WINDOW_S = 120.0

# How far back an attribute-tier match will look in the broker's order book.
# Wider than the retry window because the sequence being caught is "sent, lost
# the answer, restarted, tried again", which can span minutes.
ATTRIBUTE_WINDOW_S = 900.0


@dataclass
class Decision:
    placement: Placement
    coid: str = ""
    claim: Claim | None = None
    duplicate: bool = False        # already at the broker — adopt order_id
    blocked: bool = False          # cannot establish safety — do not send
    order_id: str = ""
    error: str = ""
    tier: Tier | None = None
    detail: str = ""
    adapter: BrokerIdem | None = None
    # Set on a block so the caller can present it and, if the user insists,
    # repeat the request with the override. `overridable` is what separates "you
    # already placed this, did you mean to place another?" — a judgement only the
    # user can make — from a refusal no confirmation should be able to clear.
    code: str = ""
    overridable: bool = False
    duplicate_of: str = ""         # broker order id of the order already placed
    age_s: float = 0.0             # how long ago that order went in

    @property
    def may_send(self) -> bool:
        return not (self.duplicate or self.blocked)

    def as_response(self) -> dict:
        """The block, in the shape the UI needs to explain it and offer a retry."""
        return {
            "ok": False, "broker": self.placement.broker, "code": self.code,
            "error": self.error, "clientOrderId": self.coid,
            "overridable": self.overridable, "duplicateOf": self.duplicate_of,
            "placedSecondsAgo": round(self.age_s) if self.age_s else None,
            "symbol": self.placement.symbol, "side": self.placement.side,
            "qty": self.placement.qty,
            "idempotencyTier": self.tier.value if self.tier else None,
        }


class IdempotencyGuard:
    def __init__(self) -> None:
        # Serialises claim decisions. Two clicks arriving on two request threads
        # is the common case, and check-then-write without this is exactly the
        # race the feature exists to close.
        self._lock = threading.RLock()

    def claim(self, placement: Placement, override: bool = False) -> Decision:
        """Decide whether this order may be sent.

        `override` is the user's explicit "yes, place it anyway", passed only
        after they were shown one of the overridable blocks below. Never
        inferred, and scoped to the single request it arrives on — the same
        contract as the Max Position override, so a confirmation cannot leave
        duplicate protection switched off.
        """
        adapter = adapter_for(placement.broker)
        fingerprint = placement.fingerprint()
        with self._lock:
            existing = store.for_fingerprint(fingerprint)

            # Unresolved first, and regardless of age: an order that may be live
            # at the broker outranks every other consideration.
            for claim in [c for c in existing if c.unresolved]:
                if not override:
                    return self._reconcile(placement, adapter, claim)
                # The user has looked at their order book and told us it is not
                # there. That is a human resolution of the claim, and recording
                # it as one keeps the registry honest — otherwise this same
                # unresolved claim would block every later attempt as well.
                store.update(claim.coid, resolved=True,
                             reason="resolved by explicit user override")
                diagnostics.event(
                    "orders", "Idempotency override", "started", level="warn",
                    broker=placement.broker, account=placement.account_id,
                    symbol=placement.symbol, side=placement.side,
                    qty=placement.qty, coid=claim.coid,
                    reason="user confirmed the unverified previous attempt is "
                           "not at the broker")

            live = [c for c in existing if c.state == PLACED]
            if live:
                # Measured from created_ts — when the order was actually placed —
                # not updated_ts, which moves every time the claim is written to
                # (a fill confirmation, an order-sync resolution). Keying the
                # window off updated_ts silently extended the suppression window
                # each time the order was touched, so "identical order placed 90s
                # ago" could keep blocking well past the documented two minutes.
                newest = max(live, key=lambda c: c.created_ts)
                age = time.time() - newest.created_ts
                if age <= RETRY_WINDOW_S and not override:
                    # Held, not silently absorbed. Whether a second identical
                    # order is a stray click or a deliberate add is something
                    # only the user knows, so this asks instead of choosing: the
                    # block names the order already in the book and is flagged
                    # overridable so the caller can offer "place anyway".
                    ago = _ago(age)
                    detail = (f"an identical order ({placement.side} "
                              f"{placement.qty} {placement.symbol}) was placed "
                              f"{ago}")
                    diagnostics.event(
                        "orders", "Duplicate order held", "rejected",
                        broker=placement.broker, account=placement.account_id,
                        symbol=placement.symbol, side=placement.side,
                        qty=placement.qty, coid=newest.coid,
                        orderId=newest.order_id,
                        reason=f"identical order placed {age:.0f}s ago — awaiting "
                               f"user confirmation")
                    return Decision(
                        placement=placement, coid=newest.coid, claim=newest,
                        blocked=True, code="DUPLICATE_ORDER", overridable=True,
                        duplicate_of=newest.order_id, age_s=age,
                        adapter=adapter, tier=adapter.tier if adapter else None,
                        detail=detail,
                        error=(f"Charticks placed {detail} ({LABEL_ORDER}"
                               f"{newest.order_id or newest.coid}) and has not sent "
                               f"this one, in case it is a repeated click. If you "
                               f"meant to place a second identical order, confirm "
                               f"to send it."))
                if age <= RETRY_WINDOW_S and override:
                    diagnostics.event(
                        "orders", "Idempotency override", "started", level="warn",
                        broker=placement.broker, account=placement.account_id,
                        symbol=placement.symbol, side=placement.side,
                        qty=placement.qty, coid=newest.coid,
                        orderId=newest.order_id,
                        reason=f"user confirmed a second identical order "
                               f"{age:.0f}s after the first")

            attempt = len(existing)
            coid = client_order_id(fingerprint, attempt)
            claim = store.add(Claim(
                coid=coid, fingerprint=fingerprint, attempt=attempt,
                account_id=placement.account_id, broker=placement.broker,
                symbol=placement.symbol, side=placement.side,
                qty=int(placement.qty), price=float(placement.price)))
            return Decision(placement=placement, coid=coid, claim=claim,
                            adapter=adapter,
                            tier=adapter.tier if adapter else None)

    # ── reconciliation ────────────────────────────────────────────────────
    def _reconcile(self, placement: Placement, adapter: BrokerIdem | None,
                   claim: Claim) -> Decision:
        """Decide what to do about a claim whose outcome we never learned."""
        if adapter is None:
            # A broker with no adapter cannot be reconciled, and guessing would
            # defeat the point. Registering one is a few lines (brokers.py).
            return self._blocked(placement, claim, None,
                                 f"{placement.broker} has no idempotency adapter, so "
                                 f"Charticks cannot check whether the previous "
                                 f"attempt at this order reached the broker. Check "
                                 f"your order book and retry if it is not there.")

        resolution, order_id, detail = self._probe(adapter, placement, claim)

        if resolution is Resolution.FOUND:
            store.update(claim.coid, state=PLACED, order_id=order_id,
                         resolved=True, reason=detail)
            diagnostics.event(
                "orders", "Duplicate order suppressed", "rejected",
                broker=placement.broker, account=placement.account_id,
                symbol=placement.symbol, side=placement.side, qty=placement.qty,
                coid=claim.coid, orderId=order_id,
                reason=f"unresolved attempt found at broker ({detail})")
            return Decision(placement=placement, coid=claim.coid, claim=claim,
                            duplicate=True, order_id=order_id, adapter=adapter,
                            tier=adapter.tier, detail=detail)

        if resolution is Resolution.ABSENT:
            # Established that it never arrived. Reuse the SAME client id: if the
            # first attempt is somehow still in flight at the broker, a native or
            # tag-echo broker will reject the second as a duplicate id rather
            # than book it twice.
            store.update(claim.coid, resolved=False, reason=f"resend: {detail}")
            diagnostics.event(
                "orders", "Order retry cleared", "success",
                broker=placement.broker, account=placement.account_id,
                symbol=placement.symbol, coid=claim.coid,
                reason=f"previous attempt confirmed absent ({detail})")
            return Decision(placement=placement, coid=claim.coid, claim=claim,
                            adapter=adapter, tier=adapter.tier, detail=detail)

        return self._blocked(placement, claim, adapter, detail)

    def _blocked(self, placement: Placement, claim: Claim,
                 adapter: BrokerIdem | None, detail: str) -> Decision:
        """The unverifiable case: a previous attempt may or may not be live.

        Overridable, because the user can do the one thing Charticks cannot —
        look at the broker's own order book — and the alternative is a trader
        unable to act. The message therefore asks them to check first, and the
        override is recorded as their decision.
        """
        broker = LABEL.get(placement.broker, placement.broker)
        diagnostics.event(
            "orders", "Order blocked by idempotency", "rejected",
            broker=placement.broker, account=placement.account_id,
            symbol=placement.symbol, side=placement.side, qty=placement.qty,
            coid=claim.coid, reason=detail)
        return Decision(
            placement=placement, coid=claim.coid, claim=claim, blocked=True,
            code="IDEMPOTENCY_UNRESOLVED", overridable=True,
            age_s=time.time() - claim.created_ts,
            adapter=adapter, tier=adapter.tier if adapter else None, detail=detail,
            error=(f"An earlier attempt at this order ({placement.side} "
                   f"{placement.qty} {placement.symbol}) was sent but never "
                   f"acknowledged, and Charticks cannot confirm what happened to "
                   f"it — {detail} It has NOT been sent again, because that is how "
                   f"duplicate live orders happen. Open your {broker} order book: "
                   f"if the order is not there, confirm to send it."))

    def _probe(self, adapter: BrokerIdem, placement: Placement,
               claim: Claim) -> tuple[Resolution, str, str]:
        """Ask the broker whether `claim` exists. Never raises."""
        from services.broker_manager import manager

        session = manager.live_session(placement.account_id)
        if session is None:
            return (Resolution.UNKNOWN, "",
                    "the account is not connected, so its order book cannot be read.")
        _broker, sess = session

        if adapter.lookup is not None:
            try:
                found = adapter.lookup(sess, claim.coid)
            except Exception as exc:
                return (Resolution.UNKNOWN, "",
                        f"the {placement.broker} lookup for client id {claim.coid} "
                        f"failed ({exc}).")
            if found:
                return Resolution.FOUND, str(found), f"client id {claim.coid} found"
            return (Resolution.ABSENT, "",
                    f"client id {claim.coid} is not in the broker's orders")

        try:
            rows = adapter.rows(sess) or []
        except Exception as exc:
            return (Resolution.UNKNOWN, "",
                    f"the {placement.broker} order book could not be read ({exc}).")

        if adapter.tier is Tier.TAG_ECHO:
            for row in rows:
                if _text(row, adapter.tag_keys) == claim.coid:
                    return (Resolution.FOUND, _text(row, adapter.id_keys),
                            f"client id {claim.coid} echoed in the order book")
            # An empty book is not proof: several SDKs return an empty list for a
            # read that failed. A book with orders in it, none carrying our tag,
            # is real evidence.
            if not rows:
                return (Resolution.UNKNOWN, "",
                        f"the {placement.broker} order book came back empty, which "
                        f"is indistinguishable from a failed read.")
            return (Resolution.ABSENT, "",
                    f"client id {claim.coid} is absent from {len(rows)} orders")

        return self._match_attributes(adapter, placement, claim, rows)

    def _match_attributes(self, adapter: BrokerIdem, placement: Placement,
                          claim: Claim, rows: list[dict]) -> tuple[Resolution, str, str]:
        """Last-resort tier: recognise the order by what it looks like.

        Conservative on purpose. A candidate is a row for the same contract, the
        same side and the same quantity. Anything that looks like our order is
        treated as our order — a false FOUND costs the user a resend they can
        make deliberately, while a false ABSENT costs them a duplicate live
        position.
        """
        if not rows:
            return (Resolution.UNKNOWN, "",
                    f"the {placement.broker} order book came back empty, which is "
                    f"indistinguishable from a failed read.")
        wanted_qty = int(placement.qty)
        for row in rows:
            symbol = _text(row, adapter.symbol_keys).upper().replace(" ", "")
            if not symbol or not _looks_like(symbol, placement):
                continue
            side = _text(row, adapter.side_keys).upper()
            if side and not side.startswith(placement.side[:1]):
                continue
            qty = _int(row, adapter.qty_keys)
            if qty and qty != wanted_qty:
                continue
            order_id = _text(row, adapter.id_keys)
            if order_id:
                return (Resolution.FOUND, order_id,
                        f"an order matching {placement.symbol} {placement.side} "
                        f"{wanted_qty} is already in the book (no client id support "
                        f"at {placement.broker}, matched on attributes)")
        return (Resolution.ABSENT, "",
                f"no order matching {placement.symbol} {placement.side} "
                f"{wanted_qty} in {len(rows)} orders")

    # ── outcomes ──────────────────────────────────────────────────────────
    def placed(self, decision: Decision, order_id: str) -> None:
        if decision.claim is None:
            return
        store.update(decision.coid, state=PLACED, order_id=str(order_id),
                     resolved=True, reason="")

    def failed(self, decision: Decision, reason: str) -> None:
        """The broker gave a definite no. A retry after this is legitimate."""
        if decision.claim is None:
            return
        store.update(decision.coid, state=FAILED, resolved=True,
                     reason=str(reason)[:300])

    def unresolved(self, decision: Decision, reason: str) -> None:
        """We do not know whether the broker got it — the case this exists for."""
        if decision.claim is None:
            return
        store.update(decision.coid, state=CLAIMED, resolved=False,
                     reason=str(reason)[:300])
        diagnostics.event(
            "orders", "Order outcome unknown", "error",
            broker=decision.placement.broker, account=decision.placement.account_id,
            symbol=decision.placement.symbol, coid=decision.coid,
            reason=f"{reason} — the next attempt at this order will be reconciled "
                   f"against the broker before anything is sent")

    def note_terminal(self, coid: str, order_id: str) -> None:
        """Called by the Order Synchronization Engine when a tracked order reaches
        a terminal state. Closes the loop: a claim whose order the sync engine has
        seen resolve cannot linger as unresolved and block a later order."""
        if not coid:
            return
        claim = store.get(coid)
        if claim is not None and not claim.resolved:
            store.update(coid, state=PLACED, order_id=str(order_id or claim.order_id),
                         resolved=True, reason="confirmed by order sync")


def _ago(seconds: float) -> str:
    """Plain English age, because "placed 94.3s ago" is a worse thing to read
    when deciding whether to double a live position."""
    seconds = max(0.0, seconds)
    if seconds < 10:
        return "moments ago"
    if seconds < 60:
        return f"{seconds:.0f} seconds ago"
    minutes = seconds / 60.0
    if minutes < 2:
        return "about a minute ago"
    return f"{minutes:.0f} minutes ago"


def _text(row: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        for candidate in (key, key.lower(), key.upper()):
            if candidate in row and row[candidate] not in (None, ""):
                return str(row[candidate]).strip()
    return ""


def _int(row: dict, keys: tuple[str, ...]) -> int:
    try:
        return int(float(_text(row, keys) or 0))
    except ValueError:
        return 0


def _looks_like(symbol: str, placement: Placement) -> bool:
    """Does a broker's trading symbol denote this contract?

    Brokers spell the same option four different ways, so this checks for the
    parts every spelling contains rather than trying to reproduce any one format.
    """
    strike = str(int(placement.strike))
    return (placement.underlying in symbol
            and strike in symbol
            and placement.opt_type in symbol)


guard = IdempotencyGuard()
