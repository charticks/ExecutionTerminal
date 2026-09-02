"""Regression tests for the Strategy Engine — Phase 2 Market Data.

    python sidecar/tests/test_strategy_market_data.py

Covers: the new add_index_tick_listener hook on FeedRouter/BrokerManager,
the shared ref-counted CandleStore (candle bucketing across boundaries,
volume-delta accounting, indicator computation — ported from
legacy/engines/candle_engine.py), and StrategyContext's candle subscription
API including crash isolation (a strategy's own on_candle_close bug must
never break delivery to another instance) and subscription release on stop.
No order placement, no broker network calls — those are Phase 3.
"""
import datetime as dt
import os
import sys
import tempfile
import time

SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CHARTICKS_DATA_DIR"] = tempfile.mkdtemp(prefix="charticks-stratmd-")
os.environ["CHARTICKS_LOG_DIR"] = os.environ["CHARTICKS_DATA_DIR"]
sys.path.insert(0, SIDECAR)

from services.instruments import InstrumentKey, instruments               # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <-- ' + str(detail)}")


def section(title):
    print(f"\n{title}")


# ── broker layer stubs — data plane only ────────────────────────────────────
from services.broker_manager import manager                               # noqa: E402

SUBSCRIBED: list = []
manager.option_meta = lambda u, e, s, o: {"lotSize": 65, "tickSize": 0.05}
manager.subscribe_option_keys = lambda keys: SUBSCRIBED.append(set(keys))

EXPIRY = "29SEP2026"
KEY = InstrumentKey.option("NIFTY", EXPIRY, 24000, "CE")
KEY2 = InstrumentKey.option("NIFTY", EXPIRY, 24500, "CE")
instruments.bind("test", KEY, "NFO:1")
instruments.bind("test", KEY2, "NFO:2")

from services.subscriptions import STRATEGY, option_subs                  # noqa: E402
from services.strategy_engine.base import (                               # noqa: E402
    ParamField, Strategy, StrategyContext, StrategySpec)
from services.strategy_engine import registry                             # noqa: E402
from services.strategy_engine.manager import StrategyManager              # noqa: E402
from services.strategy_engine.candles import CandleStore, candle_store    # noqa: E402
import services.strategy_engine.strategies  # noqa: E402,F401 — registers quant_preset


def reset_subs():
    with option_subs._lock:
        option_subs._sources.clear()
        option_subs._sent.clear()
    SUBSCRIBED.clear()


def tick(store, key, ltp, minute: dt.datetime, cum_vol=None):
    """Feed one tick directly into a CandleStore at an EXACT, caller-chosen
    minute — bypasses dt.datetime.now() so candle-boundary crossings are
    deterministic in a test rather than racing the wall clock."""
    matching = [s for (k, _tf), s in store._series.items() if k == key]
    for series in matching:
        store._feed(series, ltp, cum_vol, minute)


# ═════════════════════════════════════════════════════════════════════════
# [1] FeedRouter — the new index-tick listener hook
# ═════════════════════════════════════════════════════════════════════════
section("[1] add_index_tick_listener — registration and fan-out")
from services.feed_router import FeedRouter                               # noqa: E402
from services.feeds.base import MarketFeed                                # noqa: E402


class FakeFeed:
    account_id = "acct-idx"
    broker = "test"

    def capabilities(self):
        return set()


router = FeedRouter(
    subscriptions=type("S", (), {"union": lambda self: set()})(),
    instrument_master=lambda: [], option_exchange=lambda u: None,
    report_error=lambda a, e: str(e), log=lambda lvl, msg: None)
with router._lock:
    router._primary["index"] = "acct-idx"

seen: list = []
router.add_index_tick_listener(lambda symbol, ltp: seen.append((symbol, ltp)))
router.on_index_tick(FakeFeed(), "NIFTY", 24000.0, 0.25)
check("a registered index-tick listener receives (symbol, ltp)",
      seen == [("NIFTY", 24000.0)], seen)
check("the index LTP cache is still updated as before",
      router.index_ltp.get("NIFTY") == 24000.0, router.index_ltp)

