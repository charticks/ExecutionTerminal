"""Regression tests for Firstock — Phase 5 Live Trade Management.

    python sidecar/tests/test_firstock_live_management.py

``test_firstock_reconciliation.py`` proves Firstock's own position rows drive
RESTORING / UNMANAGED / PROTECTED / FEED_LOST through the real, shared engine.
This file proves the same thing for the features reconciliation does not touch
— Auto Hedge, Roll, Adjust Lots, Partial Exit, live SL/Target edits, Trailing
SL and Portfolio Trail — and it drives them the way production actually does:
a REAL ``order_manager.place_order`` / ``place_exit`` call, through the REAL
risk, margin and idempotency gates, landing on the REAL ``_place_firstock``,
confirmed by the REAL ``order_sync.brokers.firstock_orders`` parser reading a
Firstock-shaped order-book row. Only the network boundary is stubbed
(``firstock_client._post``), exactly as the two existing Firstock test files do.

Every other regression file that exercises Roll / Adjust Lots / Auto Hedge /
Trailing SL (``test_production_hardening.py``, ``test_live_position_management.py``)
stubs ``order_manager.place_order``/``place_exit`` directly and drives fills
through ``live_book.record_fill`` — proving the generic engine is correct, but
never that a real broker's wire shapes survive the trip through it. Routing
through the real placer and the real order-sync fill path is what this file
adds: a pass here is proof Firstock behaves exactly like every other broker
for the full live-management feature set, not just that its adapter looks
right in isolation.
"""
import os
import sys
import tempfile
import time

SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CHARTICKS_DATA_DIR"] = tempfile.mkdtemp(prefix="charticks-fslive-")
os.environ["CHARTICKS_LOG_DIR"] = os.environ["CHARTICKS_DATA_DIR"]
sys.path.insert(0, SIDECAR)

from services.feeds import firstock_client                            # noqa: E402
from services.feeds.firstock_client import FirstockClient, SIDE        # noqa: E402
from services.instruments import InstrumentKey, instruments            # noqa: E402
from services import market_session                                    # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <-- ' + str(detail)}")


def section(title):
    print(f"\n{title}")


# ── broker layer stubs (data plane only — placement, sync and margin are real)
from services.broker_manager import BrokerManager, manager              # noqa: E402

QUOTES: dict = {}
SUBSCRIBED: list = []
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
manager.option_meta = lambda u, e, s, o: {"lotSize": 65, "tickSize": 0.05}
manager.add_option_tick_listener = lambda fn: None
BrokerManager.option_feed_connected = property(lambda self: FEED_UP["on"])
market_session.is_market_open = lambda now=None, symbol=None: True

EXPIRY = "29SEP2026"
KEY = InstrumentKey.option("NIFTY", EXPIRY, 24000, "CE")       # the short a hedge protects
HEDGE_KEY = InstrumentKey.option("NIFTY", EXPIRY, 24100, "CE")  # its protective leg
ROLL_KEY = InstrumentKey.option("NIFTY", EXPIRY, 24200, "CE")   # a roll's target strike
KEY2 = InstrumentKey.option("NIFTY", EXPIRY, 24500, "CE")       # a second portfolio-trail leg

SYMS = {
    KEY: "NIFTY29SEP26C24000", HEDGE_KEY: "NIFTY29SEP26C24100",
    ROLL_KEY: "NIFTY29SEP26C24200", KEY2: "NIFTY29SEP26C24500",
}
TOKENS = {KEY: "35085", HEDGE_KEY: "35090", ROLL_KEY: "35095", KEY2: "35100"}

for _k, _tok in TOKENS.items():
    instruments.bind("firstock", _k, f"NFO:{_tok}")
    instruments.alias("firstock", _k, _tok)

