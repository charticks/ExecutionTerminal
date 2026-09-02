"""Regression tests for the Strategy Engine — Phase 1 Framework.

    python sidecar/tests/test_strategy_framework.py

Covers the registry (register/discover/duplicate-rejection), the manager's
instance roster (create/list/remove), per-instance lifecycle (start/stop,
independent state across instances of the SAME spec — the structural fix
over the legacy Tkinter bot's shared app-object state), and crash isolation
at the lifecycle boundary (a failing on_start/on_stop must not crash the
manager or affect other instances). No market data, no order placement, no
broker of any kind — those are Phase 2/3. This file also proves the shipped
`quant_preset` plugin is discoverable and its declared param schema is sane,
without exercising its (not-yet-written) trading logic.
"""
import os
import sys
import tempfile
import time

SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CHARTICKS_DATA_DIR"] = tempfile.mkdtemp(prefix="charticks-stratfw-")
os.environ["CHARTICKS_LOG_DIR"] = os.environ["CHARTICKS_DATA_DIR"]
sys.path.insert(0, SIDECAR)

from services.strategy_engine.base import (                             # noqa: E402
    ParamField, Strategy, StrategyContext, StrategySpec)
from services.strategy_engine import registry                           # noqa: E402
from services.strategy_engine.manager import (                          # noqa: E402
    ERROR, NEW, RUNNING, STOPPED, StrategyManager)
import services.strategy_engine.strategies  # noqa: E402,F401 — registers quant_preset

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <-- ' + str(detail)}")


def section(title):
    print(f"\n{title}")


# ── a small, observable test double — separate from the shipped quant_preset
#    plugin so lifecycle mechanics are verified independently of its logic ──
CALLS: list[tuple[str, str, dict]] = []   # (event, instance_id, params)


class RecordingStrategy(Strategy):
    def __init__(self):
        self.started_with: dict | None = None   # per-INSTANCE state
        self.stop_count = 0

    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        self.started_with = dict(params)
        CALLS.append(("start", ctx.instance_id, dict(params)))

    def on_stop(self) -> None:
        self.stop_count += 1
        CALLS.append(("stop", self.ctx.instance_id, {}))


class FailingOnStartStrategy(Strategy):
    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        raise RuntimeError("boom on start")

    def on_stop(self) -> None:
        pass


class FailingOnStopStrategy(Strategy):
    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        pass

    def on_stop(self) -> None:
        raise RuntimeError("boom on stop")


registry.register(StrategySpec(
    name="recording", label="Recording", description="test double",
    factory=RecordingStrategy,
    params=(ParamField("index", "Index", required=True),)))
registry.register(StrategySpec(
    name="fails_on_start", label="Fails on start", description="test double",
    factory=FailingOnStartStrategy))
registry.register(StrategySpec(
    name="fails_on_stop", label="Fails on stop", description="test double",
    factory=FailingOnStopStrategy))


# ═════════════════════════════════════════════════════════════════════════
# [1] Registry
# ═════════════════════════════════════════════════════════════════════════
section("[1] Registry — register / discover / duplicate rejection")
check("the shipped quant_preset plugin registered itself on import",
      registry.spec_for("quant_preset") is not None)
check("lookup is case-insensitive", registry.spec_for("QUANT_PRESET") is not None)
check("the test double registered", registry.spec_for("recording") is not None)
check("an unregistered name resolves to None", registry.spec_for("nope") is None)
check("registered_strategies() lists every registered name",
      set(registry.registered_strategies()) >= {"quant_preset", "recording",
                                                 "fails_on_start", "fails_on_stop"},
      registry.registered_strategies())
try:
    registry.register(StrategySpec(name="recording", label="dup",
                                   description="", factory=RecordingStrategy))
    check("re-registering the same name is refused", False, "did not raise")
except ValueError:
    check("re-registering the same name is refused", True)

qp = registry.spec_for("quant_preset")
check("quant_preset declares its index/lots/timeframe params",
      {"index", "lots", "live_interval"} <= {p.key for p in qp.params},
      [p.key for p in qp.params])


# ═════════════════════════════════════════════════════════════════════════
# [2] Instance roster — create / list / remove
# ═════════════════════════════════════════════════════════════════════════
section("[2] Instance roster")
mgr = StrategyManager()

res = mgr.create_instance("no_such_strategy", {})
check("creating against an unknown strategy is refused",
      not res.get("ok") and res.get("code") == "UNKNOWN_STRATEGY", res)

res = mgr.create_instance("recording", {"index": "NIFTY"})
check("creating a valid instance succeeds", res.get("ok"), res)
iid1 = res["id"]
inst = mgr.get(iid1)
check("a new instance starts in NEW state", inst.state == NEW, inst.state)
check("it appears in list_instances()",
      any(row["id"] == iid1 for row in mgr.list_instances()))

res = mgr.remove_instance("not-a-real-id")
check("removing an unknown id is refused",
      not res.get("ok") and res.get("code") == "NOT_FOUND", res)

res = mgr.remove_instance(iid1)
check("removing a never-started instance succeeds", res.get("ok"), res)
check("it is gone from the roster",
      not any(row["id"] == iid1 for row in mgr.list_instances()))


