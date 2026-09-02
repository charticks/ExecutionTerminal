"""End-to-end regression test for the `quant_preset` strategy plugin —
Legacy Strategy Migration Phase 1/6 (per the plan doc).

    python sidecar/tests/test_quant_preset_e2e.py

Reuses the real-pipeline harness proven in test_strategy_trading.py: only
`firstock_client._post` is stubbed, everything else — risk engine, margin,
idempotency, order_sync, live_book, LiveManager, and now the real
`quant_preset` plugin itself — runs for real. Candle ticks are injected
directly into the shared `candle_store` at chosen minute boundaries (the
same technique `test_strategy_market_data.py` uses) so candle closes are
deterministic instead of racing the wall clock.

This is the strongest available proof that the ported filter/entry/strike/
risk-rule modules are wired together correctly end to end — a real preset's
params drive a real signal, a real order, and a real LiveManager-managed
position, with zero strategy-specific code anywhere outside quant_preset.py
itself.
"""
import datetime as dt
import os
import sys
import tempfile
import time

SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CHARTICKS_DATA_DIR"] = tempfile.mkdtemp(prefix="charticks-qpe2e-")
os.environ["CHARTICKS_LOG_DIR"] = os.environ["CHARTICKS_DATA_DIR"]
sys.path.insert(0, SIDECAR)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <-- ' + str(detail)}")


def section(title):
    print(f"\n{title}")


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
bm.add_index_tick_listener = lambda fn: None
BrokerManager.option_feed_connected = property(lambda self: FEED_UP["on"])
market_session.is_market_open = lambda now=None, symbol=None: True

EXPIRY = "29SEP2026"
UNDERLYING = "NIFTY"
SPOT = 24500.0
FKEY = InstrumentKey.option(UNDERLYING, EXPIRY, 24500, "CE")
SYM = "NIFTY29SEP26C24500"
TOKEN = "35090"
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
WIRE.responses["orderMargin"] = {"marginOnNewOrder": "1000.00", "availableMargin": "999999.00"}
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
from services.strategy_engine.manager import StrategyManager              # noqa: E402
from services.strategy_engine.candles import candle_store                 # noqa: E402
from services.strategy_engine import risk_rules                           # noqa: E402
import services.strategy_engine.strategies  # noqa: E402,F401 — registers quant_preset

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


def quote(key, ltp, oi=None, bid=None, ask=None):
    QUOTES[key] = {"ltp": ltp, "bid": bid if bid is not None else ltp - 0.5,
                  "ask": ask if ask is not None else ltp + 0.5, "oi": oi, "ts": time.time()}


def confirm(order_id, side, qty, avg_price, symbol):
    WIRE.responses["orderBook"] = [{
        "orderNumber": order_id, "status": "COMPLETE",
        "fillShares": str(qty), "quantity": str(qty),
        "averagePrice": f"{avg_price:.2f}", "tradingSymbol": symbol,
        "transactionType": SIDE.get(side, side), "remarks": ""}]
    order_sync.ingest("firstock", "acct-1", firstock_orders(CLIENT_A), complete=True)


def feed_candle(key, ltp, minute, cum_vol):
    """Inject one tick into the shared candle_store at an EXACT minute,
    bypassing wall-clock timing — same technique test_strategy_market_data.py
    uses. Requires the series to already exist (i.e. the strategy has
    subscribed candles for `key`)."""
    matching = [s for (k, _tf), s in candle_store._series.items() if k == key]
    for series in matching:
        candle_store._feed(series, ltp, cum_vol, minute)


# A realistic preset — same shape as strategies/SENSEX-S1-BearDaySetup.json,
# adapted to NIFTY/CE for this test, quant filters OFF (already proven in
# isolation by test_strategy_legacy_port.py; this file's job is proving the
# WIRING, not re-proving each filter's math).
PRESET_PARAMS = {
    "index": "NIFTY", "lots": 1, "live_interval": "1min",
    "spot_detect_mode": "TIME", "spot_time": "00:01",   # always "already past"
    "entry_mode": "MARKET",
    "candle_sl_mode": "pct_entry", "sl_pct_entry": 40.0, "target_pct_entry": 50.0,
    "enable_tsl": False,
    "gexp_override": False,
    "strike_mode": "CUSTOM_RANGE", "custom_range_from": 0, "custom_range_to": 0,
    "custom_range_ce": True, "custom_range_pe": False,
    "enable_quant": False,
}


