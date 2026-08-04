import threading
import datetime as dt
import re
import time as _time_mod
from concurrent.futures import ThreadPoolExecutor

import tkinter as tk
from tkinter import messagebox
import pandas as pd

import config


def _resample_candles(df, target_tf):
    """Resample a closed-candle DataFrame to a larger timeframe.

    Requires a 'time' column (datetime). Returns None if result has < 2 rows.
    """
    minutes = {"1min": 1, "3min": 3, "5min": 5, "15min": 15}.get(target_tf, 5)
    if minutes == 1 or "time" not in df.columns:
        return df
    df2 = (
        df.set_index("time")
        .resample(f"{minutes}min")
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .dropna(subset=["open"])
        .reset_index()
    )
    return df2 if len(df2) >= 2 else None


class OrderManagerMixin:
    """Order placement (Angel One + Kotak Neo), trade opening, trade row management."""

    @staticmethod
    def _with_retry(fn, attempts=3, delay=0.5, log_fn=None):
        """Call fn() up to `attempts` times on network errors; re-raise after last attempt."""
        last_err = None
        for attempt in range(attempts):
            try:
                return fn()
            except Exception as exc:
                last_err = exc
                err_s    = str(exc).lower()
                is_net   = any(k in err_s for k in
                               ("timeout", "connection", "timed out", "connectionpool",
                                "read timed", "remotedisconnected"))
                if is_net and attempt < attempts - 1:
                    if log_fn:
                        log_fn(f"⚠️  Network error (attempt {attempt + 1}/{attempts}) — retrying...")
                    _time_mod.sleep(delay)
                else:
                    break
        raise last_err

    def poll_order_status(self, token, order_id, broker_name):
        """
        Poll broker order status after placement.
        Sets order_registry[token]["status"] to "FILLED", "REJECTED", or "PENDING".
        Runs in a background thread — does not block the caller.
        """
        def _poll():
            for _ in range(5):
                _time_mod.sleep(2)
                try:
                    status = "PENDING"
                    if broker_name == "angel" and self.angel_logged_in:
                        resp = self.smart.individual_order_details(order_id)
                        if isinstance(resp, dict):
                            os_ = (resp.get("data") or resp).get("orderstatus", "")
                            if os_ in ("complete", "COMPLETE"):
                                status = "FILLED"
                            elif os_ in ("rejected", "REJECTED", "cancelled", "CANCELLED"):
                                status = "REJECTED"
                    elif broker_name == "kotak" and self.kotak_logged_in:
                        report = self.kotak.order_report()
                        if isinstance(report, dict):
                            for o in (report.get("data") or []):
                                if str(o.get("nOrdNo") or o.get("orderId")) == str(order_id):
                                    os_ = str(o.get("ordSt", "")).lower()
                                    if os_ in ("complete", "traded"):
                                        status = "FILLED"
                                    elif "reject" in os_ or "cancel" in os_:
                                        status = "REJECTED"
                    elif broker_name == "dhan" and self.dhan_logged_in:
                        resp = self.dhan.get_order_by_id(order_id)
                        if isinstance(resp, dict):
                            os_ = str((resp.get("data") or resp).get("orderStatus", "")).upper()
                            if os_ == "TRADED":
                                status = "FILLED"
                            elif os_ in ("REJECTED", "CANCELLED"):
                                status = "REJECTED"

                    reg = getattr(self, "order_registry", {}).get(token)
                    if reg and reg.get("order_id") == order_id:
                        reg["status"] = status
                        if status == "FILLED":
                            self.log(f"✅ Order confirmed FILLED: {broker_name.upper()} {order_id}")
                            return
                        elif status == "REJECTED":
                            self.log(f"❌ Order REJECTED: {broker_name.upper()} {order_id}")
                            return
                except Exception as e:
                    print(f"poll_order_status error ({broker_name}):", e)

        threading.Thread(target=_poll, daemon=True, name=f"ord-poll-{token}").start()

    def place_order(self, token, transaction_type, broker=None, qty_override=None):
        """
        broker:       None  = honour both broker checkboxes (default)
                      "angel" = Angel One only
                      "kotak" = Kotak Neo only
        qty_override: if set, send this quantity instead of state["lot_size"]
                      (used for add-on BUY when scaling into a live position)
        """
        if self.trade_mode_var.get() == "Paper":
            print("Paper Trade Executed")
            return {"status": True}

        state = self.strike_state.get(token)
        if not state:
            return None

        # Order Params: Wait — throttle consecutive order placements
        self._enforce_order_wait()

        # Resolve order type / limit price. A manual per-trade override (set
        # by place_manual_trade) takes priority; otherwise fall back to the
        # Day Profile's Order Params (Market/Limit + Entry Price Offset %).
        manual_otype = getattr(self, "_manual_order_type", None)
        if manual_otype:
            order_type_override = manual_otype
            limit_px_override   = getattr(self, "_manual_limit_price", 0) or 0
        else:
            order_type_override = self.cfg_order_type_var.get() or "MARKET"
            limit_px_override   = 0

        # Apply Entry Price Offset (%) from Config to BOTH Market and Limit orders.
        # For MARKET: converts to LIMIT at LTP ± offset% to cap slippage.
        # For LIMIT with no explicit price: sets limit at LTP ± offset%.
        try:
            entry_pct = float(self.cfg_limit_order_pct_var.get())
        except (TypeError, ValueError):
            entry_pct = 0
        ref_price = state.get("ltp") or 0
        if entry_pct > 0 and ref_price > 0:
            if order_type_override == "MARKET":
                order_type_override = "LIMIT"
                if transaction_type == "BUY":
                    limit_px_override = round(ref_price * (1 + entry_pct / 100), 2)
                else:
                    limit_px_override = round(ref_price * (1 - entry_pct / 100), 2)
            elif order_type_override == "LIMIT" and limit_px_override <= 0:
                if transaction_type == "BUY":
                    limit_px_override = round(ref_price * (1 + entry_pct / 100), 2)
                else:
                    limit_px_override = round(ref_price * (1 - entry_pct / 100), 2)
        elif order_type_override == "LIMIT" and limit_px_override <= 0 and ref_price <= 0:
            order_type_override = "MARKET"   # no reference price — fall back

        # Effective quantity: caller override takes priority over state lot_size
        eff_qty = qty_override if qty_override is not None else state["lot_size"]

        use_angel = (broker in (None, "angel")) and self.use_angel_var.get()
        use_kotak = (broker in (None, "kotak")) and self.use_kotak_var.get()
        use_dhan  = (broker in (None, "dhan"))  and self.use_dhan_var.get()

        results       = {}   # {broker_name: response}
        any_order_sent = False

        # ── Define per-broker callables (run in parallel via ThreadPoolExecutor) ──

        def _do_angel():
            resp = None
            last_err = None
            for _attempt in range(3):
                try:
                    orderparams = {
                        "variety":         "NORMAL",
                        "tradingsymbol":   state["tradingsymbol"],
                        "symboltoken":     token,
                        "transactiontype": transaction_type,
                        "exchange":        state["exchange"],
                        "ordertype":       order_type_override,
                        "producttype":     "INTRADAY",
                        "duration":        "DAY",
                        "quantity":        eff_qty,
                        "price":           str(limit_px_override) if order_type_override == "LIMIT" else "0",
                    }
                    print("Angel Order:", orderparams)
                    resp = self.smart.placeOrder(orderparams)
                    print("Angel Response:", resp)
                    self.log(f"✅ Angel Order {transaction_type} → {state['tradingsymbol']}")
                    angel_order_id = None
                    if isinstance(resp, dict):
                        angel_order_id = resp.get("data") or resp.get("orderid")
                    elif isinstance(resp, str):
                        angel_order_id = resp
                    if angel_order_id and hasattr(self, "order_registry"):
                        self.order_registry[token] = {
                            "order_id": str(angel_order_id),
                            "broker":   "angel",
                            "side":     transaction_type,
                            "qty":      eff_qty,
                            "status":   "PENDING",
                        }
                        self.poll_order_status(token, str(angel_order_id), "angel")
                    last_err = None
                    if hasattr(self, "_set_api_status"):
                        self._set_api_status(True)
                    break
                except Exception as e:
                    last_err = e
                    err_s = str(e).lower()
                    is_network = any(k in err_s for k in
                                     ("timeout", "connection", "timed out", "connectionpool"))
                    if is_network and _attempt < 2:
                        self.log(f"⚠️  Angel order timeout (attempt {_attempt + 1}/3) — retrying...")
                        if hasattr(self, "_set_api_status"):
                            self._set_api_status(False, "timeout")
                        _time_mod.sleep(0.3)
                    else:
                        break
            if last_err is not None:
                print("Angel Order Error:", last_err)
                self.log(f"❌ Angel Order Error: {last_err}")
                return None
            # ── Angel sub-account fan-out (daemon threads, non-blocking) ────
            _angel_params = {
                "variety": "NORMAL", "tradingsymbol": state["tradingsymbol"],
                "symboltoken": token, "transactiontype": transaction_type,
                "exchange": state["exchange"], "ordertype": order_type_override,
                "producttype": "INTRADAY", "duration": "DAY", "quantity": eff_qty,
                "price": str(limit_px_override) if order_type_override == "LIMIT" else "0",
            }
            for _sc in getattr(self, "_angel_sub_sessions", []):
                def _angel_sub(sc=_sc, params=dict(_angel_params)):
                    try:
                        r = sc.placeOrder(params)
                        self.log(f"✅ Angel sub {transaction_type} → "
                                 f"{state['tradingsymbol']} | {r}")
                    except Exception as _e:
                        self.log(f"❌ Angel sub order error: {_e}")
                threading.Thread(target=_angel_sub, daemon=True).start()
            return resp

        def _do_kotak():
            try:
                kotak_sym = self._angel_to_kotak_symbol(state["tradingsymbol"])
                if not kotak_sym:
                    self.log("❌ Kotak Order skipped: no exact same-expiry symbol found")
                    return None
                self.log(f"   Kotak symbol: {kotak_sym}")
                kotak_order_type = "LMT" if order_type_override == "LIMIT" else "MKT"
                kotak_price      = str(limit_px_override) if order_type_override == "LIMIT" else "0"

                def _kotak_place():
                    return self.kotak.place_order(
                        exchange_segment = self._kotak_exchange(state["exchange"]),
                        product          = "MIS",
                        order_type       = kotak_order_type,
                        trading_symbol   = kotak_sym,
                        transaction_type = "B" if transaction_type == "BUY" else "S",
                        quantity         = str(eff_qty),
                        validity         = "DAY",
                        price            = kotak_price,
                        amo              = "NO",
                    )

                kresp = self._with_retry(_kotak_place, attempts=3, delay=0.5,
                                         log_fn=self.log)
                self.log(f"   Kotak raw response: {kresp}")
                kotak_order_ok = False
                kotak_order_id = None
                if isinstance(kresp, dict):
                    err  = kresp.get("error") or kresp.get("Error") or kresp.get("fault")
                    data = kresp.get("data") or {}
                    kotak_order_id = (data.get("nOrdNo") or data.get("orderId")
                                      or kresp.get("nOrdNo") or kresp.get("orderId"))
                    if not err and kotak_order_id:
                        kotak_order_ok = True
                    elif not err and str(kresp.get("status", "")).lower() in ("ok", "success", "200"):
                        kotak_order_ok = True
                if kotak_order_ok:
                    self.log(f"✅ Kotak Order {transaction_type} → {state['tradingsymbol']}")
                    if kotak_order_id and hasattr(self, "order_registry"):
                        self.order_registry[token] = {
                            "order_id": str(kotak_order_id),
                            "broker":   "kotak",
                            "side":     transaction_type,
                            "qty":      eff_qty,
                            "status":   "PENDING",
                        }
                        self.poll_order_status(token, str(kotak_order_id), "kotak")
                    # ── Kotak sub-account fan-out ────────────────────────
                    _kotak_otype = kotak_order_type
                    _kotak_price = kotak_price
                    _kotak_exch  = self._kotak_exchange(state["exchange"])
                    _kotak_side  = "B" if transaction_type == "BUY" else "S"
                    _kotak_qty   = str(eff_qty)
                    for _neo in getattr(self, "_kotak_sub_sessions", []):
                        def _kotak_sub(neo=_neo, ksym=kotak_sym):
                            try:
                                def _place():
                                    return neo.place_order(
                                        exchange_segment=_kotak_exch, product="MIS",
                                        order_type=_kotak_otype, trading_symbol=ksym,
                                        transaction_type=_kotak_side, quantity=_kotak_qty,
                                        validity="DAY", price=_kotak_price, amo="NO")
                                r = self._with_retry(_place, attempts=3, delay=0.5,
                                                     log_fn=self.log)
                                self.log(f"✅ Kotak sub {transaction_type} → "
                                         f"{state['tradingsymbol']} | {r}")
                            except Exception as _e:
                                self.log(f"❌ Kotak sub order error: {_e}")
                        threading.Thread(target=_kotak_sub, daemon=True).start()
                    return kresp
                else:
                    self.log(f"❌ Kotak Order rejected: {kresp}")
                    return None
            except Exception as e:
                self.log(f"❌ Kotak Order Error: {e}")
                return None

        def _do_dhan():
            try:
                dhan_tok = self._dhan_token_cache.get(token)
                if not dhan_tok:
                    dhan_tok = self._lookup_dhan_token(state["tradingsymbol"],
                                                       state["exchange"])
                    if dhan_tok:
                        self._dhan_token_cache[token] = dhan_tok
                if not dhan_tok:
                    self.log(f"⚠️  Dhan: no security_id for {state['tradingsymbol']} — order skipped")
                    return None
                print(f"Dhan order: sym={state['tradingsymbol']} security_id={dhan_tok}")
                dhan_exch  = self._dhan_exchange(state["exchange"])
                dhan_otype = self.dhan.LIMIT if order_type_override == "LIMIT" else self.dhan.MARKET
                dhan_price = float(limit_px_override) if order_type_override == "LIMIT" else 0

                def _dhan_place():
                    return self.dhan.place_order(
                        security_id      = str(dhan_tok),
                        exchange_segment = dhan_exch,
                        transaction_type = self.dhan.BUY if transaction_type == "BUY" else self.dhan.SELL,
                        quantity         = eff_qty,
                        order_type       = dhan_otype,
                        product_type     = self.dhan.INTRA,
                        price            = dhan_price,
                    )

                dresp = self._with_retry(_dhan_place, attempts=3, delay=0.5,
                                         log_fn=self.log)
                print("Dhan Order response:", dresp)
                if isinstance(dresp, dict) and dresp.get("status") == "failure":
                    self.log(f"❌ Dhan Order rejected: {dresp}")
                    return None
                self.log(f"✅ Dhan Order {transaction_type} → {state['tradingsymbol']}")
                dhan_order_id = None
                if isinstance(dresp, dict):
                    dhan_order_id = ((dresp.get("data") or dresp).get("orderId")
                                     or dresp.get("orderId"))
                if dhan_order_id and hasattr(self, "order_registry"):
                    self.order_registry[token] = {
                        "order_id": str(dhan_order_id),
                        "broker":   "dhan",
                        "side":     transaction_type,
                        "qty":      eff_qty,
                        "status":   "PENDING",
                    }
                    self.poll_order_status(token, str(dhan_order_id), "dhan")
                # ── Dhan sub-account fan-out ─────────────────────────
                _dhan_exch  = dhan_exch
                _dhan_otype = dhan_otype
                _dhan_price = dhan_price
                _dhan_side  = self.dhan.BUY if transaction_type == "BUY" else self.dhan.SELL
                _dhan_intra = self.dhan.INTRA
                _dhan_qty   = eff_qty
                _dhan_tid   = str(dhan_tok)
                for _dc in getattr(self, "_dhan_sub_sessions", []):
                    def _dhan_sub(dc=_dc):
                        try:
                            def _place():
                                return dc.place_order(
                                    security_id=_dhan_tid,
                                    exchange_segment=_dhan_exch,
                                    transaction_type=_dhan_side,
                                    quantity=_dhan_qty,
                                    order_type=_dhan_otype,
                                    product_type=_dhan_intra,
                                    price=_dhan_price)
                            r = self._with_retry(_place, attempts=3, delay=0.5,
                                                 log_fn=self.log)
                            self.log(f"✅ Dhan sub {transaction_type} → "
                                     f"{state['tradingsymbol']} | {r}")
                        except Exception as _e:
                            self.log(f"❌ Dhan sub order error: {_e}")
                    threading.Thread(target=_dhan_sub, daemon=True).start()
                return dresp
            except Exception as e:
                print("Dhan Order Error:", e)
                self.log(f"❌ Dhan Order Error: {e}")
                return None

        # ── Submit all active brokers in parallel ────────────────
        if not use_angel and not use_kotak and not use_dhan:
            self.log("⚠️  No broker selected — order skipped")
            return {"status": True}

        tasks = {}
        if use_angel and self.angel_logged_in:
            tasks["angel"] = _do_angel
        elif use_angel:
            self.log("⚠️  Angel One selected but not logged in — order skipped")
        if use_kotak and self.kotak_logged_in:
            tasks["kotak"] = _do_kotak
        elif use_kotak:
            self.log("⚠️  Kotak Neo selected but not logged in — order skipped")
        if use_dhan and self.dhan_logged_in:
            tasks["dhan"] = _do_dhan
        elif use_dhan:
            self.log("⚠️  Dhan HQ selected but not logged in — order skipped")

        if len(tasks) == 1:
            # Single broker — run directly, no thread overhead
            name, fn = next(iter(tasks.items()))
            results[name] = fn()
        else:
            # Multiple brokers — run in parallel
            with ThreadPoolExecutor(max_workers=len(tasks),
                                    thread_name_prefix="broker-order") as ex:
                futures = {name: ex.submit(fn) for name, fn in tasks.items()}
                for name, fut in futures.items():
                    try:
                        results[name] = fut.result(timeout=35)
                    except Exception as e:
                        self.log(f"❌ {name.capitalize()} order future error: {e}")
                        results[name] = None

        for resp in results.values():
            if resp:
                any_order_sent = True
                break

        # If no broker accepted the order in Live mode, signal failure to caller
        if not any_order_sent and self.trade_mode_var.get() == "Live":
            self.log(f"⚠️  All brokers failed for {state['tradingsymbol']} — rolling back state for re-entry")
            return None

        # Return whichever response we have (prefer Angel as primary)
        return (results.get("angel") or results.get("kotak")
                or results.get("dhan") or {"status": True})

    @staticmethod
    def _parse_angel_option_symbol(sym: str):
        """
        Parse an Angel One option symbol into (root, date_str, strike, opt_type).

        Two formats:
        1. Monthly: SYMBOL{DD}{MMM}{YY}{strike}{CE|PE}
           e.g. NIFTY21APR2624150CE → ('NIFTY', '2026-04-21', 24150, 'CE')
        2. Weekly:  SYMBOL{YY}{M}{DD}{strike}{CE|PE}
           M = 1-9 for Jan-Sep, O for Oct, N for Nov, D for Dec
           e.g. SENSEX2641678200CE → ('SENSEX', '2026-04-16', 78200, 'CE')

        Returns None if the symbol cannot be parsed.
        """
        import re as _re
        MONTH_MAP = {
            'JAN': '01', 'FEB': '02', 'MAR': '03', 'APR': '04',
            'MAY': '05', 'JUN': '06', 'JUL': '07', 'AUG': '08',
            'SEP': '09', 'OCT': '10', 'NOV': '11', 'DEC': '12',
        }
        # Monthly expiry: SYMBOL{DD}{MMM}{YY}{strike}{CE|PE}
        m = _re.match(r'^([A-Z]+)(\d{2})([A-Z]{3})(\d{2})(\d+)(CE|PE)$', sym)
        if m:
            root, dd, mmm, yy, strike_s, opt = m.groups()
            mon = MONTH_MAP.get(mmm)
            if not mon:
                return None
            date_str = f"20{yy}-{mon}-{dd}"
            return (root, date_str, int(strike_s), opt)

        # Weekly expiry: SYMBOL{YY}{M}{DD}{strike}{CE|PE}
        # M = 1-9 (Jan-Sep), O (Oct), N (Nov), D (Dec)
        WEEK_MONTH_MAP = {
            '1': '01', '2': '02', '3': '03', '4': '04',
            '5': '05', '6': '06', '7': '07', '8': '08',
            '9': '09', 'O': '10', 'N': '11', 'D': '12',
        }
        m = _re.match(r'^([A-Z]+)(\d{2})([1-9OND])(\d{2})(\d+)(CE|PE)$', sym)
        if m:
            root, yy, m_char, dd, strike_s, opt = m.groups()
            mon = WEEK_MONTH_MAP.get(m_char)
            if not mon:
                return None
            date_str = f"20{yy}-{mon}-{dd}"
            return (root, date_str, int(strike_s), opt)

        return None

    def _load_dhan_scrip_master(self):
        """
        Download Dhan F&O scrip master CSV once per session and build a lookup
        dict keyed by (symbol_root, expiry_date, strike_int, opt_type) → security_id.
        Dhan CSV format: NIFTY-Apr2026-24150-CE, expiry: 2026-04-21 14:30:00
        """
        if not hasattr(self, "_dhan_scrip_cache"):
            self._dhan_scrip_cache = {}
        if not hasattr(self, "_dhan_scrip_loaded"):
            self._dhan_scrip_loaded = False
        if self._dhan_scrip_loaded:
            return

        try:
            import requests as _req, csv as _csv, io as _io
            r = _req.get(
                "https://images.dhan.co/api-data/api-scrip-master.csv",
                timeout=15)
            r.raise_for_status()
            reader = _csv.DictReader(_io.StringIO(r.text))
            count = 0
            for row in reader:
                # F&O rows: SEM_SEGMENT = 'D' (NSE F&O) or 'B' (BSE F&O)
                if (row.get("SEM_SEGMENT") not in ("D", "B")
                        or row.get("SEM_INSTRUMENT_NAME") not in ("OPTIDX", "OPTFUT", "OPTSTK")):
                    continue
                sid     = row.get("SEM_SMST_SECURITY_ID", "").strip()
                sym     = row.get("SEM_TRADING_SYMBOL", "").strip()   # e.g. NIFTY-Apr2026-24150-CE
                expiry  = row.get("SEM_EXPIRY_DATE", "").strip()      # e.g. 2026-04-21 14:30:00
                strike  = row.get("SEM_STRIKE_PRICE", "").strip()     # e.g. 24150.00000
                opt_t   = row.get("SEM_OPTION_TYPE", "").strip()      # CE / PE
                if not (sid and sym and expiry and strike and opt_t):
                    continue
                # Extract root from symbol (everything before first hyphen)
                root = sym.split("-")[0]
                date_str = expiry[:10]          # "2026-04-21"
                try:
                    strike_int = int(float(strike))
                except ValueError:
                    continue
                key = (root, date_str, strike_int, opt_t)
                self._dhan_scrip_cache[key] = sid
                count += 1
            self._dhan_scrip_loaded = True
            self.log(f"📡 Dhan scrip master loaded: {count} F&O option entries")
        except Exception as e:
            self.log(f"⚠️  Dhan scrip master load failed: {e}")

    def _lookup_dhan_token(self, tradingsymbol: str, exchange: str) -> str | None:
        """
        Look up Dhan security_id for an Angel One trading symbol.
        Parses the Angel symbol into (root, date, strike, type) and matches
        against the Dhan scrip master keyed by the same tuple.
        Returns the security_id string, or None if not found.
        """
        self._load_dhan_scrip_master()
        key = self._parse_angel_option_symbol(tradingsymbol)
        if key is None:
            self.log(f"⚠️  Dhan: cannot parse symbol '{tradingsymbol}'")
            return None
        sid = self._dhan_scrip_cache.get(key)
        if sid:
            print(f"Dhan token lookup: {tradingsymbol} → key={key} → {sid}")
        else:
            self.log(f"⚠️  Dhan: no match for {tradingsymbol} (key={key})")
        return sid

    def _dhan_exchange(self, angel_exchange):
        """Map Angel One exchange codes to Dhan exchange segment strings."""
        return {
            "NFO": "NSE_FNO",
            "BFO": "BSE_FNO",
            "NSE": "NSE",
            "BSE": "BSE",
            "MCX": "MCX",
        }.get(angel_exchange, "NSE_FNO")

    def _kotak_exchange(self, angel_exchange):
        """Map Angel One exchange codes to Kotak Neo format."""
        return {
            "NFO": "nse_fo",
            "BFO": "bse_fo",
            "NSE": "nse_cm",
            "BSE": "bse_cm",
            "MCX": "mcx_fo",
        }.get(angel_exchange, "nse_fo")

    def _is_monthly_expiry_from_master(self, sym, expiry_date):
        """Return True when expiry_date is the final listed expiry in that month."""
        try:
            month_expiries = set()
            for row in getattr(self, "instrument_master", []) or []:
                if row.get("name") != sym:
                    continue
                if row.get("exch_seg") not in ("NFO", "BFO"):
                    continue
                expiry = row.get("expiry")
                if not expiry:
                    continue
                try:
                    exp_date = dt.datetime.strptime(expiry.upper(), "%d%b%Y").date()
                except Exception:
                    continue
                if exp_date.year == expiry_date.year and exp_date.month == expiry_date.month:
                    month_expiries.add(exp_date)
            return bool(month_expiries) and expiry_date == max(month_expiries)
        except Exception:
            return False

    def _parse_kotak_date_part(self, sym, date_part):
        """Parse Kotak's date fragment into an expiry date when possible."""
        MONTH_NUM = {
            'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4,
            'MAY': 5, 'JUN': 6, 'JUL': 7, 'AUG': 8,
            'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
        }
        WEEK_MONTH_NUM = {
            '1': 1, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6,
            '7': 7, '8': 8, '9': 9, 'O': 10, 'N': 11, 'D': 12,
        }
        try:
            if len(date_part) == 7 and date_part[:2].isdigit():
                yy = int(date_part[:2])
                mmm = date_part[2:5]
                dd = int(date_part[5:7])
                month = MONTH_NUM.get(mmm)
                if month:
                    return dt.date(2000 + yy, month, dd)

            if len(date_part) == 5 and date_part[:2].isdigit() and date_part[3:].isdigit():
                yy = int(date_part[:2])
                month = WEEK_MONTH_NUM.get(date_part[2])
                dd = int(date_part[3:5])
                if month:
                    return dt.date(2000 + yy, month, dd)

            if len(date_part) == 5 and date_part[:2].isdigit():
                yy = int(date_part[:2])
                mmm = date_part[2:5]
                month = MONTH_NUM.get(mmm)
                if month:
                    month_expiries = []
                    for row in getattr(self, "instrument_master", []) or []:
                        if row.get("name") != sym or row.get("exch_seg") not in ("NFO", "BFO"):
                            continue
                        expiry = row.get("expiry")
                        if not expiry:
                            continue
                        try:
                            exp_date = dt.datetime.strptime(expiry.upper(), "%d%b%Y").date()
                        except Exception:
                            continue
                        if exp_date.year == 2000 + yy and exp_date.month == month:
                            month_expiries.append(exp_date)
                    return max(month_expiries) if month_expiries else None

        except Exception:
            return None
        return None

    def _find_kotak_symbol_in_cache(self, sym, expiry_date, strike, opt, nse_cache):
        """Find a same-expiry Kotak pTrdSymbol from the loaded scrip master."""
        suffix = f"{strike}{opt}"
        matches = []
        for kotak_sym in nse_cache:
            if not kotak_sym.startswith(sym) or not kotak_sym.endswith(suffix):
                continue
            date_part = kotak_sym[len(sym):-len(suffix)]
            parsed_expiry = self._parse_kotak_date_part(sym, date_part)
            if parsed_expiry == expiry_date:
                matches.append(kotak_sym)
        if not matches:
            return None
        matches.sort(key=len, reverse=True)
        return matches[0]

    def _angel_to_kotak_symbol(self, angel_symbol):
        """Convert Angel One option symbol to Kotak Neo format.

        NSE Angel    : NIFTY07APR2622950CE  → {SYMBOL}{DD}{MMM}{YY}{STRIKE}{TYPE}
        Kotak monthly: NIFTY26APR22950CE   → {SYMBOL}{YY}{MMM}{STRIKE}{TYPE}
        Kotak weekly : NIFTY26APR0722950CE → {SYMBOL}{YY}{MMM}{DD}{STRIKE}{TYPE}
                       Both use the full 3-letter month (confirmed from scrip master cache).

        BSE Angel monthly: SENSEX26APR76500CE → {SYMBOL}{YY}{MMM}{STRIKE}{TYPE}
        Kotak BSE monthly: SENSEX26APR76500CE → same format (no conversion needed)

        MCX (CRUDEOIL etc.): monthly-only, no weekly series.
        Kotak MCX monthly  : CRUDEOIL26APR5000CE → {SYMBOL}{YY}{MMM}{STRIKE}{TYPE}
        """
        import re
        MONTH_NUM = {
            'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4,
            'MAY': 5, 'JUN': 6, 'JUL': 7, 'AUG': 8,
            'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
        }
        # MCX underlyings have only monthly options — never a weekly series.
        MCX_UNDERLYINGS = {
            "CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATURALGASM",
            "GOLD", "GOLDM", "GOLDPETAL", "SILVER", "SILVERM",
            "COPPER", "ZINC", "LEAD", "ALUMINIUM", "NICKEL",
        }

        m = re.match(r'^([A-Z]+)(\d{2})([A-Z]{3})(\d{2})(\d+)(CE|PE)$', angel_symbol)
        if not m:
            return angel_symbol   # unrecognised format — pass through unchanged

        sym, dd, mmm, yy, strike, opt = m.groups()
        month_num = MONTH_NUM.get(mmm)
        if not month_num:
            return angel_symbol

        day  = int(dd)
        year = 2000 + int(yy)

        # ── MCX path: Kotak pTrdSymbol uses the same format as Angel One ──
        # Confirmed from mcx_fo scrip master: "CRUDEOIL14MAY264600CE"
        # = {SYM}{DD}{MMM}{YY}{STRIKE}{TYPE} — identical to Angel, no conversion needed.
        if sym in MCX_UNDERLYINGS:
            return angel_symbol

        # ── BSE monthly format detection ───────────────────────────
        # The regex cannot tell BSE {SYMBOL}{YY}{MMM}{STRIKE} from NSE
        # {SYMBOL}{DD}{MMM}{YY}{STRIKE} because the BSE 5-digit strike's
        # first two digits land in the YY slot, giving year > 2040.
        if year > 2040:
            actual_strike = yy + strike          # e.g. "76" + "500" → "76500"
            return f"{sym}{dd}{mmm}{actual_strike}{opt}"

        expiry_date = dt.date(year, month_num, day)
        weekly = f"{sym}{yy}{mmm}{dd}{strike}{opt}"
        candidates = [weekly]
        if self._is_monthly_expiry_from_master(sym, expiry_date):
            monthly = f"{sym}{yy}{mmm}{strike}{opt}"
            candidates.insert(0, monthly)

        nse_cache = getattr(self, "_kotak_scrip_cache", {}).get("nse_fo", {})
        if nse_cache:
            for candidate in candidates:
                if candidate in nse_cache:
                    return candidate
            cache_match = self._find_kotak_symbol_in_cache(
                sym, expiry_date, strike, opt, nse_cache)
            if cache_match:
                self.log(f"   Kotak symbol resolved from cache: {cache_match}")
                return cache_match
            self.log(f"⚠️  Kotak exact symbol not found for {angel_symbol}; "
                     f"tried {', '.join(candidates)}")
            return None
        return candidates[0]

        # ── NSE path ──────────────────────────────────────────────
        # Kotak NSE_FO pTrdSymbol format (confirmed from 101k-entry scrip master):
        #   Monthly: {SYMBOL}{YY}{MMM}{STRIKE}{TYPE}     e.g. NIFTY26APR23900CE
        #   Weekly : {SYMBOL}{YY}{MMM}{DD}{STRIKE}{TYPE} e.g. NIFTY26APR2823900CE
        #
        # Edge case: when a monthly expiry is moved due to a holiday (e.g. last-Thu=30
        # but market closed → actual expiry=28), Angel encodes the real date (DD=28)
        # while Kotak's scrip master still records the monthly format (no DD).
        # Solution: generate both candidates, then validate against the cache.
        _, last_day = calendar.monthrange(year, month_num)
        last_thu = max(d for d in range(1, last_day + 1)
                       if dt.date(year, month_num, d).weekday() == 3)

        if day == last_thu:
            primary  = f"{sym}{yy}{mmm}{strike}{opt}"       # monthly (no DD)
            fallback = f"{sym}{yy}{mmm}{dd}{strike}{opt}"   # weekly  (with DD)
        else:
            primary  = f"{sym}{yy}{mmm}{dd}{strike}{opt}"   # weekly  (with DD)
            fallback = f"{sym}{yy}{mmm}{strike}{opt}"       # monthly (no DD) — holiday-moved expiry

        # Validate against the pre-downloaded Kotak scrip master.
        # If the primary symbol isn't known to Kotak, the fallback format is correct.
        nse_cache = getattr(self, "_kotak_scrip_cache", {}).get("nse_fo", {})
        if nse_cache:
            if primary not in nse_cache and fallback in nse_cache:
                return fallback
        return primary
    # ==========================================================
    # MANUAL PUNCH TRADE  (from Strike LTP panel buttons)
    # ==========================================================
    def manual_punch_trade(self, token, transaction_type, transaction_type_label=None):
        """
        Place an immediate BUY or SELL — from the Option Chain B/S buttons or
        (legacy) the old Strike LTP panel buttons.
        Uses the per-row lots spinbox to determine quantity.
        BUY  → opens a tracked LONG trade (calls open_trade) if none is open,
               otherwise closes an existing tracked SHORT.
        SELL → closes an existing tracked trade (LONG or SHORT) if one is
               open, otherwise opens a tracked SHORT position (calls
               open_short_trade) so a naked write gets live P&L, SL/Target
               management and lot-adjust just like a bought leg.
        """
        state = self.strike_state.get(token)
        if not state:
            messagebox.showwarning("Manual Trade",
                                   "Strike not found — add it first.")
            return

        # Update lot_size from per-row spinbox
        lots_var = self.strike_row_lots_vars.get(token)
        if lots_var:
            new_lots = max(1, lots_var.get())
            state["lot_size"] = self.get_lot_size() * new_lots

        ltp = self._resolve_live_ltp(token)

        if transaction_type == "BUY":
            if state.get("trade_open"):
                if state.get("direction") == "SHORT":
                    # Buy-to-cover the existing short leg.
                    self.manual_close_trade(token)
                    return
                messagebox.showinfo("Manual Trade",
                                    "A trade is already open for this strike.")
                return
            if ltp <= 0:
                messagebox.showwarning("Manual Trade",
                                       "No live LTP yet — cannot price the entry.")
                return
            import threading
            threading.Thread(
                target=lambda: self.open_trade(token, ltp),
                daemon=True, name="manual-buy").start()
            self.log(f"🟢 Manual BUY punched: {state['tradingsymbol']} "
                     f"@ {ltp}  qty={state['lot_size']}")

        else:  # SELL
            if state.get("trade_open"):
                # Close the existing tracked position (works for both a
                # bought LONG leg and a previously-sold SHORT leg — direction
                # is read off strike_state so the right side gets squared).
                self.manual_close_trade(token)
                return

            # No open trade yet — a naked SELL opens a tracked SHORT position
            # so it gets live P&L, SL/Target management and lot-adjust just
            # like a bought leg (previously this was a display-only row with
            # no live tracking at all).
            if ltp <= 0:
                messagebox.showwarning("Manual Trade",
                                       "No live LTP yet — cannot price the entry.")
                return
            import threading
            threading.Thread(
                target=lambda: self.open_short_trade(token, ltp),
                daemon=True, name="manual-sell").start()
            self.log(f"🔴 Manual SELL punched (open short): {state['tradingsymbol']} "
                     f"@ {ltp}  qty={state['lot_size']}")

    # ==========================================================
    # ----------------------------------------------------------
    def _get_candle_sl_target(self, token, price):
        """Return (sl_price, target_price) from candle data for PRICE_BAND mode.

        Returns (None, None) if mode is 'points' or data is unavailable.
        """
        mode = self.candle_sl_mode_var.get()
        if mode == "points":
            return None, None

        df = self.ce.get_candles(token) if self.ce else None
        if df is None or len(df) < 2:
            return None, None

        target_tf = self.sl_candle_tf_var.get()
        df = _resample_candles(df, target_tf)
        if df is None or len(df) < 2:
            return None, None

        prev = df.iloc[-2]   # last fully closed candle at target_tf

        if mode == "prev_ohlc":
            sl_field  = self.sl_ohlc_field_var.get()
            tgt_field = self.target_ohlc_field_var.get()
            return float(prev[sl_field]), float(prev[tgt_field])

        elif mode == "pct_entry":
            sl_pct  = self.sl_pct_entry_var.get() / 100
            tgt_pct = self.target_pct_entry_var.get() / 100
            return round(price * (1 - sl_pct), 2), round(price * (1 + tgt_pct), 2)

        elif mode == "swing_low":
            n = self.swing_lookback_var.get()
            window = df.iloc[-(n + 1):-1]
            if window.empty:
                return None, None
            return round(float(window["low"].min()), 2), None   # target stays as points

        return None, None

    # ==========================================================
    # DAY PROFILE — Config Panel "Order Params" applied to every trade
    # ==========================================================
    def _get_day_profile_sl_target(self):
        """SL/Target points from the Config Panel's Order Params, i.e. the
        Day Profile (save_profile_config/load_profile_config) — what every
        live/paper trade actually uses unless a manual per-trade override
        is set. The Strategy Configuration panel's own SL/Target fields
        remain independent and feed only the backtest Strategy Builder."""
        try:
            sl_points = float(self.cfg_sl_pts_var.get())
        except (TypeError, ValueError):
            sl_points = 0
        try:
            target_points = float(self.cfg_target_pts_var.get())
        except (TypeError, ValueError):
            target_points = 0
        return sl_points, target_points

    def _max_price_blocked(self, price):
        """True if Order Params 'MaxPrice' is set (>0) and price exceeds it."""
        try:
            max_price = float(self.cfg_max_price_var.get())
        except (TypeError, ValueError):
            max_price = 0
        return max_price > 0 and price > max_price

    def _enforce_order_wait(self):
        """Order Params 'Wait' — minimum seconds between consecutive order
        placements. Blocks (sleeps) the calling thread if called again too
        soon; order placement always runs off the Tk main thread so this
        never freezes the GUI."""
        try:
            wait_s = float(self.cfg_wait_var.get())
        except (TypeError, ValueError):
            wait_s = 0
        if wait_s <= 0:
            return
        with self.lock:
            last = getattr(self, "_last_order_time", 0)
            now  = _time_mod.time()
            elapsed = now - last
            if elapsed < wait_s:
                self._last_order_time = last + wait_s
            else:
                self._last_order_time = now
        remaining = self._last_order_time - now
        if remaining > 0:
            _time_mod.sleep(remaining)

    # OPEN TRADE
    # ==========================================================
    def open_trade(self, token, price, broker=None):
        with self.lock:
            state = self.strike_state.get(token)
            if not state or state["order_in_progress"]:
                return
            state["order_in_progress"] = True

        # Risk kill switch — refuse new entries when a limit is breached
        if self.risk_entry_blocked():
            with self.lock:
                s = self.strike_state.get(token)
                if s:
                    s["order_in_progress"] = False
            self.log(f"⛔ Entry blocked by risk limit: {self._risk_halt_reason or 'limit reached'}")
            return

        # Order Params: MaxPrice — refuse entries priced above the cap
        if self._max_price_blocked(price):
            with self.lock:
                s = self.strike_state.get(token)
                if s:
                    s["order_in_progress"]    = False
                    s["entry_taken_today"]    = False
                    s["entry_band_triggered"] = False
            self.log(f"⛔ Entry skipped {token}: price {price} exceeds MaxPrice "
                     f"{self.cfg_max_price_var.get()}")
            return

        # Manual overrides take priority (set by place_manual_trade)
        if getattr(self, "_manual_sl_override", None) is not None:
            sl_points     = float(self._manual_sl_override)
            target_points = float(getattr(self, "_manual_tgt_override", 0) or 0)
        else:
            sl_points, target_points = self._get_day_profile_sl_target()

        # ── Compute SL / targets BEFORE placing the order so we never open a
        #    position without a valid protective stop (validate-then-buy). ──

        # Candle-based SL/Target override (PRICE_BAND only)
        if self.entry_mode_var.get() == "PRICE_BAND":
            c_sl, c_tgt = self._get_candle_sl_target(token, price)
            if c_sl is not None:
                sl_points = price - c_sl
            if c_tgt is not None:
                target_points = c_tgt - price

        # GExp override — when enabled, ALWAYS apply the configured SL%/R:R.
        # The gamma_expansion_detector no longer gates whether the stop exists
        # (previously a False result fell back to sl = entry → instant stop-out);
        # it is kept only as an informational signal.
        df_now = self.ce.get_candles(token) if self.ce else None
        use_gexp = bool(self.gexp_override_var.get())
        if use_gexp and df_now is not None:
            try:
                gexp_signal = self.gamma_expansion_detector(df_now)
                self.log(f"ℹ️  GExp override ON ({self.gexp_method_var.get()}) — "
                         f"gamma-expansion signal={gexp_signal}")
            except Exception:
                pass

        if use_gexp:
            method = self.gexp_method_var.get()
            if method == "approach1":
                sl_pct  = self.gexp_sl_pct_var.get() / 100
                rr      = self.gexp_rr_var.get()
                sl      = round(price * (1 - sl_pct), 2)
                risk    = price - sl
                targets = [round(price + risk * rr, 2)]
            else:
                gexp_step = self.gexp_tsl_step_var.get()
                sl        = round(price - gexp_step, 2)
                targets   = []
        else:
            sl      = price - sl_points
            targets = [price + target_points] if target_points > 0 else []

        # VWAP Adaptive — override SL to lower1, targets to [upper1, upper2]
        if self.candle_sl_mode_var.get() == "vwap_adaptive" and not use_gexp:
            df_va = self.ce.get_candles(token) if self.ce else None
            if df_va is not None and len(df_va) > 0:
                import math
                lv  = df_va.iloc[-1]
                _l1 = lv.get("vwap_lower1", None)
                _u1 = lv.get("vwap_upper1", None)
                _u2 = lv.get("vwap_upper2", None)
                if all(v is not None and not math.isnan(float(v))
                       for v in [_l1, _u1, _u2]):
                    sl      = float(_l1)
                    targets = [float(_u1), float(_u2)]

        # ── Validate the stop BEFORE placing the order ──────────────────
        # A stop at/above entry has no downside room and would stop out on the
        # next downtick (the instant-close bug). Skip the entry instead.
        if sl is None or sl >= price:
            self.log(f"⛔ Entry skipped {token}: no valid stop (sl={sl}, entry={price}). "
                     f"Set SL Points > 0 or enable GExp.")
            with self.lock:
                s = self.strike_state.get(token)
                if s:
                    s["order_in_progress"]    = False
                    s["entry_taken_today"]    = False   # allow a later valid signal to re-enter
                    s["entry_band_triggered"] = False
            return

        # Stop is valid — now place the BUY order.
        resp = self._place_order_chunked(token, "BUY", broker=broker)
        if not resp:
            print("Order rejected.")
            with self.lock:
                state = self.strike_state.get(token)
                if state:
                    state["order_in_progress"]    = False
                    state["entry_taken_today"]    = False   # allow re-entry after API recovery
                    state["entry_band_triggered"] = False
            return

        with self.lock:
            state = self.strike_state[token]
            state.update({
                "trade_open":           True,
                "direction":            "LONG",
                "entry_band_triggered": False,
                "entry_taken_today":    True,
                "order_in_progress":    False,
                "entry_price":          price,
                "sl":                   sl,
                "highest_price":        price,
                "targets":              targets,
                "targets_hit":          [],
                "adj_count":            0,
                "gexp_override":        use_gexp,
                "gexp_method":          self.gexp_method_var.get() if use_gexp else "none",
                "gexp_tsl_step":        self.gexp_tsl_step_var.get() if use_gexp else 0,
            })
            self.trades_today += 1

        entry_time = dt.datetime.now()
        ema = rsi = vwap = sd1u = sd1l = vol = None
        if df_now is not None and len(df_now) > 0:
            last = df_now.iloc[-1]
            ema  = last.get("ema")
            rsi  = last.get("rsi")
            vwap = last.get("vwap")
            sd1u = last.get("vwap_upper1")
            sd1l = last.get("vwap_lower1")
            vol  = last.get("volume")

        # Actual fill price may differ from signal LTP — capture both for slippage tracking
        signal_ltp = price
        self.running_positions[token] = {
            "Date":                entry_time.date(),
            "Token":               token,
            "Symbol":              f"{self.index_var.get()}{state['strike']}{state['type']}",
            "Direction":           "LONG",
            "SL_Points":           sl_points,
            "Target_Points":       target_points,
            "EntryTime":           entry_time,
            "EntryPrice":          price,
            "IntendedEntryPrice":  signal_ltp,
            "Qty":                 state["lot_size"],
            "EMA":                 ema,
            "RSI":                 rsi,
            "VWAP":                vwap,
            "VWAP_SD1_UP":         sd1u,
            "VWAP_SD1_DOWN":       sd1l,
            "EntryVolume":         vol,
        }
        # Schedule UI work on the main thread — widget creation and
        # event bindings are not safe to call from a background thread.
        self.root.after(0, lambda t=token, p=price: self.add_trade_row(t, p))
        self.root.after(0, lambda: self.status_var.set(f"{token} BUY @ {price}"))
        self._save_state()

    # OPEN SHORT TRADE (naked sell from Option Chain)
    # ==========================================================
    def open_short_trade(self, token, price, broker=None):
        """
        Opens a tracked SHORT (sold) position. Mirrors open_trade() but
        inverts the SL/Target geometry (stop above entry, target below
        entry) and is closed with a BUY instead of a SELL. This is what
        gives a naked SELL punched from the Option Chain panel live P&L,
        SL/Target management and lot-adjust — the same as a bought leg.
        """
        with self.lock:
            state = self.strike_state.get(token)
            if not state or state["order_in_progress"]:
                return
            state["order_in_progress"] = True

        if self.risk_entry_blocked():
            with self.lock:
                s = self.strike_state.get(token)
                if s:
                    s["order_in_progress"] = False
            self.log(f"⛔ Entry blocked by risk limit: {self._risk_halt_reason or 'limit reached'}")
            return

        if self._max_price_blocked(price):
            with self.lock:
                s = self.strike_state.get(token)
                if s:
                    s["order_in_progress"]    = False
                    s["entry_taken_today"]    = False
                    s["entry_band_triggered"] = False
            self.log(f"⛔ Short entry skipped {token}: price {price} exceeds MaxPrice "
                     f"{self.cfg_max_price_var.get()}")
            return

        if getattr(self, "_manual_sl_override", None) is not None:
            sl_points     = float(self._manual_sl_override)
            target_points = float(getattr(self, "_manual_tgt_override", 0) or 0)
        else:
            sl_points, target_points = self._get_day_profile_sl_target()

        sl      = price + sl_points
        targets = [price - target_points] if target_points > 0 else []

        # Stop must sit ABOVE entry for a short — a stop at/below entry has
        # no room and would cover the position on the very next uptick.
        if sl <= price:
            self.log(f"⛔ Short entry skipped {token}: no valid stop (sl={sl}, entry={price}). "
                     f"Set SL Points > 0.")
            with self.lock:
                s = self.strike_state.get(token)
                if s:
                    s["order_in_progress"]    = False
                    s["entry_taken_today"]    = False
                    s["entry_band_triggered"] = False
            return

        # Stop is valid — now place the SELL (write) order.
        resp = self._place_order_chunked(token, "SELL", broker=broker)
        if not resp:
            print("Order rejected.")
            with self.lock:
                state = self.strike_state.get(token)
                if state:
                    state["order_in_progress"]    = False
                    state["entry_taken_today"]    = False
                    state["entry_band_triggered"] = False
            return

        with self.lock:
            state = self.strike_state[token]
            state.update({
                "trade_open":           True,
                "direction":            "SHORT",
                "entry_band_triggered": False,
                "entry_taken_today":    True,
                "order_in_progress":    False,
                "entry_price":          price,
                "sl":                   sl,
                "lowest_price":         price,
                "targets":              targets,
                "targets_hit":          [],
                "adj_count":            0,
            })
            self.trades_today += 1

        entry_time = dt.datetime.now()
        self.running_positions[token] = {
            "Date":               entry_time.date(),
            "Token":              token,
            "Symbol":             f"{self.index_var.get()}{state['strike']}{state['type']}",
            "Direction":          "SHORT",
            "SL_Points":          sl_points,
            "Target_Points":      target_points,
            "EntryTime":          entry_time,
            "EntryPrice":         price,
            "IntendedEntryPrice": price,
            "Qty":                state["lot_size"],
            "EMA": None, "RSI": None, "VWAP": None,
            "VWAP_SD1_UP": None, "VWAP_SD1_DOWN": None, "EntryVolume": None,
        }
        self.root.after(0, lambda t=token, p=price:
                         self.add_trade_row(t, p, transaction_type="SELL"))
        self.root.after(0, lambda: self.status_var.set(f"{token} SELL @ {price}"))
        self._save_state()

    def add_trade_row(self, token, entry_price, transaction_type="BUY"):
        data       = self.strike_state[token]
        entry_time = dt.datetime.now().strftime("%H:%M:%S")
        hedge_group_id = data.get("hedge_group_id")
        index_name = self.index_var.get().upper()
        instrument = f"{index_name} {data['strike']} {data['type']}"
        if hedge_group_id:
            instrument += "  🛡"

        # Defensive: if a previous row for this token is still flagged live
        # (e.g. an accidental re-entry while open), retire it to the Closed
        # section so it cannot be orphaned by the new row below.
        prev = self._active_row(token)
        if prev is not None and prev.get("status") == "RUNNING":
            prev["status"] = "CLOSED"
            try:
                prev["labels"][9].config(text="Closed")
            except Exception:
                pass
            dim_bg = "#2a2a2a"
            for w in prev["labels"]:
                try:
                    w._theme_orig_bg = dim_bg
                    disp_bg = dim_bg
                    if getattr(self, "current_theme", "dark") == "light":
                        disp_bg = self._invert_color(w, dim_bg, is_text=False)
                    w.config(bg=disp_bg)
                except Exception:
                    pass
            self._move_row_to_closed(prev)

        row = tk.Frame(self.trade_inner, bg="#1f2a38")
        # Live rows sit above the Closed divider.
        row.pack(fill="x", pady=1, before=self._closed_divider)
        tgt_disp = round(data["targets"][0], 2) if data["targets"] else 0
        sl_disp  = round(data["sl"], 2) if data.get("sl") is not None else 0

        labels  = []
        # weight=0 everywhere (matches header_frame) — columns stay compact
        # instead of stretching to fill the full window width.
        weights = [0] * 13
        # Shared with header_frame in gui_builder.py — same explicit
        # character widths + minsize keep header and row columns aligned
        # regardless of how long any individual cell's text is.
        cw = getattr(self, "_trade_col_widths", [9, 6, 19, 6, 7, 7, 7, 7, 7, 9, 12, 18, 26])
        for i in range(len(weights)):
            row.grid_columnconfigure(i, weight=weights[i], minsize=cw[i] * 7)
        row.grid_columnconfigure(len(weights), weight=1)   # trailing spacer

        # Tracks the last-committed (sent-to-broker) value for each editable
        # field so we can tell the difference between "typed but not yet
        # applied" (dirty — starred + amber border) and "in sync".
        committed = {"qty": None, "sl": None, "tgt": None}

        # Columns 0-2: Time, Type, Strike (static labels)
        is_unhedged_short = (transaction_type == "SELL" and not hedge_group_id)

        # Col 0: Time
        lbl_time = tk.Label(row, text=entry_time, bg="#1f2a38",
                            fg="#00e676", font=("Segoe UI", 9),
                            width=cw[0], anchor="center")
        lbl_time.grid(row=0, column=0, sticky="nsew", padx=2, pady=1)
        labels.append(lbl_time)

        # Col 1: Type — single Label keeps grid column alignment intact.
        # For an unhedged SELL the ▲ symbol is embedded in the text; _theme_keep_fg
        # prevents _invert_color from darkening the red fg in light theme.
        type_text = "▲ SELL" if is_unhedged_short else transaction_type
        type_fg   = "#ff1744" if is_unhedged_short else (
                    "#ff5252" if transaction_type == "SELL" else "#00e676")
        lbl_type  = tk.Label(row, text=type_text, bg="#1f2a38",
                             fg=type_fg, font=("Segoe UI", 9),
                             width=cw[1], anchor="center")
        if is_unhedged_short:
            lbl_type._theme_keep_fg = True  # bg inverts to light; fg stays red
        lbl_type.grid(row=0, column=1, sticky="nsew", padx=2, pady=1)
        labels.append(lbl_type)  # labels[1]

        # Col 2: Instrument
        lbl_inst = tk.Label(row, text=instrument, bg="#1f2a38",
                            fg="#00e676", font=("Segoe UI", 9),
                            width=cw[2], anchor="center")
        lbl_inst.grid(row=0, column=2, sticky="nsew", padx=2, pady=1)
        labels.append(lbl_inst)

        def _make_editable_cell(col, key, var, fg, entry_bg, width):
            """Entry + dirty-state marker ('*' and amber border) in one
            grid cell. Dirty clears only once _apply_sl_tgt_edit() actually
            commits the value (Enter / focus-out)."""
            cell = tk.Frame(row, bg="#1f2a38")
            cell.grid(row=0, column=col, sticky="nsew", padx=2, pady=1)
            entry = tk.Entry(cell, textvariable=var, width=width,
                             bg=entry_bg, fg=fg, insertbackground="white",
                             font=("Segoe UI", 9), justify="center",
                             highlightthickness=1, highlightbackground="#37474f",
                             highlightcolor="#37474f")
            entry.pack(side="left", fill="x", expand=True)
            marker = tk.Label(cell, text="", bg="#1f2a38", fg="#ffd740",
                              font=("Segoe UI", 9, "bold"), width=1)
            marker.pack(side="left")

            def _refresh_dirty(*_a):
                dirty = str(var.get()) != committed.get(key)
                try:
                    entry.config(highlightthickness=2 if dirty else 1,
                                 highlightbackground="#ffd740" if dirty else "#37474f",
                                 highlightcolor="#ffd740" if dirty else "#37474f")
                    marker.config(text="*" if dirty else "")
                except Exception:
                    pass

            def _commit(e=None, t=token):
                ok = self._apply_sl_tgt_edit(t)
                if ok:
                    committed["qty"] = str(qty_var.get())
                    committed["sl"]  = str(sl_var.get())
                    committed["tgt"] = str(tgt_var.get())
                    for m in (qty_marker, sl_marker, tgt_marker):
                        m.config(text="")
                    for en in (qty_entry, sl_entry, tgt_entry):
                        en.config(highlightthickness=1, highlightbackground="#37474f",
                                  highlightcolor="#37474f")

            entry.bind("<KeyRelease>", _refresh_dirty)
            entry.bind("<Return>",     _commit)
            entry.bind("<FocusOut>",   _commit)
            return entry, marker

        # Column 3 — Lot (editable Entry — displays number of lots, not raw qty)
        per_lot   = self.get_lot_size()
        init_lots = data["lot_size"] // per_lot if per_lot > 0 else 1
        qty_var   = tk.StringVar(value=str(init_lots))
        committed["qty"] = str(init_lots)
        qty_entry, qty_marker = _make_editable_cell(3, "qty", qty_var, "#ffd740", "#1a2030", cw[3])
        labels.append(qty_entry)   # index 3

        # Columns 4-5: Entry price, LTP (static labels)
        for i, val in enumerate([entry_price, entry_price], start=4):
            lbl = tk.Label(row, text=str(val), bg="#1f2a38",
                           fg="#00e676", font=("Segoe UI", 9), width=cw[i], anchor="center")
            lbl.grid(row=0, column=i, sticky="nsew", padx=2, pady=1)
            labels.append(lbl)

        # Column 6 — SL (editable Entry)
        sl_var = tk.StringVar(value=str(sl_disp))
        committed["sl"] = str(sl_disp)
        sl_entry, sl_marker = _make_editable_cell(6, "sl", sl_var, "#ff5252", "#2a1a1a", cw[6])
        labels.append(sl_entry)   # index 6

        # Column 7 — Target (editable Entry)
        tgt_var = tk.StringVar(value=str(tgt_disp))
        committed["tgt"] = str(tgt_disp)
        tgt_entry, tgt_marker = _make_editable_cell(7, "tgt", tgt_var, "#00e676", "#1a2a1a", cw[7])
        labels.append(tgt_entry)  # index 7

        # Columns 8-9: PnL, Status
        for j, val in enumerate(["0.00", "Open"], start=8):
            lbl = tk.Label(row, text=str(val), bg="#1f2a38",
                           fg="#00e676", font=("Segoe UI", 9), width=cw[j], anchor="center")
            lbl.grid(row=0, column=j, sticky="nsew", padx=2, pady=1)
            labels.append(lbl)

        # Column 10 — Adj Lot controls [− | n | +]
        adj_frame   = tk.Frame(row, bg="#1f2a38")
        adj_frame.grid(row=0, column=10, sticky="nsew", padx=2, pady=1)
        adj_lot_var = tk.StringVar(value="1")
        tk.Button(adj_frame, text="−", width=2,
                  bg="#37474f", fg="white",
                  font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
                  command=lambda t=token: self._adjust_lot(t, -1)
                  ).pack(side="left", padx=1)
        tk.Entry(adj_frame, textvariable=adj_lot_var, width=3,
                 bg="#1a2030", fg="#ffd740",
                 insertbackground="white",
                 font=("Segoe UI", 9), justify="center"
                 ).pack(side="left", padx=1)
        tk.Button(adj_frame, text="+", width=2,
                  bg="#1b5e20", fg="white",
                  font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
                  command=lambda t=token: self._adjust_lot(t, 1)
                  ).pack(side="left", padx=1)

        # Roll (column 11) — two side-by-side sub-frames so Roll 1 and Roll 2
        # appear as visually separate columns inside the single grid column.
        roll_frame = tk.Frame(row, bg="#1f2a38")
        roll_frame.grid(row=0, column=11, sticky="nsew", padx=2, pady=1)

        # ── Roll 1 sub-frame (1-step move) — Up and Down side by side ──
        # Label "Roll 1" is shown in the header by _refresh_roll_columns, not here.
        roll1_sub = tk.Frame(roll_frame, bg="#1f2a38")
        roll1_btn_row = tk.Frame(roll1_sub, bg="#1f2a38")
        roll1_btn_row.pack(fill="x")
        roll1_up_btn = tk.Button(
            roll1_btn_row, text="▲ Up", width=5,
            bg="#1565c0", fg="white",
            font=("Segoe UI", 7, "bold"), relief="flat", cursor="hand2",
            command=lambda t=token: self.roll_position(t, "up", steps=1))
        roll1_up_btn.pack(side="left", padx=(0, 1))
        roll1_dn_btn = tk.Button(
            roll1_btn_row, text="▼ Dn", width=5,
            bg="#6a1b9a", fg="white",
            font=("Segoe UI", 7, "bold"), relief="flat", cursor="hand2",
            command=lambda t=token: self.roll_position(t, "down", steps=1))
        roll1_dn_btn.pack(side="left")

        # ── Roll 2 sub-frame (2-step move) — Up and Down side by side ──
        # Label "Roll 2" is shown in the header by _refresh_roll_columns, not here.
        roll2_sub = tk.Frame(roll_frame, bg="#1f2a38")
        roll2_btn_row = tk.Frame(roll2_sub, bg="#1f2a38")
        roll2_btn_row.pack(fill="x")
        roll2_up_btn = tk.Button(
            roll2_btn_row, text="▲ Up×2", width=5,
            bg="#0d47a1", fg="white",
            font=("Segoe UI", 7, "bold"), relief="flat", cursor="hand2",
            command=lambda t=token: self.roll_position(t, "up", steps=2))
        roll2_up_btn.pack(side="left", padx=(0, 1))
        roll2_dn_btn = tk.Button(
            roll2_btn_row, text="▼ Dn×2", width=5,
            bg="#4a0072", fg="white",
            font=("Segoe UI", 7, "bold"), relief="flat", cursor="hand2",
            command=lambda t=token: self.roll_position(t, "down", steps=2))
        roll2_dn_btn.pack(side="left")

        # Apply initial visibility based on current checkbox state
        _r1 = getattr(self, "roll_pos1_var", None)
        _r2 = getattr(self, "roll_pos2_var", None)
        if _r1 and _r1.get():
            roll1_sub.pack(side="left", fill="both", expand=True, padx=(0, 2))
        if _r2 and _r2.get():
            roll2_sub.pack(side="left", fill="both", expand=True)

        # Close (column 12) — partial-close buttons replace the single
        # "✕ Close" button so the user can exit 25/50/75/100% of the lot.
        close_frame = tk.Frame(row, bg="#1f2a38")
        close_frame.grid(row=0, column=12, sticky="nsew", padx=2, pady=1)
        close_btns = {}
        for pct in (25, 50, 75, 100):
            b = tk.Button(
                close_frame, text=f"{pct}%", width=4,
                bg="#c62828" if pct == 100 else "#7a2020", fg="white",
                font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2",
                command=lambda t=token, p=pct: self.manual_close_trade_pct(t, p / 100))
            b.pack(side="left", padx=1)
            close_btns[pct] = b

        row_id = self._row_seq
        self._row_seq += 1
        self.trade_rows[row_id] = {
            "row":          row,
            "token":        token,
            "labels":       labels,
            "qty_var":      qty_var,
            "sl_var":       sl_var,
            "tgt_var":      tgt_var,
            "adj_lot_var":  adj_lot_var,
            "entry_price":  entry_price,
            "status":       "RUNNING",
            "close_btns":   close_btns,
            "roll_frame":   roll_frame,
            "roll1_sub":    roll1_sub,
            "roll2_sub":    roll2_sub,
            "roll1_btns":   (roll1_up_btn, roll1_dn_btn),
            "roll2_btns":   (roll2_up_btn, roll2_dn_btn),
        }
        self._active_row_id[token] = row_id
        # Grow trade panel up to 6 rows, then scroll
        self.root.after(0, self._update_trade_panel_height)
        # Refresh hedge button in case this is a hedge leg
        self.root.after(0, self.update_hedge_btn)
        # Freshly-built row always uses dark-theme literal colors — repaint
        # to match if the app is currently in light mode.
        self._theme_repaint_subtree(row)

    def _apply_sl_tgt_edit(self, token):
        """Apply user-typed Qty / SL / Target values to a live trade."""
        row_data = self._active_row(token)
        st       = self.strike_state.get(token)
        if not row_data or not st or not st.get("trade_open"):
            return
        try:
            new_sl  = float(row_data["sl_var"].get())
            new_tgt = float(row_data["tgt_var"].get())
            new_qty = int(float(row_data["qty_var"].get()))
        except (ValueError, TypeError):
            return   # invalid input — ignore silently

        # ── Quantity increase (add-on BUY) ──────────────────────
        old_qty   = st["lot_size"]
        extra_qty = new_qty - old_qty
        if extra_qty > 0:
            # Adding to a SHORT means selling more; a LONG adds via BUY.
            add_side = "SELL" if st.get("direction") == "SHORT" else "BUY"
            def _add_qty(eq=extra_qty, nq=new_qty, side=add_side):
                self.place_order(token, side, qty_override=eq)
                with self.lock:
                    st["lot_size"] = nq
                    if token in self.running_positions:
                        self.running_positions[token]["Qty"] = nq
                self.log(f"➕ {token}: +{eq} qty added — total {nq}")
            import threading
            threading.Thread(target=_add_qty, daemon=True,
                             name="add-qty").start()
        elif extra_qty < 0:
            # Partial exit not supported here — reset field to current qty
            row_data["qty_var"].set(str(old_qty))
            self.log(f"⚠️  To reduce qty use ✕ Close. Qty reset to {old_qty}.")
            return

        # ── SL / Target update — capped by Order Params 'MaxAdj' ──────
        old_sl  = st.get("sl")
        old_tgt = st["targets"][0] if st.get("targets") else None
        changed = (new_sl != old_sl) or (new_tgt != old_tgt)

        if changed:
            try:
                max_adj = int(self.cfg_max_adj_var.get())
            except (TypeError, ValueError):
                max_adj = 0
            if max_adj > 0 and st.get("adj_count", 0) >= max_adj:
                row_data["sl_var"].set(str(old_sl if old_sl is not None else 0))
                row_data["tgt_var"].set(str(old_tgt if old_tgt is not None else 0))
                self.log(f"⚠️  {token}: MaxAdj ({max_adj}) reached — SL/Target edit rejected")
                return False

        with self.lock:
            st["sl"]      = new_sl
            st["targets"] = [new_tgt]
            if changed:
                st["adj_count"] = st.get("adj_count", 0) + 1
        self.log(f"✏️  {token}: SL → {new_sl}  Target → {new_tgt}")
        return True

    def manual_close_trade(self, token):
        """Called when the user clicks ✕ Close on an active trade row."""
        with self.lock:
            st = self.strike_state.get(token)
            if not st or not st.get("trade_open"):
                self.log(f"⚠️  No open trade to close for token {token}")
                return
            if st.get("close_in_progress"):
                return
            st["close_in_progress"] = True

        ltp = st.get("ltp") or st.get("entry_price", 0)
        self.log(f"🔴 Manual close requested — {st.get('tradingsymbol','')}")

        # ── Disable buttons IMMEDIATELY in the main (Tkinter) thread ─────────
        # This must happen before we spin off the background thread so the user
        # cannot click twice during the order-placement latency window.
        row_data = self._active_row(token)
        if row_data and "close_btns" in row_data:
            for b in row_data["close_btns"].values():
                b.config(state="disabled", bg="#555555")

        # ── Offload order + cleanup to a daemon thread ────────────────────────
        # place_order() makes a synchronous HTTP call (Angel One REST API) that
        # can block for 3–30 seconds depending on broker response time.  If
        # called directly here (Tkinter button-click handler = main thread) the
        # entire GUI freezes for that duration.  Running it in a daemon thread
        # keeps the GUI fully responsive while the order is in-flight.
        def _do_close():
            try:
                if self.tee:
                    self.tee._exit_trade(token, ltp, "MANUAL")
                else:
                    is_short   = st.get("direction") == "SHORT"
                    close_side = "BUY" if is_short else "SELL"
                    self._place_order_chunked(token, close_side)
                    with self.lock:
                        st["trade_open"] = False
                    # Update UI: the TEE's _exit_trade normally does this;
                    # without a TEE we must do it here so the row moves to Closed.
                    _close_ltp = ltp
                    self.root.after(0, lambda t=token, l=_close_ltp, s=is_short:
                                    self._finalize_close_ui(t, l, s, "MANUAL"))
                # Reset the tick-freeze watchdog so a quiet post-close market period
                # doesn't look like a WebSocket freeze and trigger an unnecessary reconnect.
                if getattr(self, "oce", None):
                    self.oce.last_tick_time = _time_mod.time()
            finally:
                with self.lock:
                    st["close_in_progress"] = False

        threading.Thread(target=_do_close, daemon=True,
                         name=f"manual-close-{token}").start()

    def manual_close_trade_pct(self, token, pct):
        """Close `pct` (0 < pct <= 1.0) of the running position's quantity.
        pct=1.0 is a full close and just delegates to manual_close_trade()."""
        if pct >= 0.999:
            self.manual_close_trade(token)
            return

        with self.lock:
            st = self.strike_state.get(token)
            if not st or not st.get("trade_open"):
                self.log(f"⚠️  No open trade to close for token {token}")
                return
            if st.get("close_in_progress"):
                return
            per_lot     = self.get_lot_size()
            current_qty = st.get("lot_size", 0)
            if per_lot > 0:
                qty_to_close = int(round((current_qty * pct) / per_lot)) * per_lot
                qty_to_close = max(per_lot, min(current_qty, qty_to_close))
            else:
                qty_to_close = int(current_qty * pct)
            if qty_to_close <= 0 or current_qty <= 0:
                return
            full_close = qty_to_close >= current_qty

        if full_close:
            # Rounded up to the full position — let manual_close_trade()
            # own the close_in_progress flag and the actual close.
            self.manual_close_trade(token)
            return

        self.log(f"🔶 Partial close {int(pct*100)}% requested — "
                 f"{st.get('tradingsymbol','')} qty={qty_to_close}/{current_qty}")
        row_data = self._active_row(token)
        close_side = "BUY" if st.get("direction") == "SHORT" else "SELL"

        def _do_partial_close(qty=qty_to_close):
            resp = self._place_order_chunked(token, close_side, qty_override=qty)
            if resp:
                with self.lock:
                    new_qty = max(0, st["lot_size"] - qty)
                    st["lot_size"] = new_qty
                    if token in self.running_positions:
                        self.running_positions[token]["Qty"] = new_qty
                if new_qty <= 0:
                    self.root.after(0, lambda: self.manual_close_trade(token))
                else:
                    new_lots = new_qty // per_lot if per_lot > 0 else new_qty
                    if row_data:
                        self.root.after(0, lambda v=str(new_lots): row_data["qty_var"].set(v))
                self._save_state()

        threading.Thread(target=_do_partial_close, daemon=True,
                         name=f"partial-close-{token}").start()

    def _finalize_close_ui(self, token, ltp, is_short, reason="MANUAL"):
        """Update trade row UI to Closed state when no TEE is present.
        Mirrors what TradeExecutionEngine._exit_trade does for the GUI portion."""
        row_data = self._active_row(token)
        if row_data is None:
            return
        labels = row_data["labels"]
        entry   = row_data.get("entry_price", ltp)
        st      = self.strike_state.get(token)
        lot_size = st.get("lot_size", 1) if st else 1
        pnl = round((entry - ltp) * lot_size, 2) if is_short \
              else round((ltp - entry) * lot_size, 2)
        pnl_fg = "#4caf50" if pnl >= 0 else "#ff5252"
        bg     = "#1f3a1f" if pnl >= 0 else "#3a1f1f"

        def _disp(widget, orig, is_text=False):
            attr = f"_theme_orig_{'fg' if is_text else 'bg'}"
            try:
                setattr(widget, attr, orig)
            except Exception:
                pass
            if getattr(self, "current_theme", "dark") == "light":
                try:
                    return self._invert_color(widget, orig, is_text=is_text)
                except Exception:
                    return orig
            return orig

        labels[5].config(text=str(round(ltp, 2)))
        labels[8].config(text=f"{pnl:.2f}", fg=_disp(labels[8], pnl_fg, is_text=True))
        labels[9].config(text="Closed")
        for lbl in labels:
            try:
                lbl.config(bg=_disp(lbl, bg, is_text=False))
            except Exception:
                pass
        for idx in (3, 6, 7):
            if len(labels) > idx:
                try:
                    labels[idx].config(
                        state="disabled",
                        disabledbackground=_disp(labels[idx], bg, is_text=False),
                        disabledforeground=_disp(labels[idx], "#888888", is_text=True))
                except Exception:
                    pass
        if row_data.get("apply_btn"):
            try:
                row_data["apply_btn"].config(state="disabled")
            except Exception:
                pass
        for _btn in row_data.get("roll1_btns", ()):
            try: _btn.config(state="disabled", bg="#2a2a2a")
            except Exception: pass
        for _btn in row_data.get("roll2_btns", ()):
            try: _btn.config(state="disabled", bg="#2a2a2a")
            except Exception: pass
        for _btn in row_data.get("close_btns", {}).values():
            try: _btn.config(state="disabled", bg="#555555")
            except Exception: pass
        self._move_row_to_closed(row_data)
        row_data["status"] = "CLOSED"
        self._active_row_id.pop(token, None)
        self._update_trade_panel_height()
        self.update_hedge_btn()
        self.root.after(0, lambda: self.status_var.set(f"{token} MANUAL @ {ltp}"))

    def roll_position(self, token, direction, steps=None):
        """Rollup/Rolldown — close the current leg at market and open a new
        one at the next strike up/down.
        steps=1 → Roll 1: Nifty 50 pts / Sensex+Banknifty 100 pts
        steps=2 → Roll 2: Nifty 100 pts / Sensex+Banknifty 200 pts"""
        st = self.strike_state.get(token)
        if not st or not st.get("trade_open"):
            self.log(f"⚠️  No open trade to roll for token {token}")
            return
        if st.get("close_in_progress"):
            return

        strike   = st["strike"]
        opt_type = st["type"]
        side     = st.get("oc_action", "BUY")
        index    = self.index_var.get().upper()
        # Base step per Roll 1 move
        base_step = {"SENSEX": 100, "NIFTY": 50,
                     "BANKNIFTY": 100, "CRUDEOIL": 50}.get(index, 50)
        n_steps   = steps if steps is not None else 1
        delta     = base_step * n_steps
        new_strike = strike + delta if direction == "up" else strike - delta

        new_row = next((r for r in getattr(self, "oc_data", [])
                        if r["strike"] == new_strike), None)
        if not new_row:
            messagebox.showwarning(
                "Roll Position",
                f"Strike {new_strike} isn't loaded in the current Option "
                f"Chain — refresh / widen the strike range and try again.")
            return
        new_token  = new_row["ce_token"] if opt_type == "CE" else new_row["pe_token"]
        new_symbol = new_row["ce_sym"]   if opt_type == "CE" else new_row["pe_sym"]
        if not new_token:
            messagebox.showwarning(
                "Roll Position", f"No {opt_type} contract at strike {new_strike}.")
            return

        self.log(f"🔁 Rolling {direction} — {strike}{opt_type} → {new_strike}{opt_type} "
                 f"({n_steps} step{'s' if n_steps > 1 else ''})")

        # Close the current leg now (reuses the normal close codepath);
        # open the replacement leg a moment later once the close order has
        # had time to land, mirroring the rest of this module's async style.
        self.manual_close_trade(token)
        self.root.after(1500, lambda: self._oc_quick_trade(
            new_strike, opt_type, new_token, new_symbol, side))

    def _adjust_lot(self, token, delta):
        """delta=+1 → BUY more lots; delta=-1 → SELL/reduce lots."""
        row_data = self._active_row(token)
        st       = self.strike_state.get(token)
        if not row_data or not st or not st.get("trade_open"):
            return
        try:
            n_lots = int(row_data["adj_lot_var"].get())
        except (ValueError, TypeError):
            return
        if n_lots <= 0:
            return
        per_lot      = self.get_lot_size()
        current_lots = st["lot_size"] // per_lot if per_lot > 0 else 1

        # Fix 1: guard against over-reducing (must run on main thread before thread spawn)
        if delta < 0 and n_lots > current_lots:
            messagebox.showwarning(
                "Reduce Lot",
                f"Cannot reduce — only {current_lots} lot(s) running.\n"
                f"Enter ≤ {current_lots} and click  −  again."
            )
            return

        qty = n_lots * per_lot
        is_short = st.get("direction") == "SHORT"
        # Adding to a SHORT means selling more; reducing means buying back.
        if is_short:
            side = "SELL" if delta > 0 else "BUY"
        else:
            side = "BUY" if delta > 0 else "SELL"
        self.log(f"{'➕' if delta > 0 else '➖'} Adj lot: {side} {n_lots} lot(s) ({qty} qty) for {token}")
        _row_data = row_data
        _st       = st
        _per_lot  = per_lot
        _qty      = qty
        _delta    = delta
        _token    = token

        def _do_adjust():
            resp = self._place_order_chunked(_token, side, qty_override=_qty)
            if resp:
                with self.lock:
                    current_qty = _st.get("lot_size", _per_lot)
                    new_qty     = current_qty - _qty if _delta < 0 else current_qty + _qty
                    _st["lot_size"] = max(0, new_qty)
                # Fix 2: auto-close when all lots sold
                if new_qty <= 0:
                    self.root.after(0, lambda: self.manual_close_trade(_token))
                else:
                    new_lots = new_qty // _per_lot
                    self.root.after(0, lambda v=str(new_lots): _row_data["qty_var"].set(v))
                self._save_state()

        threading.Thread(target=_do_adjust, daemon=True,
                         name=f"adj-lot-{_token}").start()

    def square_off_all(self):
        """Close every RUNNING trade row at market price."""
        tokens = [d["token"] for d in self.trade_rows.values()
                  if d.get("status") == "RUNNING"]
        if not tokens:
            return
        self.log(f"🟥 Square Off All — closing {len(tokens)} position(s)")
        for token in tokens:
            self.manual_close_trade(token)

    # ==========================================================
    # RISK MANAGEMENT (kill switch)
    # ==========================================================
    def apply_risk_limits(self):
        """Snapshot current cumulative PnL as baseline, reset peak and any active halt."""
        self._risk_base_pnl    = float(self.cumulative_pnl)
        self._risk_peak_pnl    = 0.0    # trail peak resets on every Apply
        self._risk_halted      = False
        self._risk_halt_reason = ""
        self.log(f"✅ Risk limits applied — base PnL locked at {self._risk_base_pnl}")
        self.root.after(0, lambda: self.status_var.set("Risk limits applied"))

    def _compute_total_pnl(self):
        """PnL since last Apply click (realized delta + unrealized). Not all-time."""
        total = float(self.cumulative_pnl) - float(getattr(self, "_risk_base_pnl", 0))
        for tkn, pos in list(self.running_positions.items()):
            st = self.strike_state.get(tkn)
            if not st:
                continue
            ltp = st.get("ltp")
            if ltp is None:
                continue
            total += (ltp - pos["EntryPrice"]) * st.get("lot_size", pos.get("Qty", 0))
        return round(total, 2)

    def _risk_limit_breached(self):
        """Return (breached: bool, reason: str) for each individually-checked limit.
        Each limit (Max Loss, Max Trades, Profit Target) works independently —
        the 'Risk Limits' master checkbox is NOT required; it only governs whether
        the master kill-switch can be triggered in enforce_risk_limits()."""
        total = self._compute_total_pnl()
        if self.risk_max_loss_on_var.get():
            loss_limit = abs(self.risk_max_loss_var.get())
            trail_on   = getattr(self, "risk_trail_loss_var", None)
            peak       = getattr(self, "_risk_peak_pnl", 0.0)
            if trail_on and trail_on.get() and peak > 0:
                effective_floor = round(peak - loss_limit, 2)
            else:
                effective_floor = -loss_limit
            if total <= effective_floor:
                return True, f"Loss floor hit ({total:.0f} <= {effective_floor:.0f}, peak {peak:.0f})"
        if self.risk_profit_on_var.get() and total >= abs(self.risk_profit_target_var.get()):
            return True, f"Daily profit target hit ({total} >= {abs(self.risk_profit_target_var.get())})"
        if self.risk_max_trades_on_var.get() and self.trades_today >= self.risk_max_trades_var.get():
            return True, f"Max trades/day hit ({self.trades_today})"
        return False, ""

    def risk_entry_blocked(self):
        """True when a new entry must be refused (block action selected & limit breached)."""
        any_limit_on = (self.risk_max_loss_on_var.get() or
                        self.risk_profit_on_var.get() or
                        self.risk_max_trades_on_var.get())
        if not any_limit_on:
            return False
        breached, _ = self._risk_limit_breached()
        return breached and self.risk_action_block_var.get()

    def enforce_risk_limits(self):
        """Called frequently (each tick via running-pnl update).
        Triggers halt if ANY individually-checked limit is breached.
        The 'Risk Limits' master checkbox gates the overall square-off action;
        individual limits are checked regardless."""
        any_limit_on = (self.risk_max_loss_on_var.get() or
                        self.risk_profit_on_var.get() or
                        self.risk_max_trades_on_var.get())
        if not any_limit_on or self._risk_halted:
            return
        # Track peak PnL for trailing floor (only positive peaks raise the floor)
        total = self._compute_total_pnl()
        if total > getattr(self, "_risk_peak_pnl", 0.0):
            self._risk_peak_pnl = total
        breached, reason = self._risk_limit_breached()
        if not breached:
            return
        self._risk_halted      = True
        self._risk_halt_reason = reason
        self.log(f"🛑 RISK HALT — {reason}")
        self.root.after(0, lambda: self.status_var.set(f"RISK HALT: {reason}"))
        # Square off only when 'Risk Limits' master is ON or the sq-off action is checked
        if self.risk_action_sqoff_var.get():
            self.square_off_all()

    # ==========================================================
    # POSITION PERSISTENCE (crash recovery / broker reconcile)
    # ==========================================================
    def _state_file_path(self):
        from pathlib import Path
        folder = self.output_folder.get() if hasattr(self, "output_folder") else "."
        return Path(folder) / "open_positions_state.json"

    def _save_state(self):
        """Persist open positions + daily counters to disk (best-effort)."""
        import json
        try:
            data = {
                "date": dt.datetime.now().date().isoformat(),
                "cumulative_pnl": self.cumulative_pnl,
                "trades_today": self.trades_today,
                "positions": {},
            }
            for tkn, pos in list(self.running_positions.items()):
                st = self.strike_state.get(tkn, {})
                data["positions"][str(tkn)] = {
                    "tradingsymbol": st.get("tradingsymbol"),
                    "exchange":      st.get("exchange"),
                    "strike":        st.get("strike"),
                    "type":          st.get("type"),
                    "lot_size":      st.get("lot_size"),
                    "entry_price":   pos.get("EntryPrice"),
                    "sl":            st.get("sl"),
                    "targets":       st.get("targets", []),
                    "targets_hit":   st.get("targets_hit", []),
                    "highest_price": st.get("highest_price"),
                    "entry_time":    str(pos.get("EntryTime")),
                }
            p = self._state_file_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            print("save_state error:", e)

    def _load_state(self):
        """Return saved state dict if it exists and is from today, else None."""
        import json
        p = self._state_file_path()
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        if data.get("date") != dt.datetime.now().date().isoformat():
            return None   # stale (previous day) — ignore, fresh start
        return data

    # ==========================================================
    # ORDER SPLITTING (broker lot-limit enforcement)
    # ==========================================================
    def _get_max_lots_per_order(self):
        """Broker-enforced maximum lots per single order for the current
        index, further capped by Order Params 'MaxQty/Order' if set (>0)."""
        idx = self.index_var.get().upper()
        broker_cap = {"NIFTY": 27, "BANKNIFTY": 30, "SENSEX": 50}.get(idx, 30)
        try:
            cfg_cap = int(self.cfg_max_qty_per_order_var.get())
        except (TypeError, ValueError):
            cfg_cap = 0
        return min(broker_cap, cfg_cap) if cfg_cap > 0 else broker_cap

    def _place_order_chunked(self, token, transaction_type,
                              broker=None, qty_override=None):
        """Wraps place_order with automatic chunking when qty exceeds the per-order cap."""
        state = self.strike_state.get(token)
        if not state:
            return None
        per_lot  = self.get_lot_size()
        max_lots = self._get_max_lots_per_order()
        max_qty  = max_lots * per_lot if per_lot > 0 else qty_override or 1
        eff_qty  = qty_override if qty_override is not None else state["lot_size"]
        if eff_qty <= 0:
            return None
        if eff_qty <= max_qty:
            return self.place_order(token, transaction_type,
                                    broker=broker, qty_override=eff_qty)
        total_chunks = (eff_qty + max_qty - 1) // max_qty
        self.log(f"📦 Splitting {eff_qty} qty into {total_chunks} orders (max {max_qty} each)")
        remaining, last_resp, chunk_num = eff_qty, None, 0
        while remaining > 0:
            chunk_num += 1
            chunk_qty  = min(remaining, max_qty)
            remaining -= chunk_qty
            resp = self.place_order(token, transaction_type,
                                    broker=broker, qty_override=chunk_qty)
            last_resp = resp
            if resp is None:
                self.log(f"⚠️  Chunk {chunk_num}/{total_chunks} failed — stopping split")
                break
            self.log(f"✅ Chunk {chunk_num}/{total_chunks}: {chunk_qty} qty")
        return last_resp

    # ==========================================================
    # MANAGE TRADE  (called from TradeExecutionEngine via on_tick)
    # ==========================================================
    def manage_trade(self, token, ltp):
        """Delegate to TradeExecutionEngine."""
        if self.tee:
            self.tee.on_tick(token, ltp)

    # ==========================================================
    # STARTUP — SPOT ENGINE + ENGINE WIRING
    # ==========================================================
