"""Angel One SmartAPI market feed.

Lifted out of BrokerManager unchanged in behaviour: same single-socket design,
same subscribe/unsubscribe ordering, same tick parsing. What moved is only the
*ownership* — the jwt/feed tokens, the socket, the index token map and the
option-subscription diagnostics now belong to this object instead of being
fields on the broker manager, so a second broker's feed can exist alongside it
without colliding.

The one-connection-per-feed_token constraint that shapes this file is Angel's,
not Charticks': SmartAPI silently rejects or kills a second SmartWebSocketV2
opened with the same credentials, which is why index and option ticks share one
socket here. Other brokers do not necessarily have that limit, and their feeds
should not inherit this design just because it is what Angel needed.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
import traceback
from typing import Any

from logzero import logger as _file_log

from services.instruments import InstrumentKey, instruments
from services.reliability.retry_manager import RetryManager
from services.reliability.transport import Transport, TransportCallbacks
from services.reliability.ws_manager import WebSocketManager
from services.reliability.subscription_registry import SubscriptionRegistry

from .base import CAP_DEPTH, CAP_INDEX, CAP_OPTION, FeedHost, MarketFeed

# Angel SmartWebSocket exchangeType codes.
_SPOT_EXCH_TYPE = {"NSE": 1, "BSE": 3, "MCX": 5}
_OPT_EXCH_TYPE = {"NFO": 2, "BFO": 4, "MCX": 5}

# Spot (cash index) exchange per supported index. Must stay in step with
# BrokerManager._OPT_EXCH: an index whose OPTIONS we resolve but whose SPOT we
# don't will render an empty option chain forever, because the ATM strike can't
# be computed without a spot price.
_SPOT_EXCH = {"NIFTY": "NSE", "BANKNIFTY": "NSE", "FINNIFTY": "NSE",
              "MIDCPNIFTY": "NSE", "SENSEX": "BSE", "BANKEX": "BSE"}


class AngelTransport(Transport):
    """SmartWebSocketV2 mechanics behind the generic Transport contract.

    The SDK wants four callback attributes assigned before a blocking
    `connect()`, and it reports a give-up as a two-argument on_error. Both
    quirks are absorbed here so WebSocketManager never has to know them.
    """

    def __init__(self, jwt_token: str | None, api_key: str | None,
                 client_code: str | None, feed_token: str | None) -> None:
        self._args = (jwt_token, api_key, client_code, feed_token)
        self._ws: Any = None

    def open(self, cb: TransportCallbacks) -> None:
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2
        jwt_token, api_key, client_code, feed_token = self._args
        # Retries are the manager's job, not the SDK's — two competing backoff
        # loops on one socket is how a feed ends up permanently half-connected.
        ws = SmartWebSocketV2(jwt_token, api_key, client_code, feed_token,
                              max_retry_attempt=0, retry_strategy=0, retry_delay=5)
        self._ws = ws

        ws.on_open = lambda _ws: cb.on_open()
        ws.on_data = lambda _ws, msg: cb.on_data(msg)
        ws.on_close = lambda _ws: cb.on_close()

        def _on_error(a: Any, b: Any) -> None:
            # SmartWebSocketV2 signals a give-up as
            # on_error("Max retry attempt reached", "Connection closed") — the
            # first argument is NOT the socket. Both halves carry meaning, so
            # they are combined into the detail string while `b` remains the
            # object error classification keys off.
            detail = f"{a}: {b}" if a and str(a) != str(b) else str(b)
            cb.on_error(b, detail)

        ws.on_error = _on_error
        # connect() blocks for the life of the connection.
        threading.Thread(target=ws.connect, daemon=True, name="angel-ws-connect").start()

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close_connection()

    def handle(self) -> Any:
        return self._ws


class AngelFeed(MarketFeed):
    broker = "angel"

    def __init__(self, account_id: str, host: FeedHost,
                 subscriptions: SubscriptionRegistry) -> None:
        super().__init__(account_id, host)
        # Session tokens for the feed — set by apply_session() on every
        # (re)authentication, because a re-auth invalidates the previous pair.
        self.jwt_token: str | None = None
        self.feed_token: str | None = None
        self.client_code: str | None = None
        self.api_key: str | None = None

        self.subscriptions = subscriptions
        self.market_token_map: dict[str, dict] = {}
        self.should_run = False
        # The most recently BUILT transport — not necessarily the connected
        # one. Diagnostics only; anything sending on the wire must go through
        # ws.live_socket().
        self.last_transport: Transport | None = None

        # Option-subscription diagnostics (exposed via GET /market-feed): a
        # chain full of blank prices is either "never subscribed", "subscribe
        # failed" or "subscribed but no ticks" — these tell them apart.
        self.option_sub_state: str = "none"
        self.option_sub_ts: float = 0.0
        self.option_sub_tokens: int = 0
        # Same idea for the index/spot subscribe. Without a spot price the
        # option chain cannot compute an ATM strike, so a silent failure here
        # empties the chain just as thoroughly as a failed option subscribe.
        self.index_sub_state: str = "none"
        # (master length, trading date) of the last option-catalogue build.
        self._option_index_stamp: tuple[int, Any] | None = None

        self.ws = WebSocketManager(
            name="market-feed",
            build_transport=self._build_transport,
            subscribe=self._subscribe_all,
            report_error=lambda err: self.host.report_feed_error(self, err),
            on_tick=self._on_tick,
            # Cap kept low (15s, was 60s) so a retry after network restoration
            # fires soon; the health monitor's stale watchdog also force-
            # reconnects zombie sockets well before this timer would.
            retry=RetryManager(max_attempts=0, base_delay=3, cap=15),
        )

    # ── lifecycle ────────────────────────────────────────────────────────
    def apply_session(self, jwt_token: str, feed_token: str, client_code: str,
                      api_key: str) -> None:
        """Install freshly-issued session tokens. Called on first connect and
        again after every re-auth — the socket must be rebuilt afterwards, or
        it keeps presenting the invalidated pair."""
        self.jwt_token = jwt_token
        self.feed_token = feed_token
        self.client_code = client_code
        self.api_key = api_key

    def start(self) -> None:
        """Start the feed, or revive it if it is registered as running but is
        not actually connected.

        `should_run` only records that we ONCE asked the feed to start — not
        that data is flowing. Gating the start on it meant a feed that never
        established stayed dead forever: re-connecting the broker re-ran the
        REST login, saw the flag already True, and skipped the feed entirely.
        Every connect path calls this, so reconnecting is always a real remedy.
        """
        if self.should_run and self.ws.connected:
            return
        if self.should_run:
            self.host.log("warn", "⚠️  Market feed registered but not connected — restarting it")
        # Re-resolve tokens: a master reload may have changed them.
        # Publish this broker's option catalogue before the chain asks for it.
        self.ensure_option_index()
        tokens = self._resolve_index_tokens()
        if not tokens:
            self.host.log("warn", "⚠️  No index tokens resolved — market stream not started")
            return
        self.market_token_map = tokens
        for name, d in tokens.items():
            self.subscriptions.register("index", name, d)
        self.should_run = True
        self.ws.start()

    def stop(self) -> None:
        self.should_run = False
        self.ws.stop()

    def reconnect(self) -> None:
        self.ws.reconnect()

    def capabilities(self) -> set[str]:
        return {CAP_INDEX, CAP_OPTION, CAP_DEPTH}

    @property
    def connected(self) -> bool:
        return self.ws.connected

    def status(self) -> dict:
        s = self.ws.status()
        s["broker"] = self.broker
        s["account"] = self.account_id
        # Subscribe outcomes: an open socket carrying no data is a different
        # fault from a socket that never opened, and only these tell them apart.
        s["indexSubscribe"] = self.index_sub_state
        s["optionSubscribe"] = self.option_sub_state
        return s

    # ── index token resolution ───────────────────────────────────────────
    def _nearest_crude_future(self) -> dict | None:
        """The front-month CRUDEOIL future on MCX, which stands in for the spot
        of the Crude option chain. CRUDEOILM (the mini contract) is a separate
        `name` in the master and is excluded by the name match; the symbol guard
        mirrors the legacy app and costs nothing. Expired futures are dropped so
        the front month rolls over on its own."""
        from services import expiry as expiry_filter
        futs = [s for s in self.host.instrument_master()
                if s.get("name", "").upper() == "CRUDEOIL"
                and s.get("exch_seg") == "MCX"
                and s.get("instrumenttype") == "FUTCOM"
                and "CRUDEOILM" not in s.get("symbol", "")
                and s.get("token")
                and not expiry_filter.is_expired(s.get("expiry", ""))]
        if not futs:
            return None
        try:
            futs.sort(key=lambda x: dt.datetime.strptime(x["expiry"], "%d%b%Y"))
        except (KeyError, ValueError):
            return futs[0]
        return futs[0]

    def _resolve_index_tokens(self) -> dict[str, dict]:
        """Resolve every supported index's spot token from the instrument
        master. Several rows share an index name (e.g. the 'Nifty 50' display
        row and the 'NIFTY' index row), so the row whose symbol matches the
        index name exactly wins; otherwise the last candidate is kept."""
        tokens: dict[str, dict] = {}
        exact: set[str] = set()
        for s in self.host.instrument_master():
            name = s.get("name", "").upper()
            exch = _SPOT_EXCH.get(name)
            if not exch or s.get("exch_seg", "") != exch or s.get("expiry"):
                continue
            if name in exact:
                continue
            token = s.get("token")
            if not token:
                continue
            tokens[name] = {"token": str(token), "exchange": exch}
            instruments.bind(self.broker, InstrumentKey.index(name), str(token))
            if s.get("symbol", "").upper() == name:
                exact.add(name)
        # Crude Oil (MCX) has no cash index to quote — the legacy app used the
        # nearest-expiry CRUDEOIL future as the underlying, and the option chain
        # ATM is computed off it, so keep that behaviour identical here.
        crude = self._nearest_crude_future()
        if crude:
            tokens["CRUDEOIL"] = {"token": str(crude["token"]), "exchange": "MCX"}
            # Bound as INDEX, matching how it is used: the front-month future
            # stands in for a spot Crude has no cash index for, and index_ltp /
            # the ATM calculation treat it exactly like one.
            instruments.bind(self.broker, InstrumentKey.index("CRUDEOIL"), str(crude["token"]))
        missing = [i for i in list(_SPOT_EXCH) + ["CRUDEOIL"] if i not in tokens]
        if missing:
            self.host.log("warn", f"⚠️  No spot token for {', '.join(missing)} — "
                                  "their option chain cannot compute an ATM strike")
        return tokens

    # ── socket + subscriptions ───────────────────────────────────────────
    def _build_transport(self) -> Transport:
        # Rebuilt per attempt so a post-recovery reconnect picks up the tokens
        # apply_session() installed, rather than re-presenting the dead pair.
        transport = AngelTransport(self.jwt_token, self.api_key,
                                   self.client_code, self.feed_token)
        self.last_transport = transport
        return transport

    def _subscribe_all(self, ws: Any) -> None:
        token_list = [{
            "exchangeType": _SPOT_EXCH_TYPE.get(d["exchange"], 1),
            "tokens": [d["token"]],
        } for d in self.subscriptions.all("index")]
        if token_list:
            # Guarded like the option subscribe below. This runs inside on_open,
            # i.e. on the SDK's own connect thread: an escaping exception kills
            # that thread before on_close can fire, so the reconnect policy is
            # never triggered and the feed stays down forever. A failure here
            # must leave the socket open — the staleness watchdog then sees no
            # ticks and forces a genuine reconnect.
            try:
                ws.subscribe("marketwatch", 3, token_list)
                self.index_sub_state = "subscribed (on open)"
            except Exception as e:
                self.index_sub_state = f"failed on open: {e}"
                self.host.log("warn", f"[index-feed] subscribe on open failed: {e}")
        option_list = self.subscriptions.all("option")
        if option_list:
            try:
                ws.subscribe("optionchain", 3, option_list)
                self.option_sub_state = "subscribed (on open)"
                self.option_sub_ts = time.time()
            except Exception as e:
                self.option_sub_state = f"failed on open: {e}"
                self.host.log("warn", f"[option-chain] subscribe on open failed: {e}")

    # ── Angel's option catalogue ──────────────────────────────────────────
    def ensure_option_index(self) -> None:
        """Bind every tradable Angel option into the shared registry.

        Moved here from the option chain, which used to scan Angel's raw
        instrument-master rows itself — that was the last thing tying the chain
        to Angel being connected. Now each feed publishes its own catalogue in
        canonical form and the chain reads only the registry.

        Rebuilt when the master changes OR the trading date rolls over, because
        expired contracts are excluded from the index itself: an entry that was
        valid yesterday must not survive into today's lookups.
        """
        from services import expiry as expiry_filter
        master = self.host.instrument_master()
        if not master:
            return
        stamp = (len(master), expiry_filter.now_ist().date())
        if self._option_index_stamp == stamp:
            return
        bindings: list[tuple[InstrumentKey, str]] = []
        for s in master:
            if "OPT" not in s.get("instrumenttype", ""):
                continue
            name = s.get("name", "").upper()
            if s.get("exch_seg", "") != self.host.option_exchange(name):
                continue
            # Charticks' own expiry gate: the broker keeps listing yesterday's
            # contracts, so they are dropped here and can never be resolved.
            if expiry_filter.is_expired(s.get("expiry", "")):
                continue
            sym = s.get("symbol", "")
            opt = "CE" if sym.endswith("CE") else "PE" if sym.endswith("PE") else None
            if opt is None:
                continue
            try:
                strike = int(float(s.get("strike", 0)) / 100)
            except (TypeError, ValueError):
                continue
            token = s.get("token")
            if token:
                bindings.append((InstrumentKey.option(
                    name, s.get("expiry", ""), strike, opt), str(token)))
        # Rebuilt wholesale: entries dropped above (expired, wrong segment) must
        # not survive from the previous load.
        instruments.clear_broker(self.broker, segment="OPT")
        instruments.bind_many(self.broker, bindings)
        self._option_index_stamp = stamp
        self.host.log("info", f"[angel] option catalogue: {len(bindings)} contracts")

    def subscribe_keys(self, keys: set) -> None:
        """Subscribe an option set given as canonical keys.

        Translation to Angel tokens and exchange codes happens here, so the
        caller never sees either. Keys this account cannot resolve are counted
        rather than silently dropped.
        """
        grouped: dict[int, list[str]] = {}
        unresolved = 0
        for key in keys:
            token = instruments.token_for(self.broker, key)
            exch = self.host.option_exchange(key.underlying)
            if token is None or exch is None:
                unresolved += 1
                continue
            bucket = grouped.setdefault(_OPT_EXCH_TYPE.get(exch, 2), [])
            if token not in bucket:
                bucket.append(token)
        specs = [{"exchangeType": t, "tokens": toks} for t, toks in grouped.items()]
        self.subscribe_options(specs)
        if unresolved:
            self.option_sub_state += f" ({unresolved} unresolved)"

    def subscribe_options(self, token_list: list[dict]) -> None:
        """Registers option-chain tokens on the SAME socket instead of opening
        a second SmartWebSocketV2 with the same feed_token (which Angel
        rejects/kills). Unsubscribes the previous set first (index/expiry
        switch) so stale tokens stop streaming, then subscribes the new set on
        the live connection if open; otherwise it's picked up on next
        (re)connect via _subscribe_all."""
        try:
            self._subscribe_options(token_list)
        except Exception as e:
            # Record + persist before re-raising. The caller's handler only
            # published to the event hub, which is never written to a file, so
            # a failure here left `subscribeState` stuck at "none" with no
            # explanation anywhere. Re-raised so control flow is unchanged.
            self.option_sub_state = f"error: {e}"
            _file_log.error("[option-chain] subscribe path failed: %s",
                            traceback.format_exc())
            raise

    def _subscribe_options(self, token_list: list[dict]) -> None:
        prev = self.subscriptions.all("option")
        self.subscriptions.clear_kind("option")
        for i, spec in enumerate(token_list):
            # Deep-copy the token list: SmartWebSocketV2.subscribe keeps a
            # REFERENCE to it in input_request_dict and extends it on every
            # resubscribe, which would otherwise mutate the registry's copy.
            self.subscriptions.register("option", str(i), {
                "exchangeType": spec["exchangeType"],
                "tokens": list(spec["tokens"]),
            })
        # Must be the socket that is actually OPEN, not the most recently built
        # one — during a reconnect those differ, and sending on the latter
        # silently drops the subscription (the option chain then shows strikes
        # with no prices, forever).
        ws = self.ws.live_socket()
        self.option_sub_tokens = sum(len(s["tokens"]) for s in token_list)
        if ws is None:
            # Not open — the registry copy is replayed by _subscribe_all on
            # the next on_open, so nothing is lost.
            self.option_sub_state = "deferred (feed not open)"
            return
        if prev:
            try:
                ws.unsubscribe("optionchain", 3, prev)
            except Exception as e:
                self.host.log("warn", f"[option-chain] unsubscribe stale failed: {e}")
        if not token_list:
            # Distinct from "none" (never called): an empty list means a caller
            # asked us to subscribe nothing, which reads identically in the
            # chain (blank prices) but has a completely different cause.
            self.option_sub_state = "empty token list (nothing subscribed)"
            self.option_sub_ts = time.time()
            self.host.log("warn", "[option-chain] subscribe called with no tokens")
            return
        try:
            ws.subscribe("optionchain", 3, token_list)
            self.option_sub_state = "subscribed"
            self.option_sub_ts = time.time()
        except Exception as e:
            self.option_sub_state = f"failed: {e}"
            self.host.log("warn", f"[option-chain] live subscribe failed: {e}")

    # ── tick ingest (Angel wire format -> canonical) ─────────────────────
    def _on_tick(self, _ws: Any, msg: dict) -> None:
        try:
            token = msg.get("token") or msg.get("symbolToken")
            if token is None:
                return
            raw = msg.get("last_traded_price")
            if raw is None:
                raw = msg.get("ltp")
            if raw is None:
                return
            ltp = float(raw) / 100  # Angel quotes in paise

            for name, d in self.market_token_map.items():
                if str(d["token"]) == str(token):
                    prev_raw = msg.get("closed_price")
                    pct = 0.0
                    if prev_raw:
                        prev = float(prev_raw) / 100
                        pct = ((ltp - prev) / prev * 100) if prev else 0.0
                    self.host.on_index_tick(self, name, ltp, pct)
                    return

            # Not an index token — must be an option-chain tick.
            raw_vol = (msg.get("volume_trade_for_the_day")
                       or msg.get("total_traded_volume") or msg.get("volume"))
            try:
                volume = int(float(raw_vol)) if raw_vol is not None else None
            except (TypeError, ValueError):
                volume = None
            # Best bid/ask from the SNAP_QUOTE (mode 3) depth — kept INTERNAL
            # for the paper execution engine (Buy fills at ask, Sell at bid).
            # NOT surfaced in the option-chain snapshot/UI.
            bid, ask = self._best_bid_ask(msg)
            # SNAP_QUOTE (mode 3) carries open interest. Until now the chain
            # showed traded volume in its OI columns; this is the real figure,
            # and stays None when a tick omits it so the fallback still applies.
            try:
                raw_oi = msg.get("open_interest")
                oi = int(float(raw_oi)) if raw_oi is not None else None
            except (TypeError, ValueError):
                oi = None
            # Angel token -> canonical key. Every token we subscribe is bound
            # first (OptionChainAdapter._ensure_token_index / resolve_option),
            # so an unresolvable one means we are receiving a contract we never
            # asked for — reported rather than cached, since caching it under a
            # fabricated key would corrupt another contract's quote.
            key = instruments.key_for(self.broker, token)
            if key is None:
                self.host.on_unmapped_tick(self, str(token))
                return
            self.host.on_option_tick(self, key, ltp, volume, bid, ask, oi=oi)
        except Exception as e:
            self.host.log("warn", f"Market tick parse error: {e}")

    @staticmethod
    def _best_bid_ask(msg: dict) -> tuple[float | None, float | None]:
        """Extract best bid (highest buy) and best ask (lowest sell) from an
        Angel SNAP_QUOTE depth message. Prices are in paise → /100. Returns
        (None, None) when the tick carries no depth (LTP-only mode)."""
        def _best(rows: Any, want_max: bool) -> float | None:
            if not isinstance(rows, (list, tuple)):
                return None
            prices = []
            for r in rows:
                try:
                    p = float(r.get("price")) / 100
                except (TypeError, ValueError, AttributeError):
                    continue
                if p > 0:
                    prices.append(p)
            if not prices:
                return None
            return max(prices) if want_max else min(prices)

        bid = _best(msg.get("best_5_buy_data"), want_max=True)
        ask = _best(msg.get("best_5_sell_data"), want_max=False)
        return bid, ask
