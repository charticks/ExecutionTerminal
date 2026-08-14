"""Regression tests for the production sign-off fixes.

    python sidecar/tests/test_production_hardening.py

Each scenario below is a defect found during the pre-live audit, in the shape
that made it dangerous rather than in the shape that made it easy to test. They
share the style of test_live_position_management.py: real sidecar modules, only
the broker layer stubbed, no pytest — so a tester can run them on the packaged
runtime.
"""
import datetime as dt
import os
import sys
import tempfile
import time

SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CHARTICKS_DATA_DIR"] = tempfile.mkdtemp(prefix="charticks-hardening-")
os.environ["CHARTICKS_LOG_DIR"] = os.environ["CHARTICKS_DATA_DIR"]
sys.path.insert(0, SIDECAR)

from services.instruments import InstrumentKey, instruments        # noqa: E402
from services import market_session                                # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <-- ' + str(detail)}")


# ── broker layer stubs ──────────────────────────────────────────────────────
from services.broker_manager import BrokerManager, manager          # noqa: E402

QUOTES, EXITS, PLACED = {}, [], []
FEED_UP = {"on": True}

KEY = InstrumentKey.option("NIFTY", "28AUG2026", 25000, "CE")
HEDGE_KEY = InstrumentKey.option("NIFTY", "28AUG2026", 25100, "CE")
ROLL_KEY = InstrumentKey.option("NIFTY", "28AUG2026", 25200, "CE")
for k in (KEY, HEDGE_KEY, ROLL_KEY):
    instruments.bind("dhan", k, f"DHAN-{k.strike}")


def _quote(ref):
    entry = QUOTES.get(ref if isinstance(ref, InstrumentKey) else None)
    if not entry:
        return None, None, None
    return entry["ltp"], entry.get("bid"), entry.get("ask")


manager.get_option_quote = _quote
manager.get_option_ltp = lambda ref: _quote(ref)[0]
manager.get_option_tick = lambda ref: dict(QUOTES.get(ref, {}))
manager.subscribe_option_keys = lambda keys: None
manager.option_meta = lambda u, e, s, o: {"lotSize": 75, "tickSize": 0.05}
manager.connected_sessions = lambda: [("acct-1", "dhan", object())]
manager.add_option_tick_listener = lambda fn: None
BrokerManager.option_feed_connected = property(lambda self: FEED_UP["on"])
market_session.is_market_open = lambda now=None, symbol=None: True

import services.broker_positions as broker_positions               # noqa: E402

BROKER_ROWS = []


class Row:
    def __init__(self, key, side, qty, avg):
        self.account_id, self.broker = "acct-1", "dhan"
        self.raw_id = self.symbol = str(key)
        self.side, self.qty, self.avg_entry = side, qty, avg
        self.ltp, self.pnl, self.key, self.product = 0.0, 0.0, key, "NRML"

    @property
    def id(self):
        return self.key.position_id


broker_positions.read = lambda a, b, s: list(BROKER_ROWS)
broker_positions.supported = lambda b: True

from services.live_book import live_book                            # noqa: E402
from services.live_store import live_store                          # noqa: E402
from services.live_manager import live_manager, ROLL_TIMEOUT_S      # noqa: E402
from services.order_manager import order_manager                    # noqa: E402
from services.order_sync import order_sync                          # noqa: E402
from services.order_sync.base import BrokerOrder, FILLED, PENDING    # noqa: E402
from services.order_sync import engine as sync_engine               # noqa: E402
from services.hedge import hedge_manager                            # noqa: E402
import services.position_reconciler as pr                           # noqa: E402

pr.manager = manager
order_manager._mode = "live"
live_manager._started = True
type(live_manager).running = property(lambda self: True)

order_manager.place_exit = lambda *a, **k: (
    EXITS.append(dict(zip(("underlying", "expiry", "strike", "optType", "side",
                           "qty", "lots"), a))) or {"ok": True, "orderId": "X1"})


