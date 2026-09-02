"""Regression tests for the Strategy & Positions UI redesign's backend
additions — Strategy.phase(), list_instances()/instance_detail()'s
phase/positionCount fields, update_params() ("Edit"), and LivePosition's
ownerInstanceId on the position_update event.

    python sidecar/tests/test_strategy_status.py

Covers:
  [1] Strategy.phase() defaults to None on the base class.
  [2] QuantPresetStrategy.phase()'s four transitions (waiting/in_position/
      completed), driven directly off its own _armed/_tokens state.
  [3] StrategyManager.list_instances()/instance_detail() surface phase +
      positionCount, with per-instance isolation: one instance's phase()
      raising must not blank any other row.
  [4] update_params() refuses while running, succeeds while stopped, and
      persists to the roster file.
  [5] live_book's _publish() carries ownerInstanceId — set for a claimed
      position, None for an unclaimed one — via the real strategy_manager
      singleton (the only thing live_book's deferred import can resolve to).
"""
import os
import sys
import tempfile

SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CHARTICKS_DATA_DIR"] = tempfile.mkdtemp(prefix="charticks-stratstatus-")
os.environ["CHARTICKS_LOG_DIR"] = os.environ["CHARTICKS_DATA_DIR"]
sys.path.insert(0, SIDECAR)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <-- ' + str(detail)}")


def section(title):
    print(f"\n{title}")


from services.instruments import InstrumentKey, instruments               # noqa: E402
from services.broker_manager import manager as broker_manager             # noqa: E402

broker_manager.option_meta = lambda u, e, s, o: {"lotSize": 65, "tickSize": 0.05}
broker_manager.subscribe_option_keys = lambda keys: None

from services.strategy_engine.base import Strategy, StrategyContext       # noqa: E402
from services.strategy_engine import registry                             # noqa: E402
from services.strategy_engine.manager import (                            # noqa: E402
    RUNNING, StrategyManager)
import services.strategy_engine.strategies                                # noqa: E402,F401 — registers quant_preset
from services.strategy_engine.strategies.quant_preset import (            # noqa: E402
    QuantPresetStrategy, _TokenState)

EXPIRY = "29SEP2026"
KEY = InstrumentKey.option("NIFTY", EXPIRY, 24000, "CE")
KEY2 = InstrumentKey.option("NIFTY", EXPIRY, 24100, "CE")
instruments.bind("test", KEY, "NFO:1")
instruments.bind("test", KEY2, "NFO:2")
# Distinct strikes for [5]'s ownership test — the ownership FILE is shared
# across every StrategyManager() instance in this process (they all read/
# write the same data_dir()/strategy_ownership.json, unlike the in-memory
# roster), so reusing KEY/KEY2 there would collide with the claim already
# made against them in [3] and make "unclaimed" not actually unclaimed.
KEY3 = InstrumentKey.option("NIFTY", EXPIRY, 24500, "CE")
KEY4 = InstrumentKey.option("NIFTY", EXPIRY, 24600, "CE")
instruments.bind("test", KEY3, "NFO:3")
instruments.bind("test", KEY4, "NFO:4")


# ═════════════════════════════════════════════════════════════════════════
# [1] Strategy.phase() default
# ═════════════════════════════════════════════════════════════════════════
section("[1] Strategy.phase() defaults to None on the base class")


class PlainStrategy(Strategy):
    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        pass

    def on_stop(self) -> None:
        pass


check("a strategy that doesn't override phase() returns None",
      PlainStrategy().phase() is None)


# ═════════════════════════════════════════════════════════════════════════
# [2] QuantPresetStrategy.phase() transitions
# ═════════════════════════════════════════════════════════════════════════
section("[2] QuantPresetStrategy.phase() — waiting / in_position / completed")

qp = QuantPresetStrategy()
check("not yet armed -> waiting", qp.phase() == "waiting", qp.phase())

qp._armed = True
check("armed but no tokens selected -> waiting", qp.phase() == "waiting", qp.phase())

qp._tokens[KEY] = _TokenState(key=KEY, opt_type="CE")
qp._tokens[KEY2] = _TokenState(key=KEY2, opt_type="CE")
check("armed, tokens watching, no signal yet -> waiting",
      qp.phase() == "waiting", qp.phase())

qp._tokens[KEY].trade_open = True
check("any token holding a trade -> in_position", qp.phase() == "in_position", qp.phase())

qp._tokens[KEY].trade_open = False
qp._tokens[KEY].entry_taken_today = True
qp._tokens[KEY2].entry_taken_today = True
check("no open trade, every token already tried its one entry -> completed",
      qp.phase() == "completed", qp.phase())

qp._tokens[KEY2].entry_taken_today = False
check("only SOME tokens exhausted, none open -> still waiting (others may yet signal)",
      qp.phase() == "waiting", qp.phase())


# ═════════════════════════════════════════════════════════════════════════
# [3] list_instances()/instance_detail() — phase, positionCount, isolation
# ═════════════════════════════════════════════════════════════════════════
section("[3] list_instances()/instance_detail() surface phase + positionCount, isolated")


class OkStrategy(Strategy):
    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        pass

    def on_stop(self) -> None:
        pass

    def phase(self) -> str | None:
        return "waiting"


