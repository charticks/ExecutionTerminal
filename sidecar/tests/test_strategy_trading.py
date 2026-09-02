"""Regression tests for the Strategy Engine — Phase 3 Trading Integration.

    python sidecar/tests/test_strategy_trading.py

Covers: risk_rules.py's pure SL/Target derivation math, StrategyContext's
trading facade forwarding to the real order_manager with no protection
bypassed, position-ownership enforcement (a strategy instance cannot act on
a position it did not open), and — the strongest evidence for "all existing
protections continue to work" — one full real-pipeline scenario: a strategy
places a REAL entry through Firstock (only the network boundary stubbed,
exactly the harness test_firstock_live_management.py proved out), the real
risk/margin/idempotency gates run, the fill lands in live_book exactly like
a manual order's would, and LiveManager's own SL evaluation exits it with
zero strategy-specific code in LiveManager at all.
"""
import os
import sys
import tempfile
import time

import pandas as pd

SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CHARTICKS_DATA_DIR"] = tempfile.mkdtemp(prefix="charticks-strattrade-")
os.environ["CHARTICKS_LOG_DIR"] = os.environ["CHARTICKS_DATA_DIR"]
sys.path.insert(0, SIDECAR)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <-- ' + str(detail)}")


def section(title):
    print(f"\n{title}")


# ═════════════════════════════════════════════════════════════════════════
# [1] risk_rules.py — pure SL/Target derivation math
# ═════════════════════════════════════════════════════════════════════════
section("[1] risk_rules — pure math, no broker/manager involved")
from services.strategy_engine import risk_rules                           # noqa: E402

check("points mode passes the value straight through",
      risk_rules.build_rule(side="BUY", entry=100.0, sl_mode="points", sl_val=5,
                            target_mode="points", target_val=10)
      == {"slEnabled": True, "targetEnabled": True,
         "slMode": "points", "slVal": 5, "targetMode": "points", "targetVal": 10})

check("percent mode passes straight through too — LiveManager's own semantics",
      risk_rules.build_rule(side="SELL", entry=100.0, sl_mode="percent", sl_val=2)
      ["slMode"] == "percent")

check("a zero/absent value leaves that leg disabled, not zero-and-armed",
      risk_rules.build_rule(side="BUY", entry=100.0)["slEnabled"] is False)

candles = pd.DataFrame([
    {"open": 100, "high": 105, "low": 95, "close": 102},
    {"open": 102, "high": 108, "low": 98, "close": 90},   # the "previous" candle
])
rule = risk_rules.build_rule(side="BUY", entry=100.0, sl_mode="prev_ohlc",
                             sl_field="low", candles=candles)
check("prev_ohlc SL is derived from the last candle's chosen field",
      rule["slEnabled"] and rule["slMode"] == "points" and rule["slVal"] == 2.0,
      rule)   # entry 100 - low 98 = 2 points

rule = risk_rules.build_rule(side="BUY", entry=100.0, sl_mode="swing_low",
                             swing_lookback=2, candles=candles)
check("swing_low SL uses the min low over the lookback window",
      rule["slEnabled"] and rule["slVal"] == 5.0, rule)   # entry 100 - min(low)=95

section("       ...a derived price on the WRONG side of entry is refused, not inverted")
bad_candles = pd.DataFrame([{"open": 100, "high": 105, "low": 103, "close": 104}])
rule = risk_rules.build_rule(side="BUY", entry=100.0, sl_mode="prev_ohlc",
                             sl_field="low", candles=bad_candles)   # low=103 > entry
check("a prev-candle low ABOVE entry cannot become a BUY's stop",
      rule["slEnabled"] is False, rule)

check("missing candle data leaves the leg disabled rather than raising",
      risk_rules.build_rule(side="BUY", entry=100.0, sl_mode="prev_ohlc",
                            candles=None)["slEnabled"] is False)

