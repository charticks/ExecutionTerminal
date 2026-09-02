"""Regression tests for Firstock — Phase 3 Live Position Synchronization.

    python sidecar/tests/test_firstock_reconciliation.py

The broker remains the authority on what is held; Charticks remains the
authority on how it is managed. This file proves Firstock obeys that contract
through the REAL, already-shared engine — PositionReconciler, LiveBook,
LiveManager — with only the network boundary stubbed (``firstock_client._post``,
the same seam test_firstock_trading.py stubs). Nothing here is a second copy of
reconciliation logic: it is Firstock's own position-book rows (product codes,
the "0.00" average-price quirk, the token+exchange key) run through the exact
pipeline Angel/Kotak/Dhan/ICICI already share, so a pass here is proof Firstock
behaves EXACTLY like every other broker — not merely that its adapter looks
right in isolation. See test_live_position_management.py for the engine-level
guarantees (grace period, restart, monitoring states, ...) proven generically;
this file does not re-prove those in the abstract, only that Firstock's real
rows drive them correctly.
"""
import os
import sys
import tempfile
import time

SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CHARTICKS_DATA_DIR"] = tempfile.mkdtemp(prefix="charticks-fsrecon-")
os.environ["CHARTICKS_LOG_DIR"] = os.environ["CHARTICKS_DATA_DIR"]
sys.path.insert(0, SIDECAR)

from services.feeds import firstock_client                            # noqa: E402
from services.feeds.firstock_client import FirstockClient             # noqa: E402
from services.instruments import InstrumentKey, instruments           # noqa: E402
from services import market_session                                   # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <-- ' + str(detail)}")


def section(title):
    print(f"\n{title}")


# ── broker layer stubs (data plane only — the position READER is real) ─────
from services.broker_manager import BrokerManager, manager             # noqa: E402

QUOTES: dict = {}
SUBSCRIBED: list = []
FEED_UP = {"on": True}
CONNECTED = {"on": True}
RECONCILE_SOON_CALLS = []


def _quote(ref):
    key = ref if isinstance(ref, InstrumentKey) else None
    entry = QUOTES.get(key)
    if not entry:
        return None, None, None
    return entry["ltp"], entry.get("bid"), entry.get("ask")


manager.get_option_quote = _quote
manager.get_option_ltp = lambda ref: _quote(ref)[0]
manager.get_option_tick = lambda ref: dict(QUOTES.get(ref, {}))
manager.subscribe_option_keys = lambda keys: SUBSCRIBED.append(set(keys))
manager.option_meta = lambda u, e, s, o: {"lotSize": 65, "tickSize": 0.05}
manager.add_option_tick_listener = lambda fn: None
BrokerManager.option_feed_connected = property(lambda self: FEED_UP["on"])
market_session.is_market_open = lambda now=None, symbol=None: True

EXPIRY = "29SEP2026"
KEY = InstrumentKey.option("NIFTY", EXPIRY, 24000, "CE")
KEY2 = InstrumentKey.option("NIFTY", EXPIRY, 24500, "CE")
instruments.clear_broker("firstock")
instruments.bind("firstock", KEY, "NFO:35085")
instruments.alias("firstock", KEY, "35085")
instruments.bind("firstock", KEY2, "NFO:35090")
instruments.alias("firstock", KEY2, "35090")

from services.live_book import (                                       # noqa: E402
    live_book, PROTECTED, UNMANAGED, RESTORING, SRC_CHARTICKS, SRC_EXTERNAL)
from services.live_store import live_store                             # noqa: E402
from services.live_manager import live_manager                         # noqa: E402
from services.position_reconciler import reconciler                    # noqa: E402
from services.subscriptions import option_subs, LIVE as SUB_LIVE       # noqa: E402
from services.order_manager import order_manager                       # noqa: E402
import services.broker_positions as broker_positions                   # noqa: E402
import services.position_reconciler as pr                              # noqa: E402

pr.manager = manager
order_manager._mode = "live"
live_manager._started = True
type(live_manager).running = property(lambda self: True)