from services.live_book import live_book, EXITING, NO_RULE, PAUSED      # noqa: E402
from services.live_store import live_store                              # noqa: E402
from services.live_manager import live_manager, ROLL_TIMEOUT_S          # noqa: E402
from services.order_manager import LIVE, order_manager                  # noqa: E402
from services.order_sync import order_sync                              # noqa: E402
from services.order_sync.brokers import firstock_orders                 # noqa: E402
from services.hedge import hedge_manager                                # noqa: E402
from services.risk_engine import risk_engine                            # noqa: E402
from services.idempotency.store import store as idempotency_store       # noqa: E402
from services.paths import data_dir                                     # noqa: E402
from services.position_reconciler import reconciler                     # noqa: E402
import services.broker_positions as broker_positions                    # noqa: E402
import services.position_reconciler as pr                               # noqa: E402

pr.manager = manager
order_manager._mode = LIVE
live_manager._started = True
type(live_manager).running = property(lambda self: True)
risk_engine.set_config({})   # configured=True, every limit disabled


# ── the real Firstock client and order router, with only the network boundary
#    and the instrument master stubbed ──────────────────────────────────────
class FakeScrip:
    option_count = 12988

    @staticmethod
    def contract_for(key):
        sym = SYMS.get(key)
        if sym is None:
            return None
        return {"exchange": "NFO", "tradingSymbol": sym, "token": TOKENS[key],
                "subscribeId": f"NFO:{TOKENS[key]}", "lotSize": 65, "tickSize": 0.05}

    @staticmethod
    def explain_miss(key):
        return f"requested {key} is not one of this test's known strikes"


manager.router.scrip_of = lambda broker, session=None: FakeScrip()


class Wire:
    """Replaces firstock_client._post. A placeOrder/modifyOrder/cancelOrder call
    is auto-answered with a fresh order number unless a scenario scripts its
    own response — every scenario below places several real orders and does
    not want to hand-script each one individually."""

    def __init__(self):
        self.calls = []
        self.responses = {}
        self.raises = {}
        self._seq = 0

    def install(self):
        firstock_client._post = self          # type: ignore[assignment]
        return self

    def __call__(self, url, payload, timeout):
        key = url.rsplit("/", 1)[-1]
        self.calls.append({"endpoint": key, "payload": dict(payload)})
        if key in self.raises:
            raise self.raises[key]
        if key in self.responses:
            return self.responses[key]
        if key == "placeOrder":
            self._seq += 1
            return {"orderNumber": f"FS{self._seq:06d}"}
        if key in ("modifyOrder", "cancelOrder"):
            return {"orderNumber": str(payload.get("orderNumber", ""))}
        return {}


def client(account="acct-1"):
    return FirstockClient(user_id=account, jkey="tok", actid=account)


CLIENT_A = client("acct-1")
CONNECTED = {"on": True}
WIRE: Wire = Wire().install()

manager.connected_sessions = (
    lambda: [("acct-1", "firstock", CLIENT_A)] if CONNECTED["on"] else [])
manager.execution_accounts = lambda: {"acct-1"} if CONNECTED["on"] else set()
manager.execution_sessions = (
    lambda: [("acct-1", "firstock", CLIENT_A)] if CONNECTED["on"] else [])


def quote(key, ltp):
    QUOTES[key] = {"ltp": ltp, "bid": ltp - 0.5, "ask": ltp + 0.5, "ts": time.time()}


def open_ids():
    return {o.order_id for o in order_sync.open_orders()}


def confirm(order_id, side, qty, avg_price, symbol, extra_rows=()):
    """Feed Firstock's own order-book shape through the REAL parser
    (firstock_orders) and the REAL sync engine — the seam this file exists to
    exercise that no other test does for these features."""
    row = {"orderNumber": order_id, "status": "COMPLETE",
           "fillShares": str(qty), "quantity": str(qty),
           "averagePrice": f"{avg_price:.2f}", "tradingSymbol": symbol,
           "transactionType": SIDE.get(side, side), "remarks": ""}
    WIRE.responses["orderBook"] = [row, *extra_rows]
    rows = firstock_orders(CLIENT_A)
    order_sync.ingest("firstock", "acct-1", rows, complete=True)


def confirm_new(side, qty, price, symbol, before):
    """Confirm the single order that appeared since `before` (an open_ids()
    snapshot) — used for fills fired internally by live_manager (hedge, roll's
    legs, adjust-lots) whose order id this test never chose itself."""
    new = open_ids() - before
    check(f"exactly one new order to confirm ({symbol})", len(new) == 1, new)
    order_id = next(iter(new))
    confirm(order_id, side, qty, price, symbol)
    return order_id