check("points_offset rejects a SELL target on the wrong side",
      risk_rules.points_offset(100.0, 105.0, "SELL", "target") is None)
check("points_offset accepts a SELL target below entry",
      risk_rules.points_offset(100.0, 95.0, "SELL", "target") == 5.0)


# ═════════════════════════════════════════════════════════════════════════
# [2] StrategyContext.place_order — forwards to order_manager, no bypass
# ═════════════════════════════════════════════════════════════════════════
section("[2] StrategyContext.place_order forwards correctly, protections unbypassed")
from services.strategy_engine.base import Strategy, StrategyContext, StrategySpec  # noqa: E402
from services.strategy_engine import registry                             # noqa: E402
from services.strategy_engine.manager import StrategyManager              # noqa: E402
import services.strategy_engine.strategies  # noqa: E402,F401 — registers quant_preset

CAPTURED: list = []
import services.order_manager as om_module                                # noqa: E402

_real_place_order = om_module.order_manager.place_order
om_module.order_manager.place_order = lambda *a, **k: (
    CAPTURED.append((a, k)) or {"ok": True, "orderId": "T1", "results": []})


class NoopStrategy(Strategy):
    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        pass

    def on_stop(self) -> None:
        pass


registry.register(StrategySpec(name="noop_trader", label="", description="",
                               factory=NoopStrategy))
mgr = StrategyManager()
iid = mgr.create_instance("noop_trader", {})["id"]
mgr.start_instance(iid)
ctx = mgr.get(iid).ctx

res = ctx.place_order("NIFTY", "29SEP2026", 24000, "CE", "BUY", 65, 1,
                      rule={"slEnabled": True, "slMode": "points", "slVal": 5,
                           "targetEnabled": False}, tag="entry")
check("place_order reports success from order_manager's own result", res.get("ok"))
args, kwargs = CAPTURED[0]
check("mode is always LIVE — a strategy never places a paper order this way",
      args[0] == "live", args[0])
check("the contract/side/qty were forwarded unchanged",
      args[1:7] == ("NIFTY", "29SEP2026", 24000, "CE", "BUY", 65), args)
check("the rule was forwarded, not rebuilt or altered",
      kwargs.get("rule", {}).get("slVal") == 5, kwargs.get("rule"))
check("the request id is tagged with this instance — traceable, and gives "
      "idempotency a per-strategy-order fingerprint",
      kwargs.get("request_id", "").startswith(f"strategy:{iid}:"),
      kwargs.get("request_id"))
check("no override flag is ever set — a strategy order gets every protection "
      "a manual one does",
      "allow_duplicate" not in kwargs and "override_max_pos" not in kwargs, kwargs)

om_module.order_manager.place_order = _real_place_order   # restore


# ═════════════════════════════════════════════════════════════════════════
# [3] Position ownership enforcement
# ═════════════════════════════════════════════════════════════════════════
section("[3] A strategy instance cannot act on a position it did not open")
mgr2 = StrategyManager()
iid_a = mgr2.create_instance("noop_trader", {})["id"]
iid_b = mgr2.create_instance("noop_trader", {})["id"]
mgr2.start_instance(iid_a)
mgr2.start_instance(iid_b)
ctx_a, ctx_b = mgr2.get(iid_a).ctx, mgr2.get(iid_b).ctx

mgr2.claim_position("NIFTY|29SEP2026|24000|CE", iid_a)
check("the owning instance can act (ownership check passes)",
      ctx_a._require_ownership("NIFTY|29SEP2026|24000|CE") is None)
res = ctx_b.close_position("NIFTY|29SEP2026|24000|CE")
check("a different instance is refused with NOT_OWNER",
      not res.get("ok") and res.get("code") == "NOT_OWNER", res)
res = ctx_b.adjust_lots("NIFTY|29SEP2026|24000|CE", 1)
check("adjust_lots is ownership-gated the same way",
      not res.get("ok") and res.get("code") == "NOT_OWNER", res)
