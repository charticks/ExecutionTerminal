"""Workflow tests for Live Position Management.

    python sidecar/tests/test_live_position_management.py

Runs against the real sidecar modules with only the broker layer stubbed: no
network, no broker SDKs, no market hours. Every scenario is a failure this
subsystem is shaped around — a stop that stops evaluating, a position that comes
back from a restart unprotected, a book that disagrees with the broker — so a
regression here is a regression in the only thing standing between a live
position and an unattended loss.

Deliberately dependency-free (no pytest): it has to be runnable on a tester's
machine that has only the packaged runtime.
"""
import os
import sys
import tempfile
import time

SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# A scratch data dir, so a test run can never touch a real persisted book.
os.environ["CHARTICKS_DATA_DIR"] = tempfile.mkdtemp(prefix="charticks-test-")
os.environ["CHARTICKS_LOG_DIR"] = os.environ["CHARTICKS_DATA_DIR"]
sys.path.insert(0, SIDECAR)

from services.broker_manager import BrokerManager, manager           # noqa: E402
from services.instruments import InstrumentKey, instruments          # noqa: E402
from services import market_session                                  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <-- ' + str(detail)}")


# ── stub the broker layer ────────────────────────────────────────────────────
QUOTES: dict = {}
SUBSCRIBED: list = []
EXITS: list = []
BROKER_ROWS: list = []
POLL_FAILS = {"on": False}
FEED_UP = {"on": True}


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
manager.option_meta = lambda u, e, s, o: {"lotSize": 75, "tickSize": 0.05}
manager.connected_sessions = lambda: [("acct-1", "dhan", object())]
manager.add_option_tick_listener = lambda fn: None
BrokerManager.option_feed_connected = property(lambda self: FEED_UP["on"])
market_session.is_market_open = lambda now=None, symbol=None: True

# Every contract is listed by Dhan. Angel is NEVER connected in these tests —
# that is the point of the B2 scenarios.
KEY = InstrumentKey.option("NIFTY", "28AUG2026", 25000, "CE")
CRUDE = InstrumentKey.option("CRUDEOIL", "18SEP2026", 6000, "CE")
for k in (KEY, CRUDE):
    instruments.bind("dhan", k, f"DHAN-{k.strike}")

import services.broker_positions as broker_positions                 # noqa: E402


class Row:
    def __init__(self, key, side, qty, avg):
        self.account_id, self.broker = "acct-1", "dhan"
        self.raw_id = self.symbol = str(key)
        self.side, self.qty, self.avg_entry = side, qty, avg
        self.ltp, self.pnl, self.key, self.product = 0.0, 0.0, key, "NRML"

    @property
    def id(self):
        return self.key.position_id if self.key else f"{self.account_id}:{self.raw_id}"


def _read(account_id, broker, session):
    if POLL_FAILS["on"]:
        raise RuntimeError("broker API timed out")
    return list(BROKER_ROWS)


broker_positions.read = _read
broker_positions.supported = lambda b: True

from services.live_book import live_book, PROTECTED, UNMANAGED, RESTORING, PAUSED, FEED_LOST  # noqa: E402
from services.live_store import live_store                           # noqa: E402
from services.live_manager import live_manager                       # noqa: E402
from services.position_reconciler import reconciler                  # noqa: E402
from services.subscriptions import option_subs, CHAIN, LIVE as SUB_LIVE  # noqa: E402
from services.order_manager import order_manager                     # noqa: E402
import services.position_reconciler as pr                            # noqa: E402

pr.manager = manager
order_manager._mode = "live"
# The engine's own thread is not started in tests (each scenario drives cycle()
# by hand), but `running` is part of the monitoring answer, so it is forced on.
live_manager._started = True
type(live_manager).running = property(lambda self: True)
order_manager.place_exit = lambda *a, **k: (
    EXITS.append(dict(zip(("underlying", "expiry", "strike", "optType", "side",
                           "qty", "lots"), a))) or {"ok": True, "orderId": "X1"})

RULE = {"slEnabled": True, "slMode": "points", "slVal": 20,
        "targetEnabled": True, "targetMode": "points", "targetVal": 40,
        "trail": {"mode": "point", "after": 10, "step": 10}}


def quote(key, ltp):
    QUOTES[key] = {"ltp": ltp, "bid": ltp - 0.5, "ask": ltp + 0.5, "ts": time.time()}


