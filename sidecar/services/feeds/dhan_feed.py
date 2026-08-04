"""Dhan HQ live market feed (dhanhq v2, MarketFeed.Full).

Full rather than Quote, deliberately. The paper engine is already a real
bid/ask simulator fed by Angel's 5-level depth, and the Quote packet carries
neither depth nor open interest — so a Dhan-sourced contract would silently
fall back to a synthetic spread while an Angel-sourced one used the real book,
making fill quality depend on which broker happened to connect first. Full also
carries true OI, which is what lets the option chain stop labelling volume as
open interest.

The SDK is asyncio, so this feed rides `AsyncioTransport`: a private event loop
on its own thread, behind the same synchronous Transport contract the Angel
socket uses. Retry/backoff is emphatically NOT delegated to the SDK — its
`_run_async` has its own reconnect loop, which is bypassed here so that
WebSocketManager remains the single owner of reconnect policy. Two competing
backoff loops on one socket is how a feed ends up permanently half-connected.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from services.instruments import InstrumentKey, instruments
from services.reliability.retry_manager import RetryManager
from services.reliability.transport import AsyncioTransport, Transport, TransportCallbacks
from services.reliability.ws_manager import WebSocketManager

from .base import CAP_DEPTH, CAP_INDEX, CAP_OPTION, FeedHost, MarketFeed
from .dhan_scrip import DhanScripMaster

# dhanhq v2 accepts only Ticker (15), Quote (17) and Full (21). One constant, so
# switching tiers later is a single edit — see the module docstring for why Full.
FEED_MODE = 21  # MarketFeed.Full

# Dhan caps a subscription message at 100 instruments and a connection at 5000.
BATCH = 100
MAX_INSTRUMENTS = 5000

# Server-disconnection reason codes (packet type 50). 807/809 are auth failures
# that must reach session recovery, not a blind socket retry.
_DISCONNECT_REASONS = {
    805: "No. of active websocket connections exceeded",
    806: "Subscribe to Data APIs to continue",
    807: "Access Token is expired",
    808: "Invalid Client ID",
    809: "Authentication Failed",
}
_AUTH_REASONS = (807, 808, 809)


class DhanTransport(AsyncioTransport):
    """Drives dhanhq's MarketFeed on our own event loop."""

    def __init__(self, client_id: str, access_token: str,
                 instruments_list: list[tuple[int, str, int]]) -> None:
        super().__init__(name="dhan-feed")
        self._client_id = client_id
        self._access_token = access_token
        self._instruments = list(instruments_list)
        self._feed: Any = None

    def handle(self) -> Any:
        # Subscriptions are issued against the transport, not the raw SDK
        # object, because changing them means scheduling work on our loop.
        return self

    async def run(self, cb: TransportCallbacks) -> None:
        from dhanhq import DhanContext, MarketFeed

        ctx = DhanContext(client_id=self._client_id, access_token=self._access_token)
        # MarketFeed.__init__ calls asyncio.set_event_loop() on the constructing
        # thread, which would displace ours as this thread's current loop. Our
        # loop is already *running* so it keeps executing either way, but the
        # thread-local must be restored or anything calling get_event_loop()
        # later would be handed the SDK's idle loop instead.
        ours = asyncio.get_running_loop()
        feed = MarketFeed(ctx, self._instruments, version="v2")
        asyncio.set_event_loop(ours)
        self._feed = feed

        await feed.connect()          # authorises + sends the subscriptions
        cb.on_open()

        while not self._closing:
            packet = await feed.get_instrument_data()
            if packet is None:
                # process_data returns None only for a server-disconnection
                # packet, whose reason it printed and discarded. We cannot see
                # the code from here, so report a generic disconnect and let
                # the policy retry; _on_server_disconnect below captures the
                # code when the SDK exposes it.
                raise ConnectionError("Dhan feed: server disconnected")
            cb.on_data(packet)

    async def shutdown(self) -> None:
        if self._feed is not None:
            try:
                await self._feed.disconnect()
            except Exception:
                pass

    # ── live subscription changes ─────────────────────────────────────────
    def update_subscription(self, add: list[tuple[int, str, int]],
                            remove: list[tuple[int, str, int]]) -> None:
        """Send subscribe/unsubscribe for a delta on the open connection.

        The SDK's own `subscribe_symbols` runs its coroutines on the loop it
        created in `__init__`, which is NOT the loop this transport drives — so
        the messages would be queued on a loop that never runs. The v2 wire
        format is simple, so it is sent directly on our loop instead.
        """
        loop = self._loop
        feed = self._feed
        if loop is None or feed is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._send_updates(feed, add, remove), loop)

    async def _send_updates(self, feed: Any, add: list, remove: list) -> None:
        import json
        # RequestCode 21 subscribes Full; 22 unsubscribes it (Dhan pairs each
        # subscribe code with the next integer for the matching unsubscribe).
        for items, code in ((remove, FEED_MODE + 1), (add, FEED_MODE)):
            for i in range(0, len(items), BATCH):
                batch = items[i:i + BATCH]
                if not batch:
                    continue
                msg = {
                    "RequestCode": code,
                    "InstrumentCount": len(batch),
                    "InstrumentList": [
                        {"ExchangeSegment": feed.get_exchange_segment(seg),
                         "SecurityId": str(sec)}
                        for seg, sec, _mode in batch
                    ],
                }
                try:
                    await feed.ws.send(json.dumps(msg))
                except Exception:
                    # Dropped here is not lost: the desired set is rebuilt and
                    # resent in full on the next (re)connect.
                    return


