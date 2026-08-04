"""Kotak Neo live market feed, riding the account's existing NeoAPI session.

Structurally different from Angel and Dhan, and the difference drives the whole
design: NeoAPI owns its WebSocket *internally* (``self.NeoWebSocket``) and takes
callbacks on the client object — which is the same object the order router and
the positions poller use. There is no separate socket to construct, so this
feed reuses the authenticated session rather than logging in again.

That session is therefore treated as strictly READ-ONLY:

  * never login, logout, or refresh it here;
  * only WebSocket callbacks are assigned, and they are routed through a
    generation guard so a superseded transport can never fire into a live one;
  * a disconnect retries the WebSocket only. If the failure is a dead session,
    the feed reports itself down and STOPS instead of re-authenticating —
    re-auth would replace the session the order engine is holding, so it stays
    the user's explicit action.

Everything above the transport (retry policy, staleness, canonical ticks) is
the shared machinery every feed uses.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from services.instruments import InstrumentKey, instruments
from services.reliability.errors import classify_error
from services.reliability.retry_manager import RetryManager
from services.reliability.transport import Transport, TransportCallbacks
from services.reliability.ws_manager import WebSocketManager

from .base import CAP_DEPTH, CAP_INDEX, CAP_OPTION, FeedHost, MarketFeed
from .kotak_scrip import KotakScripMaster, OPT_SEGMENT, SPOT_SEGMENT

# Segments worth pulling; anything else is instruments Charticks does not trade.
SCRIP_SEGMENTS = ["nse_fo", "bse_fo", "mcx_fo", "nse_cm", "bse_cm"]

# Subscribe depth as well as quotes, so the paper engine gets real top-of-book
# here exactly as it does from Angel and Dhan. If the depth packet's shape
# differs from what is parsed below, bid/ask come back None and the engine's
# synthetic-spread fallback applies — no worse than a quote-only feed.
WANT_DEPTH = True


class KotakTransport(Transport):
    """Subscription lifecycle over a shared, already-authenticated NeoAPI client.

    Implements `Transport` so it plugs into the same reconnect policy as the
    other feeds, but "open" here means *subscribe* rather than *connect* — the
    SDK establishes the socket lazily on first subscribe.
    """

    def __init__(self, client: Any, groups: list[tuple[str, bool, list[str]]],
                 log) -> None:
        # groups: (exchange_segment, is_index, [subscribe ids])
        self._client = client
        self._groups = groups
        self._log = log
        self._live = True
        self._subscribed: list[tuple[str, bool, list[str]]] = []

    def handle(self) -> Any:
        # Subscription changes go through the transport, which owns the
        # generation guard — never the raw client.
        return self

    def open(self, cb: TransportCallbacks) -> None:
        client = self._client

        # Callbacks are isolated behind `_live`: closing this transport flips
        # the flag, so a late message from a superseded subscription is
        # discarded instead of being reported against the current connection.
        def guard(fn):
            def inner(*args: Any) -> None:
                if not self._live:
                    return
                fn(*args)
            return inner

        @guard
        def on_open(*_a: Any) -> None:
            cb.on_open()

        @guard
        def on_message(message: Any = None) -> None:
            cb.on_data(message)

        @guard
        def on_error(err: Any = None) -> None:
            cb.on_error(err, f"kotak feed: {err}")

        @guard
        def on_close(*_a: Any) -> None:
            cb.on_close()

        # WebSocket-only attributes. The order APIs do not read these, so
        # assigning them cannot affect order execution.
        client.on_open = on_open
        client.on_message = on_message
        client.on_error = on_error
        client.on_close = on_close

        threading.Thread(target=self._subscribe_all, args=(cb,), daemon=True,
                         name="kotak-ws-subscribe").start()

    def _subscribe_all(self, cb: TransportCallbacks) -> None:
        try:
            for segment, is_index, ids in self._groups:
                if not ids:
                    continue
                self._send(segment, is_index, ids, subscribe=True)
                self._subscribed.append((segment, is_index, list(ids)))
        except Exception as e:
            if self._live:
                cb.on_error(e, f"kotak subscribe failed: {e}")

    def _send(self, segment: str, is_index: bool, ids: list[str],
              subscribe: bool) -> None:
        tokens = [{"instrument_token": i, "exchange_segment": segment} for i in ids]
        fn = self._client.subscribe if subscribe else self._client.un_subscribe
        fn(instrument_tokens=tokens, isIndex=is_index, isDepth=False)
        if WANT_DEPTH and not is_index:
            try:
                fn(instrument_tokens=tokens, isIndex=is_index, isDepth=True)
            except Exception as e:
                # Quotes still flow; only bid/ask degrade to the synthetic spread.
                self._log("warn", f"[kotak] depth {'' if subscribe else 'un'}subscribe "
                                  f"failed on {segment}: {e}")

    def update(self, add: list[tuple[str, bool, list[str]]],
               remove: list[tuple[str, bool, list[str]]]) -> None:
        """Apply a subscription delta on the live connection."""
        for segment, is_index, ids in remove:
            try:
                self._send(segment, is_index, ids, subscribe=False)
            except Exception as e:
                self._log("warn", f"[kotak] unsubscribe failed on {segment}: {e}")
        for segment, is_index, ids in add:
            self._send(segment, is_index, ids, subscribe=True)

    def close(self) -> None:
        """Unsubscribe and go silent — but never log the session out.

        `un_subscribe_all()` does NOT exist in this SDK version, which is why
        the legacy engine's teardown (wrapped in a bare except) silently did
        nothing and left stale strikes streaming after every resubscribe. The
        real per-instrument call is used instead.
        """
        self._live = False
        for segment, is_index, ids in self._subscribed:
            try:
                self._send(segment, is_index, ids, subscribe=False)
            except Exception:
                pass
        self._subscribed = []


class KotakFeed(MarketFeed):
    broker = "kotak"

    def __init__(self, account_id: str, host: FeedHost) -> None:
        super().__init__(account_id, host)
        self.client: Any = None
        self.should_run = False
        self.last_transport: KotakTransport | None = None
        # Set when the feed stops because the SHARED session looks dead. It is
        # cleared only by the user reconnecting the account, which is what
        # re-authenticates — this feed never does.
        self.needs_reauth = False

        self.scrip = KotakScripMaster(self.host.log)
        self._scrip_day: str | None = None
        self._index_keys: set[InstrumentKey] = set()
        self._option_keys: set[InstrumentKey] = set()
        self._sent: set[InstrumentKey] = set()

        self.index_sub_state = "none"
        self.option_sub_state = "none"
        self.option_sub_ts = 0.0
        self.option_sub_tokens = 0

        self.ws = WebSocketManager(
            name="kotak-feed",
            build_transport=self._build_transport,
            subscribe=self._on_subscribed,
            report_error=self._report_error,
            on_tick=self._on_tick,
            retry=RetryManager(max_attempts=0, base_delay=3, cap=15),
        )

    # ── lifecycle ─────────────────────────────────────────────────────────
    def apply_session(self, client: Any) -> None:
        """Adopt the account's already-authenticated NeoAPI client.

        Called on every (re)connect, so a user-initiated re-login hands the
        feed the fresh session and clears the needs_reauth latch.
        """
        self.client = client
        self.needs_reauth = False

    def start(self) -> None:
        if self.should_run and self.ws.connected:
            return
        if self.client is None:
            self.host.log("warn", "⚠️  Kotak feed has no session — not starting")
            return
        if self.needs_reauth:
            # Deliberate: recovering needs a new login, and that would replace
            # the session the order engine holds.
            self.host.log("warn", "⚠️  Kotak feed needs re-authentication — "
                                  "reconnect the account to restore market data")
            return
        if not self._ensure_scrip():
            return
        self.should_run = True
        self.ws.start()

    def stop(self) -> None:
        self.should_run = False
        self.ws.stop()

    def reconnect(self) -> None:
        """WebSocket only — never a session refresh."""
        if self.needs_reauth:
            return
        self.ws.reconnect()

    def capabilities(self) -> set[str]:
        caps = {CAP_INDEX, CAP_OPTION}
        if WANT_DEPTH:
            caps.add(CAP_DEPTH)
        return caps

    @property
    def connected(self) -> bool:
        return self.ws.connected

    def status(self) -> dict:
        s = self.ws.status()
        s["broker"] = self.broker
        s["account"] = self.account_id
        s["indexSubscribe"] = self.index_sub_state
        s["optionSubscribe"] = self.option_sub_state
        s["scripMaster"] = self.scrip.loaded_from
        s["scripInstruments"] = self.scrip.row_count
        s["needsReauth"] = self.needs_reauth
        return s

    def _report_error(self, err: Any) -> str:
        """Classify WITHOUT triggering session recovery.

        Every other feed routes errors to SessionManager, which re-authenticates
        automatically. That is wrong here: the session is shared with the order
        engine, so a feed-initiated re-login would swap the object out from
        under an in-flight order. A dead session instead stops the feed and
        surfaces as needsReauth, leaving recovery to the user.
        """
        classification = classify_error(err)
        if classification == "session_expired":
            self.needs_reauth = True
            self.should_run = False
            self.ws.should_run = False   # no retry loop against a dead session
            self.host.log("error", "❌ Kotak market feed: session rejected — "
                                   "reconnect the Kotak account to restore market data "
                                   "(not re-authenticating automatically; the order "
                                   "engine shares this session)")
        return classification

    # ── instruments ───────────────────────────────────────────────────────
    def _ensure_scrip(self) -> bool:
        today = time.strftime("%Y%m%d")
        if self._scrip_day == today and self.scrip.row_count:
            return True
        if not self.scrip.load(self.client, SCRIP_SEGMENTS):
            return False
        instruments.clear_broker(self.broker)
        instruments.bind_many(self.broker, self.scrip.bindings())
        instruments.alias_many(self.broker, self.scrip.aliases())
        self._scrip_day = today
        self._index_keys = {k for k in self.scrip.segments if k.segment == "INDEX"}
        return True

    def _groups(self, keys: set[InstrumentKey]) -> list[tuple[str, bool, list[str]]]:
        """Bucket keys into (segment, is_index, ids). Indices need their own
        call because isIndex is a per-subscribe flag, not a per-instrument one."""
        buckets: dict[tuple[str, bool], list[str]] = {}
        for key in keys:
            token = instruments.token_for(self.broker, key)
            if token is None:
                continue
            is_index = key.segment == "INDEX"
            segment = (SPOT_SEGMENT if is_index else OPT_SEGMENT).get(key.underlying)
            if segment is None:
                continue
            buckets.setdefault((segment, is_index), []).append(token)
        return [(seg, idx, ids) for (seg, idx), ids in buckets.items()]

    def _desired(self) -> set[InstrumentKey]:
        return self._index_keys | self._option_keys

    def _build_transport(self) -> Transport:
        transport = KotakTransport(self.client, self._groups(self._desired()),
                                   self.host.log)
        self.last_transport = transport
        return transport

    def _on_subscribed(self, _handle: Any) -> None:
        self._sent = set(self._desired())
        self.index_sub_state = f"subscribed ({len(self._index_keys)} on open)"
        if self._option_keys:
            self.option_sub_state = "subscribed (on open)"
            self.option_sub_ts = time.time()

    def subscribe_keys(self, keys: set[InstrumentKey]) -> None:
        """Replace the option subscription, given canonical keys."""
        self._option_keys = set(keys)
        self.option_sub_tokens = len(self._option_keys)
        desired = self._desired()
        transport = self.last_transport
        if not self.ws.connected or transport is None:
            self.option_sub_state = "deferred (feed not open)"
            return
        add = self._groups(desired - self._sent)
        remove = self._groups(self._sent - desired)
        if not add and not remove:
            return
        try:
            transport.update(add, remove)
        except Exception as e:
            self.option_sub_state = f"failed: {e}"
            self.host.log("warn", f"[kotak] option subscribe failed: {e}")
            return
        self._sent = desired
        self.option_sub_state = "subscribed"
        self.option_sub_ts = time.time()

    # ── tick ingest (Kotak wire format -> canonical) ──────────────────────
    @staticmethod
    def _f(v: Any) -> float | None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f > 0 else None

    def _on_tick(self, _handle: Any, message: Any) -> None:
        """NeoAPI delivers {"type": "stock_feed", "data": [tick, ...]}; some
        messages are plain ack/status strings."""
        try:
            if isinstance(message, (str, bytes)):
                import json
                try:
                    message = json.loads(message)
                except Exception:
                    return  # ack / status text
            if not isinstance(message, dict):
                return
            data = message.get("data", message)
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                return
            for tick in data:
                if isinstance(tick, dict):
                    self._process(tick)
        except Exception as e:
            self.host.log("warn", f"Kotak tick parse error: {e}")

    def _process(self, tick: dict) -> None:
        ident = tick.get("tk") or tick.get("token") or tick.get("instrument_token")
        if ident is None:
            return
        key = instruments.key_for(self.broker, str(ident))
        if key is None:
            self.host.on_unmapped_tick(self, str(ident))
            return
        ltp = self._f(tick.get("ltp") or tick.get("last_traded_price") or tick.get("LTP"))
        if ltp is None:
            # A depth-only packet carries no LTP; merge its book into the
            # existing quote rather than discarding it.
            bid, ask = self._best_bid_ask(tick)
            if bid is None and ask is None:
                return
            prev_ltp = None
            from services.broker_manager import manager
            prev_ltp = manager.get_option_ltp(key)
            if prev_ltp is None:
                return
            self.host.on_option_tick(self, key, prev_ltp, None, bid, ask)
            return

        if key.segment == "INDEX":
            close = self._f(tick.get("c") or tick.get("close"))
            pct = ((ltp - close) / close * 100) if close else 0.0
            self.host.on_index_tick(self, key.underlying, ltp, pct)
            return

        raw_vol = tick.get("v") or tick.get("volume_trade_for_the_day") or tick.get("volume")
        try:
            volume = int(float(raw_vol)) if raw_vol is not None else None
        except (TypeError, ValueError):
            volume = None
        raw_oi = tick.get("oi") or tick.get("OI") or tick.get("openInterest")
        try:
            oi = int(float(raw_oi)) if raw_oi is not None else None
        except (TypeError, ValueError):
            oi = None
        bid, ask = self._best_bid_ask(tick)
        self.host.on_option_tick(self, key, ltp, volume, bid, ask, oi=oi)

    @classmethod
    def _best_bid_ask(cls, tick: dict) -> tuple[float | None, float | None]:
        """Top of book from a Kotak depth packet.

        Kotak's field naming for depth has not been verifiable without a live
        account, so several documented spellings are accepted and anything
        unrecognised yields (None, None) — which routes the paper engine to its
        existing synthetic spread rather than to a wrong price.
        """
        bid = cls._f(tick.get("bp") or tick.get("bid") or tick.get("bid_price")
                     or tick.get("bp1"))
        ask = cls._f(tick.get("sp") or tick.get("ask") or tick.get("ask_price")
                     or tick.get("sp1"))
        if bid is not None or ask is not None:
            return bid, ask
        depth = tick.get("depth") or tick.get("bids") or tick.get("buy")
        if isinstance(depth, (list, tuple)) and depth:
            first = depth[0]
            if isinstance(first, dict):
                bid = cls._f(first.get("price") or first.get("bid_price") or first.get("bp"))
        asks = tick.get("asks") or tick.get("sell")
        if isinstance(asks, (list, tuple)) and asks:
            first = asks[0]
            if isinstance(first, dict):
                ask = cls._f(first.get("price") or first.get("ask_price") or first.get("sp"))
        return bid, ask