class CrashyPhaseStrategy(Strategy):
    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        pass

    def on_stop(self) -> None:
        pass

    def phase(self) -> str | None:
        raise RuntimeError("boom in phase()")


from services.strategy_engine.base import StrategySpec                    # noqa: E402

registry.register(StrategySpec(name="status_ok", label="Ok", description="",
                               factory=OkStrategy))
registry.register(StrategySpec(name="status_crashy", label="Crashy", description="",
                               factory=CrashyPhaseStrategy))

mgr3 = StrategyManager()
ok_id = mgr3.create_instance("status_ok", {})["id"]
crashy_id = mgr3.create_instance("status_crashy", {})["id"]
mgr3.start_instance(ok_id)
mgr3.start_instance(crashy_id)

rows = {r["id"]: r for r in mgr3.list_instances()}
check("the healthy instance reports its own phase",
      rows[ok_id]["phase"] == "waiting", rows[ok_id])
check("the crashy instance's phase() failure surfaces as None, not an exception",
      rows[crashy_id]["phase"] is None, rows[crashy_id])
check("...and the healthy row is unaffected by the other one's crash",
      rows[ok_id]["phase"] == "waiting")
check("positionCount defaults to 0 with nothing claimed",
      rows[ok_id]["positionCount"] == 0 and rows[crashy_id]["positionCount"] == 0, rows)

mgr3.claim_position(KEY.position_id, ok_id)
detail = mgr3.instance_detail(ok_id)
check("instance_detail() also reports phase", detail["phase"] == "waiting", detail)
check("instance_detail()'s positionCount reflects the claim",
      detail["positionCount"] == 1, detail)
check("instance_detail()'s positionIds still lists it too (unchanged existing field)",
      detail["positionIds"] == [KEY.position_id], detail)

mgr3.stop_instance(ok_id)
mgr3.stop_instance(crashy_id)


# ═════════════════════════════════════════════════════════════════════════
# [4] update_params() — the "Edit" action
# ═════════════════════════════════════════════════════════════════════════
section("[4] update_params() refuses while running, succeeds while stopped, persists")

mgr4 = StrategyManager()
edit_id = mgr4.create_instance("status_ok", {"lots": 1})["id"]
mgr4.start_instance(edit_id)
refused = mgr4.update_params(edit_id, {"lots": 5})
check("editing a RUNNING instance is refused", not refused["ok"], refused)
check("...with the same STILL_RUNNING code remove_instance uses",
      refused.get("code") == "STILL_RUNNING", refused)
check("params are untouched by the refused edit",
      mgr4.get(edit_id).params == {"lots": 1}, mgr4.get(edit_id).params)

mgr4.stop_instance(edit_id)
applied = mgr4.update_params(edit_id, {"lots": 5, "index": "SENSEX"})
check("editing a STOPPED instance succeeds", applied["ok"], applied)
check("the new params took effect",
      mgr4.get(edit_id).params == {"lots": 5, "index": "SENSEX"}, mgr4.get(edit_id).params)

check("editing an unknown instance id is NOT_FOUND",
      mgr4.update_params("does-not-exist", {}).get("code") == "NOT_FOUND")

# "restart" — a fresh manager reading the same roster file, same pattern
# test_strategy_state.py uses.
mgr4b = StrategyManager()
mgr4b.restore()
check("the edited params survive a restart (persisted to the roster)",
      mgr4b.get(edit_id).params == {"lots": 5, "index": "SENSEX"}, mgr4b.get(edit_id).params)


# ═════════════════════════════════════════════════════════════════════════
# [5] live_book — ownerInstanceId on the published position
# ═════════════════════════════════════════════════════════════════════════
section("[5] live_book publishes ownerInstanceId — set when claimed, None otherwise")

from services.live_book import live_book                                  # noqa: E402
from services.strategy_engine.manager import strategy_manager             # noqa: E402
import bridge.hub as hubmod                                               # noqa: E402

published: list = []
original_publish = hubmod.hub.publish
hubmod.hub.publish = lambda e: published.append(e)
try:
    live_book.record_fill("NIFTY", EXPIRY, 24500, "CE", "BUY", 65, 1, 100.0)
    live_book.record_fill("NIFTY", EXPIRY, 24600, "CE", "BUY", 65, 1, 90.0)
finally:
    hubmod.hub.publish = original_publish

rows5 = [e for e in published if e.get("type") == "position_update"]
unclaimed_row = next((r for r in rows5 if r.get("id") == KEY3.position_id), None)
check("an unclaimed position's event carries no owner",
      unclaimed_row is not None and unclaimed_row.get("ownerInstanceId") is None,
      unclaimed_row)

owner_id = mgr3.create_instance("status_ok", {})["id"]
strategy_manager.claim_position(KEY4.position_id, owner_id)

published2: list = []
hubmod.hub.publish = lambda e: published2.append(e)
try:
    live_book.update_quote(KEY4, 91.0)
finally:
    hubmod.hub.publish = original_publish

rows5b = [e for e in published2 if e.get("type") == "position_update"]
claimed_row = next((r for r in rows5b if r.get("id") == KEY4.position_id), None)
check("a claimed position's event carries the owning instance id",
      claimed_row is not None and claimed_row.get("ownerInstanceId") == owner_id,
      claimed_row)


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
