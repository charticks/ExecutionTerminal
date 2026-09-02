"""Firstock live market feed (WebSocket V2).

Firstock streams plain JSON over a raw WebSocket — no vendor SDK — so this is
the first feed whose transport Charticks owns end to end. That is mostly a
simplification: there is no third-party reconnect loop to fight, which is the
problem `DhanFeed` documents at length. It does mean two things the SDK-backed
feeds get for free have to be handled explicitly here, and both are load-bearing:

* **Connecting is not the same as being authenticated.** Credentials ride in the
  query string, and the server answers with ``{"status":"success"}`` or
  ``{"status":"failed","message":"unauthenticated"}`` as the first TEXT frame.
  ``cb.on_open()`` is therefore deferred until that acknowledgement arrives —
  reporting the socket as open at TCP time would let `_subscribe_all` fire into
  a connection the server is about to close, and every subscription would be
  silently lost.

* **The server pings and expects a pong within 10 seconds.** ``websocket-client``
  answers PING frames inside ``recv_data_frame``, so ``run_forever`` handles this
  for us; the ping is counted here purely so a "connected but silent" feed can be
  told apart from a dead one in the log. A heartbeat is NOT a tick, and
  `WebSocketManager.stale` deliberately continues to measure tick silence — a
  socket held open by pings while no market data flows is exactly what
  `feed_stale()` exists to catch before an order is priced off it.

Everything else — retry policy, generation guards, staleness, error
classification — is `WebSocketManager`'s, unchanged.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

import diagnostics

from services.instruments import InstrumentKey, instruments
from services.paths import data_dir
from services.reliability.retry_manager import RetryManager
from services.reliability.transport import Transport, TransportCallbacks
from services.reliability.ws_manager import WebSocketManager

from .base import CAP_DEPTH, CAP_INDEX, CAP_OPTION, FeedHost, MarketFeed
from .firstock_client import FirstockClient, redact
from .firstock_scrip import SUPPORTED, FirstockScripMaster

WS_URL = "wss://socket.firstock.in/V2/ws"

# Firstock documents no maximum message length for the pipe-separated token
# list. A full 50-strike chain is ~202 contracts and would fit in one message,
# but discovering the real limit as a silent truncation is not a risk worth
# taking, so subscriptions are chunked and the batch count logged.
BATCH = 200

# Every price on the wire is an integer in paise. Verified against Firstock's
# own documented sample: RELIANCE (token 2885) carries
# `i_last_traded_price: 139540` for a stock trading at ₹1395.40, and
# `i_closing_price: 139100` / `i_upper_circuit_limit: 153010.0` agree. The REST
# `getQuote` response scales the same way (NIFTY 50 at `2417780`).
#
# Applied to prices ONLY. Volume, open interest and depth quantities are counts
# and are never divided — dividing a quantity by 100 is how an option chain
# starts under-reporting OI by two orders of magnitude.
PRICE_DIVISOR = 100.0

# How often the feed logs a throughput summary. Emitted from the tick path on a
# time check rather than from a timer thread — one comparison per tick is
# cheaper than a thread, and a feed with no ticks has nothing to report anyway
# (its silence is already covered by `stale`).
STATS_INTERVAL_S = 60.0

# Firstock's index list names an index by its own spelling. Anything not
# recognised here is reported rather than guessed at.
_INDEX_ALIAS = {
    "NIFTY": "NIFTY", "NIFTY 50": "NIFTY", "NIFTY50": "NIFTY",
    "BANKNIFTY": "BANKNIFTY", "NIFTY BANK": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY", "NIFTY FIN SERVICE": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY", "NIFTY MID SELECT": "MIDCPNIFTY",
    "SENSEX": "SENSEX", "BANKEX": "BANKEX",
}


class FirstockTransport(Transport):
    """One Firstock WebSocket connection.

    Knows the wire and nothing about retries — see reliability.transport for why
    that split exists.
    """

    def __init__(self, user_id: str, jkey: str, log) -> None:
        self._user_id = user_id
        self._jkey = jkey
        self._log = log
        self._app: Any = None
        self._thread: threading.Thread | None = None
        self._closing = False
        self._authenticated = False
        self._cb: TransportCallbacks | None = None
        # Counted for diagnostics only; the pong itself is the library's job.
        self.pings = 0
        self.last_ping_ts = 0.0

    # ── Transport ─────────────────────────────────────────────────────────
    def handle(self) -> Any:
        # Subscription calls are issued against the transport rather than the
        # raw socket, so batching and the authentication guard live in one place.
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
                # ping_timeout is left unset on purpose: Firstock's server is the
                # pinger, and websocket-client answers inbound PINGs inside
                # recv_data_frame. Setting our own ping_interval would add a
                # second, unnecessary keep-alive.
                app.run_forever(skip_utf8_validation=True)
            except Exception as e:
                if not self._closing:
                    cb.on_error(e, redact(f"{type(e).__name__}: {e}"))
            finally:
                # on_close drives the whole reconnect policy, so it must fire
                # even if run_forever raised before the library's own callback.
                if not self._closing:
                    try:
                        cb.on_close()
                    except Exception as exc:
                        diagnostics.exception("websocket",
                                              "Firstock on_close handler failed",
                                              exc_info=exc)

        self._thread = threading.Thread(target=run, daemon=True, name="firstock-feed")
        self._thread.start()

    def close(self) -> None:
        """Safe on an already-closed transport, and never raises — callers close
        old connections best-effort while a replacement is being built."""
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

    def _on_error(self, _app: Any, err: Any) -> None:
        if self._closing:
            return
        cb = self._cb
        if cb is not None:
            # Redacted: the connection URL carries jKey as a query parameter, so
            # any library error that quotes it back would otherwise write a live
            # session token into websocket.log — a file testers are asked to
            # email. The raw error still reaches classification.
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
            self._log("warn", f"[firstock-feed] unparseable frame: "
                              f"{redact(raw)[:200]}")
            return
        if not isinstance(msg, dict):
            return

        # Control frames carry a top-level "status"; tick frames are a map of
        # "EXCH:TOKEN" -> fields. Discriminating on the key rather than on shape
        # means a tick that happens to contain a "status" field cannot be
        # mistaken for an acknowledgement.
        status = msg.get("status")
        if status is not None:
            self._handle_control(cb, str(status), msg)
            return
        cb.on_data(msg)

    def _handle_control(self, cb: TransportCallbacks, status: str, msg: dict) -> None:
        detail = str(msg.get("message") or "")
        if status.lower() == "success":
            if self._authenticated:
                return
            self._authenticated = True
            self._log("info", f"[firstock-feed] authenticated ({detail or 'ok'})")
            cb.on_open()
            return
        # A failed handshake is a session problem, not a socket problem. It goes
        # through on_error so reliability.errors classifies it and, when the
        # jKey is dead, session recovery runs instead of an endless retry.
        self._log("error", f"[firstock-feed] authentication failed: {detail or msg}")
        cb.on_error(msg, f"Firstock websocket rejected the session: {detail or msg}")

    # ── subscriptions ─────────────────────────────────────────────────────
    def send_subscription(self, action: str, ids: list[str]) -> int:
        """Send `action` for `ids`, chunked. Returns the number of messages sent.

        Refuses before authentication: the server drops anything that arrives
        ahead of its acknowledgement, and a silently dropped subscription is a
        chain full of blank prices with nothing in the log to explain it.
        """
        app = self._app
        if app is None or not self._authenticated or not ids:
            return 0
        sent = 0
        for i in range(0, len(ids), BATCH):
            chunk = ids[i:i + BATCH]
            payload = json.dumps({"action": action, "tokens": "|".join(chunk)})
            try:
                app.send(payload)
                sent += 1
            except Exception as e:
                # Reported, not raised: on_open's subscribe is guarded by
                # WebSocketManager, but a partial failure here must still be
                # visible rather than leaving a quietly under-subscribed feed.
                self._log("warn", f"[firstock-feed] {action} batch {i // BATCH + 1} "
                                  f"failed: {e}")
                raise
        return sent


class FirstockFeed(MarketFeed):
    broker = "firstock"

    def __init__(self, account_id: str, host: FeedHost) -> None:
        super().__init__(account_id, host)
        # The account's live session, supplied by BrokerManager. Held rather
        # than copied so a re-authentication that replaces the token is visible
        # here immediately, and so exactly one object owns it.
        self.client: FirstockClient | None = None
        self.should_run = False
        self.last_transport: FirstockTransport | None = None

        self.scrip = FirstockScripMaster(data_dir(), self.host.log)
        self._scrip_day: str | None = None
        self._scrip_retry_running = False

        # Desired subscription set, canonical. Guarded because the option chain
        # replaces it from its own thread while ticks arrive on the socket's.
        self._lock = threading.RLock()
        self._index_keys: set[InstrumentKey] = set()
        self._option_keys: set[InstrumentKey] = set()
        self._sent: set[InstrumentKey] = set()

        # Spot tokens, resolved once per session from Firstock's index list.
        self._index_tokens: dict[str, str] = {}

        # Surfaced through status() so a chain full of blank prices can be told
        # apart: "never subscribed", "subscribe failed", "subscribed but silent".
        self.index_sub_state = "none"
        self.option_sub_state = "none"
        self.option_sub_ts = 0.0
        self.option_sub_tokens = 0

        # Throughput counters, reported every STATS_INTERVAL_S.
        self._ticks = 0
        self._unmapped = 0
        self._stats_ts = time.time()
        self._stats_ticks = 0

        # One-shot price-scale validation per underlying. See _check_scale.
        self._scale_checked: set[str] = set()
        self.scale_warning: str | None = None

        self.ws = WebSocketManager(
            name="firstock-feed",
            build_transport=self._build_transport,
            subscribe=self._subscribe_all,
            report_error=lambda err: self.host.report_feed_error(self, err),
            on_tick=self._on_tick,
            retry=RetryManager(max_attempts=0, base_delay=3, cap=30),
        )

    # ── lifecycle ─────────────────────────────────────────────────────────
    def apply_session(self, client: FirstockClient) -> None:
        """Session comes from the account the user already connected — this feed
        never reads the credential store itself. Re-supplied on every login so a
        reconnect presents the fresh jKey rather than the dead one.

        The client is treated as READ-ONLY here: quotes and reference data only,
        never login or logout. The order router will hold the same reference,
        and a feed teardown must not be able to end its session.
        """
        self.client = client

    def start(self) -> None:
        """Idempotent, and reconciling rather than remembering: 'started once' is
        not the same as 'currently carrying data'."""
        if self.should_run and self.ws.connected:
            return
        if self.client is None or not self.client.jkey:
            self.host.log("warn", "⚠️  Firstock feed has no session — not starting")
            return
        if self.should_run:
            self.host.log("warn", "⚠️  Firstock feed registered but not connected "
                                  "— restarting it")
        if not self._ensure_scrip():
            # A failed instrument load must NOT strand the feed for the session.
            # Kotak's did exactly that: `return` left should_run False, nothing
            # was watching, and a transient CSV download failure became a
            # permanent outage the user could only clear by reconnecting.
            self._schedule_scrip_retry()
            return
        self.should_run = True
        self.ws.start()

    def stop(self) -> None:
        self.should_run = False
        self.ws.stop()

    def reconnect(self) -> None:
        self.ws.reconnect()

    def capabilities(self) -> set[str]:
        # The V2 feed carries 5-level depth and true open interest, so this feed
        # can serve everything the option chain and paper engine need.
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
        s["optionSubscribeTokens"] = self.option_sub_tokens
        s["scripMaster"] = self.scrip.loaded_from
        s["scripInstruments"] = self.scrip.row_count
        s["scripOptions"] = self.scrip.option_count
        s["scripError"] = self.scrip.last_error
        s["ticks"] = self._ticks
        s["unmappedTicks"] = self._unmapped
        transport = self.last_transport
        s["heartbeats"] = transport.pings if transport else 0
        s["lastHeartbeatTs"] = transport.last_ping_ts if transport else 0.0
        # Non-null means decoded prices disagreed with the instrument master by
        # a power of ten — see _check_scale.
        s["scaleWarning"] = self.scale_warning
        return s

    # ── instruments ───────────────────────────────────────────────────────
    def _ensure_scrip(self) -> bool:
        """Load Firstock's master at most once a day, binding every contract into
        the registry under the "firstock" namespace."""
        today = time.strftime("%Y%m%d")
        if self._scrip_day == today and self.scrip.option_count:
            return True
        if not self.scrip.load():
            return False
        # Scoped to this broker — no other broker's namespace is touched.
        instruments.clear_broker(self.broker)
        instruments.bind_many(self.broker, self.scrip.bindings())
        # Bare tokens resolve to the same keys without displacing the composite
        # ids the socket actually uses.
        instruments.alias_many(self.broker, self.scrip.aliases())
        self._scrip_day = today
        self._resolve_index_tokens()
        return True

    def _schedule_scrip_retry(self) -> None:
        """Retry a failed instrument load with backoff, and start the socket if a
        later attempt succeeds. Bounded, because the causes that are not
        transient do not improve with repetition."""
        if self._scrip_retry_running:
            return
        self._scrip_retry_running = True

        def run() -> None:
            try:
                for attempt in range(1, 6):
                    time.sleep(min(5 * (2 ** (attempt - 1)), 60))
                    if self.ws.connected or self.client is None:
                        return
                    self.host.log("info", f"[firstock-feed] retrying instrument load "
                                          f"({attempt}/5)")
                    if self._ensure_scrip():
                        self.host.log("info", "✅ Firstock instruments loaded on retry "
                                              "— starting the feed")
                        self.should_run = True
                        self.ws.start()
                        return
                self.host.log("error", "❌ Firstock instruments could not be loaded after "
                                       "5 attempts — the feed will stay down until the "
                                       "account is reconnected")
            finally:
                self._scrip_retry_running = False

        threading.Thread(target=run, daemon=True, name="firstock-scrip-retry").start()

    def reload_instruments(self) -> bool:
        """Force a fresh instrument load. Exposed so a later order path can
        repair itself the way the Kotak one does, rather than requiring the user
        to deduce that reconnecting is the remedy."""
        self._scrip_day = None
        return self._ensure_scrip()

    def _resolve_index_tokens(self) -> None:
        """Spot tokens for every supported index, from Firstock's index list.

        The symbol files contain index DERIVATIVES only — no spot rows — so this
        is the only source. It is an authenticated REST call, which is why it
        runs here rather than in the scrip master: the master is public and must
        stay loadable without a session.

        A failure is not fatal. Options still stream; the indices simply have no
        price, and the chain cannot compute an ATM strike until they do — which
        is said out loud rather than left to be discovered.
        """
        client = self.client
        if client is None or not client.jkey:
            return
        try:
            rows = client.index_list()
        except Exception as e:
            self.host.log("warn", f"⚠️  Firstock index list unavailable ({redact(e)}) — "
                                  f"index prices will be blank and option chains "
                                  f"cannot compute an ATM strike")
            return
        if not rows:
            self.host.log("warn", "⚠️  Firstock index list returned no rows — index "
                                  "prices will be blank")
            return

        tokens: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw = str(row.get("symbol") or row.get("tradingSymbol") or "").strip().upper()
            name = _INDEX_ALIAS.get(raw)
            if name is None or name not in SUPPORTED or name in tokens:
                continue
            token = str(row.get("token") or "").strip()
            exchange = str(row.get("exchange") or "").strip().upper()
            if not token or not exchange:
                continue
            composite = f"{exchange}:{token}"
            tokens[name] = composite
            key = InstrumentKey.index(name)
            instruments.bind(self.broker, key, composite)
            instruments.alias(self.broker, key, token)

        self._index_tokens = tokens
        with self._lock:
            self._index_keys = {InstrumentKey.index(n) for n in tokens}
        missing = sorted(SUPPORTED - set(tokens))
        if missing:
            self.host.log("warn", f"⚠️  Firstock has no spot token for "
                                  f"{', '.join(missing)} — their option chain cannot "
                                  f"compute an ATM strike")
        self.host.log("info", f"[firstock-feed] index spots resolved: "
                              f"{', '.join(sorted(tokens)) or 'none'}")

    # ── socket + subscriptions ────────────────────────────────────────────
    def _build_transport(self) -> Transport:
        # Rebuilt per attempt so a post-recovery reconnect presents the jKey
        # apply_session() installed rather than re-presenting the dead one.
        client = self.client
        transport = FirstockTransport(client.user_id if client else "",
                                      client.jkey if client else "",
                                      self.host.log)
        self.last_transport = transport
        return transport

    def _desired(self) -> set[InstrumentKey]:
        with self._lock:
            return self._index_keys | self._option_keys

    def _ids_for(self, keys: set[InstrumentKey]) -> tuple[list[str], int]:
        """(subscribe ids, unresolved count) for `keys`.

        A key with no id is counted, never guessed at — an unaddressable
        contract is skipped and reported, because a wrong id would stream
        another contract's prices into this one's row.
        """
        ids: list[str] = []
        unresolved = 0
        for key in keys:
            token = instruments.token_for(self.broker, key)
            if token:
                ids.append(token)
            else:
                unresolved += 1
        return ids, unresolved

    def _subscribe_all(self, transport: Any) -> None:
        """Replay the FULL desired set on every open.

        Not a remembered delta: after a reconnect the server has forgotten what
        this connection carried, and replaying the union is the only thing that
        cannot drift. Runs inside on_open, whose exceptions WebSocketManager
        already guards.
        """
        desired = self._desired()
        ids, unresolved = self._ids_for(desired)
        if not ids:
            self.index_sub_state = "nothing to subscribe"
            self.option_sub_state = "nothing to subscribe"
            return
        batches = transport.send_subscription("subscribe", ids)
        with self._lock:
            self._sent = set(desired)
        self.index_sub_state = f"subscribed ({len(self._index_keys)} on open)"
        self.option_sub_state = ("subscribed (on open)" if not unresolved
                                 else f"subscribed on open ({unresolved} unresolved)")
        self.option_sub_ts = time.time()
        self.host.log("info", f"[firstock-feed] subscribed {len(ids)} instruments in "
                              f"{batches} message(s)"
                              + (f"; {unresolved} unresolved" if unresolved else ""))

    def subscribe_keys(self, keys: set[InstrumentKey]) -> None:
        """Replace the option subscription with `keys`.

        Canonical keys, not broker tokens — the caller (the subscription hub,
        pooling the chain, live positions and the watch list) must never have to
        know a Firstock id. The union arrives here already deduplicated, so this
        only computes the delta against what is actually on the wire.
        """
        with self._lock:
            self._option_keys = set(keys)
            self.option_sub_tokens = len(self._option_keys)
            desired = self._index_keys | self._option_keys
            add_keys = desired - self._sent
            remove_keys = self._sent - desired

        transport = self.ws.live_socket()
        if transport is None:
            # Picked up in full by _subscribe_all when the connection is built.
            self.option_sub_state = "deferred (feed not open)"
            return
        if not add_keys and not remove_keys:
            return

        add_ids, unresolved = self._ids_for(add_keys)
        remove_ids, _ = self._ids_for(remove_keys)
        try:
            # Unsubscribe first, so a chain window that has moved releases its
            # old strikes before the new ones are added — the two sets overlap
            # heavily and this keeps the live count at its true size.
            if remove_ids:
                transport.send_subscription("unsubscribe", remove_ids)
            if add_ids:
                transport.send_subscription("subscribe", add_ids)
        except Exception as e:
            # The socket is gone or refusing writes. Leave `_sent` untouched so
            # the next open replays everything, and let the manager's own
            # reconnect policy notice.
            self.option_sub_state = f"failed ({e})"
            self.host.log("warn", f"[firstock-feed] subscription update failed: {e}")
            return

        with self._lock:
            self._sent = desired
        self.option_sub_state = ("subscribed" if not unresolved
                                 else f"subscribed ({unresolved} unresolved)")
        self.option_sub_ts = time.time()
        # Firstock does not acknowledge an unsubscribe, so this line is the only
        # record that one was sent.
        self.host.log("info", f"[firstock-feed] subscription delta: +{len(add_ids)} "
                              f"-{len(remove_ids)} (live {len(desired)})")

    # ── tick ingest (Firstock wire format -> canonical) ───────────────────
    @staticmethod
    def _price(raw: Any) -> float | None:
        """A wire price as rupees, or None when absent/zero.

        Zero is treated as absent throughout: Firstock pads unfilled depth
        levels with 0, and handing the paper engine a zero bid would fill every
        sell at nothing.
        """
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        return value / PRICE_DIVISOR

    @staticmethod
    def _count(raw: Any) -> int | None:
        """A wire quantity. Never scaled — see PRICE_DIVISOR."""
        if raw is None:
            return None
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return None

    def _on_tick(self, _handle: Any, msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        now = time.time()
        for sub_id, fields in msg.items():
            if not isinstance(fields, dict):
                continue
            try:
                self._emit(str(sub_id), fields)
            except Exception as e:
                # One malformed instrument must never cost the rest of the frame.
                self.host.log("warn", f"[firstock-feed] tick parse error for "
                                      f"{sub_id}: {e}")
        self._maybe_log_stats(now)

    def _emit(self, sub_id: str, fields: dict) -> None:
        ltp = self._price(fields.get("i_last_traded_price"))
        if ltp is None:
            return

        # Resolve by the composite the master bound. `c_symbol` holds the numeric
        # token despite its name, and `c_exch_seg` the exchange — together they
        # reproduce the message's own key, which is what makes this exact rather
        # than a symbol parse.
        token = str(fields.get("c_symbol") or "").strip()
        exchange = str(fields.get("c_exch_seg") or "").strip().upper()
        composite = f"{exchange}:{token}" if exchange and token else sub_id

        key = instruments.key_for(self.broker, composite)
        if key is None and composite != sub_id:
            key = instruments.key_for(self.broker, sub_id)
        if key is None:
            self._unmapped += 1
            self.host.on_unmapped_tick(self, composite)
            return

        self._ticks += 1
        self._stats_ticks += 1

        if key.segment == "INDEX":
            close = self._price(fields.get("i_closing_price"))
            pct = ((ltp - close) / close * 100) if close else 0.0
            self._check_scale(key.underlying, ltp)
            self.host.on_index_tick(self, key.underlying, ltp, pct)
            return

        bid, ask = self._best_bid_ask(fields)
        self.host.on_option_tick(
            self, key, ltp,
            self._count(fields.get("i_volume_traded_today")),
            bid, ask,
            oi=self._count(fields.get("i_total_open_interest")),
        )

    @classmethod
    def _best_bid_ask(cls, fields: dict) -> tuple[float | None, float | None]:
        """Top of book from the 5-level depth arrays.

        Levels are taken in order and the first genuinely positive price wins,
        because Firstock pads absent levels with a zero price. When none is
        positive, (None, None) makes the paper engine fall back to its synthetic
        spread exactly as it does for any other feed with no depth — and the
        router carries the previous tick's values forward, so a trade-only
        packet never blanks a book we already have.
        """
        bid = ask = None
        buys = fields.get("best_buy")
        sells = fields.get("best_sell")
        if isinstance(buys, (list, tuple)):
            for level in buys:
                if isinstance(level, dict):
                    bid = cls._price(level.get("price"))
                    if bid is not None:
                        break
        if isinstance(sells, (list, tuple)):
            for level in sells:
                if isinstance(level, dict):
                    ask = cls._price(level.get("price"))
                    if ask is not None:
                        break
        return bid, ask

    def _check_scale(self, underlying: str, ltp: float) -> None:
        """Validate the decoded price scale against the instrument master, once
        per underlying.

        PRICE_DIVISOR is documented and was verified against Firstock's own
        sample, but a broker changing its wire scale silently is precisely the
        failure that cost a Kotak trading session — and an index spot is the one
        place it is cheaply detectable, because the option strikes for that same
        underlying bracket it. An index quoting 2,417,780 beside strikes near
        24,000 is obvious in one comparison and invisible if nobody looks.

        Detects and reports; it deliberately does NOT tear the feed down. The
        band is wide enough that a correct value can never trip it, but a
        heuristic that can kill a working feed is a worse trade than one that
        shouts.
        """
        if underlying in self._scale_checked:
            return
        expiries = instruments.expiries(underlying)
        if not expiries:
            return  # nothing to validate against yet; try again on the next tick
        strikes = instruments.strikes(underlying, expiries[0])
        if not strikes:
            return
        self._scale_checked.add(underlying)
        low, high = strikes[0] * 0.5, strikes[-1] * 2.0
        if low <= ltp <= high:
            return
        self.scale_warning = (
            f"{underlying} spot decoded as {ltp:,.2f} but its strikes span "
            f"{strikes[0]:,}–{strikes[-1]:,} — the wire price scale "
            f"(÷{PRICE_DIVISOR:g}) may be wrong")
        self.host.log("error", f"❌ Firstock price scale looks wrong: {self.scale_warning}")
        diagnostics.emit("websocket", "error", "Firstock price scale mismatch",
                         publish=True, feed="firstock-feed", underlying=underlying,
                         decodedLtp=round(ltp, 2), strikeLow=strikes[0],
                         strikeHigh=strikes[-1], divisor=PRICE_DIVISOR)

    def _maybe_log_stats(self, now: float) -> None:
        """Periodic throughput summary — the line that makes a quiet feed
        diagnosable without attaching a debugger."""
        if now - self._stats_ts < STATS_INTERVAL_S:
            return
        elapsed = now - self._stats_ts
        rate = self._stats_ticks / elapsed if elapsed > 0 else 0.0
        transport = self.last_transport
        with self._lock:
            live = len(self._sent)
        diagnostics.emit(
            "websocket", "info", "Firstock feed throughput", feed="firstock-feed",
            ticksPerSec=round(rate, 1), ticks=self._ticks, unmapped=self._unmapped,
            subscribed=live, heartbeats=transport.pings if transport else 0,
            connected=self.ws.connected)
        self._stats_ts = now
        self._stats_ticks = 0