# ═════════════════════════════════════════════════════════════════════════
# [3] Lifecycle — start / stop, independent state per instance
# ═════════════════════════════════════════════════════════════════════════
section("[3] Lifecycle — start/stop and independent per-instance state")
CALLS.clear()
iid_a = mgr.create_instance("recording", {"index": "NIFTY", "lots": 1})["id"]
iid_b = mgr.create_instance("recording", {"index": "SENSEX", "lots": 5})["id"]

res = mgr.start_instance(iid_a)
check("starting instance A succeeds", res.get("ok"), res)
res = mgr.start_instance(iid_b)
check("starting instance B succeeds", res.get("ok"), res)

check("both instances are RUNNING",
      mgr.get(iid_a).state == RUNNING and mgr.get(iid_b).state == RUNNING)
check("on_start fired once per instance, with THAT instance's own params",
      CALLS == [("start", iid_a, {"index": "NIFTY", "lots": 1}),
               ("start", iid_b, {"index": "SENSEX", "lots": 5})], CALLS)
check("each instance's strategy object holds its OWN params — no shared state",
      mgr.get(iid_a).strategy.started_with == {"index": "NIFTY", "lots": 1}
      and mgr.get(iid_b).strategy.started_with == {"index": "SENSEX", "lots": 5})
check("instance A's strategy object is not instance B's",
      mgr.get(iid_a).strategy is not mgr.get(iid_b).strategy)

res = mgr.start_instance(iid_a)
check("starting an already-running instance is a harmless no-op",
      res.get("ok") and "Already running" in res.get("detail", ""), res)

res = mgr.remove_instance(iid_a)
check("a RUNNING instance cannot be removed",
      not res.get("ok") and res.get("code") == "STILL_RUNNING", res)

res = mgr.stop_instance(iid_a)
check("stopping instance A succeeds", res.get("ok"), res)
check("instance A is STOPPED, B is still RUNNING",
      mgr.get(iid_a).state == STOPPED and mgr.get(iid_b).state == RUNNING)
check("on_stop fired for A only", ("stop", iid_a, {}) in CALLS
      and ("stop", iid_b, {}) not in CALLS, CALLS)

res = mgr.stop_instance(iid_a)
check("stopping an already-stopped instance is a harmless no-op",
      res.get("ok") and "Already stopped" in res.get("detail", ""), res)

res = mgr.start_instance("not-a-real-id")
check("starting an unknown id is refused",
      not res.get("ok") and res.get("code") == "NOT_FOUND", res)

mgr.stop_instance(iid_b)   # tidy up


# ═════════════════════════════════════════════════════════════════════════
# [4] Crash isolation at the lifecycle boundary
# ═════════════════════════════════════════════════════════════════════════
section("[4] A failing plugin cannot crash the manager or another instance")
iid_bad = mgr.create_instance("fails_on_start", {})["id"]
res = mgr.start_instance(iid_bad)
check("a raising on_start is caught, not propagated", not res.get("ok")
      and res.get("code") == "START_FAILED", res)
check("the instance lands in ERROR with the exception message recorded",
      mgr.get(iid_bad).state == ERROR and "boom on start" in mgr.get(iid_bad).error,
      mgr.get(iid_bad))

section("       ...and a healthy instance still starts fine right after")
iid_ok = mgr.create_instance("recording", {"index": "NIFTY"})["id"]
res = mgr.start_instance(iid_ok)
check("an unrelated instance is unaffected by the earlier failure",
      res.get("ok") and mgr.get(iid_ok).state == RUNNING, res)

section("       ...and a raising on_stop still leaves the instance STOPPED")
iid_bad_stop = mgr.create_instance("fails_on_stop", {})["id"]
mgr.start_instance(iid_bad_stop)
res = mgr.stop_instance(iid_bad_stop)
check("stop_instance still reports ok — the failure is logged, not surfaced as a hang",
      res.get("ok"), res)
check("the instance is STOPPED despite on_stop raising",
      mgr.get(iid_bad_stop).state == STOPPED, mgr.get(iid_bad_stop).state)

section("       ...and stop_all() tolerates one bad instance among several good ones")
mgr2 = StrategyManager()
good1 = mgr2.create_instance("recording", {"index": "NIFTY"})["id"]
good2 = mgr2.create_instance("recording", {"index": "SENSEX"})["id"]
bad = mgr2.create_instance("fails_on_stop", {})["id"]
for i in (good1, good2, bad):
    mgr2.start_instance(i)
mgr2.stop_all()
check("every instance ends up STOPPED, including the one whose on_stop raised",
      all(mgr2.get(i).state == STOPPED for i in (good1, good2, bad)),
      [mgr2.get(i).state for i in (good1, good2, bad)])


# ═════════════════════════════════════════════════════════════════════════
# [5] The manager's own background loop
# ═════════════════════════════════════════════════════════════════════════
section("[5] StrategyManager's background evaluation loop")
mgr3 = StrategyManager()
check("not running before start()", not mgr3.running)
mgr3.start()
check("running after start()", mgr3.running)
check("start() is idempotent", mgr3.start() is None and mgr3.running)

deadline = time.time() + 3.0
while time.time() < deadline and mgr3._cycles < 1:
    time.sleep(0.05)
check("the cycle actually ran at least once", mgr3._cycles >= 1, mgr3._cycles)

mgr3.stop()
time.sleep(0.05)
check("not running after stop()", not mgr3.running)


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