def position_row(key, side, qty, avg_price, product="M"):
    """Firstock's own positionBook row shape (see test_firstock_reconciliation.py's
    position_row()) — what re-arms a RESTORING position after a restart. That
    is reconciliation's job, not order-sync's: a restart re-confirms a
    position exists via the broker's OWN position book, never by inventing an
    order fill that was never tracked."""
    return {"netQuantity": str(qty if side == "BUY" else -qty),
           "tradingSymbol": SYMS[key], "token": TOKENS[key], "exchange": "NFO",
           "product": product, "lastTradedPrice": f"{avg_price:.2f}",
           "netAveragePrice": f"{avg_price:.2f}", "totalPNL": "0.00",
           "dayBuyAveragePrice": f"{avg_price:.2f}" if side == "BUY" else "0.00",
           "daySellAveragePrice": f"{avg_price:.2f}" if side == "SELL" else "0.00",
           "cfBuyAmt": "", "cfBuyQty": "", "cfSellAmt": "", "cfSellQty": ""}


def enter(key, side, qty, lots, price, rule, product="NRML", rid=""):
    """Place a REAL entry through Firstock and confirm its fill, exactly as
    the Positions tab's own "Buy"/"Sell" button does in production."""
    res = order_manager.place_order(
        LIVE, key.underlying, key.expiry, key.strike, key.opt_type, side,
        qty, "MARKET", 0.0, lots=lots, rule=rule, product=product,
        validity="DAY", request_id=rid or f"enter-{key.position_id}-{time.time()}")
    check(f"{key} entry accepted by Firstock", res.get("ok"), res)
    order_id = res["results"][0]["orderId"]
    confirm(order_id, side, qty, price, SYMS[key])
    return live_book.get(key.position_id)


def reset():
    live_book.reset()
    live_store.clear()
    order_sync.reset()
    idempotency_store.reset()
    # The idempotency journal is a same-day file on disk (by design — it must
    # survive a restart) and this whole file shares one CHARTICKS_DATA_DIR, so
    # each `reset()` needs the journal wiped too, or an exit fired in an
    # earlier section (same request_id: "exit:<positionKey>") is read back as
    # an unexpired duplicate claim in a later one.
    for _name in os.listdir(data_dir()):
        if _name.startswith("idempotency_"):
            try:
                os.remove(os.path.join(data_dir(), _name))
            except OSError:
                pass
    QUOTES.clear(); SUBSCRIBED.clear()
    FEED_UP["on"] = True; CONNECTED["on"] = True
    hedge_manager._hedged.clear()
    hedge_manager.set_config({"enabled": False})
    live_manager._pending_rolls.clear()
    order_manager._mode = LIVE
    global WIRE
    WIRE = Wire().install()
    WIRE.responses["orderMargin"] = {"marginOnNewOrder": "1000.00",
                                     "availableMargin": "999999.00"}
    WIRE.responses["limit"] = {"availableMargin": "999999.00"}
    WIRE.responses["orderBook"] = []


RULE = {"slEnabled": True, "slMode": "points", "slVal": 5,
        "targetEnabled": True, "targetMode": "points", "targetVal": 50}
TRAIL_RULE = {"slEnabled": True, "slMode": "points", "slVal": 5,
             "targetEnabled": True, "targetMode": "points", "targetVal": 500,
             "trail": {"mode": "point", "after": 3, "step": 2}}


# ═════════════════════════════════════════════════════════════════════════
# [1] Auto Hedge
# ═════════════════════════════════════════════════════════════════════════
section("[1] Auto Hedge — a real short fill through Firstock gets a real hedge leg")
reset()
hedge_manager.set_config({"enabled": True, "distancePts": 100, "retryFailed": True})
quote(KEY, 20.0)
quote(HEDGE_KEY, 8.0)