def _place(mode, underlying, expiry, strike, opt_type, side, qty, order_type,
           price, lots=0, rule=None, product="NRML", validity="DAY", **kw):
    PLACED.append({"underlying": underlying, "expiry": expiry, "strike": strike,
                   "optType": opt_type, "side": side, "qty": qty, "lots": lots,
                   "rule": rule, "product": product, "orderType": order_type,
                   "requestId": kw.get("request_id", "")})
    return {"ok": True, "orderId": f"O{len(PLACED)}", "symbol": f"{underlying}{strike}"}


order_manager.place_order = _place

RULE = {"slEnabled": True, "slMode": "points", "slVal": 20,
        "targetEnabled": True, "targetMode": "points", "targetVal": 40}


def quote(key, ltp):
    QUOTES[key] = {"ltp": ltp, "bid": ltp - 0.5, "ask": ltp + 0.5, "ts": time.time()}


def reset():
    live_book.reset()
    live_store.clear()
    order_sync.reset()
    QUOTES.clear(); EXITS.clear(); PLACED.clear(); BROKER_ROWS.clear()
    live_manager._pending_rolls.clear()
    hedge_manager._hedged.clear()
    hedge_manager.set_config({"enabled": False})


def track(order_id, side="BUY", qty=75, rule=None, exit_for="", strike=25000):
    return order_sync.track(order_id, "acct-1", "dhan", "NIFTY", "28AUG2026",
                            strike, "CE", side, qty, 75, 100.0, rule=rule,
                            exit_for=exit_for, order_type="LIMIT")


def row(order_id, status, filled=0, qty=75):
    return BrokerOrder(order_id=order_id, status=status, filled_qty=filled,
                       avg_price=100.0, raw_status=status)


# ── 1. A resting limit order keeps its tracking ─────────────────────────────
print("\n1. A resting live order is not abandoned for being old")
reset()
order = track("R1")
# Two hours old — well past the 15-minute limit this used to have.
order.created_ts = time.time() - 2 * 3600
order_sync._expire_stale([order])
check("a two-hour-old resting order is still tracked", order_sync.known("R1"))
check("and can still be modified or cancelled",
      [o.order_id for o in order_sync.resolve("R1")] == ["R1"])

# Still present in the broker's book, however many times we look.
for _ in range(10):
    order_sync.ingest("dhan", "acct-1", [row("R1", PENDING)])
check("a book that still lists it never gives up", order_sync.known("R1"))

print("   and its later fill is still booked (the reason this matters)")
quote(KEY, 100.0)
order_sync.ingest("dhan", "acct-1", [row("R1", FILLED, filled=75)])
pos = live_book.get(KEY.position_id)
check("the fill created a position", pos is not None and pos.qty == 75)
check("managed by Charticks, not adopted as an external one",
      pos is not None and pos.managed and pos.source == "charticks")

# ── 2. An order the broker has dropped IS abandoned ─────────────────────────
print("\n2. An order absent from the broker's book is given up")
reset()
order = track("G1")
order.created_ts = time.time() - sync_engine.MISSING_GRACE_S - 1
for _ in range(sync_engine.MISSING_POLLS_BEFORE_GIVE_UP - 1):
    order_sync.ingest("dhan", "acct-1", [row("OTHER", PENDING)])
check("not on the first miss", order_sync.known("G1"))
order_sync.ingest("dhan", "acct-1", [row("OTHER", PENDING)])
check("gone after the configured run of clean misses", not order_sync.known("G1"))

print("   an EMPTY book is never evidence of absence")
reset()
order = track("E1")
order.created_ts = time.time() - sync_engine.MISSING_GRACE_S - 1
for _ in range(sync_engine.MISSING_POLLS_BEFORE_GIVE_UP + 3):
    order_sync.ingest("dhan", "acct-1", [])
check("an empty read is indistinguishable from a failed one",
      order_sync.known("E1"))

print("   an incremental push is never read as 'everything else is gone'")
reset()
order = track("P1")
other = track("P2")
for o in (order, other):
    o.created_ts = time.time() - sync_engine.MISSING_GRACE_S - 1
for _ in range(sync_engine.MISSING_POLLS_BEFORE_GIVE_UP + 3):
    order_sync.ingest("dhan", "acct-1", [row("P1", PENDING)], complete=False)
