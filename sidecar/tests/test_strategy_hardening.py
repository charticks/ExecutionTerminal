"""Regression tests for the Strategy Engine — Phase 6 Production Hardening.

    python sidecar/tests/test_strategy_hardening.py

Covers the gaps this phase's own review found and fixed:
  [1] Strategy.on_tick — declared since Phase 1, never actually wired to
      anything until now. Verifies subscribe_ticks/unsubscribe_ticks,
      crash isolation (one instance's on_tick bug must not block delivery
      to another instance or to the next tick), and release on stop.
  [2] restore() with a corrupted roster file — a non-dict entry and an
      entry with a malformed field must be skipped, not abort every OTHER
      configured instance's restore.
  [3] Thread safety — concurrent start/stop from multiple real threads
      hammering the same and different instances must never corrupt the
      manager's internal state or deadlock.
  [4] A performance sanity bound — many instances sharing overlapping
      candle/tick subscriptions, fed a tick burst, complete in bounded time
      (a floor against an accidental O(n^2) or worse, not a strict
      benchmark — thresholds are deliberately generous).
"""
import json
import os
import sys
import tempfile
import threading
import time

SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CHARTICKS_DATA_DIR"] = tempfile.mkdtemp(prefix="charticks-strathard-")
os.environ["CHARTICKS_LOG_DIR"] = os.environ["CHARTICKS_DATA_DIR"]
sys.path.insert(0, SIDECAR)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <-- ' + str(detail)}")


def section(title):
    print(f"\n{title}")


from services.instruments import InstrumentKey, instruments               # noqa: E402

# ── broker layer stubs — data plane only ────────────────────────────────────
from services.broker_manager import manager                               # noqa: E402

manager.option_meta = lambda u, e, s, o: {"lotSize": 65, "tickSize": 0.05}
manager.subscribe_option_keys = lambda keys: None

EXPIRY = "29SEP2026"
KEY = InstrumentKey.option("NIFTY", EXPIRY, 24000, "CE")
instruments.bind("test", KEY, "NFO:1")

from services.strategy_engine.base import Strategy, StrategyContext, StrategySpec  # noqa: E402
from services.strategy_engine import registry                             # noqa: E402
from services.strategy_engine.manager import (                            # noqa: E402
    ERROR, RUNNING, StrategyManager)


# ═════════════════════════════════════════════════════════════════════════
# [1] on_tick — wiring, delivery, crash isolation, release
# ═════════════════════════════════════════════════════════════════════════
section("[1] Strategy.on_tick — subscribe, deliver, isolate, release")
TICKS_SEEN: list = []


class TickWatcher(Strategy):
    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        ctx.subscribe_ticks(params["key"])

    def on_stop(self) -> None:
        pass

    def on_tick(self, key, ltp) -> None:
        TICKS_SEEN.append((self.ctx.instance_id, key, ltp))


class TickCrasher(Strategy):
    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        ctx.subscribe_ticks(params["key"])

    def on_stop(self) -> None:
        pass

    def on_tick(self, key, ltp) -> None:
        raise RuntimeError("boom in on_tick")


registry.register(StrategySpec(name="tick_watcher", label="", description="",
                               factory=TickWatcher))
registry.register(StrategySpec(name="tick_crasher", label="", description="",
                               factory=TickCrasher))

mgr = StrategyManager()
iid_good = mgr.create_instance("tick_watcher", {"key": KEY})["id"]
iid_bad = mgr.create_instance("tick_crasher", {"key": KEY})["id"]
mgr.start_instance(iid_good)
mgr.start_instance(iid_bad)

check("the good instance is registered as a tick subscriber",
      iid_good in mgr._tick_subs.get(KEY, set()), mgr._tick_subs)

mgr._dispatch_tick(KEY, 100.0)
check("on_tick fired for the subscribed instance", TICKS_SEEN == [(iid_good, KEY, 100.0)],
      TICKS_SEEN)
check("a crashing on_tick did not raise out of dispatch (this line running proves it)", True)

TICKS_SEEN.clear()
mgr._dispatch_tick(KEY, 101.0)
check("delivery to the healthy instance continues after the other one crashed",
      TICKS_SEEN == [(iid_good, KEY, 101.0)], TICKS_SEEN)

mgr.stop_instance(iid_good)
TICKS_SEEN.clear()
mgr._dispatch_tick(KEY, 102.0)
check("a stopped instance's tick subscription was released — no delivery, no stale entry",
      TICKS_SEEN == [] and iid_good not in mgr._tick_subs.get(KEY, set()), TICKS_SEEN)

mgr.stop_instance(iid_bad)


# ═════════════════════════════════════════════════════════════════════════
# [2] restore() tolerates a corrupted roster file
# ═════════════════════════════════════════════════════════════════════════
section("[2] A corrupted roster entry is skipped, not fatal to the whole restore")


class Noop(Strategy):
    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        pass

    def on_stop(self) -> None:
        pass


registry.register(StrategySpec(name="noop2", label="", description="", factory=Noop))

