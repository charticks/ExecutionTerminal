import threading
import time
import datetime as dt

from SmartApi.smartWebSocketV2 import SmartWebSocketV2


class OptionChainEngine:
    """
    Single WebSocket connection for ALL subscribed tokens.
    Maintains tick_store in memory — no REST calls after startup.
    Automatically reconnects and restores subscription.

    tick_store[token] = {
        "ltp"       : float,
        "volume"    : int,      # cumulative contracts today
        "timestamp" : datetime,
    }
    """

    def __init__(self, jwt_token, api_key, client_code, feed_token):
        self.jwt_token   = jwt_token
        self.api_key     = api_key
        self.client_code = client_code
        self.feed_token  = feed_token

        self.tick_store     = {}          # {token: {ltp, volume, timestamp}}
        self.lock           = threading.Lock()
        self.sws            = None
        self.connected      = False
        self.running        = False
        self._token_list    = []          # [{exchangeType, tokens:[...]}]
        self._reconnect_lock  = threading.Lock()   # prevents concurrent reconnects
        self._reconnect_attempt = 0               # tracks consecutive failures for backoff
        self._reconnect_max_delay = 60            # seconds — cap on exponential backoff
        self._last_connect_attempt_time = 0       # epoch when _connect() was last called
        self.last_tick_time = time.time()
        self.app_ref        = None      # set by caller; used for status callbacks

        # Canary token — a non-expiring index token subscribed alongside options.
        # Its ticks prove the WebSocket connection itself is alive even when
        # option tokens go silent on expiry day.  Does NOT feed CandleEngine.
        self._canary_token    = None
        self.last_canary_tick = time.time()

        # Callbacks registered by CandleEngine
        self.on_tick_cb   = None        # called on every tick

    # ----------------------------------------------------------
    def subscribe(self, token_list, canary_token=None, canary_exchange_type=1):
        """
        token_list: [{exchangeType: int, tokens: [str, ...]}, ...]
        canary_token: optional index token (e.g. '26000') that never expires;
                      its ticks prove the connection is alive independent of option tokens.
        Call once after login. Engine handles all reconnects internally.
        """
        self._token_list   = list(token_list)
        self._canary_token = canary_token
        has_tokens = any(len(tl.get("tokens", [])) > 0 for tl in token_list)
        if not has_tokens:
            print("OCE: empty token list — WebSocket not started (add tokens first)")
            return

        if canary_token:
            already = any(canary_token in tl.get("tokens", []) for tl in self._token_list)
            if not already:
                self._token_list.append({"exchangeType": canary_exchange_type,
                                         "tokens": [canary_token]})

        self._connect()

    def _connect(self):
        # Non-blocking acquire: if another thread is already reconnecting, skip.
        if not self._reconnect_lock.acquire(blocking=False):
            return
        try:
            # ── Generation counter ──────────────────────────────────────────────
            # Each _connect() call gets a unique generation number.  Every
            # callback captures its own `gen` via closure.  If the callback fires
            # from a *previous* (now-closed) sws object, gen != self._generation
            # and the callback returns immediately — preventing the stale on_close
            # from corrupting _reconnect_attempt or scheduling a spurious retry.
            gen = getattr(self, '_generation', 0) + 1
            self._generation = gen
            self._last_connect_attempt_time = time.time()   # track for watchdog cooldown

            if self.sws:
                _old_sws = self.sws
                self.sws = None
                def _close_old():
                    try:
                        _old_sws.close_connection()
                    except Exception:
                        pass
                threading.Thread(target=_close_old, daemon=True, name="oce-close").start()
            else:
                self.sws = None

            self.sws = SmartWebSocketV2(
                self.jwt_token,
                self.api_key,
                self.client_code,
                self.feed_token,
                max_retry_attempt=0,   # library calls on_close immediately; our on_close handles retries
                retry_strategy=0,
                retry_delay=5,
            )

            def on_open(ws):
                if gen != self._generation:   # stale — old sws fired, ignore
                    return
                print("OptionChainEngine WebSocket Connected ✅")
                self._reconnect_attempt = 0   # reset backoff on successful connect
                self.connected      = True
                self.last_tick_time = time.time()
                # Subscribe ALL tokens in one call
                self.sws.subscribe("optionchain", 3, self._token_list)
                # Immediately clear the api_ok=False flag set by the watchdog freeze detector
                if self.app_ref and hasattr(self.app_ref, "_set_api_status"):
                    self.app_ref._set_api_status(True)

            def on_data(ws, msg):
                if gen != self._generation:   # stale — old sws fired, ignore
                    return
                self._on_data(msg)

            def on_close(ws):
                if gen != self._generation:   # stale — old sws fired, ignore
                    # This is the key fix: when _connect() force-closes the OLD
                    # socket via _close_old(), SmartWebSocketV2 fires the OLD
                    # on_close callback.  Without this guard that stale callback
                    # would increment _reconnect_attempt and schedule a retry
                    # thread that would kill the newly-established connection
                    # 5 seconds later, creating an infinite reconnect loop.
                    return
                self.connected = False
                if not self.running:
                    print("OptionChainEngine WebSocket Closed (stopped — no reconnect)")
                    return
                delay = min(5 * (2 ** self._reconnect_attempt), self._reconnect_max_delay)
                self._reconnect_attempt += 1
                print(f"OptionChainEngine WebSocket Closed — reconnecting in {delay}s "
                      f"(attempt {self._reconnect_attempt})")
                def _retry():
                    time.sleep(delay)
                    # Only skip if already successfully reconnected (connected=True).
                    # Do NOT guard on generation here — a watchdog reconnect that ran
                    # while we were sleeping bumped the generation but may have also
                    # failed; we should still try unless the socket is actually live.
                    if self.running and not self.connected:
                        self._connect()
                threading.Thread(target=_retry, daemon=True, name="oce-reconnect").start()

            def on_error(ws, err):
                if gen != self._generation:   # stale — old sws fired, ignore
                    return
                err_s = str(err).lower()
                if "already closed" in err_s or "connection closed" in err_s:
                    return   # transient race between on_open and subscribe — on_close handles retry
                print("OptionChainEngine WebSocket Error:", err)
                # Do NOT set self.running=False here — on_close handles reconnect
                # with exponential backoff. Setting running=False would cause permanent death.

            self.sws.on_open  = on_open
            self.sws.on_data  = on_data
            self.sws.on_close = on_close
            self.sws.on_error = on_error

            self.running = True
            threading.Thread(target=self.sws.connect, daemon=True).start()

        finally:
            self._reconnect_lock.release()

    def _on_data(self, msg):
        """Process every incoming tick — store in tick_store."""
        try:
            token = msg.get("token") or msg.get("symbolToken")
            if not token:
                return

            # Canary token: proves connection is alive but must NOT feed the
            # trading pipeline, and must NOT update last_tick_time — the watchdog
            # uses last_tick_time to detect frozen option-token feeds.
            if token == self._canary_token:
                self.last_canary_tick = time.time()
                return

            # Only option-token ticks advance the watchdog heartbeat
            self.last_tick_time = time.time()

            # LTP — explicit None check (Angel sends 0 on first ticks)
            raw_ltp = msg.get("last_traded_price")
            if raw_ltp is None:
                raw_ltp = msg.get("ltp")
            if raw_ltp is None:
                return
            try:
                ltp = float(raw_ltp) / 100
            except (TypeError, ValueError):
                return

            # Cumulative volume (contracts)
            raw_vol = msg.get("volume_trade_for_the_day") \
                   or msg.get("total_traded_volume") \
                   or msg.get("volume")
            try:
                cum_vol = int(float(raw_vol)) if raw_vol is not None else None
            except (TypeError, ValueError):
                cum_vol = None

            now = dt.datetime.now()

            with self.lock:
                self.tick_store[token] = {
                    "ltp"       : ltp,
                    "volume"    : cum_vol,
                    "timestamp" : now,
                }

            # Fire callback to CandleEngine (non-blocking)
            if self.on_tick_cb:
                self.on_tick_cb(token, ltp, cum_vol, now)

            # REAL-TIME ENTRY CALL — only fire from the currently active pipeline OCE
            if (hasattr(self, "app_ref") and self.app_ref is not None
                    and getattr(self.app_ref, "oce", None) is self):
                self.app_ref.process_tick_entry(token, ltp)

        except Exception as e:
            print("OptionChainEngine tick error:", e)

    @property
    def canary_alive(self):
        """True if the canary index token ticked within the last 30 seconds."""
        if not self._canary_token:
            return False
        return time.time() - self.last_canary_tick < 30

    def get_ltp(self, token):
        with self.lock:
            entry = self.tick_store.get(token)
            return entry["ltp"] if entry else None

    def get_tick(self, token):
        with self.lock:
            return self.tick_store.get(token, {}).copy()

    def stop(self):
        self.running = False
        try:
            if self.sws:
                self.sws.close_connection()
        except Exception:
            pass