check("a partial snapshot leaves the others tracked", order_sync.known("P2"))

print("   giving up releases what was waiting on the order")
reset()
quote(KEY, 100.0)
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0,
                      rule=RULE)
held = live_book.get(KEY.position_id)
live_book.begin_exit(held.key, 75, "stop-loss")
exit_order = track("X9", side="SELL", exit_for=held.key)
exit_order.created_ts = time.time() - sync_engine.MISSING_GRACE_S - 1
for _ in range(sync_engine.MISSING_POLLS_BEFORE_GIVE_UP):
    order_sync.ingest("dhan", "acct-1", [row("OTHER", PENDING)])
check("an abandoned EXIT re-arms the position's stop",
      live_book.get(KEY.position_id).exit_pending_qty == 0)

# ── 3. Rolling a live position sends real orders, in order ──────────────────
print("\n3. Roll is two real legs, sequenced")
reset()
quote(KEY, 100.0)
quote(ROLL_KEY, 90.0)
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0,
                      rule=RULE, product="MIS")
live_book.get(KEY.position_id)
result = live_manager.roll_position(KEY.position_id, 25200)
check("the roll is accepted", result.get("ok"), result)
check("the CLOSING leg went first", len(EXITS) == 1 and EXITS[0]["strike"] == 25000)
check("nothing was opened yet", PLACED == [])

live_manager._advance_rolls()
check("still nothing opened while the old leg is open", PLACED == [])

# The exit fills — the old position closes.
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "SELL", 75, 1, 100.0)
live_manager._advance_rolls()
check("the new leg opens once the old one is confirmed closed", len(PLACED) == 1)
check("on the requested strike", PLACED and PLACED[0]["strike"] == 25200.0)
check("same side and size", PLACED and PLACED[0]["side"] == "BUY"
      and PLACED[0]["qty"] == 75)
check("carrying the position's own risk rule", PLACED and PLACED[0]["rule"] == RULE)
check("and the product it was opened under, not a default",
      PLACED and PLACED[0]["product"] == "MIS")

print("   a roll whose close never confirms does NOT open the new leg")
reset()
quote(KEY, 100.0)
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0,
                      rule=RULE)
live_manager.roll_position(KEY.position_id, 25200)
live_manager._pending_rolls[KEY.position_id]["started_ts"] -= ROLL_TIMEOUT_S + 1
live_manager._advance_rolls()
check("the roll is abandoned rather than doubling the position", PLACED == [])
check("and the original position is untouched",
      live_book.get(KEY.position_id).qty == 75)

print("   an unmanaged position cannot be rolled")
reset()
quote(KEY, 100.0)
live_book.upsert_external(KEY, "BUY", 75, 100.0, 100.0, 1, "acct-1", "dhan")
res = live_manager.roll_position(KEY.position_id, 25200)
check("refused with a reason", not res.get("ok")
      and res.get("code") == "UNMANAGED_POSITION", res)
check("and nothing was sent", EXITS == [] and PLACED == [])

print("   a strike no broker lists is refused, not guessed at")
reset()
quote(KEY, 100.0)
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0,
                      rule=RULE)
res = live_manager.roll_position(KEY.position_id, 99000)
check("refused", not res.get("ok") and res.get("code") == "INVALID_ROLL", res)
check("nothing was closed", EXITS == [])

# ── 4. Auto-hedge fires on a confirmed fill, and carries no stop ────────────
print("\n4. Auto-hedge")
reset()
hedge_manager.set_config({"enabled": True, "distancePts": 100,
                          "retryFailed": False, "maxRetries": 1})
quote(KEY, 100.0)
order_sync.track("S1", "acct-1", "dhan", "NIFTY", "28AUG2026", 25000, "CE",
                 "SELL", 75, 75, 100.0, rule=RULE, order_type="MARKET")