before = open_ids()
res = order_manager.place_order(
    LIVE, "NIFTY", EXPIRY, 24000, "CE", "SELL", 65, "MARKET", 0.0, lots=1,
    rule=RULE, product="NRML", validity="DAY", request_id="hedge-parent")
check("the short parent is accepted by Firstock", res.get("ok"), res)
confirm(res["results"][0]["orderId"], "SELL", 65, 20.0, SYMS[KEY])

parent = live_book.get(KEY.position_id)
check("the short position is confirmed", parent is not None and parent.qty == 65)

# on_entry_fill spawns the hedge leg on a background thread — give it a moment.
deadline = time.time() + 3.0
while time.time() < deadline and not (open_ids() - before):
    time.sleep(0.05)
confirm_new("BUY", 65, 8.0, SYMS[HEDGE_KEY], before)

hedge_key = hedge_manager.hedge_of(KEY.position_id)
check("the parent<->child hedge link was recorded",
      hedge_key == HEDGE_KEY.position_id, hedge_key)
check("the hedge protects the right parent",
      hedge_manager.parents_of(hedge_key) == [KEY.position_id])
hedge_pos = live_book.get(HEDGE_KEY.position_id)
check("the hedge leg carries no risk rule of its own",
      hedge_pos is not None and hedge_pos.sl is None and hedge_pos.target is None,
      hedge_pos)

section("       ...and the link survives a restart")
live_store.flush()
hedge_manager._persist()
hedge_manager._hedged.clear()
hedge_manager._loaded = False
restored = hedge_manager.hedge_of(KEY.position_id)
check("the hedge link was read back from disk",
      restored == HEDGE_KEY.position_id, restored)

section("       ...and closing the last protected short orphans the hedge")
from bridge.hub import hub                                              # noqa: E402
PUBLISHED: list = []
_orig_publish = hub.publish
hub.publish = lambda evt: PUBLISHED.append(evt)
try:
    before = open_ids()
    result = live_manager.close_position(KEY.position_id, 1.0)
    check("the parent's close was accepted", result.get("ok"), result)
    confirm_new("BUY", 65, 20.0, SYMS[KEY], before)
finally:
    hub.publish = _orig_publish
check("the hedge is no longer linked to the closed parent",
      hedge_manager.hedge_of(KEY.position_id) is None)
check("closing the only protected short raised the orphan question",
      any(e.get("type") == "hedge_orphaned" for e in PUBLISHED), PUBLISHED)


# ═════════════════════════════════════════════════════════════════════════
# [2] Roll Up / Roll Down
# ═════════════════════════════════════════════════════════════════════════
section("[2] Roll — two real Firstock legs, strictly sequenced")
reset()
quote(KEY, 20.0)
quote(ROLL_KEY, 15.0)
pos = enter(KEY, "BUY", 65, 1, 20.0, RULE, product="MIS")
check("the source position is confirmed", pos is not None and pos.qty == 65)

before = open_ids()
result = live_manager.roll_position(KEY.position_id, 24200)
check("the roll is accepted", result.get("ok"), result)
check("the closing leg was sent, nothing else yet",
      len(open_ids() - before) == 1, open_ids() - before)
close_id = next(iter(open_ids() - before))

live_manager._advance_rolls()
check("the new leg does not open before the old one confirms closed",
      live_book.get(KEY.position_id).qty == 65)

before2 = open_ids()
confirm(close_id, "SELL", 65, 20.0, SYMS[KEY])
check("the old leg is fully closed (left the open book)",
      live_book.get(KEY.position_id) is None)
live_manager._advance_rolls()
new_id = confirm_new("BUY", 65, 15.0, SYMS[ROLL_KEY], before2)
rolled = live_book.get(ROLL_KEY.position_id)
check("the new strike is open, same size", rolled is not None and rolled.qty == 65)
check("the SAME risk rule carried over (SL derived at the new entry)",
      rolled.rule == RULE, rolled.rule)
check("the product carried over, not defaulted",
      rolled.product == "MIS", rolled.product)
check("management rules are live on the new leg",
      rolled.sl == 10.0 and rolled.target == 65.0, (rolled.sl, rolled.target))

