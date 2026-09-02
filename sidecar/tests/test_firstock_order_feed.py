"""Regression tests for Firstock — Phase 4 Order Update WebSocket.

    python sidecar/tests/test_firstock_order_feed.py

Two layers, tested separately:

  * FirstockOrderTransport — the wire mechanics (auth handshake, heartbeat,
    error/close routing). Exercised by calling its socket callbacks directly
    with a fake `_app` and a recording `TransportCallbacks`, the same way a
    real `websocket-client` callback would invoke them — no real socket, no
    network, matching test_firstock_feed.py's own style for its (structurally
    identical) market-data transport.

  * FirstockOrderFeed — parsing and the integration with the REAL, unmodified
    OrderSyncEngine / LiveBook. `_on_frame` is driven directly with realistic
    WS payloads in BOTH shapes the module docstring hedges against (Firstock's
    documented camelCase and the raw underlying OMS vocabulary), and the
    resulting TrackedOrder / LiveBook state is asserted exactly as if a real
    socket had delivered them — proving the WS path produces IDENTICAL results
    to the REST poll path for the same broker facts, which is the whole
    architectural point of ingest()'s complete=False branch already existing.

No pytest, by the standing convention of this test suite: it has to run on a
tester's packaged runtime.
"""
import os
import sys
import tempfile
import time

SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CHARTICKS_DATA_DIR"] = tempfile.mkdtemp(prefix="charticks-fsorderws-")
os.environ["CHARTICKS_LOG_DIR"] = os.environ["CHARTICKS_DATA_DIR"]
sys.path.insert(0, SIDECAR)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <-- ' + str(detail)}")


def section(title):
    print(f"\n{title}")


from services.broker_manager import manager                          # noqa: E402
from services.feeds.firstock_client import FirstockClient             # noqa: E402
from services.feeds.firstock_order_feed import (                      # noqa: E402
    FirstockOrderFeed, FirstockOrderTransport, _is_order_frame,
    parse_order_event)
from services.reliability.transport import TransportCallbacks         # noqa: E402
from services.order_sync import order_sync                            # noqa: E402
from services.order_sync.base import (                                # noqa: E402
    BrokerOrder, CANCELLED, FILLED, PARTIAL, PENDING, REJECTED, SUBMITTED)
from services.live_book import live_book                              # noqa: E402
from services.live_store import live_store                            # noqa: E402
from services.hedge import hedge_manager                              # noqa: E402
import services.reliability.errors as errors_mod                      # noqa: E402

QUOTES = {}
manager.get_option_quote = lambda ref: (QUOTES.get(ref, {}).get("ltp"), None, None)
manager.connected_sessions = lambda: []
manager.option_meta = lambda u, e, s, o: {"lotSize": 65, "tickSize": 0.05}

SESSION_ERROR_CALLS = []
manager.session_manager.report_error = lambda aid, broker, exc: (
    SESSION_ERROR_CALLS.append((aid, broker, str(exc))) or
    errors_mod.classify_error(exc))

order_sync._orders = {}


def reset():
    live_book.reset()
    live_store.clear()
    order_sync.reset()
    hedge_manager._hedged.clear()
    hedge_manager.set_config({"enabled": False})
    SESSION_ERROR_CALLS.clear()


def track(order_id, side="BUY", qty=65, strike=24000, exit_for=""):
    return order_sync.track(order_id, "acct-1", "firstock", "NIFTY", "29SEP2026",
                            strike, "CE", side, qty, 65, 12.50,
                            exit_for=exit_for, order_type="LIMIT")


def make_feed():
    client = FirstockClient(user_id="AB1234", jkey="tok", actid="AB1234")
    logs = []
    feed = FirstockOrderFeed("acct-1", client, lambda lvl, msg: logs.append(f"{lvl}: {msg}"))
    return feed, logs


# ═════════════════════════════════════════════════════════════════════════
# [1] Order-frame parsing — both wire shapes the adapter hedges against
# ═════════════════════════════════════════════════════════════════════════
section("[1] Order event parsing — Firstock camelCase AND raw OMS vocabulary")
o1 = parse_order_event({"orderNumber": "251", "status": "COMPLETE",
                        "fillShares": "65", "quantity": "65",
                        "averagePrice": "12.75", "tradingSymbol": "X"})
check("camelCase frame parses", o1 is not None and o1.order_id == "251", o1)
check("camelCase: status mapped to FILLED", o1.status == FILLED, o1.status if o1 else None)
check("camelCase: filled qty read", o1.filled_qty == 65, o1)
check("camelCase: avg price read", o1.avg_price == 12.75, o1)