order_sync.ingest("dhan", "acct-1", [row("S1", FILLED, filled=75)])
time.sleep(0.4)  # the hedge is routed off the fill thread
check("a confirmed SHORT fill places a hedge", len(PLACED) == 1, PLACED)
check("as a BUY", PLACED and PLACED[0]["side"] == "BUY")
check("further OTM", PLACED and PLACED[0]["strike"] == 25100.0)
check("with NO stop loss or target of its own",
      PLACED and PLACED[0]["rule"] is None)
check("sized to the short", PLACED and PLACED[0]["qty"] == 75)

print("   a second fill on the same short does not stack a second hedge")
order_sync.track("S2", "acct-1", "dhan", "NIFTY", "28AUG2026", 25000, "CE",
                 "SELL", 75, 75, 100.0, rule=RULE, order_type="MARKET")
order_sync.ingest("dhan", "acct-1", [row("S2", FILLED, filled=75)])
time.sleep(0.3)
check("still one hedge", len(PLACED) == 1, PLACED)

print("   a LONG entry is not hedged")
reset()
hedge_manager.set_config({"enabled": True, "distancePts": 100})
quote(KEY, 100.0)
order_sync.track("B1", "acct-1", "dhan", "NIFTY", "28AUG2026", 25000, "CE",
                 "BUY", 75, 75, 100.0, order_type="MARKET")
order_sync.ingest("dhan", "acct-1", [row("B1", FILLED, filled=75)])
time.sleep(0.3)
check("long options are already defined-risk", PLACED == [])

print("   an EXIT fill is not hedged")
reset()
hedge_manager.set_config({"enabled": True, "distancePts": 100})
quote(KEY, 100.0)
order_sync.track("Z1", "acct-1", "dhan", "NIFTY", "28AUG2026", 25000, "CE",
                 "SELL", 75, 75, 100.0, exit_for=KEY.position_id,
                 order_type="MARKET")
order_sync.ingest("dhan", "acct-1", [row("Z1", FILLED, filled=75)])
time.sleep(0.3)
check("closing a long does not open a hedge", PLACED == [])

print("   auto-hedge off means no hedge")
reset()
quote(KEY, 100.0)
order_sync.track("S3", "acct-1", "dhan", "NIFTY", "28AUG2026", 25000, "CE",
                 "SELL", 75, 75, 100.0, order_type="MARKET")
order_sync.ingest("dhan", "acct-1", [row("S3", FILLED, filled=75)])
time.sleep(0.3)
check("nothing placed", PLACED == [])

# ── 5. Exchange holiday calendar ────────────────────────────────────────────
print("\n5. Exchange holidays")
market_session.is_market_open = market_session.__dict__["is_market_open"]
import importlib                                                     # noqa: E402
importlib.reload(market_session)
check("the calendar loads", market_session.reload_holidays() > 0)
check("Republic Day 2026 is a holiday",
      market_session.holiday_for(dt.date(2026, 1, 26)) == "Republic Day")
check("the market is shut on it",
      not market_session.is_market_open(
          dt.datetime(2026, 1, 26, 10, 0)))
check("an ordinary weekday is unaffected",
      market_session.is_market_open(dt.datetime(2026, 1, 27, 10, 0)))
check("MCX keeps its own calendar",
      market_session.holiday_for(dt.date(2026, 8, 26), "CRUDEOIL") is None
      and market_session.holiday_for(dt.date(2026, 8, 26)) is not None)
err = market_session.require_open()
check("the rejection names the holiday when there is one",
      market_session.holiday_for(market_session.now_ist().date()) is None
      or "—" in (err or {}).get("error", ""))

# ── 6. Angel reports a short's cost basis ───────────────────────────────────
print("\n6. Angel short positions carry a real entry price")
import importlib                                                     # noqa: E402
bp = importlib.import_module("services.broker_positions")


class _AngelSession:
    def position(self):
        return {"data": [
            {"tradingsymbol": "NIFTY28AUG2625000CE", "symboltoken": "1",
             "netqty": "-75", "totalbuyavgprice": "0", "buyavgprice": "0",
             "totalsellavgprice": "112.5", "sellavgprice": "112.5",
             "avgnetprice": "0", "ltp": "100", "pnl": "937.5",
             "producttype": "CARRYFORWARD"},
            {"tradingsymbol": "NIFTY28AUG2625100CE", "symboltoken": "2",
             "netqty": "75", "totalbuyavgprice": "88.0", "buyavgprice": "88.0",
             "totalsellavgprice": "0", "sellavgprice": "0",
             "avgnetprice": "0", "ltp": "90", "pnl": "150",
             "producttype": "CARRYFORWARD"},
        ]}