mgr2 = StrategyManager()
roster_path = mgr2._roster_path
os.makedirs(os.path.dirname(roster_path), exist_ok=True)
with open(roster_path, "w", encoding="utf-8") as fh:
    json.dump({
        "good-1": {"specName": "noop2", "params": {}, "autoStart": True, "createdTs": 1.0},
        "not-a-dict": "this is a string, not an object",
        "good-2": {"specName": "noop2", "params": {}, "autoStart": False, "createdTs": "not-a-number"},
        123: {"specName": "noop2", "params": {}, "autoStart": True, "createdTs": 2.0},
    }, fh)

res = mgr2.restore()
check("restore() did not raise on the corrupted file (this line running proves it)", True)
check("the two well-formed entries were restored despite the malformed ones",
      res["restored"] == 3, res)   # good-1, good-2, and the int-keyed one (JSON stringifies keys)
check("the auto-start entries actually started",
      mgr2.get("good-1") is not None and mgr2.get("good-1").state == RUNNING, res)
check("an entry with a bad createdTs still restored (fell back to now())",
      mgr2.get("good-2") is not None, mgr2.get("good-2"))


# ═════════════════════════════════════════════════════════════════════════
# [3] Thread safety — concurrent start/stop from real threads
# ═════════════════════════════════════════════════════════════════════════
section("[3] Concurrent start/stop from multiple threads corrupts nothing")
mgr3 = StrategyManager()
ids = [mgr3.create_instance("noop2", {})["id"] for _ in range(8)]
stop_flag = threading.Event()
errors: list = []


def hammer(instance_id: str) -> None:
    while not stop_flag.is_set():
        try:
            mgr3.start_instance(instance_id)
            mgr3.stop_instance(instance_id)
            mgr3.list_instances()
        except Exception as exc:  # pragma: no cover - failure path itself is the check
            errors.append(exc)


threads = [threading.Thread(target=hammer, args=(iid,), daemon=True) for iid in ids
          for _ in range(3)]   # 3 threads hammering each of the 8 instances = 24 threads
for t in threads:
    t.start()
time.sleep(1.0)
stop_flag.set()
for t in threads:
    t.join(timeout=5)

check("no exception escaped any hammering thread", errors == [], errors[:3])
check("the manager's roster is still exactly the instances created — nothing "
      "duplicated or lost under concurrent access",
      {row["id"] for row in mgr3.list_instances()} == set(ids),
      {row["id"] for row in mgr3.list_instances()})
check("every instance ended up in a valid terminal-ish state (not corrupted mid-transition)",
      all(mgr3.get(iid).state in ("new", "running", "stopped", "error") for iid in ids),
      [mgr3.get(iid).state for iid in ids])
for iid in ids:   # tidy up whichever ended up RUNNING
    if mgr3.get(iid).state == RUNNING:
        mgr3.stop_instance(iid)


# ═════════════════════════════════════════════════════════════════════════
# [4] Performance sanity bound — N instances, overlapping subscriptions, a
#     tick burst
# ═════════════════════════════════════════════════════════════════════════
section("[4] Many instances sharing overlapping subscriptions handle a tick burst quickly")
from services.strategy_engine.candles import candle_store                 # noqa: E402

KEY2 = InstrumentKey.option("NIFTY", EXPIRY, 24500, "CE")
instruments.bind("test", KEY2, "NFO:2")
SHARED_KEYS = [KEY, KEY2]

N_INSTANCES = 30


class LoadStrategy(Strategy):
    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        key = SHARED_KEYS[hash(self.ctx.instance_id) % len(SHARED_KEYS)]
        ctx.subscribe_candles(key, "1min")
        ctx.subscribe_ticks(key)

    def on_stop(self) -> None:
        pass

    def on_tick(self, key, ltp) -> None:
        pass


registry.register(StrategySpec(name="load_strategy", label="", description="",
                               factory=LoadStrategy))
mgr4 = StrategyManager()
load_ids = []
for _ in range(N_INSTANCES):
    iid = mgr4.create_instance("load_strategy", {})["id"]
    mgr4.start_instance(iid)
    load_ids.append(iid)

check(f"all {N_INSTANCES} instances started despite sharing only 2 underlying series",
      all(mgr4.get(iid).state == RUNNING for iid in load_ids),
      [mgr4.get(iid).state for iid in load_ids])
check("the candle store only built 2 series, not one per instance (the ref-counting works)",
      len({k for (k, _tf) in candle_store._series if k in SHARED_KEYS}) <= 2,
      list(candle_store._series))

N_TICKS = 500
t0 = time.time()
for i in range(N_TICKS):
    mgr4._dispatch_tick(SHARED_KEYS[i % 2], 100.0 + (i % 20))
elapsed = time.time() - t0
check(f"{N_TICKS} ticks fanned out to {N_INSTANCES} instances completed in a sane bound",
      elapsed < 5.0, f"{elapsed:.2f}s")

for iid in load_ids:
    mgr4.stop_instance(iid)
check("stopping every instance releases every tick subscription",
      not mgr4._tick_subs.get(KEY) and not mgr4._tick_subs.get(KEY2),
      mgr4._tick_subs)


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