class DhanFeed(MarketFeed):
    broker = "dhan"

    def __init__(self, account_id: str, host: FeedHost) -> None:
        super().__init__(account_id, host)
        self.client_id: str | None = None
        self.access_token: str | None = None
        self.should_run = False
        self.last_transport: DhanTransport | None = None

        cache_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))),
            "app", "data_cache")
        self.scrip = DhanScripMaster(cache_dir, self.host.log)
        self._scrip_day: str | None = None

        # Desired subscription set, canonical. Indices are permanent; options
        # are replaced wholesale whenever the chain window moves.
        self._index_keys: set[InstrumentKey] = set()
        self._option_keys: set[InstrumentKey] = set()
        self._sent: set[InstrumentKey] = set()

        self.index_sub_state = "none"
        self.option_sub_state = "none"
        self.option_sub_ts = 0.0
        self.option_sub_tokens = 0

        self.ws = WebSocketManager(
            name="dhan-feed",
            build_transport=self._build_transport,
            subscribe=self._subscribe_all,
            report_error=lambda err: self.host.report_feed_error(self, err),
            on_tick=self._on_tick,
            retry=RetryManager(max_attempts=0, base_delay=3, cap=15),
        )

    # ── lifecycle ─────────────────────────────────────────────────────────
    def apply_session(self, client_id: str, access_token: str) -> None:
        """Credentials come from the account the user already connected — this
        feed never reads the credential store itself."""
        self.client_id = client_id
        self.access_token = access_token

    def start(self) -> None:
        if self.should_run and self.ws.connected:
            return
        if not (self.client_id and self.access_token):
            self.host.log("warn", "⚠️  Dhan feed has no session — not starting")
            return
        if self.should_run:
            self.host.log("warn", "⚠️  Dhan feed registered but not connected — restarting it")
        if not self._ensure_scrip():
            return
        self.should_run = True
        self.ws.start()

    def stop(self) -> None:
        self.should_run = False
        self.ws.stop()

    def reconnect(self) -> None:
        self.ws.reconnect()

    def capabilities(self) -> set[str]:
        # Full carries 5-level depth and OI, so this feed can serve every
        # capability the paper engine and option chain need.
        return {CAP_INDEX, CAP_OPTION, CAP_DEPTH}

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
        return s

    # ── instruments ───────────────────────────────────────────────────────
    def _ensure_scrip(self) -> bool:
        """Load Dhan's master at most once a day, binding every instrument into
        the registry under the "dhan" namespace."""
        today = time.strftime("%Y%m%d")
        if self._scrip_day == today and self.scrip.row_count:
            return True
        if not self.scrip.load():
            return False
        # Scoped to this broker: Angel's bindings are a separate namespace and
        # are never touched.
        instruments.clear_broker(self.broker)
        instruments.bind_many(self.broker, self.scrip.bindings())
        self._scrip_day = today
        self._index_keys = {k for k in self.scrip.segments if k.segment == "INDEX"}
        return True

    def _spec(self, key: InstrumentKey) -> tuple[int, str, int] | None:
        sec = instruments.token_for(self.broker, key)
        seg = self.scrip.segments.get(key)
        if sec is None or seg is None:
            return None
        return (seg, str(sec), FEED_MODE)

    def _desired(self) -> set[InstrumentKey]:
        return self._index_keys | self._option_keys

    def _build_transport(self) -> Transport:
        specs = [s for s in (self._spec(k) for k in self._desired()) if s]
        if len(specs) > MAX_INSTRUMENTS:
            self.host.log("warn", f"⚠️  Dhan feed asked for {len(specs)} instruments; "
                                  f"capping at {MAX_INSTRUMENTS} (one connection's limit)")
            specs = specs[:MAX_INSTRUMENTS]
        transport = DhanTransport(self.client_id or "", self.access_token or "", specs)
        self.last_transport = transport
        return transport

    def _subscribe_all(self, _handle: Any) -> None:
        """Everything was subscribed by the SDK at connect time from the list
        we handed it, so on_open only has to record what is now live."""
        self._sent = set(self._desired())
        self.index_sub_state = f"subscribed ({len(self._index_keys)} on open)"
        if self._option_keys:
            self.option_sub_state = "subscribed (on open)"
            self.option_sub_ts = time.time()

    def subscribe_keys(self, keys: set[InstrumentKey]) -> None:
        """Replace the option subscription with `keys`.

        Canonical keys, not broker tokens — the caller (the option chain) must
        not have to know Dhan security ids.
        """
        self._option_keys = set(keys)
        self.option_sub_tokens = len(self._option_keys)
        desired = self._desired()
        transport = self.last_transport
        if not self.ws.connected or transport is None:
            # Picked up in full when the connection is (re)built.
            self.option_sub_state = "deferred (feed not open)"
            return
        add = [s for s in (self._spec(k) for k in desired - self._sent) if s]
        remove = [s for s in (self._spec(k) for k in self._sent - desired) if s]
        unresolved = len([k for k in desired - self._sent if self._spec(k) is None])
        if not add and not remove:
            return
        transport.update_subscription(add, remove)
        self._sent = desired
        self.option_sub_state = ("subscribed" if not unresolved
                                 else f"subscribed ({unresolved} unresolved)")
        self.option_sub_ts = time.time()

    # ── tick ingest (Dhan wire format -> canonical) ───────────────────────
    @staticmethod
    def _f(v: Any) -> float | None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f > 0 else None

    def _on_tick(self, _handle: Any, msg: Any) -> None:
        try:
            if not isinstance(msg, dict):
                return
            sec_id = msg.get("security_id")
            if sec_id is None:
                return
            ltp = self._f(msg.get("LTP"))
            if ltp is None:
                return
            key = instruments.key_for(self.broker, str(sec_id))
            if key is None:
                self.host.on_unmapped_tick(self, str(sec_id))
                return

            if key.segment == "INDEX":
                close = self._f(msg.get("close"))
                pct = ((ltp - close) / close * 100) if close else 0.0
                self.host.on_index_tick(self, key.underlying, ltp, pct)
                return

            try:
                volume = int(msg.get("volume")) if msg.get("volume") is not None else None
            except (TypeError, ValueError):
                volume = None
            oi = msg.get("OI")
            try:
                oi = int(oi) if oi is not None else None
            except (TypeError, ValueError):
                oi = None
            bid, ask = self._best_bid_ask(msg)
            self.host.on_option_tick(self, key, ltp, volume, bid, ask, oi=oi)
        except Exception as e:
            self.host.log("warn", f"Dhan tick parse error: {e}")

    @classmethod
    def _best_bid_ask(cls, msg: dict) -> tuple[float | None, float | None]:
        """Top of book from the Full packet's 5-level depth.

        Dhan formats depth prices as 2dp STRINGS and pads absent levels with
        "0.00", so a plain float() of level 0 would hand the paper engine a
        zero bid and fill every sell at nothing. Levels are taken in order and
        the first genuinely positive price wins; when none is, (None, None)
        makes the engine fall back to its synthetic spread exactly as it does
        for an Angel tick with no depth.
        """
        depth = msg.get("depth")
        if not isinstance(depth, (list, tuple)):
            return None, None
        bid = ask = None
        for level in depth:
            if not isinstance(level, dict):
                continue
            if bid is None:
                bid = cls._f(level.get("bid_price"))
            if ask is None:
                ask = cls._f(level.get("ask_price"))
            if bid is not None and ask is not None:
                break
        return bid, ask