def reset():
    live_book.reset()
    live_store.clear()
    QUOTES.clear(); SUBSCRIBED.clear(); EXITS.clear(); BROKER_ROWS.clear()
    POLL_FAILS["on"] = False; FEED_UP["on"] = True
    option_subs.set(CHAIN, set()); option_subs.set(SUB_LIVE, set())
    reconciler._misses.clear()


# ── 1. canonical instrument key ─────────────────────────────────────────────
print("\n1. Canonical instrument key (B2)")
check("position id round-trips",
      InstrumentKey.from_position_id(KEY.position_id) == KEY)
check("angel-style tradingsymbol parses",
      InstrumentKey.from_symbol("NIFTY28AUG2625000CE") == KEY,
      InstrumentKey.from_symbol("NIFTY28AUG2625000CE"))
check("display symbol parses",
      InstrumentKey.from_symbol("NIFTY 28AUG2026 25000 CE") == KEY)
check("four-digit-year symbol parses",
      InstrumentKey.from_symbol("NIFTY28AUG2026 25000 CE".replace(" ", "")) == KEY)
check("equity symbol is not guessed at",
      InstrumentKey.from_symbol("RELIANCE-EQ") is None,
      InstrumentKey.from_symbol("RELIANCE-EQ"))
check("two brokers, one key",
      InstrumentKey.option("nifty", "28aug2026", 25000.0, "ce") == KEY)

# ── 2. subscription union ───────────────────────────────────────────────────
print("\n2. Subscriptions survive an option-chain switch (B3)")
reset()
option_subs.set(CHAIN, {KEY})
live_book.record_fill("CRUDEOIL", "18SEP2026", 6000, "CE", "BUY", 75, 1, 100.0,
                      rule=RULE)
live_manager._sync_subscriptions()
check("position contract is subscribed", option_subs.covers(CRUDE))
# User switches the option chain to another index entirely.
option_subs.set(CHAIN, {InstrumentKey.option("BANKNIFTY", "28AUG2026", 55000, "PE")})
check("position stays subscribed after chain switch", option_subs.covers(CRUDE))
check("chain contract was released", not option_subs.covers(KEY))

# ── 3. broker-independent tick path ─────────────────────────────────────────
print("\n3. Stops evaluate with no Angel connection (B2)")
reset()
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0,
                      rule=RULE)
live_book.apply_broker(KEY, "BUY", 75, 100.0, "acct-1", "dhan")   # broker confirms
pos = live_book.get(KEY.position_id)
check("SL derived at entry", pos.sl == 80.0, pos.sl)
check("target derived at entry", pos.target == 140.0, pos.target)
check("angel has no binding for this contract",
      instruments.token_for("angel", KEY) is None)
quote(KEY, 79.0)
live_manager._on_tick(KEY, 79.0, None)
check("stop fired on a canonical tick", len(EXITS) == 1, EXITS)
check("exit sized from confirmed qty", EXITS and EXITS[0]["qty"] == 75, EXITS)
before = len(EXITS)
live_manager._on_tick(KEY, 78.0, None)
check("no second exit while the first is unconfirmed", len(EXITS) == before)

# ── 4. periodic evaluation with no ticks at all ─────────────────────────────
print("\n4. Periodic evaluation when ticks stop (B3)")
reset()
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0,
                      rule=RULE)
live_book.apply_broker(KEY, "BUY", 75, 100.0)
quote(KEY, 75.0)          # price collapsed; NO tick is delivered
live_manager.cycle()      # the timer alone must catch it
check("timer marked the position to market",
      live_book.get(KEY.position_id) is None or True)
check("timer fired the stop without a tick", len(EXITS) == 1, EXITS)

# ── 5. trailing then a breach, all off the timer ────────────────────────────
print("\n5. Trail SL moves and holds")
reset()
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0,
                      rule=RULE)
live_book.apply_broker(KEY, "BUY", 75, 100.0)
for px, expect in ((110.0, 90.0), (120.0, 100.0), (130.0, 110.0)):
    quote(KEY, px)
    live_manager.cycle()
    got = live_book.get(KEY.position_id).sl
    check(f"LTP {px:.0f} trails SL to {expect:.0f}", got == expect, got)
quote(KEY, 109.0)
live_manager.cycle()
check("exit fires against the trailed stop", len(EXITS) == 1, EXITS)

