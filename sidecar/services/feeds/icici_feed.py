"""ICICI Direct (Breeze) live market feed.

Rides its OWN BreezeConnect client, built from the credentials the account
authenticated with (`apply_session`) — the Dhan model, not Kotak's shared
session. Breeze tolerates multiple generate_session calls against one day
token, and a separate client means feed teardown can never disturb an
in-flight order on the trading session.

What ICICI shares with Kotak is the needs_reauth latch, with one difference in
routing: the session token comes from a daily BROWSER login and can never be
renewed programmatically, so BrokerManager.reauthenticate short-circuits with
``permanent`` and SessionManager parks the account at session_expired with an
actionable message. This feed therefore reports errors THROUGH the host (→
SessionManager) and then latches — unlike KotakFeed, which must bypass
SessionManager because auto-reauth would swap a session shared with the order
engine.

The SDK's market socket is `breeze.sio_rate_refresh_handler.sio`, a default
``socketio.Client()`` — whose reconnection is ON by default and is disabled
here after connect, because WebSocketManager owns all retry policy. Tick
fields were verified against breeze-connect 1.0.69 source: quote ticks carry
``symbol`` ("4.1!43492"), ``last``, ``bPrice``/``sPrice`` (real top-of-book),
``ttq``, ``close``, ``change``, and — on the FO feed — ``OI``. Because quotes
already carry top-of-book, no depth (Y=2) subscription is needed at all.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import threading
import time
from typing import Any

from services.instruments import InstrumentKey, instruments
from services.paths import data_dir
from services.reliability.retry_manager import RetryManager
from services.reliability.transport import Transport, TransportCallbacks
from services.reliability.ws_manager import WebSocketManager

from .base import CAP_DEPTH, CAP_INDEX, CAP_OPTION, FeedHost, MarketFeed
from .icici_scrip import ICICIScripMaster

# Quotes (Y=1) carry bPrice/sPrice + OI already; depth (Y=2) would double the
# subscription count for five levels nothing consumes (the paper engine uses
# top-of-book only). Kept as a constant so flipping it is a one-line change.
SUBSCRIBE_DEPTH = False

# Serialises the sys.path/sys.modules shuffle in import_breeze().
_IMPORT_LOCK = threading.Lock()


def import_breeze():
    """Import BreezeConnect despite the SDK's bare ``import config``.

    breeze_connect/breeze_connect.py does a top-level ``import config`` — an
    ABSOLUTE import of a bare module name, not ``from . import config``. Which
    ``config`` that resolves to depends on whatever happens to be on sys.path,
    so the SDK is one stray same-named module away from dying on
    ``AttributeError: SECURITY_MASTER_URL``. (It used to be exactly that: the
    project root carried a config.py of broker credentials and was on sys.path
    for the legacy Tkinter app. Both are gone, but the SDK is still fragile.)

    So its own config is pre-seeded into sys.modules under the bare name for the
    duration of the import, and both sys.modules and sys.path are restored
    afterwards — the SDK inserts its directory into sys.path itself and would
    otherwise leave it shadowing ``config`` for the rest of the process.
    """
    with _IMPORT_LOCK:
        cached = sys.modules.get("breeze_connect")
        if cached is not None and getattr(cached, "BreezeConnect", None) is not None:
            return cached.BreezeConnect

        spec = importlib.util.find_spec("breeze_connect")
        if spec is None or not spec.origin:
            raise ImportError("breeze-connect is not installed. "
                              "Run: pip install breeze-connect")
        pkg_dir = os.path.dirname(spec.origin)

        # Pre-seed the SDK's own config under the bare name it imports, so its
        # `import config` is satisfied from sys.modules and never searches the
        # path. (Putting pkg_dir on sys.path instead would shadow the PACKAGE
        # with its same-named inner module, binding sys.modules["breeze_connect"]
        # to the wrong object.)
        cfg_path = os.path.join(pkg_dir, "config.py")
        cfg_spec = importlib.util.spec_from_file_location("config", cfg_path)
        if cfg_spec is None or cfg_spec.loader is None:
            raise ImportError(f"breeze-connect config.py not found at {cfg_path}")
        sdk_config = importlib.util.module_from_spec(cfg_spec)
        cfg_spec.loader.exec_module(sdk_config)

        saved_config = sys.modules.get("config")
        # The SDK also does its own `sys.path.insert(1, dirs)` at import time,
        # which would leave its directory shadowing Charticks' config.py for
        # the rest of the process — snapshot the whole list and put it back.
        saved_path = list(sys.path)
        sys.modules["config"] = sdk_config
        try:
            module = importlib.import_module("breeze_connect")
            return module.BreezeConnect
        finally:
            sys.path[:] = saved_path
            # Put Charticks' config back (or drop the SDK's), so a later bare
            # `import config` anywhere else still finds the project's file.
            if saved_config is not None:
                sys.modules["config"] = saved_config
            else:
                sys.modules.pop("config", None)


class BreezeTransport(Transport):
    """One Breeze session + market socket behind the generic Transport contract."""

    def __init__(self, api_key: str, api_secret: str, session_token: str,
                 tokens: list[str], log) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._session_token = session_token
        self._tokens = list(tokens)          # full "4.1!TOKEN" stock_tokens
        self._log = log
        self._breeze: Any = None
        self._live = True

    def handle(self) -> Any:
        # Subscription changes go through the transport (it owns the guard),
        # never the raw SDK object.
        return self

    def open(self, cb: TransportCallbacks) -> None:
        # generate_session + ws_connect are blocking REST/socket calls — run
        # them off the caller's thread, reporting through the callbacks.
        threading.Thread(target=self._open, args=(cb,), daemon=True,
                         name="icici-ws-connect").start()

    def _open(self, cb: TransportCallbacks) -> None:
        try:
            BreezeConnect = import_breeze()

            breeze = BreezeConnect(api_key=self._api_key)
            # Re-run per attempt (Angel rebuilds its socket the same way) so a
            # reconnect after re-login presents the fresh token.
            breeze.generate_session(api_secret=self._api_secret,
                                    session_token=self._session_token)
            self._breeze = breeze

            def on_ticks(tick: Any) -> None:
                if self._live:
                    cb.on_data(tick)

            breeze.on_ticks = on_ticks
            breeze.ws_connect()

            handler = getattr(breeze, "sio_rate_refresh_handler", None)
            sio = getattr(handler, "sio", None)
            if sio is None:
                raise ConnectionError("Breeze ws_connect did not establish a market socket")
            if getattr(handler, "authentication", True) is False:
                # The socket-level auth result — surfaces the daily-token death
                # even when generate_session's REST call happened to pass.
                raise PermissionError("Could not authenticate credentials. Please check token and keys")

            # OUR WebSocketManager owns reconnect policy. socketio.Client()
            # defaults to reconnection=True; left on, its retry loop races
            # ours and the feed ends up permanently half-connected.
            try:
                sio.reconnection = False
            except Exception as e:
                self._log("warn", f"[icici] could not disable SDK reconnection: {e}")

            # The SDK registers no 'disconnect' handler of its own (verified
            # against 1.0.69 — SocketEventBreeze.on_disconnect is only invoked
            # by ws_disconnect, the namespace is never registered), so this
            # does not displace anything.
            def on_disconnect() -> None:
                if self._live:
                    cb.on_close()

            try:
                sio.on("disconnect", on_disconnect)
            except Exception as e:
                self._log("warn", f"[icici] could not hook disconnect: {e}")

            if not self._live:
                return  # closed while we were connecting
            cb.on_open()
            self._subscribe_initial(cb)
        except Exception as e:
            if self._live:
                cb.on_error(e, f"icici feed: {e}")
                cb.on_close()

    def _subscribe_initial(self, cb: TransportCallbacks) -> None:
        for token in self._tokens:
            if not self._live:
                return
            try:
                self._breeze.subscribe_feeds(
                    stock_token=token,
                    get_exchange_quotes=True, get_market_depth=SUBSCRIBE_DEPTH)
            except Exception as e:
                # One bad token must not sink the rest of the window.
                self._log("warn", f"[icici] subscribe {token} failed: {e}")

    def update(self, add: list[str], remove: list[str]) -> None:
        """Apply a subscription delta on the live connection."""
        breeze = self._breeze
        if breeze is None:
            return
        for token in remove:
            try:
                breeze.unsubscribe_feeds(
                    stock_token=token,
                    get_exchange_quotes=True, get_market_depth=SUBSCRIBE_DEPTH)
            except Exception as e:
                self._log("warn", f"[icici] unsubscribe {token} failed: {e}")
        for token in add:
            try:
                breeze.subscribe_feeds(
                    stock_token=token,
                    get_exchange_quotes=True, get_market_depth=SUBSCRIBE_DEPTH)
            except Exception as e:
                self._log("warn", f"[icici] subscribe {token} failed: {e}")

    def close(self) -> None:
        """Tear down the market socket. Never touches the trading session —
        this client is the feed's own, and ws_disconnect only closes sockets."""
        self._live = False
        breeze = self._breeze
        if breeze is not None:
            try:
                breeze.ws_disconnect()
            except Exception:
                pass