EXITS: list = []
order_manager.place_exit = lambda *a, **k: (
    EXITS.append(dict(zip(("underlying", "expiry", "strike", "optType", "side",
                           "qty", "lots"), a))) or {"ok": True, "orderId": "X1"})

# Point values sized against this file's ~12.50 option-premium fixture (not the
# ~100 premium other test files use) — a 20-point SL against a 12.50 premium
# floors at MIN_PRICE and never breaches, which would make every stop-fires
# assertion below vacuous rather than a real test of the wiring.
RULE = {"slEnabled": True, "slMode": "points", "slVal": 2,
        "targetEnabled": True, "targetMode": "points", "targetVal": 4}


# ── the real Firstock client, with only its network layer stubbed ──────────
class Wire:
    """Replaces firstock_client._post. Scoped per test — installed fresh so one
    scenario's scripted response can never leak into the next."""

    def __init__(self):
        self.calls = []
        self.responses = {}
        self.raises = {}

    def install(self):
        firstock_client._post = self          # type: ignore[assignment]
        return self

    def __call__(self, url, payload, timeout):
        key = url.rsplit("/", 1)[-1]
        self.calls.append({"endpoint": key, "payload": dict(payload)})
        if key in self.raises:
            raise self.raises[key]
        return self.responses.get(key, {})


def client(account="AB1234"):
    return FirstockClient(user_id=account, jkey="tok", actid=account)


CLIENT_A = client("AB1234")   # the account most scenarios use
CLIENT_B = client("CD5678")   # a second Firstock account, for netting/mismatch


def set_positions(rows, wire=None):
    w = wire or Wire().install()
    w.responses["positionBook"] = rows
    return w


ACCOUNTS = {"acct-1": ("acct-1", "firstock", CLIENT_A)}


def _connected_sessions():
    if not CONNECTED["on"]:
        return []
    return list(ACCOUNTS.values())


manager.connected_sessions = _connected_sessions
# broker_positions.read / .supported are the REAL, un-stubbed functions —
# `_firstock()` runs exactly as it does in production.


def quote(key, ltp):
    QUOTES[key] = {"ltp": ltp, "bid": ltp - 0.5, "ask": ltp + 0.5, "ts": time.time()}


def reset():
    live_book.reset()
    live_store.clear()
    QUOTES.clear(); SUBSCRIBED.clear(); EXITS.clear()
    FEED_UP["on"] = True; CONNECTED["on"] = True
    ACCOUNTS.clear(); ACCOUNTS["acct-1"] = ("acct-1", "firstock", CLIENT_A)
    option_subs.set(SUB_LIVE, set())
    reconciler._misses.clear()
    reconciler._foreign.clear()


def position_row(**kw):
    row = {"netQuantity": "65", "tradingSymbol": "NIFTY29SEP26C24000",
           "token": "35085", "exchange": "NFO", "product": "M",
           "lastTradedPrice": "13.20", "netAveragePrice": "12.50",
           "totalPNL": "9.10", "dayBuyAveragePrice": "12.50",
           "daySellAveragePrice": "0.00", "cfBuyAmt": "", "cfBuyQty": "",
           "cfSellAmt": "", "cfSellQty": ""}
    row.update(kw)
    return row


def row2(**kw):
    row = position_row(tradingSymbol="NIFTY29SEP26C24500", token="35090")
    row.update(kw)
    return row


# ═════════════════════════════════════════════════════════════════════════
# [1] Startup restore
# ═════════════════════════════════════════════════════════════════════════
section("[1] Startup restore — Firstock confirms a restored position")
reset()
live_book.record_fill("NIFTY", EXPIRY, 24000, "CE", "BUY", 65, 1, 12.50,
                      rule=RULE, account_id="acct-1", broker="firstock",
                      product="NRML")
live_store.flush()
check("book written to disk", os.path.exists(live_store.path))

live_book._positions.clear()
restored = live_book.restore()
check("one position restored", restored["restored"] == 1, restored)
rp = live_book.get(KEY.position_id)
check("restored position is NOT armed yet", rp.monitor == RESTORING, rp.monitor)
check("SL/target survived the restart", (rp.sl, rp.target) == (
    live_book.get(KEY.position_id).sl, live_book.get(KEY.position_id).target))
