import threading
import datetime as dt
import time


class KotakDataEngine:
    """
    Wraps Kotak Neo WebSocket streaming.
    Fires on_tick_cb(angel_token, ltp, cum_vol, now) exactly like
    OptionChainEngine so the downstream CandleEngine is broker-agnostic.

    NeoAPI subscription format:
        instrument_tokens = [{"instrument_token": pTrdSymbol, "exchange_segment": "nse_fo"}, ...]
    WS tick format:
        {"type": "stock_feed", "data": [{"tk": pTrdSymbol, "ltp": "xxx", "v": "xxx"}, ...]}
    So kotak_to_angel must be keyed by pTrdSymbol (trading symbol string).
    """

    def __init__(self, kotak_client, kotak_to_angel: dict,
                 exchange_segment: str = "nse_fo"):
        """
        kotak_client      : logged-in NeoAPI instance (self.kotak)
        kotak_to_angel    : {pTrdSymbol_str: angel_token_str} mapping
        exchange_segment  : "nse_fo" / "bse_fo" / "mcx_fo"
        """
        self.client           = kotak_client
        self.kotak_to_angel   = kotak_to_angel
        self.exchange_segment = exchange_segment
        self.on_tick_cb       = None      # fn(angel_token, ltp, cum_vol, now)
        self.running          = False
        self._ltp_cache       = {}        # {angel_token: ltp} for get_ltp()
        self.tick_store       = {}        # {angel_token: {"ltp", "volume", "timestamp"}}
        self.last_tick_time   = time.time()  # watchdog compatibility
        self._last_connect_attempt_time = 0  # mirrors OptionChainEngine; enables watchdog cooldown
        self._kotak_symbols   = []        # saved for reconnect

    # ----------------------------------------------------------
    def subscribe(self, kotak_symbols: list):
        """Subscribe to Kotak Neo live feed.  kotak_symbols is a list of pTrdSymbol strings."""
        self._kotak_symbols = kotak_symbols
        self._last_connect_attempt_time = time.time()  # start cooldown clock
        self.client.on_message = self._on_message
        self.client.on_error   = self._on_error
        self.client.on_close   = self._on_close
        self.client.on_open    = self._on_open
        self.running = True
        print(f"KotakDataEngine: subscribing {len(kotak_symbols)} symbols "
              f"exch={self.exchange_segment} → {kotak_symbols[:3]}{'...' if len(kotak_symbols)>3 else ''}")
        threading.Thread(
            target=self._do_subscribe,
            args=(kotak_symbols,),
            daemon=True
        ).start()

    def _do_subscribe(self, kotak_symbols):
        """Build NeoAPI instrument_tokens dicts and call client.subscribe()."""
        try:
            instruments = [
                {"instrument_token": sym, "exchange_segment": self.exchange_segment}
                for sym in kotak_symbols
            ]
            self.client.subscribe(
                instrument_tokens=instruments,
                isIndex=False,
                isDepth=False,
            )
        except Exception as e:
            print("KotakDataEngine._do_subscribe error:", e)

    def resubscribe(self, new_kotak_to_angel: dict):
        """Swap token map and re-subscribe the *existing* WS without full teardown.
        Avoids the hang caused by calling client.subscribe() from multiple threads."""
        self.kotak_to_angel = new_kotak_to_angel
        symbols = list(new_kotak_to_angel.keys())
        self._kotak_symbols = symbols
        try:
            self.client.un_subscribe_all()
        except Exception:
            pass
        # Give the old run_forever() loop time to fully exit before we restart.
        time.sleep(0.5)
        # Re-register callbacks: un_subscribe_all() may have cleared them in the SDK.
        self.client.on_message = self._on_message
        self.client.on_error   = self._on_error
        self.client.on_close   = self._on_close
        self.client.on_open    = self._on_open
        threading.Thread(target=self._do_subscribe, args=(symbols,),
                         daemon=True, name="kotak-ws-resub").start()

    def stop(self):
        self.running = False
        try:
            self.client.un_subscribe_all()
        except Exception:
            pass

    # ----------------------------------------------------------
    def _on_open(self, msg=None):
        # neo_api_client calls on_open("The Session has been Opened!") — accept
        # the optional message string so the signature matches what the library sends.
        self.last_tick_time = time.time()
        print("KotakDataEngine: WebSocket connected")
        _app = getattr(self, "app_ref", None)
        if _app:
            _app.root.after(0, lambda: _app.log("✅ Kotak WS connected — ticks starting"))

    def _on_error(self, err=None):
        print("KotakDataEngine WebSocket error:", err)
        _app = getattr(self, "app_ref", None)
        if _app:
            _app.root.after(0, lambda e=str(err): _app.log(f"❌ Kotak WS error: {e}"))

    def _on_close(self, msg=None):
        # neo_api_client calls on_close("The Session has been Closed!") — accept
        # the optional message string so the signature matches what the library sends.
        print("KotakDataEngine: WebSocket closed")
        _app = getattr(self, "app_ref", None)
        if _app:
            _app.root.after(0, lambda: _app.log("⚠️ Kotak WS closed"))

    # ----------------------------------------------------------
    def _on_message(self, message):
        """
        NeoAPI delivers: {"type": "stock_feed", "data": [tick_dict, ...]}
        Each tick_dict has: {"tk": pTrdSymbol, "ltp": "xxx", "v": "xxx", ...}
        """
        try:
            if isinstance(message, (str, bytes)):
                import json
                try:
                    message = json.loads(message)
                except Exception:
                    return
            if not isinstance(message, dict):
                return

            # Log first few raw messages
            _cnt = getattr(self, '_dbg_msg_count', 0)
            if _cnt < 3:
                self._dbg_msg_count = _cnt + 1
                print(f"[Kotak msg #{_cnt+1}] type={message.get('type')} "
                      f"data_type={type(message.get('data')).__name__} "
                      f"sample={str(message)[:300]}")

            data = message.get("data", message)

            # data is a list of tick dicts
            if isinstance(data, list):
                for tick in data:
                    if isinstance(tick, dict):
                        self._process_tick(tick)
            elif isinstance(data, dict):
                self._process_tick(data)
            # string / other types are ack/status messages — ignore

        except Exception as e:
            print("KotakDataEngine._on_message error:", e)

    def _process_tick(self, tick: dict):
        """Process a single tick dict from the NeoAPI data list."""
        try:
            kotak_tok = str(tick.get("tk") or tick.get("token") or "")
            if not kotak_tok:
                return

            angel_tok = self.kotak_to_angel.get(kotak_tok)

            # Log first 5 ticks so we can verify the tk field format in the UI.
            _cnt = getattr(self, "_app_tick_count", 0)
            if _cnt < 5:
                self._app_tick_count = _cnt + 1
                _app = getattr(self, "app_ref", None)
                if _app:
                    _ltp_str = str(tick.get("ltp") or tick.get("last_traded_price") or "?")
                    _app.root.after(0, lambda k=kotak_tok, a=str(angel_tok),
                                        l=_ltp_str, n=_cnt + 1:
                                    _app.log(f"📡 Kotak tick#{n}: tk={k} → angel={a} ltp={l}"))

            if not angel_tok:
                return

            raw_ltp = (tick.get("ltp") or tick.get("last_traded_price")
                       or tick.get("LTP") or 0)
            try:
                ltp = float(raw_ltp)
            except (ValueError, TypeError):
                return
            if ltp <= 0:
                return

            raw_vol = (tick.get("v") or tick.get("volume_trade_for_the_day")
                       or tick.get("volume") or tick.get("vol") or 0)
            try:
                cum_vol = int(float(raw_vol))
            except (ValueError, TypeError):
                cum_vol = 0

            self._ltp_cache[angel_tok] = ltp
            now = dt.datetime.now()
            self.tick_store[angel_tok] = {"ltp": ltp, "volume": cum_vol, "timestamp": now}
            self.last_tick_time = time.time()

            if self.on_tick_cb:
                self.on_tick_cb(angel_tok, ltp, cum_vol, now)

        except Exception as e:
            print("KotakDataEngine._process_tick error:", e)

    # ----------------------------------------------------------
    def get_ltp(self, angel_token):
        """Return cached LTP for the given angel token."""
        return self._ltp_cache.get(angel_token)

    def _connect(self):
        """Called by watchdog on tick freeze — force-restart the subscription."""
        self._last_connect_attempt_time = time.time()  # start cooldown so watchdog backs off
        print("KotakDataEngine: watchdog restart — re-subscribing")
        symbols = getattr(self, "_kotak_symbols", [])
        if not symbols:
            print("KotakDataEngine: no saved symbols — cannot reconnect")
            return
        try:
            self.client.un_subscribe_all()
        except Exception:
            pass
        time.sleep(1)
        threading.Thread(target=self._do_subscribe, args=(symbols,),
                         daemon=True, name="kotak-ws-restart").start()