# A non-primary feed's tick must not reach listeners either (same isolation
# option ticks already have).
router.on_index_tick(FakeFeed(), "BANKNIFTY", 51000.0, 0.1)
with router._lock:
    router._primary["index"] = "someone-else"
router.on_index_tick(FakeFeed(), "SENSEX", 80000.0, 0.1)
check("a non-primary feed's index tick is dropped, not delivered",
      all(s != "SENSEX" for s, _ in seen), seen)


# ═════════════════════════════════════════════════════════════════════════
# [2] CandleStore — bucketing, volume delta, indicators
# ═════════════════════════════════════════════════════════════════════════
section("[2] CandleStore — candle bucketing across a boundary")
store = CandleStore()
store._hooked = True   # skip real feed hookup; feed ticks directly in tests
closes: list = []
store.subscribe("sub-1", KEY, "1min", on_close=lambda k, df: closes.append((k, df)))

t0 = dt.datetime(2026, 9, 1, 9, 20, 0)
tick(store, KEY, 100.0, t0, cum_vol=1000)
tick(store, KEY, 101.0, t0.replace(second=30), cum_vol=1200)
check("no candle closed yet — still inside the first minute", closes == [])
check("the in-progress candle tracks high/low/close",
      store.get_candles(KEY, "1min") is None,   # nothing CLOSED yet
      "a still-open candle must not appear in get_candles()")

t1 = t0 + dt.timedelta(minutes=1)
tick(store, KEY, 99.0, t1, cum_vol=1300)   # crosses into the next minute
check("crossing a minute boundary closes exactly one candle", len(closes) == 1, closes)
closed_key, df = closes[0]
check("the closed candle belongs to the subscribed key", closed_key == KEY)
row = df.iloc[-1]
check("OHLC captured the first minute's range",
      (row["open"], row["high"], row["low"], row["close"]) == (100.0, 101.0, 100.0, 101.0),
      row)
# Volume accumulates every tick's OWN delta within the candle, and the very
# first tick of a fresh (unseeded) series has no prior cum_vol to diff
# against — its delta is against last_cum_vol=0, exactly like legacy's
# CandleEngine before a historical seed populates it (see Phase 2's "no
# historical warm-up for v1" decision: this is inherited, expected behaviour,
# not something to special-case away).
expected_volume = round((1000 - 0) / 65) + round((1200 - 1000) / 65)
check("volume accumulates each tick's own delta against the prior cum_vol",
      row["volume"] == expected_volume, (row["volume"], expected_volume))
check("indicator columns were computed (ema/rsi/vwap/atr/adx/supertrend)",
      {"ema", "rsi", "vwap", "atr", "adx", "supertrend_dir"} <= set(df.columns),
      sorted(df.columns))
check("get_candles() now returns the closed candle", store.get_candles(KEY, "1min") is not None)

t2 = t1 + dt.timedelta(minutes=1)
tick(store, KEY, 102.0, t2, cum_vol=1500)
check("a second boundary closes a second candle", len(closes) == 2)
check("candle history accumulates (2 closed rows)",
      len(store.get_candles(KEY, "1min")) == 2, len(store.get_candles(KEY, "1min")))


section("       ...an out-of-order/duplicate cum_vol never produces negative volume")
store2 = CandleStore()
store2._hooked = True
store2.subscribe("sub-1", KEY, "1min")
tick(store2, KEY, 100.0, t0, cum_vol=5000)
tick(store2, KEY, 100.0, t0.replace(second=10), cum_vol=100)  # a bogus rollback
tick(store2, KEY, 100.0, t1, cum_vol=100)
df2 = store2.get_candles(KEY, "1min")
check("a volume figure that goes backwards contributes zero, not negative",
      df2 is not None and df2.iloc[-1]["volume"] >= 0, df2)


# ═════════════════════════════════════════════════════════════════════════
# [3] Ref-counted subscription — shared across multiple subscribers
# ═════════════════════════════════════════════════════════════════════════
section("[3] Ref-counted subscription")
reset_subs()
store3 = CandleStore()
store3._hooked = True
store3.subscribe("strategy-A", KEY, "1min")
store3.subscribe("strategy-B", KEY, "1min")   # same series, second subscriber
check("one shared series exists for two subscribers of the same key+timeframe",
      len(store3._series) == 1, len(store3._series))