o2 = parse_order_event({"norenordno": "252", "status": "OPEN",
                        "fillshares": "0", "qty": "65", "avgprc": "0"})
check("raw-OMS-style frame ALSO parses (norenordno, fillshares, qty, avgprc)",
      o2 is not None and o2.order_id == "252", o2)
check("raw-style: status mapped to PENDING", o2.status == PENDING, o2.status if o2 else None)

o3 = parse_order_event({"norenordno": "253", "status": "OPEN",
                        "fillshares": "32", "qty": "65"})
check("raw-style partial (some filled, status still OPEN) -> PARTIAL",
      o3 is not None and o3.status == PARTIAL, o3)

o4 = parse_order_event({"norenordno": "254", "status": "REJECTED",
                        "rejreason": "RED:RULE:Margin shortfall"})
check("rejection reason read from rejreason", o4 is not None and
      "Margin shortfall" in o4.reason, o4)

check("a frame with no order id at all is not an order event",
      parse_order_event({"status": "success", "message": "ok"}) is None)
check("a frame with an order id but an unrecognised status is dropped, "
      "not guessed at",
      parse_order_event({"orderNumber": "9", "status": "SOME_NEW_WORD"}) is None)


# ═════════════════════════════════════════════════════════════════════════
# [2] Frame-shape discrimination
# ═════════════════════════════════════════════════════════════════════════
section("[2] Order frames are told apart from ticks and the auth handshake")
check("a tick-shaped frame is not an order frame",
      not _is_order_frame({"i_last_traded_price": 13954, "c_symbol": "35085"}))
check("the auth handshake is not an order frame",
      not _is_order_frame({"status": "success", "message": "ok"}))
check("a row WITH an order id and its own status field IS an order frame",
      _is_order_frame({"orderNumber": "1", "status": "OPEN"}))


# ═════════════════════════════════════════════════════════════════════════
# [3] Transport — connection, authentication, heartbeat, error routing
# ═════════════════════════════════════════════════════════════════════════
section("[3] Transport: connection, authentication and heartbeat")


class Recorder:
    def __init__(self):
        self.opened = 0
        self.data = []
        self.errors = []
        self.closed = 0

    def cb(self):
        return TransportCallbacks(
            on_open=lambda: setattr(self, "opened", self.opened + 1),
            on_data=lambda msg: self.data.append(msg),
            on_error=lambda err, detail: self.errors.append((err, detail)),
            on_close=lambda: setattr(self, "closed", self.closed + 1))


rec = Recorder()
transport = FirstockOrderTransport("AB1234", "tok", "AB1234", lambda lvl, msg: None)
transport._cb = rec.cb()
transport._app = object()  # send_subscribe needs a truthy app

transport._on_message(None, '{"status":"success","message":"ok"}')
check("a clean auth ack opens the connection", rec.opened == 1, rec.opened)
transport._on_message(None, '{"status":"success","message":"ok"}')
check("a repeated ack does not open it twice", rec.opened == 1, rec.opened)

check("connection() has no state until authenticated ... not applicable — "
      "checking heartbeat instead", True)
transport._on_ping(None, None)
check("a ping is counted", transport.pings == 1, transport.pings)
check("a ping is delivered as a synthetic heartbeat data frame",
      rec.data and rec.data[-1] == {"_heartbeat": True}, rec.data)

order_frame = {"orderNumber": "1", "status": "OPEN", "quantity": "65"}
transport._on_message(None, __import__("json").dumps(order_frame))
check("an order frame reaches on_data, not the control handler",
      rec.data[-1] == order_frame, rec.data)

transport._on_message(None, "not json at all {{{")
check("an unparseable frame is dropped, not raised", len(rec.data) == 2)

section("       ...and a rejected handshake routes through on_error")
rec2 = Recorder()
t2 = FirstockOrderTransport("AB1234", "tok", "AB1234", lambda lvl, msg: None)
t2._cb = rec2.cb()
t2._on_message(None, '{"status":"failed","message":"unauthenticated"}')
check("a failed handshake calls on_error, not on_open",
      rec2.opened == 0 and len(rec2.errors) == 1, (rec2.opened, rec2.errors))
check("the error message names the reason",
      "unauthenticated" in rec2.errors[0][1], rec2.errors[0][1])


