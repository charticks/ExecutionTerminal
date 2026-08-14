"""The live position book — every live position Charticks knows about.

What it is
----------
One entry per contract, identified by a **broker-independent canonical key**
(``services.instruments.InstrumentKey``), not by any broker's token. Two brokers
quoting or holding the same contract produce the same entry, and the book can be
rebuilt from disk before a single broker has connected — which is what makes
restart recovery possible at all.

Three kinds of entry live here, and the difference is never hidden:

  ``charticks``  opened by this application, from a broker-confirmed fill, with
                 the risk rule captured at entry. Managed.
  ``adopted``    opened elsewhere, and the user has explicitly asked Charticks
                 to manage it. Managed from the moment it is adopted.
  ``external``   found in the broker's position book and NOT adopted. Held here
                 so it can be displayed, counted toward exposure limits and
                 squared off — but never auto-exited, and always shown as
                 unmanaged.

Where entries come from
-----------------------
  order_sync ──(broker-confirmed fill)──→ record_fill()
  reconciler ──(broker position book)───→ apply_broker() / upsert_external()

The broker's own book is the final authority on *what is held*; this book is the
authority on *how it is being managed*. Reconciliation applies the first to the
second (see services.position_reconciler).

Durability
----------
Every mutation persists through ``services.live_store``. A restarted sidecar
restores the book, marks every entry ``restoring``, and resumes management only
once the broker has confirmed the position still exists. Nothing is auto-exited
against a position we have not re-verified.
"""
from __future__ import annotations

import threading
import time
from copy import copy
from dataclasses import dataclass, field

import diagnostics
from bridge import events
from bridge.hub import hub
from services.instruments import InstrumentKey
from services.live_store import live_store

# How long after sending an order an identical one counts as a duplicate. Matches
# the paper engine's own window so both modes behave the same way.
DUPLICATE_WINDOW_S = 2.0

# ── monitoring vocabulary ────────────────────────────────────────────────────
# One position's monitoring state, computed by the live manager and mirrored in
# charticks/src/bridge/events.ts MonitorState. A position is only safe in
# PROTECTED (and NO_RULE, where the user chose to trade without a stop); every
# other value raises the monitoring alarm and is rendered differently in the
# Positions grid. There is deliberately no "unknown" — a state we cannot compute
# is FEED_LOST, because that is what it means for the trader.
PROTECTED = "protected"      # managed, rule armed, live quote flowing
NO_RULE = "no_rule"          # managed, but no SL/Target/trail was set
EXITING = "exiting"          # an automated exit is in flight
FEED_LOST = "feed_lost"      # no market data — automation cannot evaluate
PAUSED = "paused"            # automation is not running (mode/engine state)
RESTORING = "restoring"      # restored from disk, awaiting broker confirmation
UNMANAGED = "unmanaged"      # broker position Charticks is not managing

# States that mean "this position is NOT being actively protected right now".
ALARM_STATES = (FEED_LOST, PAUSED, RESTORING, UNMANAGED)

# Position provenance.
SRC_CHARTICKS = "charticks"
SRC_ADOPTED = "adopted"
SRC_EXTERNAL = "external"