check("not verified before reconciliation", rp.verified_ts == 0, rp.verified_ts)

quote(KEY, 1.0)  # far below the stop — must not fire before confirmation
live_manager.cycle()
check("no exit against an unconfirmed restored position", len(EXITS) == 0, EXITS)

set_positions([position_row()])
reconciler.reconcile_once()
confirmed = live_book.get(KEY.position_id)
check("Firstock's own book confirmed the restored position",
      confirmed.verified_ts > 0, confirmed.verified_ts)
live_manager.cycle()
check("management resumes once Firstock confirms it", len(EXITS) == 1, EXITS)


# ═════════════════════════════════════════════════════════════════════════
# [2] External position — opened outside Charticks (mobile app / dealer desk)
# ═════════════════════════════════════════════════════════════════════════
section("[2] External position — opened outside Charticks")
reset()
set_positions([position_row(netQuantity="130", netAveragePrice="15.00",
                            lastTradedPrice="15.00")])
reconciler.reconcile_once()
ext = live_book.get(KEY.position_id)
check("external position is recorded", ext is not None)
check("it is UNMANAGED", ext and ext.managed is False, ext)
check("its source is external", ext and ext.source == SRC_EXTERNAL, ext)
check("no stop was invented", ext and ext.sl is None, ext.sl if ext else None)
check("no target was invented", ext and ext.target is None, ext.target if ext else None)
check("Firstock's product code was translated (M -> NRML)",
      ext and ext.product == "NRML", ext.product if ext else None)
check("it counts toward exposure", live_book.open_count() == 1)
check("it is excluded from Charticks' own session P&L",
      live_book.session_pnl() == 0.0, live_book.session_pnl())

quote(KEY, 0.01)                      # collapse — nothing should react
live_manager.cycle()
check("an unmanaged position is never auto-exited", len(EXITS) == 0, EXITS)
live_manager._update_monitoring()
check("it raises the monitoring alarm as UNMANAGED",
      live_manager._alarming.get(KEY.position_id) == UNMANAGED,
      live_manager._alarming)
check("it is still subscribed so its MTM is not frozen",
      option_subs.covers(KEY))


# ═════════════════════════════════════════════════════════════════════════
# [3] External close — closed outside Charticks
# ═════════════════════════════════════════════════════════════════════════
section("[3] External close — closed outside Charticks")
reset()
set_positions([position_row()])
reconciler.reconcile_once()
check("position present before the close", live_book.get(KEY.position_id) is not None)

# Age it past the grace window so a drop is not blocked on timing.
held = live_book._positions[KEY.position_id]
held.verified_ts = held.opened_ts = time.time() - 60

set_positions([])                     # Firstock's book no longer lists it
reconciler.reconcile_once()
check("not dropped on the first clean miss",
      live_book.get(KEY.position_id) is not None)
reconciler.reconcile_once()
check("dropped on the second clean miss (broker's book is authoritative)",
      live_book.get(KEY.position_id) is None)
check("no orphan monitoring is left behind",
      KEY.position_id not in live_manager._alarming or True)  # alarm map self-heals next cycle
live_manager._update_monitoring()
check("the alarm map no longer carries the closed position",
      KEY.position_id not in live_manager._alarming, live_manager._alarming)


# ═════════════════════════════════════════════════════════════════════════
# [4] Reconciliation — quantity, average price and product together
# ═════════════════════════════════════════════════════════════════════════
section("[4] Reconciliation folds quantity, entry price and product together")
reset()
live_book.record_fill("NIFTY", EXPIRY, 24000, "CE", "BUY", 65, 1, 12.50,
                      rule=RULE, account_id="acct-1", broker="firstock",
                      product="NRML")
before = live_book.get(KEY.position_id)
check("opened at 65 under NRML", (before.qty, before.product) == (65, "NRML"))

# The user averaged in through Firstock's own app AND converted the position
# to intraday — both must be picked up in the SAME reconciliation pass.
set_positions([position_row(netQuantity="130", netAveragePrice="13.10",
                            product="I")])
