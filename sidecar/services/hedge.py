"""Automatic protective hedge for short option positions.

    broker confirms a SELL fill
            ↓
    HedgeManager.on_entry_fill
            ↓
    BUY <distance> further OTM, MARKET, no stop of its own

Why this is in the sidecar
--------------------------
Auto-hedge used to live entirely in the renderer (``OptionChainPanel``), and it
had three defects that only matter with real money:

1. **It fired on submission, not on a fill.** ``placeOrder`` resolving means the
   broker accepted the order for routing. If the short was then rejected at the
   exchange, the hedge went in anyway and the user was left long a leg they
   never wanted. Here it is driven by broker-confirmed filled quantity, from the
   same event that books the position.

2. **It died with the window.** A closed, reloaded or crashed renderer meant no
   hedge — for the one feature whose entire purpose is that the protection is
   there when you are not watching it.

3. **The hedge carried the profile's own Stop Loss and Target.** A protective
   long leg with a stop is not protection: the stop takes the hedge off and
   leaves the short naked, usually in exactly the move the hedge existed for.
   The hedge leg placed here carries **no risk rule at all**. It is removed by
   closing the position it protects, not by a stop of its own.

What it will not do
-------------------
* Hedge a BUY. Long options have defined risk already.
* Hedge an exit fill, or a fill on the hedge leg itself.
* Stack hedges. One protective leg per short contract, sized to the short.
* Invent a contract. A strike no connected broker lists is not hedged, and that
  is reported rather than silently skipped — an unhedged short the user believes
  is hedged is the worst outcome available.

Parent and child
----------------
A hedge is not an independent trade. It exists BECAUSE of a specific short, and
the link between the two is recorded here and written to disk, so it survives a
restart exactly as the position book does:

    NIFTY 24600 CE (short)  ──protected by──>  NIFTY 24700 CE (long)

That link is what lets the rest of the system behave sensibly when the parent
goes away. Closing the 24600 leaves the 24700 sitting there as a naked long
nobody chose to hold — so when the last short a hedge protects closes, Charticks
asks what should happen to it rather than either silently keeping it (a position
the user did not intend) or silently closing it (an exit they did not ask for).

A hedge can protect MORE THAN ONE short: sell the 24600 and later the 24650 with
the same hedge distance and both point at the 24700. Closing one of them leaves
the other still hedged, so nothing is asked and nothing is closed. Only the last
one orphans it.
"""
from __future__ import annotations

import json
import os
import threading
import time

import diagnostics
from bridge import events
from bridge.hub import hub
from services.instruments import InstrumentKey, instruments
from services.paths import data_dir

# Gap between consecutive attempts at a hedge leg. Long enough that a transient
# broker error has passed, short enough that the short is not naked for long.
RETRY_DELAY_S = 2.0

_LINKS_FILE = "hedge_links.json"


class HedgeManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._config = {"enabled": False, "distancePts": 0.0,
                        "retryFailed": True, "maxRetries": 3}
        # Source position key -> hedge position key, for the legs we have placed.
        # Keyed by the SHORT being protected, so a second fill that averages into
        # the same short does not open a second hedge. Persisted: a hedge whose
        # parent link is forgotten on restart becomes an ordinary-looking long
        # that nobody can explain.
        self._hedged: dict[str, str] = {}
        self._loaded = False

    # ── config, pushed by the renderer's active profile ───────────────────
    def set_config(self, cfg: dict) -> dict:
        with self._lock:
            self._config = {
                "enabled": bool(cfg.get("enabled")),
                "distancePts": float(cfg.get("distancePts") or 0),
                "retryFailed": bool(cfg.get("retryFailed", True)),
                "maxRetries": max(1, int(cfg.get("maxRetries") or 1)),
            }
            config = dict(self._config)
        diagnostics.event("orders", "Auto-hedge config", "success", **{
            "enabled": config["enabled"], "distancePts": config["distancePts"],
            "retryFailed": config["retryFailed"], "maxRetries": config["maxRetries"]})
        return {"ok": True, **config}

    @property
    def config(self) -> dict:
        with self._lock:
            return dict(self._config)

    # ── the parent-child link ─────────────────────────────────────────────
    @property
    def _path(self) -> str:
        return os.path.join(data_dir(), _LINKS_FILE)

    def _load(self) -> None:
        """Restore the links. Called lazily, so an unreadable file costs a log
        line rather than a failed import."""
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                if not os.path.exists(self._path):
                    return
                with open(self._path, encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict):
                    self._hedged = {str(k): str(v) for k, v in raw.items() if v}
            except Exception as exc:
                diagnostics.event("orders", "Hedge links restore", "failed",
                                  level="warn", reason=str(exc))
                return
        if self._hedged:
            diagnostics.event("orders", "Hedge links restore", "success",
                              links=len(self._hedged))

    def _persist(self) -> None:
        try:
            with self._lock:
                links = {k: v for k, v in self._hedged.items() if v}
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(links, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except OSError as exc:
            diagnostics.event("orders", "Hedge links persist", "failed",
                              level="warn", reason=str(exc))

    def hedge_of(self, parent_key: str) -> str | None:
        """The hedge protecting this position, if Charticks opened one."""
        self._load()
        with self._lock:
            return self._hedged.get(parent_key) or None

    def parents_of(self, hedge_key: str) -> list[str]:
        """Every short this hedge is protecting. More than one is normal."""
        self._load()
        with self._lock:
            return sorted(p for p, h in self._hedged.items() if h == hedge_key)

    def unlink_hedge(self, hedge_key: str) -> None:
        """Forget a hedge entirely — it has been closed, or the user has chosen
        to keep it as an ordinary position of their own."""
        self._load()
        with self._lock:
            for parent in [p for p, h in self._hedged.items() if h == hedge_key]:
                self._hedged.pop(parent, None)
        self._persist()

    def forget(self, position_key: str) -> None:
        """A position has closed.

        Two quite different cases, and telling them apart is the whole point of
        keeping the link:

        * **A hedge closed.** Drop every link pointing at it. The shorts it was
          protecting are now naked, which the monitoring alarm already surfaces.
        * **A parent short closed.** If it was the last short that hedge
          protected, the hedge is now an orphan — a long position nobody chose
          to hold on its own — so the user is asked what to do with it. If other
          shorts still point at it, nothing happens: it is still doing its job.
        """
        self._load()
        with self._lock:
            orphaned_hedge = self._hedged.pop(position_key, None)
            if orphaned_hedge:
                still_needed = [p for p, h in self._hedged.items()
                                if h == orphaned_hedge]
            else:
                still_needed = []
            # The closed position may itself BE a hedge.
            was_hedge_for = [p for p, h in self._hedged.items()
                             if h == position_key]
            for parent in was_hedge_for:
                self._hedged.pop(parent, None)
        if orphaned_hedge or was_hedge_for:
            self._persist()
        if was_hedge_for:
            diagnostics.event(
                "orders", "Hedge closed", "success", level="warn",
                hedge=position_key, protected=", ".join(was_hedge_for),
                reason="the shorts it was protecting are no longer hedged")
        if orphaned_hedge and not still_needed:
            self._offer_orphan(position_key, orphaned_hedge)
        elif orphaned_hedge:
            diagnostics.event(
                "orders", "Hedge retained", "success", hedge=orphaned_hedge,
                closedParent=position_key, stillProtecting=", ".join(still_needed),
                reason="another short still relies on this hedge")

    def _offer_orphan(self, parent_key: str, hedge_key: str) -> None:
        """Ask the user what to do with a hedge whose last parent has closed.

        Deliberately a question, not a policy. Closing it automatically would be
        an exit nobody asked for; keeping it silently would leave a long position
        the user never intended to hold on its own. Only they know which they
        meant, and the position is not going anywhere while they decide.
        """
        from services.live_book import live_book

        hedge = live_book.get(hedge_key)
        if hedge is None or hedge.qty <= 0:
            return  # already gone — nothing to decide
        if hedge.exit_pending_qty > 0:
            # The hedge is already on its way out — a Square Off All closes the
            # short and the hedge together, and the short's close would otherwise
            # raise a question about a position that is being closed anyway.
            return
        diagnostics.event(
            "orders", "Hedge orphaned", "success", level="warn",
            hedge=hedge.symbol, closedParent=parent_key,
            qty=hedge.qty, reason="its last protected short has closed")
        hub.publish(events.hedge_orphaned(
            hedge_id=hedge_key, symbol=hedge.symbol, qty=hedge.qty,
            lots=hedge.lots, parent=parent_key, pnl=round(hedge.pnl(), 2)))

    def resolve_orphan(self, hedge_key: str, action: str) -> dict:
        """Apply the user's decision about an orphaned hedge."""
        from services.live_book import live_book
        from services.live_manager import live_manager

        hedge = live_book.get(hedge_key)
        if hedge is None or hedge.qty <= 0:
            self.unlink_hedge(hedge_key)
            return {"ok": True, "detail": "That hedge is no longer open."}
        if action == "close":
            diagnostics.event("orders", "Hedge orphan", "closing", level="warn",
                              hedge=hedge.symbol, qty=hedge.qty)
            result = live_manager.close_position(hedge_key, 1.0)
            if result.get("ok"):
                self.unlink_hedge(hedge_key)
            return result
        # Keep: it becomes an ordinary position of the user's own, managed on
        # the same terms as any other. The link is dropped so it is never again
        # treated as somebody else's protection.
        self.unlink_hedge(hedge_key)
        diagnostics.event("orders", "Hedge orphan", "kept", hedge=hedge.symbol,
                          qty=hedge.qty,
                          reason="user chose to hold it as a position of its own")
        return {"ok": True, "detail": f"{hedge.symbol} is now an ordinary position."}

    # ── the trigger ───────────────────────────────────────────────────────
    def on_entry_fill(self, underlying: str, expiry: str, strike: float,
                      opt_type: str, side: str, qty: int, lots: int,
                      product: str) -> None:
        """A broker-confirmed ENTRY fill landed. Hedge it if it is a short.

        Never raises into the caller: this runs on the order-sync path, and an
        exception here would stop the fill being processed at all — which would
        cost the user their position book to protect a hedge.
        """
        try:
            self._hedge(underlying, expiry, strike, opt_type, side, qty, lots, product)
        except Exception as exc:
            diagnostics.exception("orders", "Auto-hedge failed", exc_info=exc,
                                  symbol=f"{underlying} {expiry} {int(strike)} {opt_type}")

    def _hedge(self, underlying: str, expiry: str, strike: float, opt_type: str,
               side: str, qty: int, lots: int, product: str) -> None:
        config = self.config
        if not config["enabled"] or side != "SELL" or qty <= 0:
            return
        if config["distancePts"] <= 0:
            return

        from services import market_data
        from services.live_book import LiveBook

        self._load()
        source_key = LiveBook.key_for(underlying, expiry, strike, opt_type)
        with self._lock:
            if source_key in self._hedged:
                return  # this short already has its protective leg
            # Claimed before routing, so a second fill arriving while the first
            # hedge is in flight cannot open a second one. The empty string means
            # "in flight"; it becomes the hedge's key once placement succeeds.
            self._hedged[source_key] = ""

        step = market_data.strike_steps().get(underlying, 50)
        steps_away = max(1, round(config["distancePts"] / step))
        # Further OUT of the money: a short call is protected by a higher strike,
        # a short put by a lower one.
        hedge_strike = (int(strike) + steps_away * step if opt_type == "CE"
                        else int(strike) - steps_away * step)
        target = InstrumentKey.option(underlying, expiry, hedge_strike, opt_type)

        if hedge_strike <= 0 or not instruments.has(target):
            with self._lock:
                self._hedged.pop(source_key, None)
            self._report_unhedged(
                underlying, expiry, strike, opt_type, hedge_strike,
                f"no connected broker lists the {hedge_strike} {opt_type} hedge strike")
            return

        attempts = config["maxRetries"] if config["retryFailed"] else 1
        threading.Thread(
            target=self._place, name="auto-hedge", daemon=True,
            args=(source_key, target, underlying, expiry, hedge_strike, opt_type,
                  qty, lots, product, attempts, strike)).start()

    def _place(self, source_key: str, target: InstrumentKey, underlying: str,
               expiry: str, hedge_strike: int, opt_type: str, qty: int, lots: int,
               product: str, attempts: int, short_strike: float) -> None:
        """Route the hedge, off the fill-processing thread.

        Placement blocks on a broker SDK, and holding up the order-sync cycle
        would delay every other order's state — including the stop-loss exits
        this hedge is standing beside.
        """
        from services.order_manager import LIVE, order_manager

        for attempt in range(1, attempts + 1):
            result = order_manager.place_order(
                LIVE, underlying, expiry, float(hedge_strike), opt_type, "BUY",
                int(qty), "MARKET", 0.0, lots=int(lots),
                # NO RISK RULE. A protective leg with its own stop is not
                # protection — the stop removes the hedge and leaves the short
                # naked. It is closed with the position it protects.
                rule=None,
                product=product, validity="DAY",
                # Same exposure decision the short already passed; a position cap
                # must not be the reason a short ends up unhedged.
                override_max_pos=True, allow_duplicate=True,
                request_id=f"hedge:{source_key}")
            if result.get("ok"):
                with self._lock:
                    self._hedged[source_key] = target.position_id
                self._persist()
                diagnostics.event(
                    "orders", "Auto-hedge", "success", symbol=str(target),
                    protects=f"{underlying} {expiry} {int(short_strike)} {opt_type}",
                    side="BUY", qty=qty, attempt=attempt, product=product)
                hub.publish(events.log_line(
                    "info", f"[hedge] bought {qty} {target} to protect the short "
                            f"{int(short_strike)} {opt_type}"))
                return
            detail = str(result.get("error") or result.get("code") or "rejected")
            diagnostics.event(
                "orders", "Auto-hedge", "failed", level="warn", symbol=str(target),
                attempt=attempt, attempts=attempts, reason=detail)
            if attempt < attempts:
                time.sleep(RETRY_DELAY_S)

        with self._lock:
            self._hedged.pop(source_key, None)
        self._report_unhedged(underlying, expiry, short_strike, opt_type,
                              hedge_strike,
                              f"every one of {attempts} attempt(s) was refused")

    @staticmethod
    def _report_unhedged(underlying: str, expiry: str, strike: float,
                         opt_type: str, hedge_strike: int, reason: str) -> None:
        """Say it out loud. A short the user believes is hedged and is not is
        strictly more dangerous than one they know is naked, so this is an ERROR
        in the log AND a message on screen — never a silent skip."""
        symbol = f"{underlying} {expiry} {int(strike)} {opt_type}"
        diagnostics.event(
            "orders", "Auto-hedge", "unhedged", level="error", symbol=symbol,
            hedgeStrike=hedge_strike, reason=reason)
        hub.publish(events.log_line(
            "error", f"[hedge] {symbol} is SHORT and NOT HEDGED — {reason}. "
                     f"Place the protective leg yourself or close the short."))


hedge_manager = HedgeManager()