@dataclass
class LivePosition:
    key: str                  # canonical: underlying|expiry|strike|optType
    underlying: str
    expiry: str
    strike: float
    opt_type: str
    side: str                 # net direction of the remaining quantity
    qty: int = 0
    lots: int = 0
    avg_entry: float = 0.0
    opened_ts: float = field(default_factory=time.time)
    # ── live trade management ────────────────────────────────────────────
    # The risk rule captured from the order that OPENED this position, and the
    # absolute prices derived from it. Snapshotted at entry and never re-read
    # from Settings, so changing defaults affects future trades only — same
    # contract as the paper engine.
    rule: dict | None = None
    sl: float | None = None
    target: float | None = None
    trail: dict | None = None
    sl_base: float | None = None     # SL as first set; trailing moves from here
    ltp: float = 0.0
    # ── provenance + monitoring ──────────────────────────────────────────
    source: str = SRC_CHARTICKS
    managed: bool = True
    account_id: str = ""
    broker: str = ""
    # Product the position was opened under. Carried because a Roll re-opens the
    # exposure on another strike and has to re-open it as the SAME kind of trade:
    # defaulting to NRML would silently turn an intraday roll into a positional
    # one, which is a different trade with different margin and a different
    # auto-square-off.
    product: str = "NRML"
    # Last time a quote was applied to this position, and the last time the
    # broker confirmed it still exists. Both feed the monitoring state: a
    # position with neither is one nothing is actually watching.
    last_tick_ts: float = 0.0
    verified_ts: float = 0.0
    monitor: str = RESTORING
    monitor_detail: str = ""
    # An exit order for this position is currently at the broker. Blocks a
    # second exit from being fired on the next tick while the first is still
    # unconfirmed — the difference between one stop-loss exit and several.
    exit_pending_qty: int = 0
    exit_reason: str = ""
    exit_started_ts: float = 0.0

    # ── identity ──────────────────────────────────────────────────────────
    @property
    def ikey(self) -> InstrumentKey:
        """The canonical instrument key. Derived from the contract's own
        economics, so it is identical for every broker and survives a restart
        with no broker connected."""
        return InstrumentKey.option(self.underlying, self.expiry, self.strike,
                                    self.opt_type)

    @property
    def symbol(self) -> str:
        return f"{self.underlying} {self.expiry} {int(self.strike)} {self.opt_type}"

    @property
    def exitable_qty(self) -> int:
        """Quantity an automation may still act on: confirmed, minus whatever
        is already being exited."""
        return max(0, self.qty - self.exit_pending_qty)

    @property
    def has_rule(self) -> bool:
        return self.sl is not None or self.target is not None

    def pnl(self) -> float:
        if self.avg_entry <= 0 or self.ltp <= 0:
            return 0.0
        direction = 1 if self.side == "BUY" else -1
        return (self.ltp - self.avg_entry) * self.qty * direction

    # ── persistence ───────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "key": self.key, "underlying": self.underlying, "expiry": self.expiry,
            "strike": self.strike, "optType": self.opt_type, "side": self.side,
            "qty": self.qty, "lots": self.lots, "avgEntry": self.avg_entry,
            "openedTs": self.opened_ts, "rule": self.rule, "sl": self.sl,
            "target": self.target, "trail": self.trail, "slBase": self.sl_base,
            "ltp": self.ltp, "source": self.source, "managed": self.managed,
            "accountId": self.account_id, "broker": self.broker,
            "product": self.product,
            "verifiedTs": self.verified_ts,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "LivePosition | None":
        """Rebuild a persisted position. Returns None for a row that cannot be
        trusted — a book we cannot fully parse must not produce a position with
        half its risk state, which would look managed and behave as if it had
        no stop."""
        try:
            underlying = str(raw["underlying"]).upper()
            expiry = str(raw["expiry"]).upper()
            strike = float(raw["strike"])
            opt_type = str(raw["optType"]).upper()
            qty = int(raw["qty"])
            side = str(raw["side"]).upper()
        except (KeyError, TypeError, ValueError):
            return None
        if qty <= 0 or side not in ("BUY", "SELL") or opt_type not in ("CE", "PE"):
            return None

        def _opt_float(name: str) -> float | None:
            value = raw.get(name)
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        return cls(
            key=LiveBook.key_for(underlying, expiry, strike, opt_type),
            underlying=underlying, expiry=expiry, strike=strike,
            opt_type=opt_type, side=side, qty=qty,
            lots=int(raw.get("lots") or 0),
            avg_entry=float(raw.get("avgEntry") or 0.0),
            opened_ts=float(raw.get("openedTs") or time.time()),
            rule=raw.get("rule") if isinstance(raw.get("rule"), dict) else None,
            sl=_opt_float("sl"), target=_opt_float("target"),
            trail=raw.get("trail") if isinstance(raw.get("trail"), dict) else None,
            sl_base=_opt_float("slBase"),
            ltp=float(raw.get("ltp") or 0.0),
            source=str(raw.get("source") or SRC_CHARTICKS),
            managed=bool(raw.get("managed", True)),
            account_id=str(raw.get("accountId") or ""),
            broker=str(raw.get("broker") or ""),
            product=str(raw.get("product") or "NRML").upper(),
            # Deliberately NOT restored from the file. `verified_ts` means "the
            # broker confirmed this position exists" and is what arms
            # automation; a timestamp from before the restart is evidence about
            # a process that is no longer running. It is re-earned by the first
            # successful reconciliation, and until then nothing acts on this
            # position.
            verified_ts=0.0,
            monitor=RESTORING,
            monitor_detail="restored from disk — awaiting broker confirmation",
        )


def _hedge_labels(position_key: str) -> dict:
    """How this position relates to a protective hedge, if at all.

    Imported lazily and failure-tolerant: this runs on the publish path for every
    position update, and a problem in the (purely presentational) hedge link must
    never stop a position from reaching the screen.
    """
    try:
        from services.hedge import hedge_manager

        hedge = hedge_manager.hedge_of(position_key)
        protects = hedge_manager.parents_of(position_key)
        return {"hedgedBy": hedge, "hedgeFor": protects or None}
    except Exception:
        return {}