check("set_risk refuses (returns False) rather than raising for a non-owned position",
      ctx_b.set_risk("NIFTY|29SEP2026|24000|CE", sl=10.0) is False)
check("get_position is NOT ownership-gated — any instance may read",
      ctx_b.get_position("NIFTY|29SEP2026|24000|CE") is None)  # no real position exists; just proves it didn't refuse


# ═════════════════════════════════════════════════════════════════════════
# [4] Full real pipeline — a strategy order through Firstock, managed by the
#     REAL LiveManager, with zero strategy-aware code anywhere in it
# ═════════════════════════════════════════════════════════════════════════
section("[4] End-to-end: strategy entry -> real Firstock placement -> real fill "
       "-> real LiveManager SL exit")

from services.feeds import firstock_client                                # noqa: E402
from services.feeds.firstock_client import FirstockClient, SIDE           # noqa: E402
from services.instruments import InstrumentKey, instruments               # noqa: E402
from services import market_session                                       # noqa: E402
from services.broker_manager import BrokerManager, manager as bm          # noqa: E402

QUOTES: dict = {}
FEED_UP = {"on": True}


def _quote(ref):
    entry = QUOTES.get(ref if isinstance(ref, InstrumentKey) else None)
    if not entry:
        return None, None, None
    return entry["ltp"], entry.get("bid"), entry.get("ask")


bm.get_option_quote = _quote
bm.get_option_ltp = lambda ref: _quote(ref)[0]
bm.get_option_tick = lambda ref: dict(QUOTES.get(ref, {}))
bm.subscribe_option_keys = lambda keys: None
bm.option_meta = lambda u, e, s, o: {"lotSize": 65, "tickSize": 0.05}
bm.add_option_tick_listener = lambda fn: None
BrokerManager.option_feed_connected = property(lambda self: FEED_UP["on"])
market_session.is_market_open = lambda now=None, symbol=None: True

EXPIRY = "29SEP2026"
FKEY = InstrumentKey.option("NIFTY", EXPIRY, 24000, "CE")
SYM = "NIFTY29SEP26C24000"
TOKEN = "35085"
instruments.bind("firstock", FKEY, f"NFO:{TOKEN}")
instruments.alias("firstock", FKEY, TOKEN)


class FakeScrip:
    option_count = 1

    @staticmethod
    def contract_for(key):
        return ({"exchange": "NFO", "tradingSymbol": SYM, "token": TOKEN,
                "subscribeId": f"NFO:{TOKEN}", "lotSize": 65, "tickSize": 0.05}
               if key == FKEY else None)

    @staticmethod
    def explain_miss(key):
        return "not the one contract this test knows about"


bm.router.scrip_of = lambda broker, session=None: FakeScrip()


class Wire:
    def __init__(self):
        self.calls, self.responses, self.raises, self._seq = [], {}, {}, 0

    def install(self):
        firstock_client._post = self
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
        return {}


CLIENT_A = FirstockClient(user_id="acct-1", jkey="tok", actid="acct-1")
WIRE = Wire().install()
WIRE.responses["orderMargin"] = {"marginOnNewOrder": "1000.00",
                                 "availableMargin": "999999.00"}
WIRE.responses["limit"] = {"availableMargin": "999999.00"}
WIRE.responses["orderBook"] = []

bm.connected_sessions = lambda: [("acct-1", "firstock", CLIENT_A)]
bm.execution_accounts = lambda: {"acct-1"}
bm.execution_sessions = lambda: [("acct-1", "firstock", CLIENT_A)]

from services.risk_engine import risk_engine                              # noqa: E402
from services.idempotency.store import store as idempotency_store         # noqa: E402
from services.paths import data_dir                                       # noqa: E402
from services.live_book import live_book                                  # noqa: E402
from services.live_store import live_store                                # noqa: E402
from services.live_manager import live_manager                            # noqa: E402
from services.order_manager import order_manager                          # noqa: E402
from services.order_sync import order_sync                                # noqa: E402
from services.order_sync.brokers import firstock_orders                   # noqa: E402
import services.position_reconciler as pr                                 # noqa: E402

