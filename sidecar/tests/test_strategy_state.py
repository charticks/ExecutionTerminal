"""Regression tests for the Strategy Engine — Phase 4 Strategy State.

    python sidecar/tests/test_strategy_state.py

Covers: roster persistence (which instances are configured survives a
restart, WITH their original ids — the ownership map depends on that),
position-ownership persistence, per-instance runtime state (debounced via a
second LiveStore instance, restored via `restore_state` only for instances
that actually auto-start), failure isolation during a multi-instance
restore, and a defensive-robustness check for a non-JSON-safe params value.
"restart" is simulated the way this repo's other tests simulate one: a fresh
StrategyManager() reading the same CHARTICKS_DATA_DIR another one wrote to.
"""
import os
import sys
import tempfile

SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CHARTICKS_DATA_DIR"] = tempfile.mkdtemp(prefix="charticks-stratstate-")
os.environ["CHARTICKS_LOG_DIR"] = os.environ["CHARTICKS_DATA_DIR"]
sys.path.insert(0, SIDECAR)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <-- ' + str(detail)}")


def section(title):
    print(f"\n{title}")


from services.strategy_engine.base import Strategy, StrategyContext, StrategySpec  # noqa: E402
from services.strategy_engine import registry                             # noqa: E402
from services.strategy_engine.manager import (                            # noqa: E402
    ERROR, NEW, RUNNING, StrategyManager)


class NoopStrategy(Strategy):
    STARTS: list = []

    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        NoopStrategy.STARTS.append(ctx.instance_id)

    def on_stop(self) -> None:
        pass


class FailingStrategy(Strategy):
    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        raise RuntimeError("always fails")

    def on_stop(self) -> None:
        pass


class StatefulStrategy(Strategy):
    RESTORED_WITH: dict = {}

    def __init__(self):
        self.counter = 0

    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        self.counter = params.get("startAt", 0)

    def on_stop(self) -> None:
        pass

    def get_state(self) -> dict:
        return {"counter": self.counter}

    def restore_state(self, state: dict) -> None:
        StatefulStrategy.RESTORED_WITH[self.ctx.instance_id] = dict(state)
        self.counter = state.get("counter", 0)


registry.register(StrategySpec(name="noop", label="", description="",
                               factory=NoopStrategy))
registry.register(StrategySpec(name="always_fails", label="", description="",
                               factory=FailingStrategy))
registry.register(StrategySpec(name="stateful", label="", description="",
                               factory=StatefulStrategy,
                               params=()))


# ═════════════════════════════════════════════════════════════════════════
# [1] Roster persistence and restore — original ids preserved
# ═════════════════════════════════════════════════════════════════════════
section("[1] Roster survives a restart, with the SAME instance ids")
mgr1 = StrategyManager()
res_a = mgr1.create_instance("noop", {"x": 1}, auto_start=True)
res_b = mgr1.create_instance("noop", {"x": 2}, auto_start=False)
iid_a, iid_b = res_a["id"], res_b["id"]
check("the roster file was written", os.path.exists(mgr1._roster_path))

mgr2 = StrategyManager()   # a fresh manager — simulates a process restart
NoopStrategy.STARTS.clear()
restore_res = mgr2.restore()
check("both configured instances were recreated", restore_res["restored"] == 2,
      restore_res)
check("only the auto-start instance was started", restore_res["started"] == 1,
      restore_res)
check("the recreated instances kept their ORIGINAL ids",
      mgr2.get(iid_a) is not None and mgr2.get(iid_b) is not None,
      (mgr2.list_instances()))
check("the auto-start instance is RUNNING after restore",
      mgr2.get(iid_a).state == RUNNING, mgr2.get(iid_a).state)
check("on_start actually ran for it (not just marked running)",
      iid_a in NoopStrategy.STARTS, NoopStrategy.STARTS)
check("the non-auto-start instance stays NEW — configured, not running",
      mgr2.get(iid_b).state == NEW, mgr2.get(iid_b).state)
check("params round-tripped through the roster file unchanged",
      mgr2.get(iid_a).params == {"x": 1} and mgr2.get(iid_b).params == {"x": 2},
      (mgr2.get(iid_a).params, mgr2.get(iid_b).params))

section("       ...and restore() is idempotent — calling it again does nothing")
again = mgr2.restore()
check("a second restore() on the same manager is a no-op",
      again == {"restored": 0, "started": 0, "failed": 0}, again)


# ═════════════════════════════════════════════════════════════════════════
# [2] Position-ownership persistence — lazy-loaded, not just eager-restored
# ═════════════════════════════════════════════════════════════════════════
section("[2] Position ownership survives a restart (lazy load path)")
mgr3 = StrategyManager()
owner_id = mgr3.create_instance("noop", {})["id"]
mgr3.claim_position("NIFTY|29SEP2026|24000|CE", owner_id)
check("the ownership file was written", os.path.exists(mgr3._ownership_path))

