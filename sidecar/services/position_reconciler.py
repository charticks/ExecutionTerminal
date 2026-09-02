"""Reconciliation between the broker's position book and Charticks' own.

    broker position book  ──┐
                            ├──→ reconcile ──→ live_book ──→ UI + automation
    persisted live book   ──┘

The rule
--------
**The broker is the authority on what is held. Charticks is the authority on how
it is managed.** Every cycle applies the first to the second:

  in both books          → resume/continue management, adopting the broker's
                           quantity (exits are sized from it)
  broker only            → an UNMANAGED position. Never silently assumed to be
                           ours, never given an invented stop; surfaced so the
                           user can adopt or ignore it
  our book only          → the position is gone at the broker. Removed — but
                           only after a SUCCESSFUL read of every connected
                           account, and never on the first miss, because a
                           just-filled order can lead its own position book

Why the grace rules matter
--------------------------
The two failure directions are not symmetric. Wrongly keeping a position costs a
rejected exit; wrongly dropping one disarms a live stop loss and says nothing.
So a position is only removed when we positively read the books that could have
contained it, twice, and it was in neither.
"""
from __future__ import annotations

import threading
import time

import diagnostics
from bridge import events
from bridge.hub import hub

from services import broker_positions
from services.broker_manager import manager
from services.instruments import InstrumentKey
from services.live_book import SRC_EXTERNAL, UNMANAGED, live_book

# How often to re-read the broker books. Matches the previous positions poller;
# fast enough to notice an external trade within a few seconds, slow enough not
# to spend an account's rate limit on it.
RECONCILE_INTERVAL_S = 4.0
# Consecutive clean misses before a position is removed from our book.
MISSES_BEFORE_DROP = 2
# A position younger than this is never dropped: the broker's position book can
# lag the fill that created it by a second or two.
DROP_GRACE_S = 20.0