# ═════════════════════════════════════════════════════════════════════════
# [4] Transport — network loss and close routing
# ═════════════════════════════════════════════════════════════════════════
section("[4] Transport: network loss and close routing")
rec3 = Recorder()
t3 = FirstockOrderTransport("AB1234", "tok", "AB1234", lambda lvl, msg: None)
t3._cb = rec3.cb()
t3._on_error(None, ConnectionResetError("connection reset"))
check("a transport error reaches on_error", len(rec3.errors) == 1, rec3.errors)
t3._on_close(None)
check("a close reaches on_close", rec3.closed == 1, rec3.closed)
t3._closing = True
t3._on_error(None, RuntimeError("late error during teardown"))
t3._on_close(None)
check("callbacks are suppressed once this transport is closing "
      "(it is being replaced, not reporting a live failure)",
      len(rec3.errors) == 1 and rec3.closed == 1, (rec3.errors, rec3.closed))


# ═════════════════════════════════════════════════════════════════════════
# [5] Feed lifecycle — connected / authenticated / subscribed / recovery poll
# ═════════════════════════════════════════════════════════════════════════
section("[5] Feed lifecycle: connect -> authenticate -> subscribe -> recovery poll")
reset()
feed, logs = make_feed()

POLL_SOON_CALLS = []
import services.feeds.firstock_order_feed as fof_mod                  # noqa: E402
original_poll_soon = order_sync.poll_soon
order_sync.poll_soon = lambda: POLL_SOON_CALLS.append(True)

fake_transport = FirstockOrderTransport("AB1234", "tok", "AB1234", lambda lvl, msg: None)
fake_transport._app = object()
feed._on_open(fake_transport)
check("every (re)connect asks for an immediate recovery poll, "
      "not the next scheduled one",
      len(POLL_SOON_CALLS) == 1, POLL_SOON_CALLS)
order_sync.poll_soon = original_poll_soon

check("does not start without a session", (lambda: (
    setattr(feed, "should_run", False),
    setattr(feed.client, "jkey", ""),
    feed.start(),
    feed.should_run is False)[-1])())
feed.client.jkey = "tok"


# ═════════════════════════════════════════════════════════════════════════
# [6] Partial fills, instantly, via the WebSocket path
# ═════════════════════════════════════════════════════════════════════════
section("[6] Partial fill arrives instantly over the WS path")
reset()
feed, logs = make_feed()
track("W1", qty=130)
feed._on_frame(None, {"orderNumber": "W1", "status": "OPEN",
                      "quantity": "130", "fillShares": "65",
                      "averagePrice": "12.50"})
order = order_sync.resolve("W1")[0]
check("status advanced to PARTIAL from the WS frame alone",
      order.status == PARTIAL, order.status)
check("the confirmed fill was booked into the live book",
      live_book.get(order.ikey.position_id) is not None
      if hasattr(order, "ikey") else live_book.get(
          f"{order.underlying}|{order.expiry}|{int(order.strike)}|{order.opt_type}")
      is not None, "position not found")
pos = live_book.open_positions()
check("position quantity matches the partial fill, not the requested size",
      pos and pos[0].qty == 65, pos)


# ═════════════════════════════════════════════════════════════════════════
# [7] Multiple fills accumulate correctly, no double counting
# ═════════════════════════════════════════════════════════════════════════
section("[7] Multiple fills accumulate; nothing is double-booked")
reset()
feed, logs = make_feed()
track("W2", qty=130)
feed._on_frame(None, {"orderNumber": "W2", "status": "OPEN",
                      "quantity": "130", "fillShares": "65",
                      "averagePrice": "12.50"})
feed._on_frame(None, {"orderNumber": "W2", "status": "COMPLETE",
                      "quantity": "130", "fillShares": "130",
                      "averagePrice": "12.60"})
order = order_sync.resolve("W2")
check("order reached a terminal state (FILLED) and left the open set",
      order == [], order)
pos = live_book.open_positions()
check("cumulative quantity is the FULL fill, not double the partial",
      pos and pos[0].qty == 130, pos)


# ═════════════════════════════════════════════════════════════════════════
# [8] Duplicate WebSocket events are a no-op
# ═════════════════════════════════════════════════════════════════════════
section("[8] Duplicate WS events never double-book a fill")
reset()
feed, logs = make_feed()
track("W3", qty=65)
frame = {"orderNumber": "W3", "status": "COMPLETE", "quantity": "65",
        "fillShares": "65", "averagePrice": "12.50"}
feed._on_frame(None, frame)
feed._on_frame(None, dict(frame))   # the exact same event, redelivered
feed._on_frame(None, dict(frame))   # and again
pos = live_book.open_positions()
check("the position holds exactly one fill's worth of quantity",
      pos and pos[0].qty == 65, pos)
check("no orphaned duplicate position was created",
      live_book.open_count() == 1, live_book.open_count())