class LiveBook:
    """Thread-safe. Written from confirmed fills and broker reconciliation,
    read by risk validation and the live manager."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._positions: dict[str, LivePosition] = {}
        self._orders_today = 0
        self._realised = 0.0
        # contract|side -> time last submitted, for the duplicate window.
        self._recent: dict[str, float] = {}
        self._session_date = time.strftime("%Y-%m-%d")
        live_store.bind(self.state)

    @staticmethod
    def key_for(underlying: str, expiry: str, strike: float, opt_type: str) -> str:
        return InstrumentKey.option(underlying, expiry, strike, opt_type).position_id

    # ── persistence ───────────────────────────────────────────────────────
    def state(self) -> dict:
        with self._lock:
            return {
                "sessionDate": self._session_date,
                "ordersToday": self._orders_today,
                "realised": round(self._realised, 2),
                "positions": [p.to_dict() for p in self._positions.values()
                              # External positions are the broker's record, not
                              # ours: re-read on every startup rather than
                              # restored, so a stale one can never reappear.
                              if p.qty > 0 and p.source != SRC_EXTERNAL],
            }

    def restore(self) -> dict:
        """Load the persisted book at startup. Positions come back in
        RESTORING: management resumes only once the reconciler has matched them
        against the broker's own position book.

        The session counters (orders today, realised P&L) are restored only for
        the SAME trading day — they are session limits, and carrying yesterday's
        count into today would halt trading against a limit already spent.
        """
        state = live_store.load()
        if not state:
            return {"ok": True, "restored": 0}
        rows = state.get("positions")
        restored: list[LivePosition] = []
        skipped = 0
        for raw in rows if isinstance(rows, list) else []:
            pos = LivePosition.from_dict(raw) if isinstance(raw, dict) else None
            if pos is None:
                skipped += 1
                continue
            restored.append(pos)
        same_day = state.get("sessionDate") == time.strftime("%Y-%m-%d")
        with self._lock:
            for pos in restored:
                self._positions[pos.key] = pos
            if same_day:
                self._orders_today = int(state.get("ordersToday") or 0)
                self._realised = float(state.get("realised") or 0.0)
        diagnostics.event(
            "orders", "Live book restore", "success" if not skipped else "partial",
            level="warn" if restored else "info",
            positions=len(restored), unreadableRows=skipped,
            countersRestored=same_day, savedTs=state.get("savedTs"),
            symbols=", ".join(p.symbol for p in restored) or None)
        for pos in restored:
            self._publish(pos)
        return {"ok": True, "restored": len(restored), "skipped": skipped,
                "sameDay": same_day}

    def _persist(self, immediate: bool = False) -> None:
        if immediate:
            live_store.flush()
        else:
            live_store.mark()

    # ── writes ─────────────────────────────────────────────────────────────
    def record_fill(self, underlying: str, expiry: str, strike: float,
                    opt_type: str, side: str, qty: int, lots: int,
                    price: float, token: str = "",
                    rule: dict | None = None, account_id: str = "",
                    broker: str = "", product: str = "NRML") -> None:
        """Record a broker-confirmed live fill. A same-side order averages in; an
        opposite-side one reduces (and closes at zero), booking realised P&L."""
        if qty <= 0:
            return
        key_obj = InstrumentKey.option(underlying, expiry, strike, opt_type)
        # MARKET orders carry no price (0). Marking one in at 0 would value the
        # whole position as pure profit and could trip Profit Target on its own,
        # so fall back to the current quote, and leave it at 0 when even that is
        # unavailable — session_pnl() then excludes it rather than guessing.
        if price <= 0:
            from services.broker_manager import manager
            ltp, _bid, _ask = manager.get_option_quote(key_obj)
            price = ltp if ltp and ltp > 0 else 0.0
        key = key_obj.position_id
        # Set inside the lock, published outside it: hub.publish reaches the
        # WebSocket layer, and holding the book's lock across that would couple
        # position updates to network back-pressure.
        changed: LivePosition | None = None
        closed = False
        with self._lock:
            pos = self._positions.get(key)
            if pos is None or pos.qty <= 0:
                pos = LivePosition(
                    key=key, underlying=underlying.upper(), expiry=expiry.upper(),
                    strike=strike, opt_type=opt_type.upper(), side=side, qty=qty,
                    lots=lots, avg_entry=price, ltp=price, rule=rule,
                    source=SRC_CHARTICKS, managed=True, account_id=account_id,
                    broker=broker, product=(product or "NRML").upper(),
                    verified_ts=time.time())
                self._apply_rule(pos)
                self._positions[key] = pos
                changed = pos
            elif pos.side == side:
                total = pos.qty + qty
                pos.avg_entry = ((pos.avg_entry * pos.qty) + (price * qty)) / total
                pos.qty = total
                pos.lots += lots
                # A fill Charticks placed on a position it was not managing
                # (adopted or external) makes it ours from here: the order
                # carried a rule, and half-managing a netted position is worse
                # than either extreme.
                if rule is not None:
                    pos.rule = rule
                    pos.source = SRC_CHARTICKS if pos.source == SRC_EXTERNAL else pos.source
                    pos.managed = True
                # Averaging in moves the cost basis, so SL/Target derived from
                # it must move too — otherwise a stop set for the first fill
                # sits at the wrong distance for the combined position.
                self._apply_rule(pos)
                changed = pos
            else:
                closing = min(pos.qty, qty)
                direction = 1 if pos.side == "BUY" else -1
                self._realised += (price - pos.avg_entry) * closing * direction
                pos.qty -= closing
                pos.lots = max(0, pos.lots - lots)
                # This fill satisfied part (or all) of any in-flight exit.
                pos.exit_pending_qty = max(0, pos.exit_pending_qty - closing)
                changed = pos
                if pos.qty <= 0:
                    self._positions.pop(key, None)
                    closed = True
                    diagnostics.event("orders", "Live position closed", "success",
                                      symbol=pos.symbol,
                                      reason=pos.exit_reason or None,
                                      realised=round(self._realised, 2))
        if closed:
            # The hedge that protected this short is no longer attached to
            # anything, so a later short on the same contract earns a fresh one.
            from services.hedge import hedge_manager
            hedge_manager.forget(key)
        # Immediate: a fill changes what is held, and losing that to a crash in
        # the next half second is the exact failure this store exists to stop.
        self._persist(immediate=True)
        if changed is not None:
            self._publish(changed, closed=closed)

    def upsert_external(self, key: InstrumentKey, side: str, qty: int,
                        avg_entry: float, ltp: float, lots: int,
                        account_id: str, broker: str) -> LivePosition | None:
        """Record a position found in the broker's book that Charticks did not
        open. Never managed: it is displayed, counted toward exposure limits and
        can be squared off, but no stop, target or trail is invented for it.
        The user adopts it explicitly (see ``adopt``) or leaves it alone."""
        if qty <= 0:
            return None
        pid = key.position_id
        with self._lock:
            pos = self._positions.get(pid)
            if pos is not None and pos.source != SRC_EXTERNAL:
                return None  # already ours — apply_broker() owns this row
            if pos is None:
                pos = LivePosition(
                    key=pid, underlying=key.underlying, expiry=key.expiry,
                    strike=key.strike, opt_type=key.opt_type, side=side,
                    qty=qty, lots=lots, avg_entry=avg_entry, ltp=ltp,
                    source=SRC_EXTERNAL, managed=False,
                    account_id=account_id, broker=broker,
                    monitor=UNMANAGED,
                    monitor_detail="opened outside Charticks — no stop loss, "
                                   "target or trail is being applied")
                self._positions[pid] = pos
                diagnostics.event(
                    "orders", "External position detected", "success",
                    level="warn", symbol=pos.symbol, side=side, qty=qty,
                    broker=broker, account=account_id,
                    reason="not managed by Charticks until adopted")
            else:
                pos.side, pos.qty, pos.lots = side, qty, lots
                pos.avg_entry = avg_entry or pos.avg_entry
                if ltp > 0:
                    pos.ltp = ltp
                pos.account_id, pos.broker = account_id, broker
            pos.verified_ts = time.time()
            snapshot = copy(pos)
        self._publish(snapshot)
        return snapshot

    def apply_broker(self, key: InstrumentKey, side: str, qty: int,
                     avg_entry: float, account_id: str = "",
                     broker: str = "") -> str | None:
        """Apply the broker's own figures to a position we already hold. The
        broker is the authority on quantity: a size we disagree with is adopted
        from it, not argued with, because every automated exit is sized from
        this number.

        Returns a short description when something actually differed, for the
        reconciliation log; None when the books already agreed.
        """
        pid = key.position_id
        note: str | None = None
        with self._lock:
            pos = self._positions.get(pid)
            if pos is None:
                return None
            pos.verified_ts = time.time()
            if account_id:
                pos.account_id = account_id
            if broker:
                pos.broker = broker
            flipped = bool(side and side != pos.side and qty > 0)
            if flipped:
                note = f"side {pos.side}→{side}"
                pos.side = side
                # A stop derived for a long sits BELOW the price; the same number
                # on a short is already breached, and leaving it would fire an
                # exit the instant the flip was noticed. Re-derive from the rule
                # against the broker's own cost basis, or carry no stop at all —
                # never a stale one pointing the wrong way.
                pos.sl = pos.target = pos.sl_base = pos.trail = None
                if avg_entry > 0:
                    pos.avg_entry = avg_entry
                self._apply_rule(pos)
            if qty > 0 and qty != pos.qty:
                note = (f"{note}, " if note else "") + f"qty {pos.qty}→{qty}"
                # Keep lots proportional to the quantity we now believe in;
                # exits are split by lots and a stale lot count would size the
                # child orders wrongly.
                lot_size = int(pos.qty / pos.lots) if pos.lots else 0
                pos.qty = qty
                pos.lots = max(1, qty // lot_size) if lot_size else pos.lots
                # A quantity that shrank may be smaller than the exit we have in
                # flight; never let the claim exceed what is held.
                pos.exit_pending_qty = min(pos.exit_pending_qty, pos.qty)
            if avg_entry > 0 and pos.avg_entry <= 0:
                # Only fill a gap. A broker's average is its own accounting of
                # the whole day; ours is the cost basis the SL and Target were
                # derived from, and overwriting it would move both.
                pos.avg_entry = avg_entry
                self._apply_rule(pos)
                note = (f"{note}, " if note else "") + "entry price learned from broker"
        if note:
            self._persist()
        return note

    def drop(self, key_or_id, reason: str) -> bool:
        """Remove a position the broker no longer reports. Called by
        reconciliation only, and only after a successful read of that account's
        book — never on a failed poll, which would silently disarm a position
        that is very much still open."""
        pid = key_or_id.position_id if isinstance(key_or_id, InstrumentKey) else str(key_or_id)
        with self._lock:
            pos = self._positions.pop(pid, None)
        if pos is None:
            return False
        diagnostics.event("orders", "Live position removed", "success",
                          symbol=pos.symbol, reason=reason,
                          source=pos.source, qty=pos.qty)
        # A position closed OUTSIDE Charticks reaches the book this way rather
        # than through record_fill, and it orphans a hedge just as surely — the
        # user closing a short in their broker's own app must not leave the
        # protective leg behind with nothing to say about it.
        from services.hedge import hedge_manager
        hedge_manager.forget(pid)
        self._persist(immediate=True)
        pos.qty = 0
        self._publish(pos, closed=True)
        return True

    def adopt(self, key_or_id, rule: dict | None) -> dict:
        """Take over management of a position Charticks did not open.

        The rule is applied against the position's existing cost basis, exactly
        as if the position had been opened with it, so an adopted position is
        managed on identical terms to a native one — there is no second, weaker
        kind of management.
        """
        pid = key_or_id.position_id if isinstance(key_or_id, InstrumentKey) else str(key_or_id)
        with self._lock:
            pos = self._positions.get(pid)
            if pos is None or pos.qty <= 0:
                return {"ok": False, "code": "NO_POSITION",
                        "error": "That position is no longer open."}
            if pos.avg_entry <= 0:
                return {"ok": False, "code": "NO_COST_BASIS",
                        "error": f"{pos.symbol} has no entry price from the broker "
                                 f"yet, so a stop loss cannot be placed against it."}
            pos.rule = rule or pos.rule
            pos.source = SRC_ADOPTED
            pos.managed = True
            pos.monitor = RESTORING
            pos.monitor_detail = "adopted — arming"
            self._apply_rule(pos)
            snapshot = copy(pos)
        diagnostics.event("orders", "Position adopted", "success", level="warn",
                          symbol=snapshot.symbol, side=snapshot.side,
                          qty=snapshot.qty, sl=snapshot.sl,
                          target=snapshot.target, entry=snapshot.avg_entry)
        self._persist(immediate=True)
        self._publish(snapshot)
        return {"ok": True, "position": snapshot.key, "sl": snapshot.sl,
                "target": snapshot.target}

    def release(self, key_or_id) -> dict:
        """Stop managing a position without closing it — the inverse of adopt.
        It stays in the book, visible and plainly marked unmanaged."""
        pid = key_or_id.position_id if isinstance(key_or_id, InstrumentKey) else str(key_or_id)
        with self._lock:
            pos = self._positions.get(pid)
            if pos is None:
                return {"ok": False, "code": "NO_POSITION",
                        "error": "That position is no longer open."}
            pos.managed = False
            pos.source = SRC_EXTERNAL
            pos.sl = pos.target = pos.trail = pos.sl_base = None
            pos.rule = None
            pos.monitor = UNMANAGED
            pos.monitor_detail = "management released by the user"
            snapshot = copy(pos)
        diagnostics.event("orders", "Position released", "success", level="warn",
                          symbol=snapshot.symbol, qty=snapshot.qty,
                          reason="user chose not to manage this position")
        self._persist(immediate=True)
        self._publish(snapshot)
        return {"ok": True}

    # ── live trade management state ───────────────────────────────────────
    @staticmethod
    def _apply_rule(pos: LivePosition) -> None:
        """Derive SL / Target / trail from the position's rule and cost basis.

        Shares `_compute_risk` with the paper engine so a live position's stop
        sits exactly where the same trade's paper stop would — the two engines
        must not disagree about what a rule means. Caller must hold the lock.
        """
        from services.paper_engine import _compute_risk, _trail_of

        if not pos.rule or pos.avg_entry <= 0:
            return
        risk = _compute_risk(pos.avg_entry, pos.side, pos.rule)
        pos.sl = risk.get("sl")
        pos.target = risk.get("target")
        pos.sl_base = pos.sl
        pos.trail = _trail_of(pos.rule)

    def update_quote(self, key: InstrumentKey, ltp: float) -> LivePosition | None:
        """Mark the position on this contract to market. Called from the tick
        feed and from the live manager's periodic sweep — both address the
        position by canonical key, so this works on whichever broker's feed
        happens to be carrying the contract."""
        if key is None or ltp <= 0:
            return None
        with self._lock:
            pos = self._positions.get(key.position_id)
            if pos is None:
                return None
            pos.ltp = ltp
            pos.last_tick_ts = time.time()
            snapshot = copy(pos)
        self._publish(snapshot)
        return snapshot

    def set_monitor(self, key_id: str, state: str, detail: str = "") -> bool:
        """Record a position's monitoring state. Returns True when it CHANGED,
        so the caller can log and publish a transition rather than a heartbeat."""
        with self._lock:
            pos = self._positions.get(key_id)
            if pos is None or (pos.monitor == state and pos.monitor_detail == detail):
                return False
            pos.monitor = state
            pos.monitor_detail = detail
            snapshot = copy(pos)
        self._publish(snapshot)
        return True

    def _publish(self, pos: LivePosition, closed: bool = False) -> None:
        """Push the position to the renderer.

        Deliberately the ONE publisher of live position rows: the broker
        position poller used to publish its own, keyed by broker token, so the
        same contract appeared twice and neither row could say whether it was
        being managed. Everything the renderer needs to distinguish a protected
        position from an unprotected one is carried here.
        """
        hub.publish(events.position_update({
            "id": pos.key,
            "symbol": pos.symbol,
            "underlying": pos.underlying,
            "expiry": pos.expiry,
            "strike": int(pos.strike),
            "optType": pos.opt_type,
            "side": pos.side,
            "qty": 0 if closed else pos.qty,
            "lots": 0 if closed else pos.lots,
            "entry": round(pos.avg_entry, 2),
            "ltp": round(pos.ltp, 2),
            "pnl": round(pos.pnl(), 2),
            "sl": pos.sl,
            "target": pos.target,
            "trail": pos.trail,
            "managed": pos.managed,
            "monitorState": pos.monitor,
            "monitorDetail": pos.monitor_detail,
            "source": pos.source,
            # The exit in flight, ON the position rather than as a separate row.
            # A square-off is something that happens TO a position, not a second
            # position, and the grid renders one row from entry to close: these
            # two fields are what let it show "Exit Pending" and lock the
            # controls instead of growing a duplicate row for the exit order.
            #
            # Published from the book rather than derived from `monitorState`
            # because the monitor is recomputed once per evaluation cycle, and a
            # row must not stay interactive for up to three quarters of a second
            # after the user has already sent the exit.
            "exitPendingQty": pos.exit_pending_qty,
            "exitReason": pos.exit_reason or None,
            # The hedge relationship, both ways. A hedge is not an independent
            # trade and must not read like one: the protective leg is labelled
            # with what it protects, and the short with the fact that it is
            # covered. See services/hedge.py.
            **_hedge_labels(pos.key),
            "account": pos.account_id or None,
            "broker": pos.broker or None,
            "closed": closed,
        }))

    def republish(self) -> None:
        """Re-send every position. Used when a renderer (re)connects: position
        events are deltas, so a reloaded window would otherwise show an empty
        book until the next tick — indistinguishable from having no positions."""
        for pos in self.open_positions():
            self._publish(pos)

    def open_positions(self) -> list[LivePosition]:
        """Snapshot for the live manager. Copies, so evaluation never holds the
        lock while it decides — and never mutates the book by accident."""
        with self._lock:
            return [copy(p) for p in self._positions.values() if p.qty > 0]

    def managed_positions(self) -> list[LivePosition]:
        """Only the positions automation may act on."""
        return [p for p in self.open_positions() if p.managed]

    def subscription_keys(self) -> set[InstrumentKey]:
        """Every contract the book needs market data for — INCLUDING unmanaged
        ones, which still have to mark to market and would otherwise show a
        frozen P&L."""
        return {p.ikey for p in self.open_positions()}

    def get(self, key: str) -> LivePosition | None:
        with self._lock:
            pos = self._positions.get(key)
            return copy(pos) if pos else None

    def set_risk(self, key: str, sl: float | None = None,
                 target: float | None = None, trail_after: float | None = None,
                 trail_step: float | None = None) -> bool:
        """Edit ONE live position's risk settings. Mirrors
        PaperEngine.set_risk, so the same edit means the same thing in both
        modes. Never touches another position or the profile defaults."""
        with self._lock:
            pos = self._positions.get(key)
            if pos is None:
                return False
            if sl is not None:
                pos.sl = sl
                # A manual stop becomes the new baseline point trailing steps
                # from — otherwise the next tick would undo the user's edit.
                pos.sl_base = sl
            if target is not None:
                pos.target = target
            if trail_after is not None or trail_step is not None:
                trail = dict(pos.trail or {"mode": "point", "after": 0, "step": 0})
                if trail_after is not None:
                    trail["after"] = float(trail_after)
                if trail_step is not None:
                    trail["step"] = float(trail_step)
                pos.trail = (trail if trail["after"] > 0 and trail["step"] > 0
                             else None)
            snapshot = copy(pos)
        self._persist(immediate=True)
        self._publish(snapshot)
        return True

    def move_stop(self, key: str, new_sl: float) -> bool:
        """Trailing only ever tightens; a pullback never gives ground back."""
        with self._lock:
            pos = self._positions.get(key)
            if pos is None or pos.sl is None:
                return False
            better = new_sl > pos.sl if pos.side == "BUY" else new_sl < pos.sl
            if not better:
                return False
            pos.sl = new_sl
        # A trailed stop that a crash reverts to its original level is a real
        # loss of protection, so the move is persisted like any other change.
        self._persist()
        return True

    def begin_exit(self, key: str, qty: int, reason: str) -> bool:
        """Claim `qty` for an automated exit. False when there is nothing left
        to claim — which is what stops a stop-loss firing again on every tick
        while the first exit order is still unconfirmed at the broker.

        Publishes, so the row enters its "Exit Pending" state the moment the
        claim is made rather than at the next evaluation cycle — the user must
        never be able to press Close twice because the first press had not
        visibly landed.
        """
        with self._lock:
            pos = self._positions.get(key)
            if pos is None or qty <= 0 or pos.exitable_qty < qty:
                return False
            pos.exit_pending_qty += qty
            pos.exit_reason = reason
            pos.exit_started_ts = time.time()
            snapshot = copy(pos)
        self._publish(snapshot)
        return True

    def end_exit(self, key: str) -> None:
        """Release the claim — the exit order reached a terminal state. A
        rejected or cancelled exit must not leave the position permanently
        unprotected, so the claim is dropped and the stop can fire again.

        Also returns the row to its normal, interactive state: an exit that was
        refused has to hand the controls back, or the position is left looking
        like it is closing forever.
        """
        with self._lock:
            pos = self._positions.get(key)
            if pos is None or pos.exit_pending_qty == 0:
                return
            pos.exit_pending_qty = 0
            pos.exit_started_ts = 0.0
            pos.exit_reason = ""
            snapshot = copy(pos)
        self._publish(snapshot)

    def release_stale_exits(self, older_than_s: float) -> list[str]:
        """Drop exit claims whose order never reached a terminal state. Without
        this, one lost order id would silently disable that position's stop for
        the rest of the session."""
        cutoff = time.time() - older_than_s
        released, snapshots = [], []
        with self._lock:
            for pos in self._positions.values():
                if pos.exit_pending_qty > 0 and 0 < pos.exit_started_ts < cutoff:
                    pos.exit_pending_qty = 0
                    pos.exit_started_ts = 0.0
                    pos.exit_reason = ""
                    released.append(pos.key)
                    snapshots.append(copy(pos))
        for snapshot in snapshots:
            self._publish(snapshot)
        return released

    def reset(self) -> None:
        """Start a fresh session — clears counters and the book."""
        with self._lock:
            stale = list(self._positions.values())
            self._positions.clear()
            self._orders_today = 0
            self._realised = 0.0
            self._recent.clear()
            self._session_date = time.strftime("%Y-%m-%d")
        self._persist(immediate=True)
        for pos in stale:
            pos.qty = 0
            self._publish(pos, closed=True)
        diagnostics.event("orders", "Live session reset", "success")

    # ── reads (used by risk validation) ────────────────────────────────────
    def open_count(self) -> int:
        """Open positions, INCLUDING ones opened outside Charticks.

        Max Positions is an exposure limit, and exposure the broker is carrying
        is exposure whoever opened it. Counting only our own would let the limit
        be doubled by placing half the trades in the broker's own terminal.
        """
        with self._lock:
            return sum(1 for p in self._positions.values() if p.qty > 0)

    def held_lots(self, underlying: str, expiry: str, strike: float,
                  opt_type: str) -> int:
        with self._lock:
            pos = self._positions.get(self.key_for(underlying, expiry, strike, opt_type))
            return pos.lots if pos else 0

    def orders_today(self) -> int:
        with self._lock:
            return self._orders_today

    def working_order_id(self, underlying: str, expiry: str, strike: float,
                         opt_type: str, side: str) -> str | None:
        """Duplicate guard for LIVE: a short-window guard against the same order
        being fired twice — a double-click, a retry loop, or a re-submitted
        request. Not a substitute for the broker's own working-order list."""
        key = f"{self.key_for(underlying, expiry, strike, opt_type)}|{side}"
        cutoff = time.time() - DUPLICATE_WINDOW_S
        with self._lock:
            sent = self._recent.get(key)
            return key if sent is not None and sent >= cutoff else None

    def note_submitted(self, underlying: str, expiry: str, strike: float,
                       opt_type: str, side: str) -> None:
        """Record that this contract + side was just sent, opening the duplicate
        window. Called immediately before routing, so a second request racing
        the first still sees it.

        Also where the Max Trades counter increments — once per order, not per
        fill. Counting in record_fill instead made a partially-filled order
        count twice (once for the partial, once for the remainder), inflating
        the session's trade count against the user's limit.
        """
        key = f"{self.key_for(underlying, expiry, strike, opt_type)}|{side}"
        with self._lock:
            self._recent[key] = time.time()
            self._orders_today += 1
        self._persist()

    def session_pnl(self) -> float:
        """Realised + open mark-to-market for CHARTICKS' OWN trading.

        Positions opened elsewhere are excluded: Max Loss and Profit Target
        measure this session's trading, and halting Charticks because of a
        position it neither opened nor manages would be a limit the user cannot
        act on. (Exposure limits do count them — see open_count.)
        """
        from services.broker_manager import manager

        with self._lock:
            positions = list(self._positions.values())
            total = self._realised
        for pos in positions:
            # avg_entry 0 means we never learned the fill price (market order,
            # no quote at the time). Its P&L is unknowable, so it contributes
            # nothing rather than its full mark as fictitious profit.
            if pos.qty <= 0 or pos.avg_entry <= 0 or pos.source == SRC_EXTERNAL:
                continue
            ltp, _bid, _ask = manager.get_option_quote(pos.ikey)
            if not ltp or ltp <= 0:
                continue
            direction = 1 if pos.side == "BUY" else -1
            total += (ltp - pos.avg_entry) * pos.qty * direction
        return round(total, 2)

    def snapshot(self) -> dict:
        with self._lock:
            positions = [p for p in self._positions.values() if p.qty > 0]
            return {
                "positions": [
                    {"id": p.key, "symbol": p.symbol, "side": p.side, "qty": p.qty,
                     "lots": p.lots, "avgEntry": round(p.avg_entry, 2),
                     "ltp": round(p.ltp, 2), "sl": p.sl, "target": p.target,
                     "managed": p.managed, "source": p.source,
                     "monitorState": p.monitor, "monitorDetail": p.monitor_detail,
                     "account": p.account_id, "broker": p.broker,
                     "lastTickTs": p.last_tick_ts, "verifiedTs": p.verified_ts,
                     "exitPendingQty": p.exit_pending_qty}
                    for p in positions
                ],
                "ordersToday": self._orders_today,
                "realised": round(self._realised, 2),
                "sessionDate": self._session_date,
                "storage": live_store.status(),
            }


live_book = LiveBook()