class PositionReconciler:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._misses: dict[str, int] = {}
        # Broker rows we could not resolve to a canonical contract (equity legs,
        # futures, an unparseable symbol). Displayed, never managed. Tracked so
        # they can be flagged closed when they disappear.
        self._foreign: set[str] = set()
        self._last_ok_ts = 0.0
        self._last_error: str | None = None
        self._cycles = 0
        self._accounts_ok = 0
        self._accounts_failed = 0
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="position-reconciler")
            self._thread.start()

    def reconcile_soon(self) -> None:
        """Run a pass now (used after a broker connects or a fill lands)."""
        self._wake.set()

    def _run(self) -> None:
        while True:
            try:
                if manager.connected_sessions():
                    self.reconcile_once()
            except Exception as exc:
                self._last_error = str(exc)
                diagnostics.exception("orders", "Position reconciliation failed",
                                      exc_info=exc)
            self._wake.wait(timeout=RECONCILE_INTERVAL_S)
            self._wake.clear()

    # ── one pass ──────────────────────────────────────────────────────────
    def reconcile_once(self) -> dict:
        rows, ok_accounts, failed_accounts = self._read_all()
        by_key: dict[InstrumentKey, list] = {}
        foreign: list = []
        net_pnl = 0.0
        for row in rows:
            net_pnl += row.pnl
            if row.key is None:
                foreign.append(row)
            else:
                by_key.setdefault(row.key, []).append(row)

        seen: set[str] = set()
        for key, group in by_key.items():
            seen.add(key.position_id)
            self._apply(key, group)

        # Removal is only safe when we actually READ every connected account.
        # A failed poll is not evidence of absence, and treating it as such
        # would disarm a live position because an API call timed out.
        if ok_accounts and not failed_accounts:
            self._drop_missing(seen)
        else:
            with self._lock:
                self._misses.clear()

        self._publish_foreign(foreign)
        hub.publish(events.pnl_update(round(net_pnl, 2)))

        with self._lock:
            self._cycles += 1
            self._accounts_ok = ok_accounts
            self._accounts_failed = failed_accounts
            if ok_accounts and not failed_accounts:
                self._last_ok_ts = time.time()
        return {"ok": True, "accountsRead": ok_accounts,
                "accountsFailed": failed_accounts, "positions": len(by_key),
                "foreign": len(foreign)}

    def _read_all(self):
        rows, ok, failed = [], 0, 0
        for account_id, broker, session in manager.connected_sessions():
            if not broker_positions.supported(broker):
                continue
            try:
                rows.extend(broker_positions.read(account_id, broker, session))
                ok += 1
            except Exception as exc:
                failed += 1
                self._last_error = f"{broker}: {exc}"
                classification = manager.session_manager.report_error(
                    account_id, broker, exc)
                if classification != "session_expired":
                    hub.publish(events.log_line(
                        "warn", f"[positions] {broker} poll failed "
                                f"({classification}): {exc}"))
        return rows, ok, failed

    def _apply(self, key: InstrumentKey, group: list) -> None:
        """Fold one contract's broker rows into the live book.

        A contract can be held in more than one account — live orders fan out
        across every execution account — so the rows are netted into a single
        canonical position, which is exactly how the live book has always
        recorded them.
        """
        signed = sum(r.qty if r.side == "BUY" else -r.qty for r in group)
        if signed == 0:
            return  # long and short legs cancel — nothing is held
        side = "BUY" if signed > 0 else "SELL"
        qty = abs(signed)
        same = [r for r in group if r.side == side and r.avg_entry > 0]
        avg = (sum(r.avg_entry * r.qty for r in same) / sum(r.qty for r in same)
               if same else 0.0)
        ltp = next((r.ltp for r in group if r.ltp > 0), 0.0)
        account = group[0].account_id
        broker = group[0].broker
        # Whichever row actually names one. A position held across several
        # accounts is rare and, when it happens, every leg is opened the same
        # way in practice — there is no meaningful way to "net" a product code
        # the way quantity nets, so the first answer is taken as the answer.
        product = next((r.product for r in group if r.product), "")

        with self._lock:
            self._misses.pop(key.position_id, None)

        existing = live_book.get(key.position_id)
        if existing is not None:
            note = live_book.apply_broker(key, side, qty, avg, account, broker,
                                          product)
            if note:
                diagnostics.event(
                    "orders", "Position reconciled", "adjusted", level="warn",
                    symbol=str(key), broker=broker, account=account,
                    change=note, reason="broker book is authoritative on size")
            return
        live_book.upsert_external(key, side, qty, avg, ltp,
                                  self._lots_for(key, qty), account, broker,
                                  product)

    @staticmethod
    def _lots_for(key: InstrumentKey, qty: int) -> int:
        """Lots for a position we did not open, from the contract's lot size.

        0 when no loaded instrument master lists the contract — the exit path
        then splits on raw quantity rather than trusting a made-up lot count.
        """
        try:
            meta = manager.option_meta(key.underlying, key.expiry, key.strike,
                                       key.opt_type) or {}
        except Exception:
            return 0
        lot = int(meta.get("lotSize") or 0)
        return qty // lot if lot > 0 and qty % lot == 0 else 0

    def _drop_missing(self, seen: set[str]) -> None:
        for pos in live_book.open_positions():
            if pos.key in seen:
                continue
            # An exit we ourselves sent is in flight — the position leaving the
            # broker's book is the expected outcome, and record_fill will close
            # it properly with its realised P&L. Dropping it here would lose
            # that. Wait for the fill.
            if pos.exit_pending_qty > 0:
                continue
            age = time.time() - max(pos.opened_ts, pos.verified_ts)
            if pos.verified_ts > 0 and age < DROP_GRACE_S:
                continue
            with self._lock:
                misses = self._misses.get(pos.key, 0) + 1
                self._misses[pos.key] = misses
            if misses < MISSES_BEFORE_DROP:
                continue
            with self._lock:
                self._misses.pop(pos.key, None)
            live_book.drop(pos.key, "not present in the broker's position book")

    def _publish_foreign(self, rows: list) -> None:
        """Positions Charticks cannot express as an option contract — an equity
        or futures leg, or a symbol no rule could parse. Shown, clearly labelled
        as not manageable, so the Positions tab never hides real exposure."""
        current: set[str] = set()
        for row in rows:
            current.add(row.id)
            hub.publish(events.position_update({
                "id": row.id, "symbol": row.symbol, "side": row.side,
                "qty": row.qty, "lots": 0, "entry": row.avg_entry,
                "ltp": row.ltp, "pnl": row.pnl, "sl": None, "target": None,
                "managed": False, "monitorState": UNMANAGED,
                "monitorDetail": "not an option contract Charticks can manage",
                "source": SRC_EXTERNAL, "account": row.account_id,
                "broker": row.broker, "closed": False,
            }))
        with self._lock:
            gone = self._foreign - current
            self._foreign = current
        for pid in gone:
            hub.publish(events.position_update(
                {"id": pid, "qty": 0, "pnl": 0, "closed": True}))

    # ── startup ───────────────────────────────────────────────────────────
    def restore_and_start(self) -> dict:
        """Restore the persisted book, then begin reconciling.

        Restored positions arm only once a broker confirms them — see
        LiveManager.evaluate — so the window between "sidecar up" and "broker
        connected" cannot fire a stop against a position that no longer exists.
        """
        result = live_book.restore()
        self.start()
        self.reconcile_soon()
        return result

    def status(self) -> dict:
        with self._lock:
            return {
                "cycles": self._cycles,
                "lastCleanReadTs": self._last_ok_ts,
                "accountsRead": self._accounts_ok,
                "accountsFailed": self._accounts_failed,
                "pendingRemoval": dict(self._misses),
                "foreignRows": len(self._foreign),
                "lastError": self._last_error,
                "intervalMs": int(RECONCILE_INTERVAL_S * 1000),
            }


reconciler = PositionReconciler()