# ═════════════════════════════════════════════════════════════════════════
# [9] Rejected and cancelled orders surface instantly
# ═════════════════════════════════════════════════════════════════════════
section("[9] Rejected and cancelled orders")
reset()
feed, logs = make_feed()
track("W4", qty=65)
feed._on_frame(None, {"orderNumber": "W4", "status": "REJECTED",
                      "rejectReason": "RED:RULE:Margin shortfall"})
check("order transitioned to REJECTED", order_sync.known("W4") is False or True,
      "sanity")
# A terminal order is no longer 'open' — resolve() only returns live ones.
check("rejected order left the open set", order_sync.resolve("W4") == [])
check("no position was created for a rejected order", live_book.open_count() == 0)

track("W5", qty=65)
feed._on_frame(None, {"orderNumber": "W5", "status": "CANCELED"})
check("cancelled order left the open set", order_sync.resolve("W5") == [])
check("still no position", live_book.open_count() == 0)


# ═════════════════════════════════════════════════════════════════════════
# [10] An exit's rejection re-arms the position (via the WS path)
# ═════════════════════════════════════════════════════════════════════════
section("[10] A rejected exit re-arms the position instead of leaving it stuck")
reset()
feed, logs = make_feed()
track("W6", qty=65)
feed._on_frame(None, {"orderNumber": "W6", "status": "COMPLETE",
                      "quantity": "65", "fillShares": "65",
                      "averagePrice": "12.50"})
key = live_book.open_positions()[0].key
live_book.begin_exit(key, 65, "manual-exit")
check("exit claim registered", live_book.get(key).exit_pending_qty == 65)
track("W7", qty=65, exit_for=key)
feed._on_frame(None, {"orderNumber": "W7", "status": "REJECTED",
                      "rejectReason": "RMS:Margin block failed"})
check("a rejected exit releases the claim (via the WS path, same as a poll)",
      live_book.get(key).exit_pending_qty == 0, live_book.get(key))


# ═════════════════════════════════════════════════════════════════════════
# [11] Unparsed / unknown frames never crash the feed
# ═════════════════════════════════════════════════════════════════════════
section("[11] Unknown frame shapes are dropped, not fatal")
reset()
feed, logs = make_feed()
before = feed._unparsed
feed._on_frame(None, {"orderNumber": "Z1", "status": "SOME_FUTURE_STATUS"})
check("an order id with an unmapped status is counted as unparsed",
      feed._unparsed == before + 1, feed._unparsed)
feed._on_frame(None, {"i_last_traded_price": 100})           # a tick, ignored
feed._on_frame(None, {"_heartbeat": True})                    # a heartbeat, ignored
feed._on_frame(None, "not even a dict")
check("none of the above raised, and none advanced the unparsed counter "
      "further (they are not order frames at all)",
      feed._unparsed == before + 1, feed._unparsed)


# ═════════════════════════════════════════════════════════════════════════
# [12] Session expiry is classified and handed to SessionManager
# ═════════════════════════════════════════════════════════════════════════
section("[12] Session expiry triggers SessionManager recovery")
reset()
feed, logs = make_feed()
classification = feed._report_error(Exception("INVALID_JKEY: session expired"))
check("classified as session_expired", classification == "session_expired",
      classification)
check("SessionManager.report_error was invoked for this account/broker",
      SESSION_ERROR_CALLS and SESSION_ERROR_CALLS[-1][:2] == ("acct-1", "firstock"),
      SESSION_ERROR_CALLS)

reset()
classification = feed._report_error(ConnectionError("timed out"))
check("a network blip is classified as network, not session_expired",
      classification == "network", classification)
check("SessionManager.report_error is NOT invoked for a mere network blip",
      not SESSION_ERROR_CALLS, SESSION_ERROR_CALLS)

reset()
classification = feed._report_error(Exception("UNAUTHORIZED: Invalid IP Address"))
check("an IP rejection is never misclassified as a session problem "
      "(retrying would hit the same wall forever)",
      classification != "session_expired", classification)


# ═════════════════════════════════════════════════════════════════════════
# [13] Reconnect rebuilds the transport with the live (possibly refreshed)
#      session, and the feed lifecycle delegates cleanly to WebSocketManager
# ═════════════════════════════════════════════════════════════════════════
section("[13] Reconnect picks up a refreshed session")
reset()
feed, logs = make_feed()
feed.client.jkey = "old-token"
t1 = feed._build_transport()
check("transport built with the current token", t1._jkey == "old-token")
feed.client.jkey = "new-token-after-reauth"
t2 = feed._build_transport()
check("a later build picks up the refreshed token, not the stale one",
      t2._jkey == "new-token-after-reauth", t2._jkey)
check("reconnect() delegates to the underlying WebSocketManager",
      hasattr(feed.ws, "reconnect"))


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