class ICICIFeed(MarketFeed):
    broker = "icici"

    def __init__(self, account_id: str, host: FeedHost) -> None:
        super().__init__(account_id, host)
        self.api_key: str | None = None
        self.api_secret: str | None = None
        self.session_token: str | None = None
        self.should_run = False
        self.last_transport: BreezeTransport | None = None
        # Latched when the shared day-token dies. Cleared only by a user
        # re-login (apply_session) — never by this feed re-authenticating.
        self.needs_reauth = False

        self.scrip = ICICIScripMaster(data_dir(), self.host.log)
        self._scrip_day: str | None = None

        self._index_keys: set[InstrumentKey] = set()
        self._option_keys: set[InstrumentKey] = set()
        self._sent: set[InstrumentKey] = set()

        self.index_sub_state = "none"
        self.option_sub_state = "none"
        self.option_sub_ts = 0.0
        self.option_sub_tokens = 0

        self.ws = WebSocketManager(
            name="icici-feed",
            build_transport=self._build_transport,
            subscribe=self._on_subscribed,
            report_error=self._report_error,
            on_tick=self._on_tick,
            retry=RetryManager(max_attempts=0, base_delay=3, cap=15),
        )

    # ── lifecycle ─────────────────────────────────────────────────────────
    def apply_session(self, api_key: str, api_secret: str, session_token: str) -> None:
        """Install the credentials the account just authenticated with. Called
        on every (re)connect, so a fresh daily login clears the latch."""
        self.api_key = api_key
        self.api_secret = api_secret
        self.session_token = session_token
        self.needs_reauth = False

    def start(self) -> None:
        if self.should_run and self.ws.connected:
            return
        if not (self.api_key and self.api_secret and self.session_token):
            self.host.log("warn", "⚠️  ICICI feed has no session — not starting")
            return
        if self.needs_reauth:
            self.host.log("warn", "⚠️  ICICI feed needs a fresh daily login — "
                                  "use Login with ICICI on the Brokers screen")
            return
        if not self._ensure_scrip():
            return
        self.should_run = True
        self.ws.start()

    def stop(self) -> None:
        self.should_run = False
        self.ws.stop()

    def reconnect(self) -> None:
        """WebSocket only — a dead day-token can't be revived by reconnecting."""
        if self.needs_reauth:
            return
        self.ws.reconnect()

    def capabilities(self) -> set[str]:
        # bPrice/sPrice on every quote tick is real top-of-book, which is all
        # the paper engine consumes — so DEPTH is honest without Y=2.
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
        s["needsReauth"] = self.needs_reauth
        return s

    def _report_error(self, err: Any) -> str:
        """Route through SessionManager (whose icici reauth short-circuits as
        `permanent`, parking health at session_expired with guidance), then
        latch so the socket never retries against a dead day-token."""
        classification = self.host.report_feed_error(self, err)
        if classification == "session_expired":
            self.needs_reauth = True
            self.should_run = False
            self.ws.should_run = False
            self.host.log("error", "❌ ICICI market feed: session key rejected — "
                                   "log in again from the Brokers screen "
                                   "(ICICI's session expires daily)")
        return classification

    # ── instruments ───────────────────────────────────────────────────────
    def _ensure_scrip(self) -> bool:
        today = time.strftime("%Y%m%d")
        if self._scrip_day == today and self.scrip.row_count:
            return True
        if not self.scrip.load():
            return False
        instruments.clear_broker(self.broker)
        instruments.bind_many(self.broker, self.scrip.bindings())
        instruments.alias_many(self.broker, self.scrip.aliases())
        self._scrip_day = today
        self._index_keys = {k for k in self.scrip.prefixes if k.segment == "INDEX"}
        return True

    def _stock_token(self, key: InstrumentKey) -> str | None:
        token = instruments.token_for(self.broker, key)
        prefix = self.scrip.prefixes.get(key)
        if token is None or prefix is None:
            return None
        return f"{prefix}!{token}"

    def _desired(self) -> set[InstrumentKey]:
        return self._index_keys | self._option_keys

    def _build_transport(self) -> Transport:
        tokens = [t for t in (self._stock_token(k) for k in self._desired()) if t]
        transport = BreezeTransport(self.api_key or "", self.api_secret or "",
                                    self.session_token or "", tokens, self.host.log)
        self.last_transport = transport
        return transport

    def _on_subscribed(self, _handle: Any) -> None:
        """The transport sends the initial subscriptions itself right after
        on_open (they need its thread), so this only records what is live."""
        self._sent = set(self._desired())
        self.index_sub_state = f"subscribed ({len(self._index_keys)} on open)"
        if self._option_keys:
            self.option_sub_state = "subscribed (on open)"
            self.option_sub_ts = time.time()

    def subscribe_keys(self, keys: set[InstrumentKey]) -> None:
        """Replace the option subscription with `keys` (canonical)."""
        self._option_keys = set(keys)
        self.option_sub_tokens = len(self._option_keys)
        desired = self._desired()
        transport = self.last_transport
        if not self.ws.connected or transport is None:
            self.option_sub_state = "deferred (feed not open)"
            return
        add = [t for t in (self._stock_token(k) for k in desired - self._sent) if t]
        remove = [t for t in (self._stock_token(k) for k in self._sent - desired) if t]
        unresolved = sum(1 for k in desired - self._sent if self._stock_token(k) is None)
        if not add and not remove:
            return
        try:
            transport.update(add, remove)
        except Exception as e:
            self.option_sub_state = f"failed: {e}"
            self.host.log("warn", f"[icici] option subscribe failed: {e}")
            return
        self._sent = desired
        self.option_sub_state = ("subscribed" if not unresolved
                                 else f"subscribed ({unresolved} unresolved)")
        self.option_sub_ts = time.time()

    # ── tick ingest (Breeze wire format -> canonical) ─────────────────────
    @staticmethod
    def _f(v: Any) -> float | None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f > 0 else None

    def _on_tick(self, _handle: Any, tick: Any) -> None:
        try:
            if not isinstance(tick, dict):
                return
            symbol = tick.get("symbol")
            if not symbol:
                return  # order/strategy/OHLC payloads — not market quotes
            # The full "4.1!TOKEN" string is aliased at bind time, so the raw
            # symbol resolves directly; the bare tail is the primary binding.
            key = instruments.key_for(self.broker, str(symbol))
            if key is None and "!" in str(symbol):
                key = instruments.key_for(self.broker, str(symbol).split("!", 1)[1])
            if key is None:
                self.host.on_unmapped_tick(self, str(symbol))
                return

            ltp = self._f(tick.get("last") or tick.get("ltp"))
            if ltp is None:
                return

            if key.segment == "INDEX":
                # `change` is the day's % change where present; else derive
                # from close (previous close on the cash feed).
                pct = None
                raw_change = tick.get("change")
                try:
                    pct = float(raw_change) if raw_change is not None else None
                except (TypeError, ValueError):
                    pct = None
                if pct is None:
                    close = self._f(tick.get("close"))
                    pct = ((ltp - close) / close * 100) if close else 0.0
                self.host.on_index_tick(self, key.underlying, ltp, pct)
                return

            raw_vol = tick.get("ttq")
            try:
                volume = int(float(raw_vol)) if raw_vol is not None else None
            except (TypeError, ValueError):
                volume = None
            raw_oi = tick.get("OI")
            try:
                oi = int(float(raw_oi)) if raw_oi is not None else None
            except (TypeError, ValueError):
                oi = None
            # Real top-of-book from the quote tick itself. Zero/absent →
            # None → the paper engine's synthetic-spread fallback.
            bid = self._f(tick.get("bPrice"))
            ask = self._f(tick.get("sPrice"))
            self.host.on_option_tick(self, key, ltp, volume, bid, ask, oi=oi)
        except Exception as e:
            self.host.log("warn", f"ICICI tick parse error: {e}")