# ═════════════════════════════════════════════════════════════════════════
# [1] Arming — index tick -> strike selection -> candle subscription
# ═════════════════════════════════════════════════════════════════════════
section("[1] A real quant_preset instance arms off a spot tick and subscribes")
mgr = StrategyManager()
iid = mgr.create_instance("quant_preset", dict(PRESET_PARAMS))["id"]
res = mgr.start_instance(iid)
check("the instance starts cleanly", res.get("ok"), res)

mgr._dispatch_tick(UNDERLYING, SPOT)   # the arming tick
strategy = mgr.get(iid).strategy
check("the strategy armed off the spot tick", strategy._armed)
check("it selected exactly the ATM CE strike CUSTOM_RANGE(0,0,CE) describes",
      list(strategy._tokens.keys()) == [FKEY], list(strategy._tokens.keys()))
check("the contract has a real candle series subscribed",
      (FKEY, "1min") in candle_store._series, list(candle_store._series))


# ═════════════════════════════════════════════════════════════════════════
# [2] Candle-close entry -> real Firstock order -> real fill -> real position
# ═════════════════════════════════════════════════════════════════════════
section("[2] Five closed candles trigger a real MARKET entry")
t0 = dt.datetime(2026, 9, 1, 9, 20, 0)
quote(FKEY, 20.0, oi=60000)
before_orders = {o.order_id for o in order_sync.open_orders()}
for i in range(6):   # 6 ticks at 6 distinct minutes -> 5 CLOSED candles
    feed_candle(FKEY, 20.0 + i * 0.1, t0 + dt.timedelta(minutes=i), 1000 + i * 50)

closed = candle_store.get_candles(FKEY, "1min")
check("candles actually closed", closed is not None and len(closed) >= 5, closed)
new_orders = {o.order_id for o in order_sync.open_orders()} - before_orders
check("a real entry order was placed through Firstock", len(new_orders) == 1, new_orders)

order_id = next(iter(new_orders))
confirm(order_id, "BUY", 65, 20.5, SYM)

pos = live_book.get(FKEY.position_id)
check("the fill landed in live_book, owned by this strategy instance",
      pos is not None and pos.qty == 65, pos)
check("the strategy instance is recorded as the position's owner",
      mgr.owner_of(FKEY.position_id) == iid)
# pct_entry: sl = entry*(1-0.40) is nonsensical for an option premium of
# ~20 (would floor at MIN_PRICE) — deliberately chosen thresholds here are
# small percentages so the assertion is meaningful arithmetic, not a floor.
expected_sl = round(20.5 * (1 - 0.40), 2)
expected_target = round(20.5 * (1 + 0.50), 2)
check("SL matches the pct_entry formula (ported byte-for-byte from _get_candle_sl_target)",
      pos.sl == expected_sl, (pos.sl, expected_sl))
check("Target matches the pct_entry formula",
      pos.target == expected_target, (pos.target, expected_target))

state = strategy._tokens[FKEY]
check("no further entry is attempted for this contract today",
      state.entry_taken_today and state.trade_open)


# ═════════════════════════════════════════════════════════════════════════
# [3] LiveManager (unmodified) manages the exit — no strategy code involved
# ═════════════════════════════════════════════════════════════════════════
section("[3] LiveManager evaluates and exits it — zero strategy-aware code")
before = {o.order_id for o in order_sync.open_orders()}
quote(FKEY, expected_sl - 1.0)   # below the strategy-derived SL
live_manager.cycle()
new = {o.order_id for o in order_sync.open_orders()} - before
check("the SL fired through the ordinary LiveManager evaluation path", len(new) == 1, new)
confirm(next(iter(new)), "SELL", 65, expected_sl - 1.0, SYM)
check("the position is now closed",
      live_book.get(FKEY.position_id) is None
      and any(p.key == FKEY.position_id for p in live_book.closed_positions()))


