"""Firstock Order Update WebSocket — push-driven order lifecycle events.

    Exchange
        |
    Firstock order-update frame
        |
    FirstockOrderTransport (wire mechanics)  <- THIS FILE, all of it
        |
    FirstockOrderFeed (parse -> BrokerOrder)  <- THIS FILE
        |
    order_sync.ingest(..., complete=False)    <- broker-agnostic, unchanged
        |
    OrderSync -> LiveBook -> LiveManager -> Renderer

Every line above `order_sync.ingest()` is broker-specific and lives here, in
Firstock's own adapter, exactly like every other Firstock wire detail already
does in this package. Nothing below that line — OrderSync, LiveBook,
LiveManager, the renderer — has to change or knows this feed exists; the
engine's own docstring already anticipated it: "a broker with an order
WebSocket implements the same OrderSource and calls ingest() directly."

Protocol uncertainty, and the trigger-plus-verify design that follows from it
------------------------------------------------------------------------------
Firstock's own SDKs disagree with each other about this socket's exact shape.
The generation Charticks has actually validated against a live account — the
same auth handshake and URL `firstock_feed.py`'s market-data feed already
proves out ("Live ticks are streaming") — delivers order (and position)
updates to an authenticated connection automatically, discriminated from a
tick frame purely by which fields are present, with no separate subscribe
call. A newer, differently-shaped SDK generation shows an explicit
``{"t":"o","actid":...}`` subscribe message and a topic-tagged frame envelope
instead. Nothing here assumes either is complete or permanent.

So this feed treats every parsed WS frame as encouragement, never as the
final word:

  * A frame is parsed with a WIDENED set of field-name candidates covering
    both Firstock's documented REST vocabulary (``orderNumber``,
    ``fillShares``, ...) and the raw underlying OMS vocabulary some of its own
    SDK code paths show verbatim (``norenordno``, ``fillshares``, ...) — the
    same defensive-widening idiom ``services.order_sync.brokers.firstock_orders``
    already uses for the REST order book, for the same reason: a renamed or
    unanticipated field degrades to "frame ignored", never to a wrong state.
  * ``order_sync.ingest(..., complete=False)`` books only what it could parse.
    A frame this feed cannot read is silently dropped rather than guessed at.
  * Every (re)connect polls the REST order book once regardless (the
    "recovery poll"), and any parse failure is logged so a real mismatch is
    visible rather than silently eating events forever.

The result is genuinely event-driven — a well-formed frame updates the order
book in milliseconds, with no dependency on the exact field names being
guessed correctly — while correctness is never more than one REST poll away
from self-healing if they were not.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

import diagnostics

from services.order_sync import order_sync
from services.order_sync.base import (
    CANCELLED,
    FILLED,
    PARTIAL,
    REJECTED,
    BrokerOrder,
    as_float,
    as_int,
    first,
    map_status,
)
from services.reliability.retry_manager import RetryManager
from services.reliability.transport import Transport, TransportCallbacks
from services.reliability.ws_manager import WebSocketManager

from .firstock_client import FirstockClient, redact

WS_URL = "wss://socket.firstock.in/V2/ws"

# A dead order-feed socket is not diagnosed by ticks (it carries none) but by
# the SERVER's own heartbeat going silent — see FirstockOrderTransport._on_ping.
# Generous: an idle account can legitimately see zero order EVENTS for hours,
# and this must only ever catch a socket that has stopped answering ITS OWN
# pings, never "nothing happened."
STALE_AFTER_S = 120.0

# Field-name candidates covering both shapes this socket's frames might use —
# see the module docstring. `first()` (services.order_sync.base) matches
# case-insensitively, so only the root spelling has to be guessed, not casing.
_ORDER_ID_KEYS = ("orderNumber", "norenordno", "nestordernumber", "orderId", "order_id")
_STATUS_KEYS = ("status",)
_FILLED_KEYS = ("fillShares", "fillshares", "flqty", "filledQuantity",
               "filled_qty", "filledQty")
_QTY_KEYS = ("quantity", "qty", "orderQuantity", "orderquantity")
_PRICE_KEYS = ("averagePrice", "avgprc", "avgPrice", "avg_price", "prc")
_REASON_KEYS = ("rejectReason", "rejreason", "rejReason", "message", "errormsg")

_EVENT_LABEL = {FILLED: "Final Fill", PARTIAL: "Partial Fill",
                REJECTED: "Rejected", CANCELLED: "Cancelled"}


def _is_order_frame(row: dict) -> bool:
    """Does this frame carry an order identifier at all?

    The cheap, broker-agnostic-at-the-parsing-level test that lets one socket
    safely ignore anything that is not an order update — a position push, or a
    tick, should Firstock ever multiplex either onto this connection — without
    having to know their shapes, only that this one is absent.
    """
    return first(row, *_ORDER_ID_KEYS) is not None


def parse_order_event(row: dict) -> BrokerOrder | None:
    """One WS order frame -> BrokerOrder, or None when it cannot be read.

    Never guesses: an order id with no recognisable status is dropped rather
    than assigned one, exactly like ``order_sync.brokers._order`` for the REST
    order book — the periodic poll is what actually confirms a frame this
    could not parse.
    """
    order_id = first(row, *_ORDER_ID_KEYS)
    if not order_id:
        return None
    raw = str(first(row, *_STATUS_KEYS) or "")
    filled = as_int(first(row, *_FILLED_KEYS))
    total = as_int(first(row, *_QTY_KEYS))
    status = map_status(raw, filled, total)
    if status is None:
        return None
    return BrokerOrder(
        order_id=str(order_id), status=status, filled_qty=filled,
        avg_price=as_float(first(row, *_PRICE_KEYS)), raw_status=raw,
        reason=str(first(row, *_REASON_KEYS) or ""))


class FirstockOrderTransport(Transport):
    """One Firstock order-update WebSocket connection.

    Deliberately the same shape as ``firstock_feed.FirstockTransport`` — same
    URL, same query-string auth, same ``{"status": "success"/"failed"}``
    handshake — because that is the ONE wire behaviour Charticks has actually
    proven against a live Firstock account. A second, independent connection
    rather than piggy-backing on the market-data socket: order-frame parsing
    bugs must never be able to disrupt tick delivery, or vice versa, and the
    two have unrelated lifecycles (this one is per broker SESSION, not per
    subscription window).
    """

    def __init__(self, user_id: str, jkey: str, actid: str, log) -> None:
        self._user_id = user_id
        self._jkey = jkey
        self._actid = actid
        self._log = log
        self._app: Any = None
        self._thread: threading.Thread | None = None
        self._closing = False
        self._authenticated = False
        self._cb: TransportCallbacks | None = None
        self.pings = 0
        self.last_ping_ts = 0.0

    # ── Transport ─────────────────────────────────────────────────────────
    def handle(self) -> Any:
        return self

    def open(self, cb: TransportCallbacks) -> None:
        import websocket

        self._cb = cb
        self._closing = False
        self._authenticated = False

        url = (f"{WS_URL}?userId={self._user_id}&jKey={self._jkey}"
               f"&source=developer-api")

        app = websocket.WebSocketApp(
            url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_ping=self._on_ping,
        )
        self._app = app

        def run() -> None:
            try:
                app.run_forever(skip_utf8_validation=True)
            except Exception as e:
                if not self._closing:
                    cb.on_error(e, redact(f"{type(e).__name__}: {e}"))
            finally:
                if not self._closing:
                    try:
                        cb.on_close()
                    except Exception as exc:
                        diagnostics.exception(
                            "orders", "Firstock order-feed on_close handler failed",
                            exc_info=exc)

        self._thread = threading.Thread(target=run, daemon=True,
                                        name="firstock-order-feed")
        self._thread.start()

    def close(self) -> None:
        self._closing = True
        app = self._app
        self._app = None
        if app is None:
            return
        try:
            app.close()
        except Exception:
            pass

    # ── socket callbacks ──────────────────────────────────────────────────
    def _on_ping(self, _app: Any, _data: Any) -> None:
        self.pings += 1
        self.last_ping_ts = time.time()
        diagnostics.event("orders", "Order WS", "heartbeat",
                          account=self._actid)
        cb = self._cb
        if cb is not None:
            # A synthetic data frame, not an order row: WebSocketManager's own
            # staleness clock is driven by on_data, and a heartbeat is exactly
            # the liveness evidence that clock exists to look for — an order
            # feed can go legitimately silent on EVENTS for hours, but a
            # socket that has stopped answering its own pings is dead.
            cb.on_data({"_heartbeat": True})

    def _on_error(self, _app: Any, err: Any) -> None:
        if self._closing:
            return
        cb = self._cb
        if cb is not None:
            cb.on_error(err, redact(f"{type(err).__name__}: {err}"))

    def _on_close(self, _app: Any, status: Any = None, msg: Any = None) -> None:
        if self._closing:
            return
        cb = self._cb
        if cb is not None:
            cb.on_close()

    def _on_message(self, _app: Any, raw: Any) -> None:
        cb = self._cb
        if cb is None:
            return
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            self._log("warn", f"[firstock-order-feed] unparseable frame: "
                              f"{redact(raw)[:200]}")
            return
        if not isinstance(msg, dict):
            return

        # The auth handshake is the ONLY frame with a top-level "status" and no
        # order identifier. An order ROW may also carry a "status" field (its
        # own lifecycle state), so checking for an order id first is what
        # keeps a genuine order update from ever being swallowed as the
        # handshake — the same discrimination firstock_feed.py makes between
        # its control frame and a tick frame that happens to be shaped
        # differently.
        status = msg.get("status")
        if status is not None and not _is_order_frame(msg):
            self._handle_control(cb, str(status), msg)
            return
        cb.on_data(msg)

    def _handle_control(self, cb: TransportCallbacks, status: str, msg: dict) -> None:
        detail = str(msg.get("message") or "")
        if status.lower() == "success":
            if self._authenticated:
                return
            self._authenticated = True
            self._log("info", f"[firstock-order-feed] authenticated ({detail or 'ok'})")
            cb.on_open()
            return
        self._log("error", f"[firstock-order-feed] authentication failed: "
                           f"{detail or msg}")
        cb.on_error(msg, f"Firstock order feed rejected the session: {detail or msg}")

    def send_subscribe(self) -> bool:
        """Best-effort order-feed subscribe hint — see the module docstring.

        Never load-bearing for correctness: the socket's demonstrated default
        is to deliver this account's order updates once authenticated, with no
        subscribe call at all, and every (re)connect also triggers a REST
        recovery poll regardless of whether this send succeeds, is ignored, or
        was never needed in the first place.
        """
        app = self._app
        if app is None or not self._authenticated:
            return False
        try:
            app.send(json.dumps({"t": "o", "actid": self._actid}))
            return True
        except Exception as e:
            self._log("warn", f"[firstock-order-feed] subscribe hint failed "
                              f"(non-fatal — order updates are expected to "
                              f"arrive without it): {e}")
            return False


class FirstockOrderFeed:
    """One Firstock account's order-update WebSocket.

    NOT a ``MarketFeed`` and never registered with ``FeedRouter``: orders are
    not market data, have no subscription window, and their lifecycle is tied
    to the account's broker session (connect/disconnect), not to what the
    option chain or a position happens to need ticks for. Owned directly by
    ``BrokerManager``, in the same place every other Firstock-only connect
    step already lives — this is a new per-broker hook, not a new layer.
    """

    broker = "firstock"

    def __init__(self, account_id: str, client: FirstockClient, log) -> None:
        self.account_id = account_id
        self.client = client
        self._log = log
        self.should_run = False
        self.last_transport: FirstockOrderTransport | None = None
        self._events = 0
        self._unparsed = 0

        self.ws = WebSocketManager(
            name=f"firstock-order-feed-{account_id}",
            build_transport=self._build_transport,
            subscribe=self._on_open,
            report_error=self._report_error,
            on_tick=self._on_frame,
            retry=RetryManager(max_attempts=0, base_delay=3, cap=30),
            stale_after=STALE_AFTER_S,
        )

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self) -> None:
        """Idempotent, exactly like every other feed's `start()` — callers
        reconcile with this rather than tracking whether they already
        called it."""
        if self.should_run and self.ws.connected:
            return
        if not self.client.jkey:
            self._log("warn", "⚠️  Firstock order feed has no session — not starting")
            return
        self.should_run = True
        self.ws.start()

    def stop(self) -> None:
        self.should_run = False
        self.ws.stop()

    def reconnect(self) -> None:
        self.ws.reconnect()

    @property
    def connected(self) -> bool:
        return self.ws.connected

    def status(self) -> dict:
        s = self.ws.status()
        s["broker"] = self.broker
        s["account"] = self.account_id
        s["orderEvents"] = self._events
        s["unparsedFrames"] = self._unparsed
        transport = self.last_transport
        s["heartbeats"] = transport.pings if transport else 0
        s["lastHeartbeatTs"] = transport.last_ping_ts if transport else 0.0
        return s

    # ── wiring ────────────────────────────────────────────────────────────
    def _build_transport(self) -> Transport:
        # Rebuilt per attempt, exactly like the market feed, so a reconnect
        # after re-authentication presents the fresh jKey rather than the
        # dead one — `self.client` is the SAME object BrokerManager updates
        # in place on every login.
        client = self.client
        transport = FirstockOrderTransport(client.user_id, client.jkey,
                                           client.actid, self._log)
        self.last_transport = transport
        return transport

    def _report_error(self, err: Any) -> str:
        """Classify a transport error and, for a dead session, hand it to the
        SAME session-recovery path REST order placement already uses — a
        session that dies on this socket is exactly as dead everywhere else."""
        from services.reliability.errors import classify_error

        classification = classify_error(err)
        if classification == "session_expired":
            diagnostics.event("orders", "Order WS", "session_expired",
                              level="error", account=self.account_id,
                              reason=redact(str(err)))
            from services.broker_manager import manager
            manager.session_manager.report_error(self.account_id, "firstock", err)
        return classification

    def _on_open(self, transport: Any) -> None:
        """Runs on every (re)connect. WebSocketManager already guards this
        against a raised exception ending the transport thread, so a failure
        in the recovery poll below can never strand the feed."""
        diagnostics.event("orders", "Order WS", "connected",
                          account=self.account_id)
        diagnostics.event("orders", "Order WS", "authenticated",
                          account=self.account_id)
        subscribed = transport.send_subscribe()
        diagnostics.event("orders", "Order WS", "subscribed",
                          account=self.account_id, sentHint=subscribed)
        # The one poll a (re)connect owes: this socket has no memory of what
        # happened while it was down (or before it ever came up — the very
        # first connect), and the account's whole order book is the only
        # thing that can say what changed in that window. Off-thread via
        # poll_soon(), exactly as every other post-amend recovery read is —
        # a REST call must never block this socket's own message loop.
        diagnostics.event("orders", "Order WS", "recovery_poll",
                          account=self.account_id)
        order_sync.poll_soon()

    def _on_frame(self, _handle: Any, msg: Any) -> None:
        if not isinstance(msg, dict) or msg.get("_heartbeat"):
            return  # heartbeat already logged by the transport itself
        if not _is_order_frame(msg):
            return  # some other frame shape this feed does not act on
        order = parse_order_event(msg)
        if order is None:
            self._unparsed += 1
            self._log("warn", f"[firstock-order-feed] unrecognised order "
                              f"frame: {redact(msg)}")
            return
        self._events += 1
        label = _EVENT_LABEL.get(order.status, "Order Update")
        diagnostics.event(
            "orders", f"Order WS {label}", order.status,
            level="warn" if order.status == REJECTED else "info",
            account=self.account_id, orderId=order.order_id,
            filledQty=order.filled_qty,
            avgPrice=round(order.avg_price, 2) or None,
            reason=order.reason or None)
        # complete=False: this frame describes ONE order, not the account's
        # whole book, so it must never be read as "every other open order just
        # disappeared" — see OrderSyncEngine.ingest.
        order_sync.ingest("firstock", self.account_id, [order], complete=False)