section("       ...a roll whose close never confirms does NOT open the new leg")
reset()
quote(KEY, 20.0)
enter(KEY, "BUY", 65, 1, 20.0, RULE)
live_manager.roll_position(KEY.position_id, 24200)
live_manager._pending_rolls[KEY.position_id]["started_ts"] -= ROLL_TIMEOUT_S + 1
live_manager._advance_rolls()
check("nothing was opened at the roll target — not doubled",
      live_book.get(ROLL_KEY.position_id) is None)
check("the original position is untouched", live_book.get(KEY.position_id).qty == 65)

section("       ...a second roll cannot start while one is already in flight")
reset()
quote(KEY, 20.0)
enter(KEY, "BUY", 65, 1, 20.0, RULE)
live_manager.roll_position(KEY.position_id, 24200)
res = live_manager.roll_position(KEY.position_id, 24200)
# The first roll's closing leg has already claimed the whole exitable
# quantity, so a second attempt is refused as EXIT_IN_FLIGHT — that check
# runs before ROLL_IN_PROGRESS is ever reached (services/live_manager.py
# roll_position). Either code proves the same thing: no second roll, and no
# duplicated exposure.
check("refused rather than starting a second, overlapping roll",
      not res.get("ok") and res.get("code") in ("EXIT_IN_FLIGHT", "ROLL_IN_PROGRESS"),
      res)


# ═════════════════════════════════════════════════════════════════════════
# [3] Adjust Lots
# ═════════════════════════════════════════════════════════════════════════
section("[3] Adjust Lots — increase places a real entry, decrease a real partial exit")
reset()
quote(KEY, 20.0)
pos = enter(KEY, "BUY", 65, 1, 20.0, RULE)

before = open_ids()
res = live_manager.adjust_lots(KEY.position_id, 1)
check("the increase is accepted", res.get("ok") and res.get("delta") == 1, res)
confirm_new("BUY", 65, 22.0, SYMS[KEY], before)
grown = live_book.get(KEY.position_id)
check("quantity grew by exactly one lot", grown.qty == 130, grown.qty)
check("average price is the fill-weighted blend",
      abs(grown.avg_entry - 21.0) < 1e-6, grown.avg_entry)

before = open_ids()
res = live_manager.adjust_lots(KEY.position_id, -1)
check("the decrease is accepted", res.get("ok") and res.get("delta") == -1, res)
confirm_new("SELL", 65, 21.0, SYMS[KEY], before)
shrunk = live_book.get(KEY.position_id)
check("quantity shrank by exactly one lot", shrunk.qty == 65, shrunk.qty)
check("the rule (SL/Target) is untouched by a reduce",
      shrunk.sl == grown.sl and shrunk.target == grown.target,
      (shrunk.sl, shrunk.target))

section("       ...a second adjust cannot fire while an exit is already in flight")
live_book.begin_exit(KEY.position_id, shrunk.exitable_qty, "manual-exit")
res = live_manager.adjust_lots(KEY.position_id, -1)
check("refused with EXIT_IN_FLIGHT",
      not res.get("ok") and res.get("code") == "EXIT_IN_FLIGHT", res)
live_book.end_exit(KEY.position_id)


# ═════════════════════════════════════════════════════════════════════════
# [4] Partial Exit
# ═════════════════════════════════════════════════════════════════════════
section("[4] Partial Exit — quantity, MTM and realised P&L stay correct")
reset()
quote(KEY, 20.0)
pos = enter(KEY, "BUY", 130, 2, 20.0, RULE)

before = open_ids()
res = live_manager.close_position(KEY.position_id, 0.5)
check("the partial exit is accepted", res.get("ok") and res.get("qty") == 65, res)
confirm_new("SELL", 65, 26.0, SYMS[KEY], before)
half = live_book.get(KEY.position_id)
check("half the quantity remains", half.qty == 65, half.qty)
check("realised P&L booked the half that closed",
      abs(half.realised - (26.0 - 20.0) * 65) < 1e-6, half.realised)
quote(KEY, 24.0)
live_book.update_quote(KEY, 24.0)
half = live_book.get(KEY.position_id)
check("open MTM reflects only the remaining quantity",
      abs(half.pnl() - (24.0 - 20.0) * 65) < 1e-6, half.pnl())