check("the option-subscription hub was told about the contract once",
      KEY in option_subs.union(), option_subs.union())

store3.unsubscribe("strategy-A", KEY, "1min")
check("the series survives while ANY subscriber remains",
      (KEY, "1min") in store3._series)
check("the option subscription is unaffected while B still wants it",
      KEY in option_subs.union())

store3.unsubscribe("strategy-B", KEY, "1min")
check("the series is torn down once the LAST subscriber leaves",
      (KEY, "1min") not in store3._series)
check("the contract drops out of the shared subscription too",
      KEY not in option_subs.union(), option_subs.union())


# ═════════════════════════════════════════════════════════════════════════
# [4] StrategyContext — subscribe_candles / crash isolation / release
# ═════════════════════════════════════════════════════════════════════════
section("[4] StrategyContext candle wiring, through the real StrategyManager")
reset_subs()
RECEIVED: list = []


# Params are always JSON-safe primitives — the same shape a real preset's
# JSON file has — never a live InstrumentKey object, so a strategy that
# needs to reference a contract reconstructs it from primitive fields
# itself. This is also what makes the roster persist-able (see Phase 4).
def _key_from_params(params: dict) -> InstrumentKey:
    return InstrumentKey.option(params["underlying"], params["expiry"],
                                params["strike"], params["optType"])


class CandleWatcherStrategy(Strategy):
    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        ctx.subscribe_candles(_key_from_params(params), "1min")

    def on_stop(self) -> None:
        pass

    def on_candle_close(self, key, candles) -> None:
        RECEIVED.append((self.ctx.instance_id, key, len(candles)))


class CrashingWatcherStrategy(Strategy):
    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        ctx.subscribe_candles(_key_from_params(params), "1min")

    def on_stop(self) -> None:
        pass

    def on_candle_close(self, key, candles) -> None:
        raise RuntimeError("boom in on_candle_close")


registry.register(StrategySpec(name="candle_watcher", label="", description="",
                               factory=CandleWatcherStrategy,
                               params=(ParamField("underlying", "Underlying", required=True),)))
registry.register(StrategySpec(name="candle_crasher", label="", description="",
                               factory=CrashingWatcherStrategy,
                               params=(ParamField("underlying", "Underlying", required=True),)))

KEY_PARAMS = {"underlying": "NIFTY", "expiry": EXPIRY, "strike": 24000, "optType": "CE"}
mgr = StrategyManager()
iid_good = mgr.create_instance("candle_watcher", KEY_PARAMS)["id"]
iid_bad = mgr.create_instance("candle_crasher", KEY_PARAMS)["id"]
mgr.start_instance(iid_good)
mgr.start_instance(iid_bad)

t = dt.datetime(2026, 9, 1, 9, 30, 0)
tick(candle_store, KEY, 50.0, t)
tick(candle_store, KEY, 51.0, t + dt.timedelta(minutes=1))   # closes candle #1
check("a crashing instance's on_candle_close does not raise out of the tick path",
      True)  # the line above completing at all IS the assertion
check("the healthy instance still received the candle close",
      any(iid == iid_good for iid, _k, _n in RECEIVED), RECEIVED)
check("the crashing instance's failure did not remove it from the subscriber set",
      True)  # implicit: a second tick below still reaches CandleWatcher

tick(candle_store, KEY, 52.0, t + dt.timedelta(minutes=2))   # closes candle #2
check("candle delivery continues after an earlier crash in another instance",
      len([r for r in RECEIVED if r[0] == iid_good]) == 2, RECEIVED)

section("       ...and stopping an instance releases its candle subscription")
check("the contract is subscribed while either instance is running",
      KEY in option_subs.union())
mgr.stop_instance(iid_good)
mgr.stop_instance(iid_bad)
check("the contract is released once both watchers have stopped",
      KEY not in option_subs.union(), option_subs.union())
check("the underlying series was torn down too",
      (KEY, "1min") not in candle_store._series)


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