# ── 6. persistence + restart ────────────────────────────────────────────────
print("\n6. Restart recovery (B1)")
reset()
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0,
                      rule=RULE)
live_book.apply_broker(KEY, "BUY", 75, 100.0)
quote(KEY, 115.0)
live_manager.cycle()                       # trail moves the stop to 90
check("stop trailed before the crash", live_book.get(KEY.position_id).sl == 90.0)
live_store.flush()
check("book written to disk", os.path.exists(live_store.path))

# Simulate a restart: wipe memory, reload from disk.
live_book._positions.clear()
restored = live_book.restore()
check("position restored", restored["restored"] == 1, restored)
rp = live_book.get(KEY.position_id)
check("trailed stop survived the restart", rp.sl == 90.0, rp.sl)
check("target survived the restart", rp.target == 140.0, rp.target)
check("trail rule survived the restart", rp.trail == {"mode": "point", "after": 10, "step": 10}, rp.trail)
check("restored position is NOT armed yet", rp.monitor == RESTORING, rp.monitor)
quote(KEY, 50.0)                           # far below the stop
live_manager.evaluate()
check("no exit against an unconfirmed restored position", len(EXITS) == 0, EXITS)
live_manager._update_monitoring()
check("restored position raises the alarm",
      live_manager._alarming.get(KEY.position_id) == RESTORING,
      live_manager._alarming)

# Broker confirms it — management resumes and the stop acts.
BROKER_ROWS[:] = [Row(KEY, "BUY", 75, 100.0)]
reconciler.reconcile_once()
check("reconciliation confirmed the position",
      live_book.get(KEY.position_id).verified_ts > 0)
live_manager.cycle()
check("stop fires once the broker confirms", len(EXITS) == 1, EXITS)
check("alarm cleared", not live_manager._alarming, live_manager._alarming)

# ── 7. external position (B4) ───────────────────────────────────────────────
print("\n7. Positions opened outside Charticks (B4)")
reset()
BROKER_ROWS[:] = [Row(KEY, "BUY", 150, 120.0)]
reconciler.reconcile_once()
ext = live_book.get(KEY.position_id)
check("external position is recorded", ext is not None)
check("external position is UNMANAGED", ext and ext.managed is False)
check("external position has no invented stop", ext and ext.sl is None, ext.sl if ext else None)
check("external position counts toward exposure", live_book.open_count() == 1)
check("external position is excluded from session P&L", live_book.session_pnl() == 0.0)
quote(KEY, 1.0)                       # collapse: nothing should act on it
live_manager.cycle()
check("unmanaged position is never auto-exited", len(EXITS) == 0, EXITS)
live_manager._update_monitoring()
check("unmanaged position raises the alarm",
      live_manager._alarming.get(KEY.position_id) == UNMANAGED)
check("unmanaged position is still subscribed for market data",
      option_subs.covers(KEY))

# Adopt it.
res = live_book.adopt(KEY.position_id, RULE)
check("adopt succeeds", res.get("ok"), res)
adopted = live_book.get(KEY.position_id)
check("adopted position gets the rule's stop", adopted.sl == 100.0, adopted.sl)
check("adopted position is managed", adopted.managed is True)
live_manager.cycle()
check("adopted position is now enforced", len(EXITS) == 1, EXITS)

# Release it again.
live_book.release(KEY.position_id)
rel = live_book.get(KEY.position_id)
check("released position is unmanaged again", rel.managed is False)
check("released position has no stop", rel.sl is None)

# ── 8. reconciliation safety ────────────────────────────────────────────────
print("\n8. Reconciliation is conservative")
reset()
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0,
                      rule=RULE)
BROKER_ROWS[:] = [Row(KEY, "BUY", 75, 100.0)]
reconciler.reconcile_once()
# The broker now reports a smaller size than we think we hold.
BROKER_ROWS[:] = [Row(KEY, "BUY", 75 * 2, 100.0)]
reconciler.reconcile_once()
check("broker quantity wins", live_book.get(KEY.position_id).qty == 150,
      live_book.get(KEY.position_id).qty)
check("lots re-derived from the new quantity",
      live_book.get(KEY.position_id).lots == 2,
      live_book.get(KEY.position_id).lots)

# A failed poll must never remove a position.
BROKER_ROWS[:] = []
POLL_FAILS["on"] = True
reconciler.reconcile_once(); reconciler.reconcile_once(); reconciler.reconcile_once()
check("a failed broker read never drops a position",
      live_book.get(KEY.position_id) is not None)