mgr4 = StrategyManager()   # fresh manager, never called restore() or claim_position()
check("ownership is readable via the LAZY load path alone (owns() alone triggers it)",
      mgr4.owns("NIFTY|29SEP2026|24000|CE", owner_id))
check("a position never claimed by anyone belongs to no one",
      not mgr4.owns("NIFTY|29SEP2026|24000|CE", "some-other-id"))


# ═════════════════════════════════════════════════════════════════════════
# [3] Per-instance runtime state — debounced write, restored only on auto-start
# ═════════════════════════════════════════════════════════════════════════
section("[3] Runtime state (get_state/restore_state) survives a restart")
StatefulStrategy.RESTORED_WITH.clear()
mgr5 = StrategyManager()
iid_stateful = mgr5.create_instance("stateful", {"startAt": 7}, auto_start=True)["id"]
mgr5.start_instance(iid_stateful)
mgr5.get(iid_stateful).strategy.counter = 42   # simulate the strategy having done work
mgr5.cycle()            # marks the runtime-state store dirty
mgr5.flush_state()      # force the write now rather than waiting on the debounce
check("the runtime-state file was written",
      os.path.exists(mgr5._state_store.path))

mgr6 = StrategyManager()   # fresh manager — simulates a restart
res6 = mgr6.restore()
check("the auto-started instance received its saved state via restore_state",
      StatefulStrategy.RESTORED_WITH.get(iid_stateful) == {"counter": 42},
      StatefulStrategy.RESTORED_WITH)
check("the restored strategy's own field reflects the saved state, not the "
      "fresh on_start default",
      mgr6.get(iid_stateful).strategy.counter == 42,
      mgr6.get(iid_stateful).strategy.counter)

section("       ...and a STOPPED instance's saved state is kept but not applied")
mgr7 = StrategyManager()
iid_parked = mgr7.create_instance("stateful", {"startAt": 1}, auto_start=False)["id"]
mgr7.start_instance(iid_parked)
mgr7.get(iid_parked).strategy.counter = 99
mgr7.cycle()
mgr7.flush_state()
mgr7.stop_instance(iid_parked)   # now STOPPED — the roster still has auto_start=False

StatefulStrategy.RESTORED_WITH.clear()
mgr8 = StrategyManager()
mgr8.restore()
check("a non-auto-start instance is never started, so restore_state is never called",
      iid_parked not in StatefulStrategy.RESTORED_WITH)
check("it is still configured (in the roster) even though never started",
      mgr8.get(iid_parked) is not None and mgr8.get(iid_parked).state == NEW)


# ═════════════════════════════════════════════════════════════════════════
# [4] Failure isolation during restore — one bad instance doesn't block others
# ═════════════════════════════════════════════════════════════════════════
section("[4] One auto-start instance failing does not stop the others restoring")
mgr9 = StrategyManager()
iid_ok = mgr9.create_instance("noop", {}, auto_start=True)["id"]
iid_bad = mgr9.create_instance("always_fails", {}, auto_start=True)["id"]

mgr10 = StrategyManager()
res10 = mgr10.restore()
check("both instances were recreated", res10["restored"] == 2, res10)
check("the healthy one started", res10["started"] == 1, res10)
check("the failing one is counted, not silently dropped", res10["failed"] == 1, res10)
check("the healthy instance ended up RUNNING",
      mgr10.get(iid_ok).state == RUNNING, mgr10.get(iid_ok).state)
check("the failing instance landed in ERROR, not stuck NEW or crashing restore()",
      mgr10.get(iid_bad).state == ERROR, mgr10.get(iid_bad).state)


# ═════════════════════════════════════════════════════════════════════════
# [5] Defensive robustness — a non-JSON-safe params value cannot crash persist
# ═════════════════════════════════════════════════════════════════════════
section("[5] A non-serialisable params value degrades to 'not persisted', not a crash")
mgr11 = StrategyManager()


class Unserializable:
    pass


res = mgr11.create_instance("noop", {"bad": Unserializable()})
check("create_instance still reports success — the persistence failure is contained",
      res.get("ok"), res)
check("the instance is usable in memory even though it could not be persisted",
      mgr11.get(res["id"]) is not None)
# Calling the persist path directly must also not raise, for a manager whose
# roster already contains the bad value (e.g. added after the fact).
try:
    mgr11._persist_roster()
    check("_persist_roster() itself does not raise on unserialisable content", True)
except Exception as exc:
    check("_persist_roster() itself does not raise on unserialisable content",
          False, exc)


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