# The module-level `read` is stubbed for the reconciliation scenarios above, so
# the real Angel reader is called directly.
rows = bp._angel("acct-1", _AngelSession())
short = next(r for r in rows if r.side == "SELL")
long_ = next(r for r in rows if r.side == "BUY")
check("a short's entry comes from the SELL average", short.avg_entry == 112.5,
      short.avg_entry)
check("a long's entry still comes from the BUY average", long_.avg_entry == 88.0,
      long_.avg_entry)
check("so a short can be adopted (it has a cost basis)", short.avg_entry > 0)

# ── 7. Kill switch survives a restart ───────────────────────────────────────
print("\n7. Kill switch durability")
from services.kill_switch import KillSwitch, kill_switch             # noqa: E402

kill_switch.engage("audit test")
revived = KillSwitch()
check("a halt engaged before a restart is still in force", revived.halted)
check("with its original reason", revived.state()["reason"] == "audit test")
kill_switch.release()
revived = KillSwitch()
check("an explicit release clears it for good", not revived.halted)

# ── 8. Freeze quantity applies at every broker ──────────────────────────────
print("\n8. Exchange freeze quantity")
from services.broker_limits import limit_resolver                    # noqa: E402

for broker in ("angel", "kotak", "dhan", "icici"):
    limits = limit_resolver._from_config(broker, "NIFTY")
    check(f"{broker} inherits the NIFTY freeze cap",
          limits is not None and limits.max_lots_per_order == 27, limits)
check("and an unlisted underlying still gets the wildcard cap",
      limit_resolver._from_config("kotak", "SOMETHING").max_lots_per_order == 27)

# ── 9. A square-off is a state of the position, not a second row ────────────
print("\n9. Square-off is carried ON the position")
reset()
PUBLISHED = []
_real_publish = live_book._publish
live_book._publish = lambda pos, closed=False: (
    PUBLISHED.append({"id": pos.key, "exitPendingQty": pos.exit_pending_qty,
                      "exitReason": pos.exit_reason, "closed": closed})
    or _real_publish(pos, closed))
quote(KEY, 100.0)
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0,
                      rule=RULE)
PUBLISHED.clear()
held = live_book.get(KEY.position_id)
live_book.begin_exit(held.key, 75, "manual-exit")
check("claiming an exit publishes the position immediately",
      any(p["exitPendingQty"] == 75 for p in PUBLISHED), PUBLISHED)
check("carrying the reason the row will show",
      any(p["exitReason"] == "manual-exit" for p in PUBLISHED))
check("and no second position was created", len(live_book.open_positions()) == 1)

PUBLISHED.clear()
live_book.end_exit(held.key)
check("a refused exit hands the row back", PUBLISHED
      and PUBLISHED[-1]["exitPendingQty"] == 0, PUBLISHED)
check("with the reason cleared", PUBLISHED and not PUBLISHED[-1]["exitReason"])
check("still one position", len(live_book.open_positions()) == 1)
live_book._publish = _real_publish

# ── 10. A hedge belongs to its parent ───────────────────────────────────────
print("\n10. Hedge parent-child relationship")
reset()
hedge_manager.set_config({"enabled": True, "distancePts": 100,
                          "retryFailed": False, "maxRetries": 1})
quote(KEY, 100.0)
order_sync.track("H1", "acct-1", "dhan", "NIFTY", "28AUG2026", 25000, "CE",
                 "SELL", 75, 75, 100.0, rule=RULE, order_type="MARKET")
order_sync.ingest("dhan", "acct-1", [row("H1", FILLED, filled=75)])
time.sleep(0.4)
check("the link is recorded",
      hedge_manager.hedge_of(KEY.position_id) == HEDGE_KEY.position_id,
      hedge_manager.hedge_of(KEY.position_id))