check("the remaining rule is untouched by the partial exit",
      half.sl == 15.0 and half.target == 70.0, (half.sl, half.target))


# ═════════════════════════════════════════════════════════════════════════
# [5] & [6] Live SL / Target editing
# ═════════════════════════════════════════════════════════════════════════
section("[5] Live SL editing — takes effect on the very next cycle, no restart")
reset()
quote(KEY, 20.0)
pos = enter(KEY, "BUY", 65, 1, 20.0, RULE)
check("the manual edit is accepted",
      live_book.set_risk(KEY.position_id, sl=18.0))
before = open_ids()
quote(KEY, 17.5)                       # below the NEW sl, above the old one
live_manager.cycle()
check("the position exited at the EDITED stop, not the original one",
      len(open_ids() - before) == 1, open_ids() - before)
confirm_new("SELL", 65, 17.5, SYMS[KEY], before)
check("the position is flat and closed out",
      live_book.get(KEY.position_id) is None
      and any(p.key == KEY.position_id for p in live_book.closed_positions()))

section("[6] Live Target editing — applies instantly, other rules untouched")
reset()
quote(KEY, 20.0)
pos = enter(KEY, "BUY", 65, 1, 20.0, RULE)
check("the target edit is accepted",
      live_book.set_risk(KEY.position_id, target=25.0))
edited = live_book.get(KEY.position_id)
check("SL is unaffected by a target-only edit", edited.sl == 15.0, edited.sl)
before = open_ids()
quote(KEY, 26.0)                       # above the NEW target, below the original
live_manager.cycle()
check("the position exited at the EDITED target",
      len(open_ids() - before) == 1, open_ids() - before)
confirm_new("SELL", 65, 26.0, SYMS[KEY], before)


# ═════════════════════════════════════════════════════════════════════════
# [7] Trailing Stop Loss — including restart/reconnect survival
# ═════════════════════════════════════════════════════════════════════════
section("[7] Trailing SL — steps the stop, then survives a restart")
reset()
quote(KEY, 20.0)
pos = enter(KEY, "BUY", 65, 1, 20.0, TRAIL_RULE)
check("starting stop is the plain SL", pos.sl == 15.0, pos.sl)

quote(KEY, 24.0)                       # +4 pts: one 3-pt step earned -> +2
live_manager.cycle()
step1 = live_book.get(KEY.position_id)
check("the stop trailed up by one step", step1.sl == 17.0, step1.sl)

quote(KEY, 27.0)                       # +7 pts from entry: two steps earned
live_manager.cycle()
step2 = live_book.get(KEY.position_id)
check("a further favourable move trails again", step2.sl == 19.0, step2.sl)

section("       ...and the trailed stop survives a restart, not just the plain one")
live_store.flush()
disk_sl = None
import json                                                              # noqa: E402
with open(live_store.path, encoding="utf-8") as fh:
    for row in json.load(fh).get("positions", []):
        if row.get("key") == KEY.position_id:
            disk_sl = row.get("sl")
check("the trailed level (not the original) was persisted",
      disk_sl == 19.0, disk_sl)

live_book._positions.clear()
restored = live_book.restore()
check("one position restored", restored["restored"] == 1, restored)
rp = live_book.get(KEY.position_id)
check("the trailed stop survived the restart", rp.sl == 19.0, rp.sl)
check("it is not armed until Firstock re-confirms it", rp.verified_ts == 0)

# Re-confirmation after a restart comes from Firstock's OWN position book via
# reconciliation, not from an order fill — order-sync only ever updates an
# order it tracked itself, and nothing tracked this position's entry order
# any more after `live_book._positions.clear()` reset the in-memory book.
WIRE.responses["positionBook"] = [position_row(KEY, "BUY", 65, 20.0)]
reconciler.reconcile_once()
rp = live_book.get(KEY.position_id)
check("Firstock's own book re-armed the restored, trailed position",
      rp.verified_ts > 0, rp.verified_ts)