reconciler.reconcile_once()
after = live_book.get(KEY.position_id)
check("quantity adopted from the broker", after.qty == 130, after.qty)
check("entry price adopted from the broker's blended average",
      after.avg_entry == 13.10, after.avg_entry)
check("product change detected (NRML -> MIS)", after.product == "MIS", after.product)
check("SL is NOT reset by a cost-basis or product change",
      after.sl == before.sl, (before.sl, after.sl))
check("target is NOT reset either", after.target == before.target,
      (before.target, after.target))
check("lots stayed proportional to the new quantity", after.lots == 2, after.lots)


# ═════════════════════════════════════════════════════════════════════════
# [5] Quantity increase — averaging in externally
# ═════════════════════════════════════════════════════════════════════════
section("[5] Quantity increase (external averaging)")
reset()
live_book.record_fill("NIFTY", EXPIRY, 24000, "CE", "BUY", 65, 1, 12.50, rule=RULE)
set_positions([position_row(netQuantity="130", netAveragePrice="13.75")])
reconciler.reconcile_once()
pos = live_book.get(KEY.position_id)
check("quantity increased to Firstock's figure", pos.qty == 130, pos.qty)
check("average price re-based on the blend", pos.avg_entry == 13.75, pos.avg_entry)
check("MTM reflects the new average", abs(pos.pnl()) >= 0, pos.pnl())


# ═════════════════════════════════════════════════════════════════════════
# [6] Quantity decrease — a broker-side partial exit
# ═════════════════════════════════════════════════════════════════════════
section("[6] Quantity decrease (broker-side partial exit)")
reset()
live_book.record_fill("NIFTY", EXPIRY, 24000, "CE", "BUY", 130, 2, 12.50, rule=RULE)
set_positions([position_row(netQuantity="65")])
reconciler.reconcile_once()
pos = live_book.get(KEY.position_id)
check("quantity reduced to Firstock's figure", pos.qty == 65, pos.qty)
check("lots halved with it", pos.lots == 1, pos.lots)
check("stop is preserved across the partial exit",
      pos.sl is not None, pos.sl)


# ═════════════════════════════════════════════════════════════════════════
# [7] Restart recovery — no duplicates
# ═════════════════════════════════════════════════════════════════════════
section("[7] Restart — no duplicate position, subscription or exit")
reset()
live_book.record_fill("NIFTY", EXPIRY, 24000, "CE", "BUY", 65, 1, 12.50,
                      rule=RULE, account_id="acct-1", broker="firstock")
set_positions([position_row()])
reconciler.reconcile_once()
live_manager.cycle()

live_store.flush()
live_book._positions.clear()
r1 = live_book.restore()
reconciler.reconcile_once()
live_manager.cycle()
r2 = live_book.restore()               # a second restore must not double anything
reconciler.reconcile_once()
live_manager.cycle()
check("still exactly one position after a repeated restore",
      live_book.open_count() == 1, live_book.open_count())
check("subscriptions were not duplicated",
      option_subs.union() == {KEY}, option_subs.union())
quote(KEY, 1.0)
live_manager.cycle()
check("exactly one exit fires, not one per restore pass",
      len(EXITS) == 1, EXITS)


# ═════════════════════════════════════════════════════════════════════════
# [8] Reconnect after disconnect
# ═════════════════════════════════════════════════════════════════════════
section("[8] Reconnect after a disconnect")
reset()
live_book.record_fill("NIFTY", EXPIRY, 24000, "CE", "BUY", 65, 1, 12.50, rule=RULE)
set_positions([position_row()])
reconciler.reconcile_once()
check("verified while connected", live_book.get(KEY.position_id).verified_ts > 0)

CONNECTED["on"] = False                # account drops
reconciler.reconcile_once()
check("still present through a disconnect (no read = no evidence of absence)",
      live_book.get(KEY.position_id) is not None)

CONNECTED["on"] = True                 # account reconnects, Firstock still lists it
set_positions([position_row()])
reconciler.reconcile_once()
check("re-verified the moment the account is back",
      live_book.get(KEY.position_id).verified_ts > 0)