check("and readable from the hedge's side",
      hedge_manager.parents_of(HEDGE_KEY.position_id) == [KEY.position_id])

print("   the link survives a restart")
from services.hedge import HedgeManager                               # noqa: E402

revived = HedgeManager()
check("a fresh engine still knows what protects what",
      revived.hedge_of(KEY.position_id) == HEDGE_KEY.position_id)

print("   a SHARED hedge is not orphaned when one parent closes")
ORPHANS = []
import bridge.hub as hub_module                                       # noqa: E402

_real_hub_publish = hub_module.hub.publish
hub_module.hub.publish = lambda e: (
    ORPHANS.append(e) if e.get("type") == "hedge_orphaned" else None)
# A second short pointing at the same hedge.
second = InstrumentKey.option("NIFTY", "28AUG2026", 25050, "CE")
hedge_manager._hedged[second.position_id] = HEDGE_KEY.position_id
hedge_manager._persist()
hedge_manager.forget(KEY.position_id)
check("no question asked while another short still relies on it", ORPHANS == [])
check("and the hedge keeps its remaining parent",
      hedge_manager.parents_of(HEDGE_KEY.position_id) == [second.position_id])

print("   the LAST parent closing orphans it, and asks")
# The hedge has to exist as a position for the question to be worth asking.
quote(HEDGE_KEY, 40.0)
live_book.record_fill("NIFTY", "28AUG2026", 25100, "CE", "BUY", 75, 1, 40.0)
ORPHANS.clear()
hedge_manager.forget(second.position_id)
check("the user is asked", len(ORPHANS) == 1, ORPHANS)
check("about the right hedge",
      ORPHANS and ORPHANS[0]["hedgeId"] == HEDGE_KEY.position_id)
check("and nothing was closed on its own",
      live_book.get(HEDGE_KEY.position_id) is not None)

print("   'keep' makes it an ordinary position")
res = hedge_manager.resolve_orphan(HEDGE_KEY.position_id, "keep")
check("accepted", res.get("ok"), res)
check("the link is gone", hedge_manager.parents_of(HEDGE_KEY.position_id) == [])
check("the position is untouched",
      live_book.get(HEDGE_KEY.position_id) is not None)

print("   closing a HEDGE leaves its shorts unhedged, not silently re-hedged")
reset()
hedge_manager._hedged[KEY.position_id] = HEDGE_KEY.position_id
hedge_manager.forget(HEDGE_KEY.position_id)
check("the short no longer claims to be hedged",
      hedge_manager.hedge_of(KEY.position_id) is None)
hub_module.hub.publish = _real_hub_publish

# ── 11. Every live order reaches the Order Book ─────────────────────────────
print("\n11. Order Book carries every order, not just entries")
reset()
EVENTS = []
import bridge.hub as hubmod                                           # noqa: E402
_real_pub = hubmod.hub.publish
hubmod.hub.publish = lambda e: EVENTS.append(e)
try:
    quote(KEY, 100.0)
    order_sync.track("E1", "acct-1", "dhan", "NIFTY", "28AUG2026", 25000, "CE",
                     "BUY", 75, 75, 100.0, rule=RULE, order_type="LIMIT")
    order_sync.ingest("dhan", "acct-1", [row("E1", FILLED, filled=75)])
    # An EXIT: originates in the sidecar, so the renderer has no row for it.
    held = live_book.get(KEY.position_id)
    order_sync.track("X1", "acct-1", "dhan", "NIFTY", "28AUG2026", 25000, "CE",
                     "SELL", 75, 75, 0.0, exit_for=held.key, order_type="MARKET")
    order_sync.ingest("dhan", "acct-1", [row("X1", FILLED, filled=75)])
finally:
    hubmod.hub.publish = _real_pub

updates = [e for e in EVENTS if e.get("type") == "order_update"]
entry = [e for e in updates if e["id"] == "E1"]
exits = [e for e in updates if e["id"] == "X1"]
check("the entry order is published", len(entry) >= 1)
check("the EXIT order is published too", len(exits) >= 1, updates)
need = ("underlying", "expiry", "strike", "optType", "lotSize", "orderType")
check("every update carries the structured contract",
      all(all(k in e and e[k] is not None for k in need) for e in updates),
      [{k: e.get(k) for k in need} for e in updates])