# ═════════════════════════════════════════════════════════════════════════
# [4] GExp approach-1 preset — fixed SL%/R:R target
# ═════════════════════════════════════════════════════════════════════════
section("[4] A GExp approach-1 preset (SENSEX-S4 style) computes SL/Target correctly")
KEY2 = InstrumentKey.option(UNDERLYING, EXPIRY, 24600, "CE")
SYM2 = "NIFTY29SEP26C24600"
instruments.bind("firstock", KEY2, "NFO:35091")
instruments.alias("firstock", KEY2, "35091")
_orig_contract_for = FakeScrip.contract_for
FakeScrip.contract_for = staticmethod(
    lambda key: ({"exchange": "NFO", "tradingSymbol": SYM2, "token": "35091",
                 "subscribeId": "NFO:35091", "lotSize": 65, "tickSize": 0.05}
                if key == KEY2 else _orig_contract_for(key)))

gexp_params = {**PRESET_PARAMS, "custom_range_from": 100, "custom_range_to": 100,
              "gexp_override": True, "gexp_method": "approach1",
              "gexp_sl_pct": 35.0, "gexp_rr": 3.0}
mgr2 = StrategyManager()
iid2 = mgr2.create_instance("quant_preset", gexp_params)["id"]
mgr2.start_instance(iid2)
mgr2._dispatch_tick(UNDERLYING, SPOT)
strat2 = mgr2.get(iid2).strategy
check("the GExp preset selected the +100 strike CUSTOM_RANGE describes",
      list(strat2._tokens.keys()) == [KEY2], list(strat2._tokens.keys()))

QUOTE_PRICE2 = 25.0   # ctx.get_tick's snapshot at signal time — feed_candle()
                      # only injects candle data, it does NOT move the tick
                      # cache (that's broker_manager.option_ticks, updated by
                      # real tick ingestion in production); this is the price
                      # the strategy actually builds its GExp rule from.
FILL_PRICE2 = 25.5    # the confirmed average fill price — deliberately
                      # different from QUOTE_PRICE2, same as section [2]'s
                      # 20.0-vs-20.5 split, to prove the FIXED points offset
                      # gets applied to the real fill, not re-derived from it.
quote(KEY2, QUOTE_PRICE2, oi=60000)
before2 = {o.order_id for o in order_sync.open_orders()}
t1 = dt.datetime(2026, 9, 1, 10, 0, 0)
for i in range(6):
    feed_candle(KEY2, 25.0 + i * 0.1, t1 + dt.timedelta(minutes=i), 1000 + i * 50)
new2 = {o.order_id for o in order_sync.open_orders()} - before2
check("the GExp preset also placed a real entry order", len(new2) == 1, new2)
confirm(next(iter(new2)), "BUY", 65, FILL_PRICE2, SYM2)

pos2 = live_book.get(KEY2.position_id)
# GExp approach-1 resolves to a POINTS offset (risk_rules.py's Design
# Decision A) computed once from the signal-time estimate, then LiveManager
# (real, unmodified) applies that fixed offset to the actual fill — so the
# expected absolute SL/Target are anchored to FILL_PRICE2, not QUOTE_PRICE2.
gexp_rule = risk_rules.gexp_approach1_rule(QUOTE_PRICE2, 35.0, 3.0)
gexp_sl = round(FILL_PRICE2 - gexp_rule["slVal"], 2)
gexp_target = round(FILL_PRICE2 + gexp_rule["targetVal"], 2)
check("GExp approach-1 SL applies the signal-time points offset to the real fill",
      pos2.sl == gexp_sl, (pos2.sl, gexp_sl))
check("GExp approach-1 Target applies the same fixed R:R offset to the real fill",
      pos2.target == gexp_target, (pos2.target, gexp_target))

mgr2.stop_instance(iid2)
mgr.stop_instance(iid)


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