pr.manager = bm
order_manager._mode = "live"
live_manager._started = True
type(live_manager).running = property(lambda self: True)
risk_engine.set_config({})

live_book.reset()
live_store.clear()
order_sync.reset()
idempotency_store.reset()
for name in os.listdir(data_dir()):
    if name.startswith("idempotency_"):
        os.remove(os.path.join(data_dir(), name))


def quote(key, ltp):
    QUOTES[key] = {"ltp": ltp, "bid": ltp - 0.5, "ask": ltp + 0.5, "ts": time.time()}


def confirm(order_id, side, qty, avg_price, symbol):
    WIRE.responses["orderBook"] = [{
        "orderNumber": order_id, "status": "COMPLETE",
        "fillShares": str(qty), "quantity": str(qty),
        "averagePrice": f"{avg_price:.2f}", "tradingSymbol": symbol,
        "transactionType": SIDE.get(side, side), "remarks": ""}]
    order_sync.ingest("firstock", "acct-1", firstock_orders(CLIENT_A), complete=True)


class LiveStrategy(Strategy):
    """A minimal but REAL strategy: enters once on_start, with an SL derived
    through risk_rules.build_rule — exactly the shape a ported quant_preset
    instance would produce, just without the candle/filter machinery around
    the decision to enter."""

    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        rule = risk_rules.build_rule(side="BUY", entry=20.0,
                                     sl_mode="points", sl_val=5,
                                     target_mode="points", target_val=50)
        res = ctx.place_order("NIFTY", EXPIRY, 24000, "CE", "BUY", 65, 1,
                              rule=rule, product="NRML", tag="entry")
        ctx.log("info", "entry placed", ok=res.get("ok"))

    def on_stop(self) -> None:
        pass


registry.register(StrategySpec(name="live_strategy", label="", description="",
                               factory=LiveStrategy))
mgr3 = StrategyManager()
iid3 = mgr3.create_instance("live_strategy", {})["id"]
quote(FKEY, 20.0)
res = mgr3.start_instance(iid3)
check("the strategy instance started without error", res.get("ok"), res)

tracked = [o for o in order_sync.open_orders() if o.underlying == "NIFTY"]
check("the real order router tracked exactly one live order for this entry",
      len(tracked) == 1, tracked)
confirm(tracked[0].order_id, "BUY", 65, 20.0, SYM)

pos = live_book.get(FKEY.position_id)
check("the fill landed in live_book exactly like a manual order's would",
      pos is not None and pos.qty == 65 and pos.source == "charticks", pos)
check("the position is armed (verified_ts set) — a real broker-confirmed fill",
      pos.verified_ts > 0, pos.verified_ts)
check("the strategy instance was recorded as this position's owner",
      mgr3.owner_of(FKEY.position_id) == iid3, mgr3.owner_of(FKEY.position_id))
check("SL/Target derived by risk_rules landed on the position unchanged",
      pos.sl == 15.0 and pos.target == 70.0, (pos.sl, pos.target))

section("       ...LiveManager evaluates and exits it with NO strategy-aware code")
before = {o.order_id for o in order_sync.open_orders()}
quote(FKEY, 14.0)   # below the strategy-derived SL
live_manager.cycle()
new_orders = {o.order_id for o in order_sync.open_orders()} - before
check("the SL fired through the ordinary LiveManager evaluation path",
      len(new_orders) == 1, new_orders)
confirm(next(iter(new_orders)), "SELL", 65, 14.0, SYM)
check("the position is now closed",
      live_book.get(FKEY.position_id) is None
      and any(p.key == FKEY.position_id for p in live_book.closed_positions()))


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