check("so the renderer can BUILD a row it never placed",
      exits and exits[-1]["underlying"] == "NIFTY"
      and exits[-1]["strike"] == 25000 and exits[-1]["optType"] == "CE")
check("and the exit is labelled as one",
      exits and exits[-1].get("exitFor") == KEY.position_id, exits)
check("a rejected order would carry it as well (snapshot shape)",
      "exitFor" in order_sync.snapshot()["orders"][0])

# ── 12. Adjusting lots on a LIVE position sends real orders ─────────────────
print("\n12. Adj Lots on a live position")
reset()
quote(KEY, 100.0)
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 150, 2, 100.0,
                      rule=RULE, product="NRML")
res = live_manager.adjust_lots(KEY.position_id, 2)
check("adding 2 lots is accepted", res.get("ok"), res)
check("and places a REAL order", len(PLACED) == 1, PLACED)
check("for exactly 2 lots", PLACED and PLACED[0]["lots"] == 2)
check("sized from the position's own lot size (75)",
      PLACED and PLACED[0]["qty"] == 150, PLACED)
check("on the same side", PLACED and PLACED[0]["side"] == "BUY")
check("carrying the position's rule so the added lots are managed too",
      PLACED and PLACED[0]["rule"] == RULE)

PLACED.clear(); EXITS.clear()
res = live_manager.adjust_lots(KEY.position_id, -1)
check("reducing by 1 lot is accepted", res.get("ok"), res)
check("as a partial EXIT, not a new order", len(EXITS) == 1 and PLACED == [])
check("for exactly one lot's quantity", EXITS and EXITS[0]["qty"] == 75, EXITS)

res = live_manager.adjust_lots(KEY.position_id, 1)
check("a second resize is refused while an exit is in flight",
      not res.get("ok") and res.get("code") == "EXIT_IN_FLIGHT", res)

reset()
quote(KEY, 100.0)
live_book.upsert_external(KEY, "BUY", 150, 100.0, 100.0, 2, "acct-1", "dhan")
res = live_manager.adjust_lots(KEY.position_id, 1)
check("an unmanaged position is refused with a reason",
      not res.get("ok") and res.get("code") == "UNMANAGED_POSITION", res)

# ── 13. A closed live trade is kept as history ──────────────────────────────
print("\n13. Completed live trades are retained")
reset()
quote(KEY, 100.0)
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0,
                      rule=RULE)
check("open book has it", len(live_book.open_positions()) == 1)
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "SELL", 75, 1, 108.0)
check("it leaves the OPEN book", live_book.open_positions() == [])

hist = live_book.closed_positions()
check("but is kept as a completed trade", len(hist) == 1, hist)
done = hist[0] if hist else None
check("with the entry price", done and done.avg_entry == 100.0)
check("the exit price", done and done.exit_price == 108.0)
check("the quantity traded (not the zero it ends on)", done and done.closed_qty == 75)
check("realised P&L", done and round(done.realised, 2) == 600.0, done.realised if done else None)
check("an open time and a close time",
      done and done.opened_ts > 0 and done.closed_ts > 0)

print("   automation must NOT see a closed trade")
check("not in managed_positions", live_book.managed_positions() == [])
check("not counted toward exposure limits", live_book.open_count() == 0)
check("not subscribed for market data", live_book.subscription_keys() == set())

print("   history survives a restart, same day")
live_store.flush()
live_book._positions.clear(); live_book._closed.clear()
live_book.restore()
check("restored", len(live_book.closed_positions()) == 1,
      live_book.closed_positions())
check("with its realised P&L intact",
      live_book.closed_positions()
      and round(live_book.closed_positions()[0].realised, 2) == 600.0)

print("   and can be cleared")
live_book.clear_history()
check("cleared", live_book.closed_positions() == [])

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name in FAIL:
        print(f"  FAILED: {name}")
sys.exit(1 if FAIL else 0)
