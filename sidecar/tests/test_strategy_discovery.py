"""Regression tests for the Strategy Engine's preset discovery/rescan —
Legacy Strategy Migration Phase 4.

    python sidecar/tests/test_strategy_discovery.py

Covers:
  [1] A directory of valid preset JSON files becomes one instance per file,
      instance id = filename stem, source="discovered", params = the file's
      own contents unchanged.
  [2] Malformed files (bad JSON, a JSON array instead of an object) are
      skipped and reported, without aborting the rest of the scan.
  [3] Non-.json files in the same directory are ignored entirely.
  [4] Re-running discover() against an unchanged directory creates nothing
      new — idempotent, not a duplicate — and a file added since the first
      scan is picked up on the next one.
  [5] A missing strategies/ directory is a no-op, not an error.
  [6] An id already taken by a MANUALLY created instance blocks discovery
      of a same-named file (create_instance's ALREADY_EXISTS path), rather
      than silently overwriting a user's own instance.
"""
import json
import os
import sys
import tempfile

SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CHARTICKS_DATA_DIR"] = tempfile.mkdtemp(prefix="charticks-stratdisc-")
os.environ["CHARTICKS_LOG_DIR"] = os.environ["CHARTICKS_DATA_DIR"]
sys.path.insert(0, SIDECAR)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <-- ' + str(detail)}")


def section(title):
    print(f"\n{title}")


import services.strategy_engine.strategies                                # noqa: E402,F401 — registers quant_preset
from services.strategy_engine import discovery                            # noqa: E402
from services.strategy_engine.manager import StrategyManager              # noqa: E402


def write(directory, name, content):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(content, str):
            fh.write(content)
        else:
            json.dump(content, fh)
    return path


# ═════════════════════════════════════════════════════════════════════════
# [1] Valid presets become instances
# ═════════════════════════════════════════════════════════════════════════
section("[1] A directory of valid preset files becomes one instance per file")
d1 = tempfile.mkdtemp(prefix="charticks-presets-")
write(d1, "SENSEX-S1-BearDaySetup.json", {"index": "SENSEX", "lots": 1})
write(d1, "ma cv.json", {"index": "NIFTY", "lots": 2})

mgr1 = StrategyManager()
res1 = discovery.discover(mgr1, d1)
check("both files were scanned", res1["scanned"] == 2, res1)
check("both became instances", res1["created"] == 2, res1)
check("nothing was skipped", res1["skipped"] == 0, res1)
check("no errors", res1["errors"] == [], res1)

inst = mgr1.get("SENSEX-S1-BearDaySetup")
check("instance id is the filename stem", inst is not None)
check("params are the file's own contents, unchanged",
      inst.params == {"index": "SENSEX", "lots": 1}, inst.params if inst else None)
check("source is 'discovered'", inst.source == "discovered")
check("a filename with a space in it also became an instance",
      mgr1.get("ma cv") is not None)
check("discovered instances default to NOT auto-starting",
      inst.auto_start is False)


# ═════════════════════════════════════════════════════════════════════════
# [2] Malformed files are skipped, not fatal to the scan
# ═════════════════════════════════════════════════════════════════════════
section("[2] Malformed JSON files are skipped and reported")
d2 = tempfile.mkdtemp(prefix="charticks-presets-")
write(d2, "good.json", {"index": "NIFTY"})
write(d2, "bad-json.json", "{not valid json")
write(d2, "an-array.json", [1, 2, 3])

mgr2 = StrategyManager()
res2 = discovery.discover(mgr2, d2)
check("all three files were scanned", res2["scanned"] == 3, res2)
check("only the good one became an instance", res2["created"] == 1, res2)
check("the two bad ones are reported as errors, not silently dropped",
      len(res2["errors"]) == 2, res2["errors"])
check("the good instance still exists despite the other two failing",
      mgr2.get("good") is not None)
check("the malformed instances were never created",
      mgr2.get("bad-json") is None and mgr2.get("an-array") is None)


# ═════════════════════════════════════════════════════════════════════════
# [3] Non-.json files are ignored
# ═════════════════════════════════════════════════════════════════════════
section("[3] Non-.json files in the same directory are ignored")
d3 = tempfile.mkdtemp(prefix="charticks-presets-")
write(d3, "preset.json", {"index": "NIFTY"})
write(d3, "readme.txt", "not a preset")
write(d3, "notes.md", "# notes")

mgr3 = StrategyManager()
res3 = discovery.discover(mgr3, d3)
check("only the .json file was scanned", res3["scanned"] == 1, res3)
check("only the .json file became an instance", res3["created"] == 1, res3)


# ═════════════════════════════════════════════════════════════════════════
# [4] Idempotent rescan, and picks up a file added later
# ═════════════════════════════════════════════════════════════════════════
section("[4] Rescanning an unchanged directory creates nothing new; a new file is picked up")
d4 = tempfile.mkdtemp(prefix="charticks-presets-")
write(d4, "one.json", {"index": "NIFTY"})

mgr4 = StrategyManager()
first = discovery.discover(mgr4, d4)
check("first scan creates the one instance", first["created"] == 1, first)

again = discovery.discover(mgr4, d4)
check("rescanning the same, unchanged directory creates nothing new",
      again["created"] == 0, again)
check("...and reports it as skipped, not an error",
      again["skipped"] == 1 and again["errors"] == [], again)
check("still exactly one instance in the roster",
      len(mgr4.list_instances()) == 1, mgr4.list_instances())

write(d4, "two.json", {"index": "BANKNIFTY"})
third = discovery.discover(mgr4, d4)
check("a file added after the first scan is picked up on the next one",
      third["created"] == 1 and third["skipped"] == 1, third)
check("now two instances total", len(mgr4.list_instances()) == 2)


# ═════════════════════════════════════════════════════════════════════════
# [5] A missing directory is a no-op
# ═════════════════════════════════════════════════════════════════════════
section("[5] A missing strategies/ directory is a no-op, not an error")
mgr5 = StrategyManager()
res5 = discovery.discover(mgr5, os.path.join(tempfile.mkdtemp(), "does-not-exist"))
check("scanned/created/skipped are all zero", res5 == {"scanned": 0, "created": 0, "skipped": 0, "errors": []}, res5)


# ═════════════════════════════════════════════════════════════════════════
# [6] A manually-created instance blocks a same-named discovered one
# ═════════════════════════════════════════════════════════════════════════
section("[6] A manually-created instance with the same id is never overwritten")
d6 = tempfile.mkdtemp(prefix="charticks-presets-")
write(d6, "my-strategy.json", {"index": "NIFTY", "lots": 5})

mgr6 = StrategyManager()
manual = mgr6.create_instance("quant_preset", {"index": "SENSEX", "lots": 99},
                              instance_id="my-strategy", source="manual")
check("the manual instance was created", manual["ok"], manual)

res6 = discovery.discover(mgr6, d6)
check("discovery does not report a creation for the colliding id",
      res6["created"] == 0 and res6["skipped"] == 1, res6)
kept = mgr6.get("my-strategy")
check("the manually-created instance's own params were never overwritten",
      kept.params == {"index": "SENSEX", "lots": 99}, kept.params)
check("its source is still 'manual', not 'discovered'", kept.source == "manual")


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
