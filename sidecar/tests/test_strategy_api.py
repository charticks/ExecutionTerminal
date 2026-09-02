"""Regression tests for the Strategy Engine — Phase 5 Terminal UI backend.

    python sidecar/tests/test_strategy_api.py

Unlike every other file in this suite, this one drives the actual FastAPI
app via TestClient rather than calling the service layer directly — every
other phase's test exercises services/strategy_engine/* itself (already
covered exhaustively in test_strategy_{framework,market_data,trading,state}.py),
but Phase 5's OWN new work is the REST/WS wiring in server.py, and only a
real HTTP round trip proves that wiring — request parsing, status/error
shapes, the /stream late-joiner replay — actually works. Everything below
the handler (StrategyManager itself) is already proven; this file exists to
prove the six new endpoints and the three new event types reach it and come
back correctly, and that a just-connected /stream client sees existing
instances immediately.
"""
import os
import sys
import tempfile

SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CHARTICKS_DATA_DIR"] = tempfile.mkdtemp(prefix="charticks-stratapi-")
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


class ApiTestStrategy(Strategy):
    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        ctx.log("info", "hello from api test strategy")

    def on_stop(self) -> None:
        pass


registry.register(StrategySpec(name="api_test", label="API Test",
                               description="a no-op strategy for endpoint tests",
                               factory=ApiTestStrategy))

import server                                                              # noqa: E402
from fastapi.testclient import TestClient                                  # noqa: E402

client = TestClient(server.app)
AUTH = {"Authorization": "Bearer dev"}


# ═════════════════════════════════════════════════════════════════════════
# [1] Auth — every endpoint requires the bearer token
# ═════════════════════════════════════════════════════════════════════════
section("[1] Every strategies endpoint is bearer-gated, same as the rest of the API")
res = client.get("/strategies")
check("GET /strategies without a token is refused",
      res.status_code == 401, res.status_code)
res = client.get("/strategies", headers=AUTH)
check("GET /strategies with the token succeeds", res.status_code == 200, res.status_code)


# ═════════════════════════════════════════════════════════════════════════
# [2] GET /strategies — specs + instances
# ═════════════════════════════════════════════════════════════════════════
section("[2] GET /strategies lists registered specs and configured instances")
body = client.get("/strategies", headers=AUTH).json()
spec_names = {s["name"] for s in body["specs"]}
check("the shipped quant_preset plugin is listed", "quant_preset" in spec_names, spec_names)
check("the test plugin registered above is listed too", "api_test" in spec_names, spec_names)
api_spec = next(s for s in body["specs"] if s["name"] == "api_test")
check("a spec's serialised form has no non-JSON-safe fields (no 'factory' key)",
      "factory" not in api_spec, api_spec)
check("instances starts empty in this fresh process", body["instances"] == [], body["instances"])


# ═════════════════════════════════════════════════════════════════════════
# [3] POST /strategies — create, then GET the detail view
# ═════════════════════════════════════════════════════════════════════════
section("[3] POST /strategies creates an instance; GET .../{id} returns its detail")
res = client.post("/strategies", headers=AUTH,
                  json={"strategy": "api_test", "params": {"x": 1}})
check("creation succeeds", res.status_code == 200 and res.json().get("ok"), res.json())
iid = res.json()["id"]

res = client.post("/strategies", headers=AUTH, json={"strategy": "no_such_strategy"})
check("creating an unknown strategy is refused, not a 500",
      res.status_code == 200 and not res.json().get("ok")
      and res.json().get("code") == "UNKNOWN_STRATEGY", res.json())

detail = client.get(f"/strategies/{iid}", headers=AUTH).json()
check("the detail view has the roster fields", detail["strategy"] == "api_test"
      and detail["params"] == {"x": 1}, detail)
check("the detail view has the Phase-5-only fields (pnl/positionIds/logs)",
      "pnl" in detail and "positionIds" in detail and "logs" in detail, detail)
check("a freshly-created (not yet started) instance has no P&L and no positions",
      detail["pnl"] == 0.0 and detail["positionIds"] == [], detail)

res = client.get("/strategies/not-a-real-id", headers=AUTH)
check("GET on an unknown instance id is a 404, not a 500", res.status_code == 404, res.status_code)


# ═════════════════════════════════════════════════════════════════════════
# [4] Start / stop / delete lifecycle over HTTP
# ═════════════════════════════════════════════════════════════════════════
section("[4] start/stop/delete over HTTP drive the real StrategyManager")
res = client.post(f"/strategies/{iid}/start", headers=AUTH)
check("start succeeds", res.json().get("ok"), res.json())
detail = client.get(f"/strategies/{iid}", headers=AUTH).json()
check("the instance is RUNNING per the detail view", detail["state"] == "running", detail)
check("on_start's log line landed in the detail view's log tail",
      any("hello from api test strategy" in row["message"] for row in detail["logs"]),
      detail["logs"])

res = client.post(f"/strategies/{iid}/remove", headers=AUTH)
check("removing a RUNNING instance is refused over HTTP too",
      res.status_code == 200 and not res.json().get("ok")
      and res.json().get("code") == "STILL_RUNNING", res.json())

res = client.post(f"/strategies/{iid}/stop", headers=AUTH)
check("stop succeeds", res.json().get("ok"), res.json())
res = client.post(f"/strategies/{iid}/remove", headers=AUTH)
check("removing a STOPPED instance succeeds", res.json().get("ok"), res.json())
res = client.get(f"/strategies/{iid}", headers=AUTH)
check("it is gone after removal", res.status_code == 404, res.status_code)


# ═════════════════════════════════════════════════════════════════════════
# [5] /stream — a late-joining client sees existing instances immediately
# ═════════════════════════════════════════════════════════════════════════
section("[5] A just-connected /stream client is replayed existing strategy state")
res = client.post("/strategies", headers=AUTH, json={"strategy": "api_test", "params": {}})
iid2 = res.json()["id"]
client.post(f"/strategies/{iid2}/start", headers=AUTH)

with client.websocket_connect("/stream?token=dev") as ws:
    seen_status, seen_log = False, False
    # The replay is a bounded burst of frames sent immediately on connect;
    # read a generous but finite number before giving up; the two
    # strategy events are close to the head of the general position/broker
    # replay, per the module's own docstring next to their emission.
    for _ in range(200):
        try:
            frame = ws.receive_json()
        except Exception:
            break
        if frame.get("type") == "strategy_status" and frame.get("id") == iid2:
            seen_status = True
        if frame.get("type") == "strategy_log" and frame.get("instanceId") == iid2:
            seen_log = True
        if seen_status and seen_log:
            break
    check("the running instance's status was replayed to the new client", seen_status)
    check("its recent log line was replayed too", seen_log)

client.post(f"/strategies/{iid2}/stop", headers=AUTH)
client.post(f"/strategies/{iid2}/remove", headers=AUTH)


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