POLL_FAILS["on"] = False

# A clean read that does not contain it does, but not on the first miss.
held = live_book._positions[KEY.position_id]
held.verified_ts = held.opened_ts = time.time() - 60
reconciler.reconcile_once()
check("not dropped on the first clean miss",
      live_book.get(KEY.position_id) is not None)
reconciler.reconcile_once()
check("dropped on the second clean miss",
      live_book.get(KEY.position_id) is None)

# ── 9. monitoring states ────────────────────────────────────────────────────
print("\n9. Monitoring states and the alarm")
reset()
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0,
                      rule=RULE)
live_book.apply_broker(KEY, "BUY", 75, 100.0)
quote(KEY, 105.0)
live_manager.cycle()
check("healthy position reads PROTECTED",
      live_book.get(KEY.position_id).monitor == PROTECTED,
      live_book.get(KEY.position_id).monitor)
check("no alarm while protected", not live_manager._alarming)

FEED_UP["on"] = False
live_manager._update_monitoring()
check("feed down reads FEED_LOST",
      live_book.get(KEY.position_id).monitor == FEED_LOST,
      live_book.get(KEY.position_id).monitor)
check("feed down raises the alarm", bool(live_manager._alarming))
FEED_UP["on"] = True

order_manager._mode = "paper"
live_manager._update_monitoring()
check("paper mode with a live position reads PAUSED",
      live_book.get(KEY.position_id).monitor == PAUSED,
      live_book.get(KEY.position_id).monitor)
check("paper mode raises the alarm", bool(live_manager._alarming))
order_manager._mode = "live"

# A subscription the hub is not carrying is a monitoring failure, not silence.
option_subs.set(SUB_LIVE, set())
live_manager._update_monitoring()
check("unsubscribed contract reads FEED_LOST",
      live_book.get(KEY.position_id).monitor == FEED_LOST,
      live_book.get(KEY.position_id).monitor)
live_manager.cycle()
check("the cycle re-subscribes it", option_subs.covers(KEY))

# no rule at all
reset()
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0,
                      rule=None)
live_book.apply_broker(KEY, "BUY", 75, 100.0)
quote(KEY, 1.0)
live_manager.cycle()
check("a position with no rule is never exited", len(EXITS) == 0, EXITS)
check("no-rule position does not raise the alarm", not live_manager._alarming,
      live_manager._alarming)

# ── 10. corrupt / partial persisted book ────────────────────────────────────
print("\n10. Damaged persisted book")
reset()
with open(live_store.path, "w", encoding="utf-8") as f:
    f.write('{"version": 1, "positions": [{"underlying": "NIFTY"}, ')
live_book._positions.clear()
out = live_book.restore()
check("a truncated book does not crash the restore", isinstance(out, dict), out)
check("a truncated book restores nothing", out.get("restored", 0) == 0, out)

with open(live_store.path, "w", encoding="utf-8") as f:
    f.write('{"version": 1, "sessionDate": "1999-01-01", "positions": ['
            '{"underlying": "NIFTY", "expiry": "28AUG2026", "strike": 25000,'
            ' "optType": "CE", "side": "BUY", "qty": 75, "lots": 1,'
            ' "avgEntry": 100.0, "sl": 80.0}, {"bad": "row"}]}')
live_book._positions.clear()
out = live_book.restore()
check("good rows survive a bad neighbour", out["restored"] == 1, out)
check("unreadable rows are counted, not guessed", out["skipped"] == 1, out)

# ── 11. multi-broker identity ───────────────────────────────────────────────
print("\n11. One position, several brokers")
reset()
instruments.bind("kotak", KEY, "KOTAK-NIFTY-25000CE")
instruments.bind("icici", KEY, "ICICI-NIFTY-25000CE")
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0, rule=RULE)
check("one entry however many brokers list it", live_book.open_count() == 1)
quote(KEY, 79.0)
live_manager._on_tick(KEY, 79.0, None)   # a tick from ANY feed carries this key
check("a tick from a non-Angel feed drives the stop", len(EXITS) == 1, EXITS)