quote(KEY, 30.0)                       # +10 pts from entry: three steps earned
live_manager.cycle()
resumed = live_book.get(KEY.position_id)
check("trailing continues from the RESTORED level, not the original SL",
      resumed.sl == 21.0, resumed.sl)


# ═════════════════════════════════════════════════════════════════════════
# [8] Portfolio Trail — first test of this feature against a real broker
# ═════════════════════════════════════════════════════════════════════════
section("[8] Portfolio Trail — combined MTM across real Firstock positions")
reset()
quote(KEY, 20.0)
quote(KEY2, 30.0)
enter(KEY, "BUY", 65, 1, 20.0, RULE)
enter(KEY2, "BUY", 65, 1, 30.0, RULE)
# A third, EXTERNAL Firstock position — never opened by Charticks — must be
# excluded from both the combined P&L and the exit.
live_book.upsert_external(ROLL_KEY, "BUY", 65, 15.0, 15.0, 1, "acct-1", "firstock")
quote(ROLL_KEY, 15.0)

live_manager.set_portfolio_trail(
    {"enabled": True, "activateAfter": 500, "trailDistance": 200})

quote(KEY, 30.0); quote(KEY2, 40.0)     # combined open profit: 650 + 650 = 1300
live_manager.cycle()
check("armed once combined profit cleared activateAfter",
      live_manager._pt_armed, live_manager._peak_pnl)

before = open_ids()
quote(KEY, 24.0); quote(KEY2, 34.0)     # combined profit gave back 400 (> 200)
live_manager.cycle()
fired = open_ids() - before
check("BOTH managed legs received a real Firstock exit order in one pass",
      len(fired) == 2, fired)
for oid in fired:
    tracked = next(o for o in order_sync.open_orders() if o.order_id == oid)
    confirm(oid, "SELL", 65,
            24.0 if tracked.strike == 24000 else 34.0,
            SYMS[KEY] if tracked.strike == 24000 else SYMS[KEY2])
check("both managed positions are now flat and closed out",
      live_book.get(KEY.position_id) is None
      and live_book.get(KEY2.position_id) is None)
check("the external position was excluded from the sweep",
      live_book.get(ROLL_KEY.position_id).qty == 65)


# ═════════════════════════════════════════════════════════════════════════
# [9] Position Monitoring — the states not already proven generically
# ═════════════════════════════════════════════════════════════════════════
section("[9] Monitoring — NO_RULE, EXITING and PAUSED for a real Firstock position")
reset()
quote(KEY, 20.0)
# No rule at all — mirrors the hedge leg's own placement convention.
res = order_manager.place_order(
    LIVE, "NIFTY", EXPIRY, 24000, "CE", "BUY", 65, "MARKET", 0.0, lots=1,
    rule=None, product="NRML", validity="DAY", request_id="norule")
confirm(res["results"][0]["orderId"], "BUY", 65, 20.0, SYMS[KEY])
live_manager._update_monitoring()
check("a confirmed position with no SL/Target is NO_RULE",
      live_book.get(KEY.position_id).monitor == NO_RULE,
      live_book.get(KEY.position_id).monitor)

section("       ...EXITING while a real Firstock exit order is unconfirmed")
reset()
quote(KEY, 20.0)
pos = enter(KEY, "BUY", 65, 1, 20.0, RULE)
before = open_ids()
result = live_manager.close_position(KEY.position_id, 1.0)
check("the exit was sent", result.get("ok"), result)
live_manager._update_monitoring()
check("the position shows EXITING while the exit is unconfirmed",
      live_book.get(KEY.position_id).monitor == EXITING,
      live_book.get(KEY.position_id).monitor)
confirm_new("SELL", 65, 20.0, SYMS[KEY], before)

section("       ...PAUSED when Charticks is in Paper mode")
reset()
quote(KEY, 20.0)
pos = enter(KEY, "BUY", 65, 1, 20.0, RULE)
order_manager._mode = "paper"
live_manager._update_monitoring()
check("a Firstock position shows PAUSED while the app is in Paper mode",
      live_book.get(KEY.position_id).monitor == PAUSED,
      live_book.get(KEY.position_id).monitor)
order_manager._mode = LIVE


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