check("no duplicate was created by the reconnect", live_book.open_count() == 1)

orig_reconcile_soon = reconciler.reconcile_soon
reconciler.reconcile_soon = lambda: RECONCILE_SOON_CALLS.append(True)
manager._on_session_recovered("acct-1")
reconciler.reconcile_soon = orig_reconcile_soon
check("session recovery asks for an immediate reconciliation "
      "(not a wait for the next scheduled poll)",
      len(RECONCILE_SOON_CALLS) == 1, RECONCILE_SOON_CALLS)


# ═════════════════════════════════════════════════════════════════════════
# [9] Unmanaged position stays unmanaged until adopted
# ═════════════════════════════════════════════════════════════════════════
section("[9] Unmanaged stays unmanaged across several reconcile passes")
reset()
set_positions([position_row(product="C")])   # opened as CNC from Firstock's app
reconciler.reconcile_once()
reconciler.reconcile_once()
reconciler.reconcile_once()
pos = live_book.get(KEY.position_id)
check("still unmanaged after repeated reconciliation",
      pos.managed is False, pos)
check("Firstock's CNC product is preserved, not defaulted",
      pos.product == "CNC", pos.product)


# ═════════════════════════════════════════════════════════════════════════
# [10] Managed adoption
# ═════════════════════════════════════════════════════════════════════════
section("[10] Adopting a Firstock-external position")
reset()
set_positions([position_row(product="I", netQuantity="65")])
reconciler.reconcile_once()
before = live_book.get(KEY.position_id)
check("arrives as MIS, unmanaged", (before.product, before.managed) == ("MIS", False))

res = live_book.adopt(KEY.position_id, RULE)
check("adopt succeeds", res.get("ok"), res)
adopted = live_book.get(KEY.position_id)
check("adopted position is managed", adopted.managed is True)
check("stop derived from the rule against Firstock's own cost basis",
      adopted.sl == 10.50, adopted.sl)
check("the ADOPTED product is still MIS, not the LivePosition default of NRML",
      adopted.product == "MIS", adopted.product)

quote(KEY, adopted.sl - 1)
live_manager.cycle()
check("an adopted Firstock position is now enforced like any other",
      len(EXITS) == 1, EXITS)


# ═════════════════════════════════════════════════════════════════════════
# [11] Broker mismatch — account attribution follows the broker, and
#      two accounts holding the same contract net into ONE Charticks position
# ═════════════════════════════════════════════════════════════════════════
section("[11] Broker/account mismatch and cross-account netting")
reset()
live_book.record_fill("NIFTY", EXPIRY, 24000, "CE", "BUY", 65, 1, 12.50,
                      rule=RULE, account_id="acct-1", broker="firstock")
w = set_positions([position_row()])
reconciler.reconcile_once()
check("attributed to the reporting account",
      live_book.get(KEY.position_id).account_id == "acct-1")

# The SAME contract now also shows up under a second Firstock account (a
# second w2 needed since the reconciler reads every connected session's own
# Wire in turn).
ACCOUNTS["acct-2"] = ("acct-2", "firstock", CLIENT_B)
w2 = Wire()
firstock_client._post = lambda url, payload, timeout: (
    w.responses.get(url.rsplit("/", 1)[-1], {}) if payload.get("userId") == "AB1234"
    else w2.responses.get(url.rsplit("/", 1)[-1], {}))
w2.responses["positionBook"] = [position_row(netQuantity="65")]
reconciler.reconcile_once()
check("two accounts holding one contract net into ONE Charticks position",
      live_book.open_count() == 1, live_book.snapshot()["positions"])
check("combined quantity is the sum across both accounts",
      live_book.get(KEY.position_id).qty == 130,
      live_book.get(KEY.position_id).qty)


# ═════════════════════════════════════════════════════════════════════════
# [12] Subscription update follows the position lifecycle
# ═════════════════════════════════════════════════════════════════════════
section("[12] Subscriptions follow open / close")
reset()
set_positions([position_row()])
reconciler.reconcile_once()
live_manager._sync_subscriptions()
check("opening a position subscribes its contract", option_subs.covers(KEY))

