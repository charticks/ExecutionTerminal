import os
import json as _json
import threading
import time
import datetime as dt
import pyotp
import requests

import tkinter as tk
from tkinter import messagebox

import config
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2


def _load_accounts():
    """
    Load accounts.json from the project root.
    Returns {"angel": [...], "kotak": [...], "dhan": [...]}.
    Each list has one entry per account; index 0 = primary.
    Returns empty lists if the file is absent or malformed.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "accounts.json")
    if not os.path.exists(path):
        return {"angel": [], "kotak": [], "dhan": []}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = _json.load(fh)
        return {k: data.get(k, []) for k in ("angel", "kotak", "dhan")}
    except Exception as exc:
        print(f"[accounts.json] parse error — using config.py only: {exc}")
        return {"angel": [], "kotak": [], "dhan": []}


class LoginMixin:
    """Login (Angel One + Kotak Neo), instrument master, market LTP WebSocket."""

    def _init_sub_account_lists(self):
        """Log in to sub-accounts (index 1+) from accounts.json in a background thread."""
        accounts = _load_accounts()

        # ── Angel sub-accounts ────────────────────────────────────
        self._angel_sub_sessions = []
        for acct in accounts["angel"][1:]:
            label = acct.get("label", "angel-sub")
            try:
                totp = pyotp.TOTP(acct["totp_secret"]).now()
                sc   = SmartConnect(api_key=acct["api_key"], timeout=30)
                data = sc.generateSession(acct["client_id"], acct["pin"], totp)
                if not data.get("status"):
                    raise Exception(data)
                self._angel_sub_sessions.append(sc)
                self.log(f"✅ Angel sub [{label}] logged in")
            except Exception as e:
                self.log(f"❌ Angel sub [{label}] login failed: {e}")

        # ── Kotak sub-accounts ────────────────────────────────────
        self._kotak_sub_sessions = []
        for acct in accounts["kotak"][1:]:
            label = acct.get("label", "kotak-sub")
            try:
                from neo_api_client import NeoAPI
                neo = NeoAPI(consumer_key=acct["consumer_key"],
                             environment="prod",
                             access_token=None, neo_fin_key=None)
                kotak_totp = pyotp.TOTP(acct["totp_secret"]).now()
                login_resp = neo.totp_login(
                    mobile_number=str(acct["mobile_no"]),
                    ucc=str(acct["ucc"]), totp=kotak_totp)
                if isinstance(login_resp, dict):
                    err = login_resp.get("error") or login_resp.get("Error")
                    if err:
                        raise Exception(err)
                resp = neo.totp_validate(mpin=str(acct["mpin"]))
                ok = isinstance(resp, dict) and not (
                    resp.get("error") or resp.get("Error") or resp.get("fault"))
                if not ok:
                    raise Exception(f"validate rejected: {resp}")
                self._kotak_sub_sessions.append(neo)
                self.log(f"✅ Kotak sub [{label}] logged in")
            except Exception as e:
                self.log(f"❌ Kotak sub [{label}] login failed: {e}")

        # ── Dhan sub-accounts ─────────────────────────────────────
        self._dhan_sub_sessions = []
        for acct in accounts["dhan"][1:]:
            label = acct.get("label", "dhan-sub")
            try:
                from dhanhq import dhanhq as DhanHQ, DhanContext
                ctx  = DhanContext(client_id=acct["client_id"],
                                   access_token=acct["access_token"])
                dhan = DhanHQ(ctx)
                resp = dhan.get_fund_limits()
                if isinstance(resp, dict) and resp.get("status") == "failure":
                    raise Exception(resp.get("remarks") or "auth failed")
                self._dhan_sub_sessions.append(dhan)
                self.log(f"✅ Dhan sub [{label}] logged in")
            except Exception as e:
                self.log(f"❌ Dhan sub [{label}] login failed: {e}")

        self.root.after(0, self._refresh_broker_indicators)

    def login(self):
        """
        Log in to the broker(s) selected via the Broker Selection checkboxes.

        Rules:
          • Angel One is ALWAYS logged in when selected — it is the live
            data / WebSocket source as well as an order broker.
          • Kotak Neo is logged in only when its checkbox is ticked.
          • If neither checkbox is ticked the user is warned and login aborts.
          • The top-level login_indicator stays green if at least one broker
            succeeds; the per-broker indicators show individual status.
        """
        use_angel = self.use_angel_var.get()
        use_kotak = self.use_kotak_var.get()
        use_dhan  = self.use_dhan_var.get()

        if not use_angel and not use_kotak and not use_dhan:
            messagebox.showerror(
                "Broker Selection Error",
                "Please select at least one broker before logging in.\n\n"
                "• Tick 'Angel One', 'Kotak Neo', and/or 'Dhan HQ' in the\n"
                "  Broker Selection section above.")
            return

        any_success = False

        # ── Angel One Login ─────────────────────────────────────
        if use_angel:
            import time as _login_time
            _MAX_LOGIN_ATTEMPTS = 3
            _LOGIN_RETRY_DELAY  = 5
            for _attempt in range(1, _MAX_LOGIN_ATTEMPTS + 1):
                try:
                    totp = pyotp.TOTP(config.TOTP_SECRET).now()
                    self.smart = SmartConnect(api_key=config.API_KEY, timeout=30)
                    data = self.smart.generateSession(
                        config.CLIENT_ID, config.PIN, totp)
                    if not data.get("status"):
                        raise Exception("Angel One Login Failed — bad response")
                    self.jwt_token    = data["data"]["jwtToken"].replace("Bearer ", "")
                    self.feed_token   = data["data"]["feedToken"]
                    self.client_code  = data["data"]["clientcode"]
                    self.angel_logged_in = True
                    self.is_logged_in    = True
                    any_success = True
                    self.log(f"✅ Angel One Login Success (attempt {_attempt})")
                    break
                except Exception as e:
                    self.angel_logged_in = False
                    if _attempt < _MAX_LOGIN_ATTEMPTS:
                        self.log(f"⚠️  Angel login {_attempt}/{_MAX_LOGIN_ATTEMPTS} failed: {e} — retrying in {_LOGIN_RETRY_DELAY}s")
                        if hasattr(self, "status_var"):
                            self.status_var.set(f"Login retry {_attempt}/{_MAX_LOGIN_ATTEMPTS}...")
                        _login_time.sleep(_LOGIN_RETRY_DELAY)
                    else:
                        self.log(f"❌ Angel One Login Failed after {_MAX_LOGIN_ATTEMPTS} attempts: {e}")
                        messagebox.showerror("Angel Login Error", str(e))
                        self.login_indicator.config(fg="red")
                        self._refresh_broker_indicators()
                        return
        else:
            self.log("ℹ️  Angel One skipped (not selected)")

        # ── Kotak Neo Login ─────────────────────────────────────
        if use_kotak:
            try:
                from neo_api_client import NeoAPI
            except ImportError:
                self.kotak_logged_in = False
                msg = ("neo_api_client package not installed.\n"
                       "Run:  pip install --force-reinstall "
                       "\"git+https://github.com/Kotak-Neo/Kotak-neo-api-v2.git@v2.0.1#egg=neo_api_client\"")
                self.log(f"❌ Kotak: {msg}")
                messagebox.showerror("Kotak – Missing Package", msg)
                use_kotak = False   # skip rest of kotak block

        if use_kotak:
            try:
                # ── Kotak Neo SDK v2 login flow ─────────────────────
                # v2 uses: totp_login(mobile, ucc, totp) → totp_validate(mpin)
                # consumer_secret is no longer required in v2
                self.log("🔄 Kotak Neo: initialising NeoAPI (v2 SDK)...")
                self.kotak = NeoAPI(
                    consumer_key = config.KOTAK_CONSUMER_KEY,
                    environment  = "prod",
                    access_token = None,
                    neo_fin_key  = None,
                )

                # ── Step 1: TOTP login (generates view token + session id) ──
                kotak_totp = pyotp.TOTP(config.KOTAK_TOTP_SECRET).now()
                self.log(f"🔄 Kotak Neo: Step 1 — totp_login() TOTP={kotak_totp}...")
                login_resp = self.kotak.totp_login(
                    mobile_number = str(config.KOTAK_MOBILE_NO),
                    ucc           = str(config.KOTAK_UCC),
                    totp          = kotak_totp,
                )
                self.log(f"   Kotak totp_login() raw response: {login_resp}")

                # Check Step 1 for explicit errors
                if isinstance(login_resp, dict):
                    err1 = (login_resp.get("error") or login_resp.get("Error")
                            or login_resp.get("message", ""))
                    stat1 = str(login_resp.get("status", "")).lower()
                    if err1 and stat1 not in ("ok", "success", "200", ""):
                        raise Exception(f"Kotak totp_login() failed — {err1}")

                # ── Step 2: Validate MPIN (generates trade token) ──────────
                self.log(f"🔄 Kotak Neo: Step 2 — totp_validate() MPIN...")
                resp = self.kotak.totp_validate(mpin=str(config.KOTAK_MPIN))
                self.log(f"   Kotak totp_validate() raw response: {resp}")

                # Success detection — v2 typically returns dict with status/data
                kotak_ok = False
                if resp is not None:
                    if isinstance(resp, dict):
                        status_val = str(resp.get("status", "")).lower()
                        error_val  = resp.get("error") or resp.get("Error") or resp.get("fault")
                        has_token  = bool(resp.get("token") or resp.get("access_token")
                                          or resp.get("trade_token")
                                          or (isinstance(resp.get("data"), dict)
                                              and (resp["data"].get("token")
                                                   or resp["data"].get("trade_token"))))
                        if not error_val:
                            if status_val in ("ok", "success", "200") or has_token:
                                kotak_ok = True
                            elif status_val == "":
                                kotak_ok = True   # no status but no error → optimistic
                    elif resp is True or str(resp).lower() in ("ok", "success"):
                        kotak_ok = True

                if kotak_ok:
                    self.kotak_logged_in = True
                    any_success = True
                    self.log("✅ Kotak Neo Login Success")
                else:
                    raise Exception(
                        f"Kotak login response did not indicate success.\n"
                        f"Raw: {resp}\n\n"
                        "Check in config.py:\n"
                        "  KOTAK_CONSUMER_KEY — UUID token from Kotak Neo app Trade API card\n"
                        "  KOTAK_MOBILE_NO    — with +91 prefix e.g. +919884437745\n"
                        "  KOTAK_UCC          — your client code e.g. W7RSR\n"
                        "  KOTAK_MPIN         — 6-digit MPIN\n"
                        "  KOTAK_TOTP_SECRET  — base32 secret from TOTP registration")

            except Exception as e:
                self.kotak_logged_in = False
                self.log(f"❌ Kotak Neo Login Failed: {e}")
                # Always show a popup so the error is visible
                messagebox.showerror(
                    "Kotak Neo Login Error",
                    f"{e}\n\n"
                    "Check the log panel for the raw API responses.\n"
                    "Verify all KOTAK_* values in config.py are correct.\n\n"
                    f"neo_api_client version: {_get_neo_version()}")
        else:
            if not use_kotak:
                pass   # already handled import-error branch above
            self.log("ℹ️  Kotak Neo skipped (not selected)")

        # ── Dhan Login ──────────────────────────────────────────
        if use_dhan:
            try:
                from dhanhq import dhanhq as DhanHQ
            except ImportError:
                self.dhan_logged_in = False
                msg = ("dhanhq package not installed.\n"
                       "Run:  pip install dhanhq")
                self.log(f"❌ Dhan: {msg}")
                messagebox.showerror("Dhan – Missing Package", msg)
                use_dhan = False

        if use_dhan:
            try:
                from dhanhq import dhanhq as DhanHQ, DhanContext
                self.log("🔄 Dhan HQ: initialising client...")
                ctx = DhanContext(
                    client_id    = config.DHAN_CLIENT_ID,
                    access_token = config.DHAN_ACCESS_TOKEN,
                )
                self.dhan_context = ctx   # stored for use in data engine
                self.dhan = DhanHQ(ctx)
                # Verify connectivity with a lightweight REST call
                resp = self.dhan.get_fund_limits()
                if isinstance(resp, dict) and resp.get("status") == "failure":
                    raise Exception(
                        resp.get("remarks") or resp.get("message") or "Dhan auth failed")
                self.dhan_logged_in = True
                any_success = True
                self.log("✅ Dhan Login Success")
                # Pre-load scrip master in background so it's ready before first order
                threading.Thread(
                    target=self._load_dhan_scrip_master,
                    daemon=True, name="dhan-scrip-preload").start()
            except Exception as e:
                self.dhan_logged_in = False
                self.log(f"❌ Dhan Login Failed: {e}")
                messagebox.showerror(
                    "Dhan Login Error",
                    f"{e}\n\n"
                    "Check config.py:\n"
                    "  DHAN_CLIENT_ID    — your Dhan client ID\n"
                    "  DHAN_ACCESS_TOKEN — access token from Dhan developer portal")
        else:
            if not use_dhan:
                self.log("ℹ️  Dhan HQ skipped (not selected)")

        # ── Sub-account login (accounts.json) ───────────────────
        threading.Thread(target=self._init_sub_account_lists,
                         daemon=True, name="sub-acct-login").start()

        # ── Final status ────────────────────────────────────────
        self._refresh_broker_indicators()
        if any_success:
            self.is_logged_in = True
            self.login_indicator.config(fg="green")
            brokers_ok = []
            if self.angel_logged_in:
                brokers_ok.append("Angel One")
            if self.kotak_logged_in:
                brokers_ok.append("Kotak Neo")
            if self.dhan_logged_in:
                brokers_ok.append("Dhan HQ")
            self.status_var.set(
                "Logged In: " + " + ".join(brokers_ok) + " ✅")
            # Start background REST poll so OC LTPs populate before bot starts
            if self.angel_logged_in:
                self.start_oc_ltp_poll()
            # Toggle Login → Logout (red) and auto-load instrument master
            if hasattr(self, "login_btn"):
                self.login_btn.config(text="Logout", bg="#b71c1c", fg="white",
                                      command=self.logout)
            self.root.after(200, self.load_master_threaded)
        else:
            self.login_indicator.config(fg="red")
            self.status_var.set("Login Failed ❌")

    def logout(self):
        """Reverse of login(): tears down broker sessions and resets the
        Login button back to its initial green state."""
        was_running = getattr(self, "is_running", False)
        if was_running:
            if not messagebox.askyesno(
                    "Logout",
                    "The bot is currently running with live/paper trades.\n"
                    "Logging out now may orphan open positions.\n\n"
                    "Logout anyway?"):
                return

        self.is_logged_in    = False
        self.angel_logged_in = False
        self.kotak_logged_in = False
        self.dhan_logged_in  = False
        self.smart       = None
        self.jwt_token    = None
        self.feed_token   = None
        self.client_code  = None

        self.market_ws_should_run = False
        try:
            if self.sws_market:
                self.sws_market.close_connection()
        except Exception:
            pass

        self._refresh_broker_indicators()
        self.login_indicator.config(fg="red")
        if hasattr(self, "login_btn"):
            self.login_btn.config(text="Login", bg="#00c853", fg="white",
                                  command=self.login)
        self.status_var.set("Logged Out")
        self.log("🔒 Logged out")

        # Stop the bot if it was running when logout was triggered
        if was_running:
            try:
                self.stop_bot()
            except Exception:
                pass
            messagebox.showinfo(
                "Logged Out",
                "You have been logged out.\nThe bot has been stopped automatically.")

    # ==========================================================
    # LOAD INSTRUMENT MASTER
    # ==========================================================
    def load_master(self):
        import json
        base_dir     = os.path.dirname(os.path.abspath(__file__))
        cache_folder = os.path.join(base_dir, "data_cache")
        os.makedirs(cache_folder, exist_ok=True)
        today     = dt.datetime.now().strftime("%Y%m%d")
        file_name = os.path.join(cache_folder,
                                 f"instrument_master_{today}.json")
        if os.path.exists(file_name):
            with open(file_name) as f:
                self.instrument_master = json.load(f)
            print("Loaded Master from Cache:", file_name)
            self.status_var.set("Master Loaded (Cached) ✅")
            self.master_indicator.config(fg="green")
            self.update_expiry_list()
            self.update_gap_controls()
            self._load_dhan_master(cache_folder, today)
            if self.angel_logged_in:
                self.start_market_ltp_stream()
            return
        print("Downloading Master File...")
        url = ("https://margincalculator.angelbroking.com/OpenAPI_File"
               "/files/OpenAPIScripMaster.json")
        r = requests.get(url)
        self.instrument_master = r.json()
        with open(file_name, "w") as f:
            json.dump(self.instrument_master, f)
        print("Master Downloaded & Saved:", file_name)
        self.status_var.set("Master Loaded (Fresh) ✅")
        self.master_indicator.config(fg="green")
        self.update_expiry_list()
        self._load_dhan_master(cache_folder, today)
        if self.angel_logged_in:
            self.start_market_ltp_stream()

    def _load_dhan_master(self, cache_folder, today):
        """Download (or load from cache) the Dhan scrip master CSV.
        Sets self._dhan_scrip_master_path so _build_dhan_token_map() skips the download.
        """
        file_name = os.path.join(cache_folder, f"dhan_scrip_master_{today}.csv")
        if not os.path.exists(file_name):
            try:
                self.log("🔄 Downloading Dhan scrip master...")
                r = requests.get(
                    "https://images.dhan.co/api-data/api-scrip-master.csv",
                    timeout=30)
                r.raise_for_status()
                with open(file_name, "w", encoding="utf-8", newline="") as f:
                    f.write(r.text)
                self.log(f"✅ Dhan scrip master saved ({file_name})")
            except Exception as e:
                self.log(f"⚠️  Dhan scrip master download failed: {e}")
                return
        else:
            self.log(f"ℹ️  Dhan scrip master cached ({file_name})")
        self._dhan_scrip_master_path = file_name

    def load_master_threaded(self):
        threading.Thread(target=self._load_master_safe,
                         daemon=True).start()

    def _load_kotak_scrip_master(self):
        """
        Download Kotak scrip master CSVs for nse_fo and bse_fo once per session.
        Builds self._kotak_scrip_cache = {"nse_fo": {pTrdSymbol: pSymbol}, "bse_fo": {...}}
        pTrdSymbol = trading symbol string (e.g. "NIFTY26APR2823900CE") — lookup key.
        pSymbol    = numeric scrip code (e.g. "137372") — WS subscription token / value.
        so _build_kotak_token_map() can do O(1) lookups instead of per-token HTTP calls.
        """
        if not self.kotak_logged_in:
            return
        if getattr(self, "_kotak_scrip_cache", None):
            return   # already loaded this session
        self._kotak_scrip_cache = {}
        import io as _io, requests as _req, pandas as _pd
        from concurrent.futures import ThreadPoolExecutor, as_completed as _asc
        try:
            from neo_api_client.api.scrip_master_api import ScripMasterAPI as _SMA
            scrip_api = _SMA(self.kotak.api_client)

            # Phase 1: fetch download URLs sequentially (NeoAPI client may not be
            # thread-safe, so we don't parallelise the API calls themselves).
            seg_urls = {}
            for seg in ("nse_fo", "bse_fo", "mcx_fo"):
                try:
                    self.log(f"🔄 Kotak: fetching {seg} scrip master URL...")
                    url_result = scrip_api.scrip_master_init(exchange_segment=seg)
                    if isinstance(url_result, dict):
                        self.log(f"⚠️  Kotak scrip master {seg}: {url_result}")
                        continue
                    seg_urls[seg] = str(url_result)
                except Exception as e:
                    self.log(f"⚠️  Kotak scrip master {seg} URL failed: {e}")

            # Phase 2: download + parse all CSVs in parallel — this is where
            # the ~10-15 s wait was; now all three run simultaneously.
            def _download_seg(seg, csv_url):
                self.log(f"🔄 Kotak: downloading {seg} CSV...")
                r = _req.get(csv_url, timeout=30)
                r.raise_for_status()
                df = _pd.read_csv(_io.StringIO(r.text))
                df.columns = df.columns.str.strip()
                cache = {}
                sym_col = next((c for c in df.columns if c.strip() == "pSymbol"), None)
                tok_col = next((c for c in df.columns if c.strip() == "pTrdSymbol"), None)
                if sym_col and tok_col:
                    for sym, tok in zip(df[sym_col].astype(str), df[tok_col].astype(str)):
                        sym = sym.strip(); tok = tok.strip()
                        if sym and tok and tok != "nan":
                            cache[tok] = sym
                self.log(f"📡 Kotak {seg} scrip master: {len(cache)} options cached")
                if seg == "mcx_fo" and cache:
                    crude_samples = [k for k in cache if "CRUDE" in k.upper()][:6]
                    print(f"[Kotak mcx_fo] CRUDE sample symbols: {crude_samples}")
                return seg, cache

            with ThreadPoolExecutor(max_workers=3, thread_name_prefix="kotak-scrip") as pool:
                futs = {pool.submit(_download_seg, seg, url): seg
                        for seg, url in seg_urls.items()}
                for fut in _asc(futs):
                    try:
                        seg, cache = fut.result()
                        self._kotak_scrip_cache[seg] = cache
                    except Exception as e:
                        self.log(f"⚠️  Kotak scrip master {futs[fut]} download failed: {e}")
        except Exception as e:
            self.log(f"⚠️  Kotak scrip master load failed: {e}")

    def _load_master_safe(self):
        self.progress.start()
        try:
            self.status_var.set("Loading instrument master...")
            self.load_master()
            # Pre-load Kotak scrip master in background (enables fast OC token mapping)
            if self.kotak_logged_in:
                threading.Thread(
                    target=self._load_kotak_scrip_master,
                    daemon=True, name="kotak-scrip-preload").start()
        finally:
            self.progress.stop()

    # ==========================================================
    # MARKET LTP STREAM  (index prices — separate WS)
    # ==========================================================
    def start_market_ltp_stream(self):
        """
        Start the live index-price WebSocket. Resolves the index tokens once and
        delegates the actual connect (and all reconnects) to _connect_market(),
        which mirrors OptionChainEngine's resilient reconnect pattern so a transient
        Angel disconnect can never permanently kill the index feed.
        """
        market_tokens = self.get_market_tokens()
        if not market_tokens:
            return
        self.market_token_map = market_tokens
        self.market_ws_should_run = True
        # REST safety net — keeps the index labels live whenever the WS is down.
        # Started once; it idles (no REST calls) while the WS is connected.
        if not getattr(self, "_market_rest_fallback_started", False):
            self._market_rest_fallback_started = True
            threading.Thread(target=self._market_ltp_rest_fallback,
                             daemon=True, name="market-ltp-rest").start()
        self._connect_market()

    def _connect_market(self):
        """(Re)establish the Market LTP WebSocket. Safe to call repeatedly —
        a generation counter neutralises callbacks from any superseded socket."""
        lock = getattr(self, "_market_reconnect_lock", None)
        if lock is None:
            lock = self._market_reconnect_lock = threading.Lock()
        # If another thread is already (re)connecting, let it finish.
        if not lock.acquire(blocking=False):
            return
        try:
            gen = getattr(self, "_market_generation", 0) + 1
            self._market_generation = gen

            # Force-close any previous socket in the background so its callbacks
            # stop firing (they are also gen-guarded below).
            old = getattr(self, "sws_market", None)
            if old is not None:
                def _close_old(_o=old):
                    try:
                        _o.close_connection()
                    except Exception:
                        pass
                threading.Thread(target=_close_old, daemon=True,
                                 name="market-close").start()

            self.sws_market = SmartWebSocketV2(
                self.jwt_token, config.API_KEY,
                self.client_code, self.feed_token,
                max_retry_attempt=0,   # our on_close owns reconnection, not the library
                retry_strategy=0,
                retry_delay=5)

            exch_map   = {"NSE": 1, "BSE": 3, "MCX": 5}
            token_list = [{
                "exchangeType": exch_map.get(d["exchange"], 1),
                "tokens": [d["token"]]
            } for d in self.market_token_map.values()]
            print("Subscribing Market Tokens:", token_list)

            def on_open(ws):
                if gen != self._market_generation:
                    return
                print("Market WebSocket Connected ✅")
                self._market_reconnect_attempt = 0   # reset backoff on success
                self.market_ws_connected = True
                self.market_ws_running   = True
                self.sws_market.subscribe("marketwatch", 3, token_list)

            def on_data(ws, msg):
                if gen != self._market_generation:
                    return
                try:
                    token = msg.get("token") or msg.get("symbolToken")
                    raw   = msg.get("last_traded_price")
                    if raw is None:
                        raw = msg.get("ltp")
                    if raw is None:
                        return
                    ltp = float(raw) / 100

                    # Previous day close → points change and intraday % change
                    prev_raw = msg.get("closed_price")
                    change_color = "#aaaaaa"
                    if prev_raw:
                        prev = float(prev_raw) / 100
                        chg = ltp - prev
                        pct = (chg / prev * 100) if prev else 0
                        change_color = "#00e676" if chg >= 0 else "#ff5252"

                    for name, d in self.market_token_map.items():
                        if str(d["token"]) == str(token):
                            self.market_ltp_cache[name] = ltp
                            lbl = self.market_ltp_labels[name]
                            self.root.after(
                                0, lambda l=lbl, v=ltp: l.config(text=f"{v:.2f}"))
                            chg_widgets = self.market_change_labels.get(name)
                            if chg_widgets and prev_raw:
                                pts_lbl, pct_lbl, arrow_lbl = chg_widgets
                                arrow = "↑" if chg >= 0 else "↓"
                                pts_text = f"{chg:+.2f}"
                                pct_text = f"{pct:+.2f}%"
                                col = change_color
                                self.root.after(0, lambda w=pts_lbl, t=pts_text, c=col:
                                                w.config(text=t, fg=c))
                                self.root.after(0, lambda w=pct_lbl, t=pct_text, c=col:
                                                w.config(text=t, fg=c))
                                self.root.after(0, lambda w=arrow_lbl, t=arrow, c=col:
                                                w.config(text=t, fg=c))
                            # Auto-refresh option chain if it has 0 rows and this
                            # is the currently selected index (ATM unknown at load)
                            if (not self.oc_data
                                    and name.upper() == self.index_var.get().upper()):
                                self.root.after(
                                    200, self.refresh_option_chain)
                            break
                except Exception as e:
                    print("Market LTP error:", e)

            def on_error(ws, err):
                if gen != self._market_generation:
                    return
                err_s = str(err).lower()
                if "already closed" in err_s or "connection closed" in err_s:
                    return   # transient race with subscribe — on_close handles retry
                print("Market WebSocket Error:", err)
                # NEVER stop the stream here — on_close owns reconnection with
                # exponential backoff. Killing should_run = permanent feed death.

            def on_close(ws):
                if gen != self._market_generation:
                    return   # stale callback from a superseded socket — ignore
                self.market_ws_connected = False
                self.market_ws_running   = False
                if not self.market_ws_should_run:
                    print("Market WebSocket Closed (stopped — no reconnect)")
                    return
                attempt = getattr(self, "_market_reconnect_attempt", 0)
                delay = min(5 * (2 ** attempt), 60)
                self._market_reconnect_attempt = attempt + 1
                print(f"Market WebSocket Closed — reconnecting in {delay}s "
                      f"(attempt {self._market_reconnect_attempt})")
                def _retry():
                    time.sleep(delay)
                    if self.market_ws_should_run and not self.market_ws_connected:
                        self._connect_market()
                threading.Thread(target=_retry, daemon=True,
                                 name="market-reconnect").start()

            self.sws_market.on_open  = on_open
            self.sws_market.on_data  = on_data
            self.sws_market.on_error = on_error
            self.sws_market.on_close = on_close
            threading.Thread(target=self.sws_market.connect,
                             daemon=True).start()
        finally:
            lock.release()

    def _market_ltp_rest_fallback(self):
        """
        Safety net: while the Market WS is NOT connected, fetch index LTPs via
        REST every ~4s so the top-panel price never goes blank during an outage.
        Idles (no REST calls) the entire time the WS is healthy.
        Only runs when Angel One is logged in (smart client available).
        """
        while getattr(self, "market_ws_should_run", False):
            try:
                if not self.smart:
                    time.sleep(4)
                    continue
                if getattr(self, "market_ws_connected", False):
                    time.sleep(4)
                    continue
                tokens = getattr(self, "market_token_map", {}) or {}
                # Group tokens by exchange for batched getMarketData calls.
                by_exch = {}
                for name, d in tokens.items():
                    by_exch.setdefault(d["exchange"], []).append((name, str(d["token"])))
                for exch, items in by_exch.items():
                    try:
                        resp = self.smart.getMarketData("LTP", {exch: [t for _, t in items]})
                    except Exception as e:
                        print("[MarketFallback] REST error:", e)
                        continue
                    if not (resp and resp.get("status")):
                        continue
                    ltp_by_token = {
                        str(it.get("symbolToken", "")): float(it.get("ltp", 0) or 0)
                        for it in resp.get("data", {}).get("fetched", [])
                    }
                    for name, tok in items:
                        ltp = ltp_by_token.get(tok, 0)
                        if ltp <= 0:
                            continue
                        self.market_ltp_cache[name] = ltp
                        lbl = self.market_ltp_labels.get(name)
                        if lbl is not None:
                            self.root.after(0, lambda l=lbl, v=ltp:
                                            l.winfo_exists() and l.config(text=f"{v:.2f}"))
                        if (not self.oc_data
                                and name.upper() == self.index_var.get().upper()):
                            self.root.after(200, self.refresh_option_chain)
            except Exception as e:
                print("[MarketFallback] loop error:", e)
            time.sleep(4)