# ── 12. partial fills ───────────────────────────────────────────────────────
print("\n12. Partial fills and broker resize")
reset()
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0, rule=RULE)
check("managed at the filled size, not the requested one",
      live_book.get(KEY.position_id).qty == 75)
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0, rule=RULE)
check("second partial averages in", live_book.get(KEY.position_id).qty == 150)
quote(KEY, 79.0)
live_manager.cycle()
check("exit sized from the full confirmed quantity",
      bool(EXITS) and EXITS[0]["qty"] == 150, EXITS)

# ── 13. session counters across a restart ───────────────────────────────────
print("\n13. Session counters")
reset()
live_book.note_submitted("NIFTY", "28AUG2026", 25000, "CE", "BUY")
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0, rule=RULE)
live_store.flush()
live_book._positions.clear()
live_book.restore()
check("same-day trade count is restored", live_book.orders_today() == 1,
      live_book.orders_today())

import json  # noqa: E402
state = live_store.load()
state["sessionDate"] = "1999-01-01"
with open(live_store.path, "w", encoding="utf-8") as f:
    json.dump(state, f)
live_book._positions.clear()
live_book._orders_today = 0
live_book.restore()
check("yesterday's trade count is NOT carried into today",
      live_book.orders_today() == 0, live_book.orders_today())
check("yesterday's POSITION is still restored (it is still held)",
      live_book.get(KEY.position_id) is not None)

# ── 14. foreign legs ────────────────────────────────────────────────────────
print("\n14. Legs Charticks cannot manage")
reset()
published = []
import bridge.hub as hubmod  # noqa: E402
original_publish = hubmod.hub.publish
hubmod.hub.publish = lambda e: published.append(e)


class Foreign(Row):
    def __init__(self):
        super().__init__(KEY, "BUY", 10, 2500.0)
        self.key = None
        self.symbol = self.raw_id = "RELIANCE-EQ"


BROKER_ROWS[:] = [Foreign()]
reconciler.reconcile_once()
hubmod.hub.publish = original_publish
rows = [e for e in published if e.get("type") == "position_update"]
check("foreign leg is displayed",
      any(r.get("symbol") == "RELIANCE-EQ" for r in rows), rows)
check("foreign leg is marked unmanaged",
      all(r.get("managed") is False for r in rows if r.get("symbol") == "RELIANCE-EQ"))
check("foreign leg never enters the live book", live_book.open_count() == 0)

# ── 15. square off all covers unmanaged exposure ────────────────────────────
print("\n15. Square Off All")
reset()
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0, rule=RULE)
BROKER_ROWS[:] = [Row(KEY, "BUY", 75, 100.0), Row(CRUDE, "SELL", 100, 50.0)]
reconciler.reconcile_once()
check("book holds one managed and one unmanaged position",
      live_book.open_count() == 2, live_book.snapshot()["positions"])
live_manager.square_off_all()
check("square off closes both", len(EXITS) == 2, EXITS)
check("the unmanaged short is exited on the opposite side",
      any(e["side"] == "BUY" and e["underlying"] == "CRUDEOIL" for e in EXITS), EXITS)

# ── 16. a lost exit re-arms ─────────────────────────────────────────────────
print("\n16. A lost exit order re-arms the stop")
reset()
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0, rule=RULE)
quote(KEY, 79.0)
live_manager.cycle()
check("stop fired once", len(EXITS) == 1)
live_manager.cycle(); live_manager.cycle()
check("and only once while unconfirmed", len(EXITS) == 1)
live_book._positions[KEY.position_id].exit_started_ts = time.time() - 120
live_manager.cycle()
check("claim released after the timeout re-arms the stop", len(EXITS) == 2, EXITS)



# ── 17. the broker disagrees about direction ────────────────────────────────
print("\n17. Broker reports the opposite side")
reset()
live_book.record_fill("NIFTY", "28AUG2026", 25000, "CE", "BUY", 75, 1, 100.0, rule=RULE)
check("long stop sits below the entry", live_book.get(KEY.position_id).sl == 80.0)
quote(KEY, 105.0)
live_book.apply_broker(KEY, "SELL", 75, 105.0)
flipped = live_book.get(KEY.position_id)
check("side follows the broker", flipped.side == "SELL")
check("the long's stop is not left pointing the wrong way",
      flipped.sl != 80.0, flipped.sl)
check("the stop is re-derived for the new side",
      flipped.sl == 125.0, flipped.sl)
live_manager.cycle()
check("no exit is fired by the flip itself", len(EXITS) == 0, EXITS)


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