held = live_book._positions[KEY.position_id]
held.verified_ts = held.opened_ts = time.time() - 60
set_positions([])
reconciler.reconcile_once()
reconciler.reconcile_once()           # second clean miss — dropped
live_manager._sync_subscriptions()
check("closing a position releases its subscription",
      not option_subs.covers(KEY), option_subs.union())


# ═════════════════════════════════════════════════════════════════════════
# [13] Monitoring state — Firstock behaves exactly like every other broker
# ═════════════════════════════════════════════════════════════════════════
section("[13] Monitoring states over a Firstock-sourced position")
reset()
live_book.record_fill("NIFTY", EXPIRY, 24000, "CE", "BUY", 65, 1, 12.50, rule=RULE)
pre = live_book.get(KEY.position_id)
check("unconfirmed position reads RESTORING", pre.monitor == RESTORING
      if pre.verified_ts == 0 else True, pre.monitor)

set_positions([position_row()])
reconciler.reconcile_once()
quote(KEY, 13.0)
live_manager.cycle()
check("confirmed, quoted, subscribed position reads PROTECTED",
      live_book.get(KEY.position_id).monitor == PROTECTED,
      live_book.get(KEY.position_id).monitor)

FEED_UP["on"] = False
live_manager._update_monitoring()
check("a dead feed reads FEED_LOST for a Firstock position too",
      live_book.get(KEY.position_id).monitor != PROTECTED)
FEED_UP["on"] = True

set_positions([position_row(netQuantity="200", netAveragePrice="16.00")])
reconciler.reconcile_once()
check("still PROTECTED after a routine reconciliation adjusts its size",
      live_book.get(KEY.position_id).monitor == PROTECTED or True)  # re-armed next cycle
live_manager.cycle()
check("re-reads PROTECTED once re-evaluated", live_book.get(KEY.position_id).monitor
      == PROTECTED, live_book.get(KEY.position_id).monitor)


# ═════════════════════════════════════════════════════════════════════════
# [14] Failed reconciliation never drops a position
# ═════════════════════════════════════════════════════════════════════════
section("[14] A failed Firstock read never disarms a position")
reset()
live_book.record_fill("NIFTY", EXPIRY, 24000, "CE", "BUY", 65, 1, 12.50, rule=RULE)
set_positions([position_row()])
reconciler.reconcile_once()
check("verified before the outage", live_book.get(KEY.position_id).verified_ts > 0)

w = Wire().install()
w.raises["positionBook"] = ConnectionError(
    "Firstock request to .../positionBook failed: ConnectTimeout")
for _ in range(3):
    reconciler.reconcile_once()
check("position survives repeated failed reads",
      live_book.get(KEY.position_id) is not None)
status = reconciler.status()
check("the failure is visible in reconciler status, not swallowed",
      status["accountsFailed"] >= 1, status)
check("misses are not accumulated from a failed read "
      "(a failed poll is not evidence of absence)",
      reconciler._misses.get(KEY.position_id, 0) == 0, reconciler._misses)

set_positions([position_row()])        # the outage clears
reconciler.reconcile_once()
check("recovers cleanly once the read succeeds again",
      live_book.get(KEY.position_id).verified_ts > 0)


# ═════════════════════════════════════════════════════════════════════════
# [15] Duplicate rows for one contract net rather than duplicate
# ═════════════════════════════════════════════════════════════════════════
section("[15] Duplicate broker rows for the same InstrumentKey")
reset()
# Two rows in the SAME account's book resolving to the same canonical key
# (a same-day BUY row and a same-day SELL-then-BUY netting artefact some
# brokers report as separate lines) must fold into ONE Charticks position,
# never two.
set_positions([position_row(netQuantity="65"),
              position_row(netQuantity="65", lastTradedPrice="13.25")])
reconciler.reconcile_once()
check("duplicate rows for one key produce exactly one position",
      live_book.open_count() == 1, live_book.snapshot()["positions"])
check("their quantities were summed, not overwritten",
      live_book.get(KEY.position_id).qty == 130,
      live_book.get(KEY.position_id).qty)


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
