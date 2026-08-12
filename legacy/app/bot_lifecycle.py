import threading
import time
import datetime as dt
import os
import re

import pandas as pd
import requests
import pytz
import tkinter as tk
from tkinter import messagebox

import config

from engines import (
    OptionChainEngine,
    CandleEngine,
    QuantFilterEngine,
    TradeExecutionEngine,
)


class BotLifecycleMixin:
    """Bot startup, engine wiring, watchdog, stop, and trade save."""

    def start_bot(self):
        if self.trade_mode_var.get() == "Backtest":
            self.start_backtest()
            return
        if not self.is_logged_in:
            messagebox.showerror("Error", "Login first")
            return
        if self.is_running:
            messagebox.showinfo("Already Running", "Bot is already running.", parent=self.root)
            return
        # ── Multi-strategy mode: run queue takes priority ──────────
        if getattr(self, "preset_run_queue", None):
            self.is_running = True
            threading.Thread(target=self._start_multi_strategy_bot,
                             daemon=True, name="multi-strategy").start()
            self.status_var.set("Multi-Strategy Starting...")
            self.start_indicator.config(fg="green")
            self._refresh_start_stop_btn()
            return
        # ── Broker guard (Live mode only) ──────────────────────
        if self.trade_mode_var.get() == "Live":
            if not self.use_angel_var.get() and not self.use_kotak_var.get() and not self.use_dhan_var.get():
                messagebox.showerror(
                    "Broker Selection Error",
                    "No broker selected.\n\n"
                    "Tick at least one broker in the Broker Selection "
                    "panel before starting Live trading.")
                return
            if self.use_angel_var.get() and not self.angel_logged_in:
                messagebox.showerror(
                    "Login Required",
                    "Angel One is selected but not logged in.\n"
                    "Please click Login first.")
                return
            if self.use_kotak_var.get() and not self.kotak_logged_in:
                ans = messagebox.askyesno(
                    "Kotak Not Logged In",
                    "Kotak Neo is selected but not logged in.\n\n"
                    "Continue without Kotak orders?")
                if not ans:
                    return
            if self.use_dhan_var.get() and not self.dhan_logged_in:
                ans = messagebox.askyesno(
                    "Dhan Not Logged In",
                    "Dhan HQ is selected but not logged in.\n\n"
                    "Continue without Dhan orders?")
                if not ans:
                    return
        # ── Data feed validation (all non-backtest modes) ─────
        _feed = self.data_feed_broker_var.get()
        if _feed == "Angel One" and not self.angel_logged_in:
            messagebox.showerror("Data Feed Error",
                "Data Feed is Angel One but Angel One is not logged in.\n"
                "Login to Angel One, or switch Data Feed to Kotak Neo / Dhan HQ.")
            return
        elif _feed == "Kotak Neo" and not self.kotak_logged_in:
            messagebox.showerror("Data Feed Error",
                "Data Feed is Kotak Neo but Kotak Neo is not logged in.\n"
                "Login to Kotak Neo, or switch Data Feed to Angel One / Dhan HQ.")
            return
        elif _feed == "Dhan HQ" and not self.dhan_logged_in:
            messagebox.showerror("Data Feed Error",
                "Data Feed is Dhan HQ but Dhan HQ is not logged in.\n"
                "Login to Dhan HQ, or switch Data Feed to Angel One / Kotak Neo.")
            return
        is_manual   = self.trade_exec_mode_var.get() == "Manual"
        is_market   = self.entry_mode_var.get() == "MARKET"
        gexp_on     = self.gexp_override_var.get()
        skip_fields = set()
        if is_manual:
            # Manual mode: no auto-entry params needed — skip all signal fields
            skip_fields |= {"Entry Price", "Tolerance", "SL Points", "Target Points"}
        else:
            if is_market:
                skip_fields |= {"Entry Price", "Tolerance"}
            if gexp_on:
                skip_fields |= {"SL Points", "Target Points"}
        for name, entry in self.numeric_entries.items():
            if name in skip_fields:
                continue
            if not entry.get_value():
                messagebox.showerror("Input Error", f"{name} is required.")
                return
        if not self.spot_entry.get_value() and not is_manual:
            messagebox.showerror("Input Error", "Spot Time is required.")
            return
        # Reset risk kill-switch state for a fresh session (daily counters are
        # restored from same-day saved state during reconcile in _run_all_engines).
        self._risk_halted      = False
        self._risk_halt_reason = ""
        self.is_running = True
        threading.Thread(target=self._run_all_engines,
                         daemon=True).start()
        self.status_var.set("Bot Running...")
        self.start_indicator.config(fg="green")
        self._refresh_start_stop_btn()

    def _run_all_engines(self):
        """
        Orchestrates the 4-engine startup sequence.
        Runs in a background thread.
        """
        try:
            # Reset tick log for fresh session
            with self.tick_lock:
                self.tick_log.clear()
            self._tick_flush_stop.clear()

            is_manual_mode = self.trade_exec_mode_var.get() == "Manual"

            # ── 1. Parse spot time ────────────────────────────
            spot_text = self.spot_entry.get_value()
            spot_detect_mode = self.spot_detect_mode_var.get()

            # ── 2. Wait / detect spot based on mode ──────────
            spot = None

            if spot_text:
                try:
                    spot_time = dt.datetime.strptime(spot_text, "%H:%M").time()
                except ValueError:
                    spot_time = None
                else:
                    if spot_detect_mode == "TIME":
                        # Wait until spot time, then fetch candle-close aligned price
                        while self.is_running:
                            if dt.datetime.now().time() >= spot_time:
                                break
                            time.sleep(1)
                        spot = self.get_spot_price_at_time(spot_time)

                    elif spot_detect_mode == "EMA_CROSS":
                        self.log("⏳ Waiting for EMA cross signal...")
                        spot = self._wait_for_ema_cross_spot(spot_time)

                    elif spot_detect_mode == "PDH_PDL":
                        self.log("⏳ Waiting for PDH/PDL touch signal...")
                        spot = self._wait_for_pdh_pdl_spot(spot_time)
            elif is_manual_mode:
                self.log("⚡ Manual mode — skipping spot time detection")
            if not spot:
                if is_manual_mode:
                    self.log("⚠️  Could not get spot — starting WS for OC data only")
                    spot = 0
                else:
                    self.status_var.set("Spot fetch failed ❌")
                    return

            # ── 4. Calculate ATM + strikes ───────────────────
            if spot:
                atm, step = self.calculate_atm(spot)
                strikes   = self.generate_strikes(atm, step)
                print("INDEX:", self.index_var.get())
                print("SPOT:", spot, "| ATM:", atm)
                print("Generated Strikes:", strikes)
                selected = self.map_strikes_to_tokens(strikes)
                print("Selected Tokens:", selected)
            else:
                atm = step = 0
                selected = []

            if not selected and not is_manual_mode:
                mode = self.strike_mode_var.get()
                if not strikes:
                    if mode == "RELATIVE":
                        msg = ("No strikes generated — in RELATIVE mode you must tick "
                               "at least one CE or PE checkbox in the "
                               "'Directional Strike Selection' panel.")
                    elif mode == "ROUND":
                        msg = (f"No strikes generated — ATM {atm} is not a multiple of 500 "
                               f"(ROUND mode requires institutional round numbers). "
                               f"Switch to LEGACY or RELATIVE mode.")
                    else:
                        msg = f"No strikes generated for mode={mode}, ATM={atm}."
                    self.root.after(0, lambda m=msg: self.log(f"❌ {m}"))
                else:
                    self.root.after(0, lambda: self.log(
                        f"❌ No tokens mapped for strikes {strikes} — "
                        f"check expiry '{self.expiry_var.get()}' is correct."))
                self.root.after(0, lambda: self.start_indicator.config(fg="red"))
                self.root.after(0, self._refresh_start_stop_btn)
                self.status_var.set("No tokens found ❌")
                self.is_running = False
                return

            # ── 5. Initialise strike state ────────────────────
            lot_size = self.get_lot_size() * max(1, self.lots_var.get())
            index    = self.index_var.get().upper()
            exch_map = {"NIFTY": "NFO", "BANKNIFTY": "NFO",
                        "SENSEX": "BFO", "CRUDEOIL": "MCX"}
            exch     = exch_map.get(index, "NFO")

            if selected:
                self.clear_strike_panel()
                with self.lock:
                    for strike, opt_type, token, tradingsymbol in selected:
                        self.strike_state[token] = {
                            "exchange":            exch,
                            "tradingsymbol":       tradingsymbol,
                            "lot_size":            lot_size,
                            "strike":              strike,
                            "type":                opt_type,
                            "trade_open":          False,
                            "entry_taken_today":   False,
                            "order_in_progress":   False,
                            "close_in_progress":   False,
                            "entry_price":         None,
                            "highest_price":       0,
                            "sl":                  None,
                            "targets":             [],
                            "targets_hit":         [],
                            "entry_band_triggered": False,
                            "gexp_override":       False,
                            "gexp_method":         "none",
                            "gexp_tsl_step":       0,
                            "ltp":                 None,
                        }

                # ── 6. Build Strike LTP panel ─────────────────────
                for token, data in self.strike_state.items():
                    self._add_strike_ltp_row(
                        token, data["strike"], data["type"], data["lot_size"])

            else:
                pass  # selected is populated; seed runs in background below

            # ── 7. Reconcile saved positions (crash recovery / auto-resume) ──
            # Mutates `selected` in place so recovered tokens get subscribed below.
            try:
                self._reconcile_positions(selected)
            except Exception as e:
                print("reconcile_positions error:", e)

            # ── 8-12. Start per-broker engine pipelines ────────
            trade_tokens = [t for _, _, t, _ in selected]
            oc_tokens = [row[k] for row in self.oc_data
                         for k in ("ce_token", "pe_token")
                         if row.get(k) and row[k] not in trade_tokens]

            if not trade_tokens and not oc_tokens:
                if is_manual_mode:
                    msg = (
                        "No strikes or Option Chain data available.\n\n"
                        "To use Manual mode you need at least one of:\n"
                        "  • Load a strategy preset (sets strike selection), OR\n"
                        "  • Click 'Fetch OC' to load the Option Chain BEFORE\n"
                        "    clicking Start Bot.\n\n"
                        "Stop the bot, configure your settings, then restart."
                    )
                else:
                    msg = (
                        "No tokens to subscribe.\n\n"
                        "Load a strategy preset or configure your strike selection\n"
                        "before starting the bot."
                    )
                self.root.after(0, lambda m=msg: messagebox.showwarning("Cannot Start Bot", m))
                self.root.after(0, lambda: self.start_indicator.config(fg="red"))
                self.root.after(0, self._refresh_start_stop_btn)
                self.status_var.set("No tokens — bot stopped ❌")
                self.is_running = False
                return

            # Start pipelines immediately with no seed data — OCE connects right away,
            # entries fire within ~2s of spot time instead of waiting 10+ seconds for seeding.
            self.status_var.set("Starting live data feed...")
            self._start_broker_pipelines(selected, exch, lot_size, {}, oc_tokens)

            # Seed historical candles in background so VWAP/EMA warmup data arrives
            # without blocking entry.  try_late_seed() injects it into CE when ready.
            def _bg_seed(_sel=selected, _exch=exch, _lot=lot_size):
                seed = self._seed_historical_candles(_sel, _exch, _lot)
                _ce = getattr(self, "ce", None)
                if _ce and seed:
                    for _tok, _data in seed.items():
                        _ce.try_late_seed(
                            _tok,
                            _data.get("df"),
                            _data.get("last_cum_vol", 0),
                            _data.get("current_candle"),
                            _data.get("last_candle_minute"),
                        )
                    _n = len(seed)
                    self.root.after(0, lambda n=_n: self.log(
                        f"📊 Historical warmup applied ({n} tokens)"))

            threading.Thread(target=_bg_seed, daemon=True, name="candle-seed-bg").start()

            # ── Tick capture init ─────────────────────────────
            if self.save_ticks_var.get():
                self._init_tick_log_for_tokens()
                self._start_periodic_tick_flush()
                fmt_info  = self.tick_save_format_var.get()
                flush_info = self.tick_flush_mins_var.get()
                self.log(f"🔴 Tick capture ON | "
                         f"format={fmt_info} | "
                         f"auto-flush every {flush_info} min | "
                         f"folder: tick_data/")
                self.root.after(5000, self._tick_count_refresh_loop)

            # ── 13. Status + log ──────────────────────────────
            exec_mode = self.trade_exec_mode_var.get()
            if spot:
                lines = [f"Spot: {spot}\n", f"ATM: {atm}\n",
                         f"Entry Mode: {exec_mode}\n",
                         "Selected Strikes:\n"]
                for data in self.strike_state.values():
                    lines.append(f" - {data['strike']} {data['type']}\n")
                status_txt = f"Spot: {spot} | ATM: {atm} | {exec_mode} | Engines Running ✅"
            else:
                lines = [f"Entry Mode: {exec_mode}\n",
                         "⚡ MANUAL MODE — WS started for OC data\n",
                         "Select a strike from the Option Chain and click 'Place Trade Now'\n"]
                status_txt = f"Manual Mode | WS Running ✅ | Select strike from OC"
            if exec_mode == "Manual":
                lines.append("⚡ MANUAL MODE — use 'Place Trade Now' to enter\n")
            def _do_log(ls=lines, st=status_txt):
                self.status_var.set(st)
                for line in ls:
                    self.log_box.insert("end", line)
                self.log_box.see("end")
            self.root.after(0, _do_log)

            # ── 14. Watchdog ──────────────────────────────────
            # Skip starting a new watchdog when _run_all_engines is re-entered
            # during an expiry auto-rotation — the existing watchdog keeps running.
            if not getattr(self, '_rotating_expiry', False):
                threading.Thread(target=self._watchdog,
                                 daemon=True).start()

        except Exception as e:
            import traceback
            print("Engine startup error:", e)
            traceback.print_exc()
            self.root.after(0, lambda msg=str(e): self.status_var.set(f"Startup error: {msg}"))

    # ==========================================================
    # SPOT DETECTION WAITERS  (live mode)
    # ==========================================================
    def _fetch_index_candles_now(self, n_bars=100):
        """
        Fetch recent index candles from REST for EMA/PDH-PDL detection.
        Returns a DataFrame with columns: time, open, high, low, close, volume.
        Uses the candle interval from spot_candle_tf_var.
        """
        token, exch = self.get_spot_token()
        if not token:
            return None
        interval_map = {"1min": "ONE_MINUTE", "3min": "THREE_MINUTE",
                        "5min": "FIVE_MINUTE", "15min": "FIFTEEN_MINUTE"}
        tf           = self.spot_candle_tf_var.get()
        api_interval = interval_map.get(tf, "ONE_MINUTE")
        tf_mins      = int(tf.replace("min", ""))
        now          = dt.datetime.now()
        fetch_from   = now - dt.timedelta(minutes=tf_mins * (n_bars + 5))
        try:
            params = {
                "exchange":    exch,
                "symboltoken": token,
                "interval":    api_interval,
                "fromdate":    fetch_from.strftime("%Y-%m-%d %H:%M"),
                "todate":      now.strftime("%Y-%m-%d %H:%M"),
            }
            resp = self.smart.getCandleData(params)
            if not (resp and resp.get("status")):
                return None
            data = resp.get("data", [])
            if not data:
                return None
            df = pd.DataFrame(data,
                              columns=["time","open","high","low","close","volume"])
            df["time"] = pd.to_datetime(df["time"])
            if df["time"].dt.tz is not None:
                df["time"] = (df["time"].dt.tz_convert("Asia/Kolkata")
                              .dt.tz_localize(None))
            else:
                df["time"] += pd.Timedelta(hours=5, minutes=30)
            for col in ["open","high","low","close","volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            return df.dropna(subset=["close"]).sort_values("time").reset_index(drop=True)
        except Exception as e:
            print("_fetch_index_candles_now error:", e)
            return None

    def _wait_for_ema_cross_spot(self, earliest_time):
        """
        Poll index candles every candle-interval until EMA cross is detected.
        earliest_time: don't trigger before this time (user-set spot time field).
        Returns the close price at the moment of cross, or None on timeout/stop.
        """
        tf_str   = self.spot_candle_tf_var.get()
        tf_mins  = int(tf_str.replace("min", ""))
        poll_sec = tf_mins * 60   # re-check every candle length

        while self.is_running:
            now = dt.datetime.now()
            if now.time() < earliest_time:
                time.sleep(10)
                continue
            if now.time() >= dt.time(15, 30):
                self.log("Market closed — EMA cross not detected today")
                return None
            df = self._fetch_index_candles_now(n_bars=max(
                self.ema_slow_var.get() + 10, 60))
            if df is not None and self.check_ema_cross_signal(df):
                spot = float(df.iloc[-1]["close"])
                self.log(f"✅ EMA Cross detected @ {now.strftime('%H:%M:%S')} "
                         f"| Spot = {spot}")
                return spot
            time.sleep(poll_sec)
        return None

    def _wait_for_pdh_pdl_spot(self, earliest_time):
        """
        Poll index candles every candle-interval until a PDH/PDL touch is detected.
        Returns the close price at the moment of touch, or None on timeout/stop.
        """
        tf_str   = self.spot_candle_tf_var.get()
        tf_mins  = int(tf_str.replace("min", ""))
        poll_sec = tf_mins * 60

        pdh, pdl = self.compute_pdh_pdl(dt.datetime.now().date())
        if pdh is None and pdl is None:
            self.log("⚠️  No PDH/PDL available — falling back to spot time")
            # Fallback: use spot time
            spot_time = dt.datetime.strptime(
                self.spot_entry.get_value(), "%H:%M").time()
            while self.is_running:
                if dt.datetime.now().time() >= spot_time:
                    break
                time.sleep(1)
            return self.get_spot_price_at_time(spot_time)

        self.log(f"📊 PDH={pdh}  PDL={pdl}  | waiting for touch...")

        while self.is_running:
            now = dt.datetime.now()
            if now.time() < earliest_time:
                time.sleep(10)
                continue
            if now.time() >= dt.time(15, 30):
                self.log("Market closed — PDH/PDL not touched today")
                return None
            df = self._fetch_index_candles_now(n_bars=30)
            if df is not None and self.check_pdh_pdl_touch(df, pdh, pdl):
                spot = float(df.iloc[-1]["close"])
                self.log(f"✅ PDH/PDL Touch detected @ {now.strftime('%H:%M:%S')} "
                         f"| Spot = {spot}")
                return spot
            time.sleep(poll_sec)
        return None

    # ==========================================================
    # HISTORICAL CANDLE SEED  (REST — only at startup, once)
    # ==========================================================
    def _seed_historical_candles(self, selected, exch, lot_size):
        """
        Fetch historical candles for each token at startup.
        Returns {token: {df, last_cum_vol, current_candle, last_candle_minute}}
        Uses throttled sequential fetching to avoid rate limits.
        """
        result = {}
        interval_map = {
            "1min": "ONE_MINUTE",
            "3min": "THREE_MINUTE",
            "5min": "FIVE_MINUTE",
        }
        api_interval = interval_map.get(self.live_interval_var.get(),
                                        "THREE_MINUTE")
        now          = dt.datetime.now()
        market_open  = now.replace(hour=9, minute=15, second=0, microsecond=0)
        if now < market_open:
            market_open -= dt.timedelta(days=1)
        ema_seed_from = market_open - dt.timedelta(days=2)   # 2 days is enough for EMA/RSI warmup

        _RATE_LIMIT_BACKOFFS = (3, 6, 12)   # seconds to wait on rate-limit per attempt

        for i, (strike, opt_type, token, tradingsymbol) in enumerate(selected):
            if i > 0:
                time.sleep(1.0)   # Angel rate limit: 1.0s between candle fetches

            try:
                params = {
                    "exchange":    exch,
                    "symboltoken": token,
                    "interval":    api_interval,
                    "fromdate":    ema_seed_from.strftime("%Y-%m-%d %H:%M"),
                    "todate":      now.strftime("%Y-%m-%d %H:%M"),
                }
                resp = None
                for attempt in range(3):
                    try:
                        resp = self.smart.getCandleData(params)
                        if resp and resp.get("status"):
                            break
                    except Exception as e:
                        err_s = str(e).lower()
                        print(f"Candle fetch retry ({token}):", e)
                        if ("access rate" in err_s or "access denied" in err_s
                                or "exceeding" in err_s):
                            wait = _RATE_LIMIT_BACKOFFS[min(attempt, 2)]
                            print(f"[Seed] Rate limit hit ({token}) — waiting {wait}s")
                            time.sleep(wait)
                        else:
                            time.sleep(2)

                if not resp or not resp.get("status"):
                    print(f"[Seed] Skipped {token} — candle fetch failed")
                    self.root.after(0, lambda sym=tradingsymbol: self.log(
                        f"⚠️  Seed skipped {sym} — trading without historical warmup data"))
                    continue

                data = resp.get("data", [])
                if not data:
                    continue

                df = pd.DataFrame(data, columns=[
                    "time", "open", "high", "low", "close", "volume"])
                df["time"] = pd.to_datetime(df["time"])
                if df["time"].dt.tz is not None:
                    df["time"] = (df["time"]
                                  .dt.tz_convert("Asia/Kolkata")
                                  .dt.tz_localize(None))
                else:
                    df["time"] += pd.Timedelta(hours=5, minutes=30)

                df = df.sort_values("time")
                for col in ["open", "high", "low", "close", "volume"]:
                    df[col] = df[col].astype(float)

                # Compute indicators on ALL days (EMA warmup)
                df["ema"] = df["close"].ewm(
                    span=self.ema_period_var.get(), adjust=False).mean()
                df["rsi"] = self.compute_rsi(
                    df["close"], self.rsi_period_var.get())

                # Filter to today
                df = df[df["time"].dt.date == now.date()].copy()
                df = df.sort_values("time").reset_index(drop=True)

                if df.empty:
                    continue

                # VWAP
                df["typical_price"] = (df["high"]+df["low"]+df["close"])/3
                df["pv"]            = df["typical_price"] * df["volume"]
                df["cum_pv"]        = df["pv"].cumsum()
                df["cum_vol"]       = df["volume"].cumsum()
                df["vwap"]          = df["cum_pv"] / df["cum_vol"]
                roll_std            = (df["typical_price"]-df["vwap"]).rolling(
                    30, min_periods=1).std()
                df["std"]           = roll_std
                df["vwap_upper1"]   = df["vwap"] + roll_std
                df["vwap_lower1"]   = df["vwap"] - roll_std
                df["vwap_upper2"]   = df["vwap"] + 2*roll_std
                df["vwap_lower2"]   = df["vwap"] - 2*roll_std
                df.bfill(inplace=True)

                # Capture last (partial) candle
                last_candle_dict     = None
                last_candle_minute   = None
                if len(df) > 1:
                    last_row = df.iloc[-1]
                    t = last_row["time"]
                    if not isinstance(t, dt.datetime):
                        t = pd.Timestamp(t).to_pydatetime()
                    if t.tzinfo is not None:
                        t = t.replace(tzinfo=None)
                    t = t.replace(second=0, microsecond=0)
                    last_candle_dict = {
                        "time":          t,
                        "open":          float(last_row["open"]),
                        "high":          float(last_row["high"]),
                        "low":           float(last_row["low"]),
                        "close":         float(last_row["close"]),
                        "volume":        int(last_row["volume"]),
                        "ema":           float(last_row.get("ema", 0)),
                        "rsi":           float(last_row.get("rsi", 50)),
                        "typical_price": float(last_row.get("typical_price", 0)),
                        "pv":            float(last_row.get("pv", 0)),
                        "cum_pv":        float(last_row.get("cum_pv", 0)),
                        "cum_vol":       float(last_row.get("cum_vol", 0)),
                        "vwap":          float(last_row.get("vwap", 0)),
                        "std":           float(last_row.get("std", 0)),
                        "vwap_upper1":   float(last_row.get("vwap_upper1", 0)),
                        "vwap_lower1":   float(last_row.get("vwap_lower1", 0)),
                        "vwap_upper2":   float(last_row.get("vwap_upper2", 0)),
                        "vwap_lower2":   float(last_row.get("vwap_lower2", 0)),
                    }
                    last_candle_minute = t
                    df = df.iloc[:-1]

                df = df.tail(600)

                # Seed last_cum_vol
                total_lots  = int(df["volume"].sum())
                if last_candle_dict:
                    total_lots += last_candle_dict["volume"]
                last_cum_vol = total_lots * lot_size

                result[token] = {
                    "df":                  df,
                    "last_cum_vol":        last_cum_vol,
                    "current_candle":      last_candle_dict,
                    "last_candle_minute":  last_candle_minute,
                }

            except Exception as e:
                print(f"[Seed] Error for {token}: {e}")

        return result

    # ==========================================================
    # WATCHDOG
    # ==========================================================
    def _watchdog(self):
        FREEZE_TIMEOUT = 15
        # Minimum seconds between watchdog-forced reconnects.  This gives any
        # in-progress reconnect (triggered by on_close or a previous watchdog
        # cycle) time to complete before we force another one.  It also prevents
        # a rapid-reconnect storm when the subscribed token stops ticking (e.g.
        # expired option, position closed).  Setting it larger than FREEZE_TIMEOUT
        # means the watchdog will always wait at least one full grace period before
        # retrying, even if _reconnect_attempt is non-zero.
        RECONNECT_COOLDOWN = 30   # seconds (was 60 — reduced so watchdog rescues faster after on_close retries fail)
        while self.is_running:
            try:
                if self.oce:
                    delta = time.time() - self.oce.last_tick_time
                    secs_since_reconnect = (
                        time.time() - getattr(self.oce, '_last_connect_attempt_time', 0)
                    )
                    print(f"Heartbeat OK | Last tick: {int(delta)} sec")
                    # Trigger a reconnect when:
                    #   • The OCE WebSocket is supposed to be running
                    #   • Market is open
                    #   • No tick has arrived for FREEZE_TIMEOUT seconds
                    #   • AND we are not in a recent reconnect cooldown
                    #
                    # Replacing the old "_reconnect_attempt == 0" guard with a
                    # time-based cooldown means the watchdog can rescue a stuck
                    # reconnect (e.g. sws.connect() hanging, on_open never fires)
                    # after RECONNECT_COOLDOWN seconds, regardless of the attempt
                    # counter value.
                    if (self.oce.running and self.is_market_open()
                            and delta > FREEZE_TIMEOUT
                            and secs_since_reconnect > RECONNECT_COOLDOWN):
                        print(
                            f"⚠️ Tick freeze detected — {int(delta)}s since last tick, "
                            f"{int(secs_since_reconnect)}s since last reconnect"
                        )
                        if hasattr(self, "_set_api_status"):
                            self._set_api_status(False, f"no ticks {int(delta)}s")
                        # Angel-specific: canary token detects expiry-day token rejection.
                        # Kotak/Dhan engines don't have a canary — always reconnect directly.
                        _is_angel = self.data_feed_broker_var.get() == "Angel One"
                        if (_is_angel
                                and getattr(self.oce, "canary_alive", False)
                                and getattr(self.oce, "_reconnect_attempt", 0) >= 5
                                and not getattr(self, '_rotating_expiry', False)):
                            print("🔄 Canary alive — expiry-day token rejection — triggering expiry rotation")
                            threading.Thread(target=self._auto_rotate_to_next_expiry,
                                             daemon=True, name="expiry-rotate").start()
                        else:
                            self.oce._connect()
                            self.oce.last_tick_time = time.time()
                            print("🔄 Data feed reconnect initiated — waiting for on_open...")
                    elif self.oce.running and delta <= FREEZE_TIMEOUT and not getattr(self, "angel_api_ok", True):
                        if hasattr(self, "_set_api_status"):
                            self._set_api_status(True)
            except Exception as e:
                print("Watchdog error:", e)
            time.sleep(5)

    # ==========================================================
    # EXPIRY ROTATION HELPERS
    # ==========================================================
    def _get_next_expiry(self):
        """
        Returns the nearest expiry strictly after today for the currently selected
        index, by scanning instrument_master.  Returns None if none found.
        """
        if not self.instrument_master:
            return None
        index = self.index_var.get().upper()
        today = dt.date.today()
        expiries_raw = set()
        for x in self.instrument_master:
            name = x.get("name", "").upper()
            inst = x.get("instrumenttype", "")
            exch = x.get("exch_seg", "")
            if index == "CRUDEOIL":
                if name == "CRUDEOIL" and exch == "MCX" and "OPT" in inst:
                    expiries_raw.add(x.get("expiry"))
            else:
                if name == index and "OPT" in inst:
                    expiries_raw.add(x.get("expiry"))

        def _parse(e):
            for fmt in ("%d%b%Y", "%Y-%m-%d"):
                try:
                    return dt.datetime.strptime(e, fmt).date()
                except Exception:
                    pass
            return None

        future = [(d, e) for e in expiries_raw
                  if (d := _parse(e)) is not None and d > today]
        if not future:
            return None
        future.sort(key=lambda x: x[0])
        return future[0][1]

    def _auto_rotate_to_next_expiry(self):
        """
        Stop the current OCE pipeline, rotate expiry_var to the nearest future
        expiry, then restart all engines.  Trade state (positions, PnL, history)
        is preserved — only the WebSocket subscription and candle buffers reset.
        """
        if getattr(self, '_rotating_expiry', False):
            return
        self._rotating_expiry = True
        try:
            next_expiry = self._get_next_expiry()
            if not next_expiry:
                msg = "⚠️ Auto-rotation failed — no future expiry in instrument master."
                self.root.after(0, lambda m=msg: self.log(m))
                print("[AutoRotate] No future expiry available — stopping rotation")
                return
            old_expiry = self.expiry_var.get()
            print(f"[AutoRotate] Rotating {old_expiry} → {next_expiry}")
            self.root.after(0, lambda e=next_expiry, o=old_expiry: self.log(
                f"🔄 Auto-rotating expiry: {o} → {e}"))
            self.expiry_var.set(next_expiry)
            self.root.after(0, self.refresh_option_chain)
            # Restart engines with new expiry — reads expiry_var which is now next_expiry
            self._run_all_engines()
        finally:
            self._rotating_expiry = False

    # ==========================================================
    # STOP BOT
    # ==========================================================
    # ==========================================================
    # TICK DATA CAPTURE
    # ==========================================================
    def _record_tick(self, token, ltp, cum_vol, now):
        """
        Called on every incoming tick for selected strike tokens.
        Appends a lightweight dict to tick_log[token].
        This runs in the WebSocket thread — lock is minimal (list.append is GIL-safe
        in CPython, but we use tick_lock for the initialisation check).
        """
        if not self.save_ticks_var.get():
            return
        # Initialise list for new token
        if token not in self.tick_log:
            with self.tick_lock:
                if token not in self.tick_log:
                    self.tick_log[token] = []

        # Compute per-tick volume delta from previous cum_vol
        with self.tick_lock:
            tlist = self.tick_log[token]
            prev_cum = tlist[-1]["cum_volume"] if tlist else None

        if prev_cum is not None and cum_vol is not None and cum_vol >= prev_cum:
            vol_delta = cum_vol - prev_cum
        else:
            vol_delta = None
        
        ist = pytz.timezone("Asia/Kolkata")
        now_ist = dt.datetime.now(ist)

        tick_dict = {
            "timestamp": now_ist.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "ltp":        ltp,
            "cum_volume": cum_vol,
            "vol_delta":  vol_delta,
        }
        with self.tick_lock:
            self.tick_log[token].append(tick_dict)

    def _init_tick_log_for_tokens(self):
        """Initialise empty tick lists for all selected strike tokens."""
        with self.tick_lock:
            for token in self.strike_state:
                if token not in self.tick_log:
                    self.tick_log[token] = []

    def _get_tick_folder(self):
        """Return (and create if needed) the tick_data sub-folder."""
        tick_dir = os.path.join(self.output_folder.get(), "tick_data")
        os.makedirs(tick_dir, exist_ok=True)
        return tick_dir

    def _flush_ticks_to_disk(self, tokens=None, label=""):
        """
        Write accumulated tick data to disk.
        tokens: list of tokens to flush; None = all tokens.
        label:  suffix for filenames (e.g. 'final', 'snapshot', '14:30').
        Each token gets its own file: <symbol>_<token>_ticks_<date>.<ext>
        Also creates: <symbol>_<token>_1min_<date>.<ext>
        """
        if not self.save_ticks_var.get():
            return

        tick_dir = self._get_tick_folder()
        fmt      = self.tick_save_format_var.get()   # "CSV" or "Excel"
        date_str = dt.datetime.now().strftime("%Y%m%d")

        with self.tick_lock:
            tokens_to_flush = list(tokens or self.tick_log.keys())

        total_written = 0

        for token in tokens_to_flush:
            with self.tick_lock:
                tlist = self.tick_log.get(token, [])
                if not tlist:
                    continue

                rows = list(tlist)
                self.tick_log[token] = []

            st     = self.strike_state.get(token, {})
            symbol = f"{st.get('strike','')}{st.get('type','')}"
            base   = f"{symbol}_{token}_ticks_{date_str}"

            # ==============================
            # 🟢 RAW TICK DATAFRAME
            # ==============================
            df_tick = pd.DataFrame(rows, columns=[
                "timestamp", "ltp", "cum_volume", "vol_delta"
            ])

            if df_tick.empty:
                continue

            # ==============================
            # 🔥 BUILD 1-MIN CANDLES
            # ==============================
            df_candle = df_tick.copy()

            df_candle["timestamp"] = pd.to_datetime(
                df_candle["timestamp"], format="%Y-%m-%d %H:%M:%S.%f")
            df_candle.set_index("timestamp", inplace=True)

            ohlc = df_candle["ltp"].resample("1min").ohlc()
            vol  = df_candle["vol_delta"].fillna(0).resample("1min").sum()

            df_1min = ohlc.copy()
            df_1min["volume"] = vol

            # Keep only valid candles
            df_1min = df_1min[df_1min["open"].notna()]
            df_1min.reset_index(inplace=True)

            try:
                # ==============================
                # 💾 SAVE TICK DATA
                # ==============================
                if fmt == "CSV":
                    fpath = os.path.join(tick_dir, f"{base}.csv")
                    write_header = not os.path.exists(fpath)

                    df_tick.to_csv(
                        fpath,
                        mode="a",
                        header=write_header,
                        index=False
                    )

                else:  # Excel
                    fpath = os.path.join(tick_dir, f"{base}.xlsx")

                    if os.path.exists(fpath):
                        try:
                            existing = pd.read_excel(fpath)
                            df_tick  = pd.concat([existing, df_tick], ignore_index=True)
                        except Exception:
                            pass

                    with pd.ExcelWriter(fpath, engine="xlsxwriter") as wr:
                        df_tick.to_excel(wr, sheet_name="Ticks", index=False)

                # ==============================
                # 💾 SAVE 1-MIN CANDLES
                # ==============================
                candle_base = f"{symbol}_{token}_1min_{date_str}"

                if fmt == "CSV":
                    cpath = os.path.join(tick_dir, f"{candle_base}.csv")
                    write_header = not os.path.exists(cpath)

                    df_1min.to_csv(
                        cpath,
                        mode="a",
                        header=write_header,
                        index=False
                    )

                else:  # Excel
                    cpath = os.path.join(tick_dir, f"{candle_base}.xlsx")

                    if os.path.exists(cpath):
                        try:
                            existing = pd.read_excel(cpath)
                            df_1min  = pd.concat([existing, df_1min], ignore_index=True)
                        except Exception:
                            pass

                    with pd.ExcelWriter(cpath, engine="xlsxwriter") as wr:
                        df_1min.to_excel(wr, sheet_name="1min", index=False)

                total_written += len(df_tick)

            except Exception as e:
                print(f"Tick flush error ({token}): {e}")

        if total_written:
            self.log(f"💾 Tick flush [{label}]: {total_written:,} rows → {tick_dir}")
            self.root.after(0, self._update_tick_count_label)

    def _update_tick_count_label(self):
        """Refresh the 'N ticks buffered' label in the GUI."""
        with self.tick_lock:
            total = sum(len(v) for v in self.tick_log.values())
        if hasattr(self, "tick_count_label"):
            self.tick_count_label.config(
                text=f"{total:,} ticks buffered across "
                     f"{len(self.tick_log)} tokens")

    def _save_tick_snapshot(self):
        """Button handler — manual snapshot flush."""
        label = dt.datetime.now().strftime("%H%M%S")
        threading.Thread(
            target=self._flush_ticks_to_disk,
            args=(None, f"snap_{label}"),
            daemon=True
        ).start()
        self.status_var.set(f"Tick snapshot saving... [{label}]")

    def _start_periodic_tick_flush(self):
        """Start background thread for periodic auto-flush."""
        mins = self.tick_flush_mins_var.get()
        if mins <= 0:
            return    # disabled
        self._tick_flush_stop.clear()

        def _loop():
            interval = mins * 60
            while not self._tick_flush_stop.wait(interval):
                label = dt.datetime.now().strftime("%H%M")
                self._flush_ticks_to_disk(label=f"auto_{label}")

        self._tick_flush_thread = threading.Thread(
            target=_loop, daemon=True, name="tick-flush")
        self._tick_flush_thread.start()

    def _stop_periodic_tick_flush(self):
        """Signal the auto-flush thread to stop."""
        self._tick_flush_stop.set()

    def stop_bot(self):
        if not self.is_running:
            return
        self.is_running = False
        self.status_var.set("Bot Stopping...")
        self.start_indicator.config(fg="red")
        self._refresh_start_stop_btn()

        # Close open positions
        for token in list(self.running_positions.keys()):
            state = self.strike_state.get(token)
            ltp   = state.get("ltp") if state else None
            if ltp:
                self._place_order_chunked(token, "SELL")
                pos = self.running_positions[token]
                pnl = round((ltp - pos["EntryPrice"]) * pos["Qty"], 2)
                self.cumulative_pnl += pnl
                exit_time = dt.datetime.now()
                record = {
                    "Date":          pos["Date"],
                    "Token":         pos["Token"],
                    "Symbol":        pos["Symbol"],
                    "SL_Points":     pos["SL_Points"],
                    "Target":        pos["Target_Points"],
                    "EntryTime":     pos["EntryTime"],
                    "EntryPrice":    pos["EntryPrice"],
                    "Qty":           pos["Qty"],
                    "StopNumeric":   pos["EntryPrice"] - pos["SL_Points"],
                    "TargetPrice":   pos["EntryPrice"] + pos["Target_Points"],
                    "ExitTime":      exit_time,
                    "ExitPrice":     ltp,
                    "P&L":           pnl,
                    "EMA":           pos["EMA"],
                    "RSI":           pos["RSI"],
                    "VWAP":          pos["VWAP"],
                    "VWAP_SD1_UP":   pos["VWAP_SD1_UP"],
                    "VWAP_SD1_DOWN": pos["VWAP_SD1_DOWN"],
                    "EntryVolume":   pos["EntryVolume"],
                    "Reason":        "MANUAL_STOP",
                    "Cumulative P&L": self.cumulative_pnl,
                }
                self.completed_trades.append(record)
                del self.running_positions[token]

        # Persist final state (all positions closed above -> empty positions file)
        self._save_state()

        # Stop all broker pipeline objects (QFE/CE callbacks nulled, feed engines stopped)
        self._stop_all_pipelines()

        # Stop engines
        if self.oce:
            self.oce.stop()
        if self._oc_engine and self._oc_engine is not self.oce:
            try:
                self._oc_engine.stop()
            except Exception:
                pass
            self._oc_engine = None

        # Flush remaining tick data to disk before saving trades
        self._stop_periodic_tick_flush()
        if self.save_ticks_var.get():
            with self.tick_lock:
                total_buffered = sum(len(v) for v in self.tick_log.values())
            if total_buffered:
                self.log(f"💾 Flushing {total_buffered:,} remaining ticks to disk...")
                self._flush_ticks_to_disk(label="final")
            else:
                self.log("ℹ️  No buffered ticks to flush.")

        # Save trades
        if self.completed_trades:
            self._save_live_trades()
        else:
            self.status_var.set("No Trades to Save.")

        # Pre-bot OC LTPs resume via the REST poll loop (start_oc_ltp_poll), which
        # keeps running while logged in and repaints once is_running flips False.
        # Also reconnect the lightweight pre-bot WebSocket so the chain keeps
        # ticking live instead of dropping back to 10s REST-only polling.
        self.root.after(500, self._start_pre_bot_oc_ws)

    # ==========================================================
    # POSITION RECONCILE (crash recovery / auto-resume)
    # ==========================================================
    def _fetch_broker_positions(self):
        """Best-effort net-long positions across enabled+logged-in brokers.

        Returns (by_token, by_key):
          by_token: {str(angel_token): net_qty}            (Angel — exact token match)
          by_key:   {(root,date,strike,type): net_qty}     (all brokers, parsed symbol)
        Response shapes vary by SDK version, so every field access is defensive.
        """
        by_token, by_key = {}, {}

        def _add_key(sym, qty):
            try:
                k = self._parse_angel_option_symbol(sym)
            except Exception:
                k = None
            if k:
                by_key[k] = by_key.get(k, 0) + qty

        # Angel — position() has symboltoken (== our token) + netqty
        if getattr(self, "angel_logged_in", False) and getattr(self, "smart", None):
            try:
                resp = self.smart.position()
                rows = (resp.get("data") if isinstance(resp, dict) else None) or []
                for r in rows:
                    try:
                        qty = int(float(r.get("netqty") or r.get("netQty") or 0))
                    except (TypeError, ValueError):
                        qty = 0
                    if qty <= 0:
                        continue
                    tok = str(r.get("symboltoken") or r.get("symbolToken") or "")
                    if tok:
                        by_token[tok] = by_token.get(tok, 0) + qty
                    sym = r.get("tradingsymbol") or r.get("tradingSymbol")
                    if sym:
                        _add_key(sym, qty)
            except Exception as e:
                self.log(f"⚠️  Angel position fetch failed: {e}")

        # Kotak — positions() (symbols are Kotak-format; parsed-key match best-effort)
        if getattr(self, "kotak_logged_in", False) and getattr(self, "kotak", None):
            try:
                resp = self.kotak.positions()
                rows = (resp.get("data") if isinstance(resp, dict) else None) or []
                for r in rows:
                    try:
                        buy  = float(r.get("flBuyQty") or r.get("buyQty") or 0)
                        sell = float(r.get("flSellQty") or r.get("sellQty") or 0)
                        qty  = int(buy - sell)
                    except (TypeError, ValueError):
                        qty = 0
                    if qty <= 0:
                        continue
                    sym = r.get("trdSym") or r.get("sym") or r.get("tradingsymbol")
                    if sym:
                        _add_key(sym, qty)
            except Exception as e:
                self.log(f"⚠️  Kotak position fetch failed: {e}")

        # Dhan — get_positions()
        if getattr(self, "dhan_logged_in", False) and getattr(self, "dhan", None):
            try:
                resp = self.dhan.get_positions()
                rows = (resp.get("data") if isinstance(resp, dict) else None) or []
                for r in rows:
                    try:
                        qty = int(float(r.get("netQty") or r.get("netqty") or 0))
                    except (TypeError, ValueError):
                        qty = 0
                    if qty <= 0:
                        continue
                    sym = r.get("tradingSymbol") or r.get("tradingsymbol")
                    if sym:
                        _add_key(sym, qty)
            except Exception as e:
                self.log(f"⚠️  Dhan position fetch failed: {e}")

        return by_token, by_key

    def _reconcile_positions(self, selected):
        """Restore same-day saved positions and auto-resume SL/target monitoring
        after a restart. Mutates `selected` in place so recovered tokens get
        subscribed by the pipeline startup that follows."""
        saved = self._load_state()
        if not saved:
            return

        # Restore daily counters so risk limits survive a same-day restart
        self.cumulative_pnl = saved.get("cumulative_pnl", self.cumulative_pnl)
        self.trades_today   = saved.get("trades_today", self.trades_today)

        positions = saved.get("positions", {})
        if not positions:
            return

        # Paper/Backtest: counters restored above; no broker to reconcile against.
        if self.trade_mode_var.get() != "Live":
            return

        by_token, by_key = self._fetch_broker_positions()
        resumed, closed = 0, 0
        existing_tokens = {t for _, _, t, _ in selected}

        for tok_str, p in positions.items():
            token   = tok_str   # live tick feed is string-keyed
            sym     = p.get("tradingsymbol")
            entry_px = p.get("entry_price")
            try:
                key = self._parse_angel_option_symbol(sym) if sym else None
            except Exception:
                key = None

            confirmed = (str(tok_str) in by_token and by_token[str(tok_str)] > 0) \
                or (key is not None and key in by_key and by_key[key] > 0)
            if not confirmed:
                closed += 1
                continue

            st = {
                "exchange":            p.get("exchange"),
                "tradingsymbol":       sym,
                "lot_size":            p.get("lot_size"),
                "strike":              p.get("strike"),
                "type":                p.get("type"),
                "trade_open":          True,
                "entry_taken_today":   True,
                "order_in_progress":   False,
                "close_in_progress":   False,
                "entry_price":         entry_px,
                "highest_price":       p.get("highest_price") or entry_px,
                "sl":                  p.get("sl"),
                "targets":             p.get("targets", []),
                "targets_hit":         p.get("targets_hit", []),
                "entry_band_triggered": False,
                "gexp_override":       False,
                "gexp_method":         "none",
                "gexp_tsl_step":       0,
                "ltp":                 entry_px,
            }
            with self.lock:
                self.strike_state[token] = st

            tgts = p.get("targets") or []
            self.running_positions[token] = {
                "Date":               dt.datetime.now().date(),
                "Token":              token,
                "Symbol":             f"{self.index_var.get()}{p.get('strike')}{p.get('type')}",
                "SL_Points":          (entry_px - p["sl"]) if (entry_px and p.get("sl") is not None) else 0,
                "Target_Points":      (tgts[0] - entry_px) if (tgts and entry_px) else 0,
                "EntryTime":          dt.datetime.now(),
                "EntryPrice":         entry_px,
                "IntendedEntryPrice": entry_px,
                "Qty":                p.get("lot_size"),
                "EMA": None, "RSI": None, "VWAP": None,
                "VWAP_SD1_UP": None, "VWAP_SD1_DOWN": None, "EntryVolume": None,
            }

            # Ensure subscription + LTP row for tokens not in today's selection
            if token not in existing_tokens:
                selected.append((p.get("strike"), p.get("type"), token, sym))
                existing_tokens.add(token)
                try:
                    self._add_strike_ltp_row(token, p.get("strike"),
                                             p.get("type"), p.get("lot_size"))
                except Exception:
                    pass

            self.root.after(0, lambda t=token, e=entry_px: self.add_trade_row(t, e))
            resumed += 1

        self.log(f"♻️  Reconcile: resumed {resumed}, already closed {closed}")
        if resumed or closed:
            self.root.after(0, lambda r=resumed, c=closed: messagebox.showinfo(
                "Position Reconcile",
                f"Resumed monitoring {r} open position(s).\n"
                f"{c} saved position(s) already closed at broker."))
        self._save_state()

    # ==========================================================
    # BROKER PIPELINE HELPERS
    # ==========================================================
    def _init_strike_state(self, token, strike, opt_type, tradingsymbol, exch, lot_size):
        """Return a fresh strike_state dict for one token."""
        return {
            "exchange":             exch,
            "tradingsymbol":        tradingsymbol,
            "lot_size":             lot_size,
            "strike":               strike,
            "type":                 opt_type,
            "trade_open":           False,
            "entry_taken_today":    False,
            "order_in_progress":    False,
            "close_in_progress":    False,
            "entry_price":          None,
            "highest_price":        0,
            "sl":                   None,
            "targets":              [],
            "targets_hit":          [],
            "entry_band_triggered": False,
            "gexp_override":        False,
            "gexp_method":          "none",
            "gexp_tsl_step":        0,
            "ltp":                  None,
            "oc_action":            "BUY",
        }

    def _build_kotak_token_map(self, selected, oc_tokens=None):
        """
        Map Angel scrip tokens → Kotak scrip tokens.
        Uses cached scrip master (built at Load Master time via _load_kotak_scrip_master)
        for O(1) lookups.  Falls back to per-token search_scrip() only for trade tokens
        if the cache is unavailable.
        Returns {kotak_token_str: angel_token_str}.
        """
        mapping = {}
        if not self.kotak_logged_in:
            return mapping
        index    = self.index_var.get().upper()
        exch_seg = {"SENSEX": "bse_fo", "CRUDEOIL": "mcx_fo"}.get(index, "nse_fo")
        cache    = getattr(self, "_kotak_scrip_cache", {}).get(exch_seg, {})

        # Build full list: trade tokens + OC tokens
        trade_set = {at for _, _, at, _ in selected}
        items = [(at, sym) for _, _, at, sym in selected]
        if oc_tokens:
            for row in self.oc_data:
                for tok_key, sym_key in [("ce_token", "ce_sym"), ("pe_token", "pe_sym")]:
                    at  = row.get(tok_key, "")
                    sym = row.get(sym_key, "")
                    if at and sym and at in oc_tokens and at not in trade_set:
                        items.append((at, sym))

        total          = len(items)
        _sample_logged = False
        _cache_sampled = False
        print(f"[Kotak] token map: exch={exch_seg}, cache_size={len(cache)}, items={total}")
        for angel_token, tradingsymbol in items:
            kotak_sym = self._angel_to_kotak_symbol(tradingsymbol)
            if not kotak_sym:
                continue
            is_trade = angel_token in trade_set
            print(f"[Kotak] {'TRADE' if is_trade else 'OC   '} {tradingsymbol!r:30s} → {kotak_sym!r}")

            # Fast path: O(1) lookup from pre-downloaded scrip master.
            # cache keys are pTrdSymbol strings; we map pTrdSymbol → angel_token
            # so the WS "tk" field (also pTrdSymbol) maps directly to angel_token.
            if cache:
                if kotak_sym in cache:
                    print(f"[Kotak]   cache HIT  → sym={kotak_sym}")
                    mapping[kotak_sym] = angel_token
                    continue
                print(f"[Kotak]   cache MISS")
                # Cache loaded but symbol not found — skip OC tokens silently
                if angel_token not in trade_set:
                    continue

            # Slow fallback via search_scrip() — trade tokens only, used when cache missing
            if angel_token not in trade_set:
                continue
            try:
                results = self.kotak.search_scrip(
                    exchange_segment=exch_seg, symbol=kotak_sym)
                matched = False
                if isinstance(results, list):
                    if not _sample_logged and results:
                        avail = [r.get("pTrdSymbol") or r.get("symbol") or ""
                                 for r in results[:3]]
                        print(f"[Kotak] sample results for '{kotak_sym}': {avail}")
                        _sample_logged = True
                    elif not _sample_logged and not results:
                        print(f"[Kotak] search_scrip('{exch_seg}', '{kotak_sym}') → empty")
                        _sample_logged = True
                    for r in results:
                        # pTrdSymbol = trading symbol string (WS "tk" field)
                        trd_sym = str(r.get("pTrdSymbol") or r.get("symbol") or r.get("sym", ""))
                        if trd_sym == kotak_sym and trd_sym:
                            mapping[trd_sym] = angel_token
                            matched = True
                            break
                        elif not matched and trd_sym and kotak_sym[-4:] in trd_sym:
                            mapping[trd_sym] = angel_token
                            matched = True
                elif isinstance(results, dict):
                    for r in (results.get("data") or []):
                        trd_sym = str(r.get("pTrdSymbol") or r.get("symbol") or "")
                        if trd_sym and (trd_sym == kotak_sym or kotak_sym[-4:] in trd_sym):
                            mapping[trd_sym] = angel_token
                            break
            except Exception as e:
                self.log(f"⚠️  Kotak token lookup failed for {tradingsymbol}: {e}")

        trade_mapped = sum(1 for t in mapping.values() if t in trade_set)
        oc_mapped    = len(mapping) - trade_mapped
        self.log(f"📡 Kotak token map: {trade_mapped} trade + {oc_mapped} OC = "
                 f"{len(mapping)}/{total} tokens mapped "
                 f"({'cache' if cache else 'search_scrip'})")
        if trade_mapped == 0 and len(trade_set) > 0:
            self.log(f"⚠️  Kotak trade tokens not mapped — check exchange: {exch_seg}, "
                     f"index: {index}")
        return mapping

    def _build_dhan_token_map(self, selected, oc_tokens=None):
        """
        Build {dhan_security_id_str: angel_token_str} by parsing
        Dhan's scrip master CSV (downloaded at Load Master time, or on demand).
        Also populates self._dhan_token_cache = {angel_token: dhan_security_id}.

        Dhan CSV format: symbol like 'NIFTY-Apr2026-24150-CE',
        SEM_EXM_EXCH_ID = 'NSE'/'BSE', SEM_SEGMENT = 'D'.
        Angel format: 'NIFTY21APR2624150CE' — must be parsed into the same key.
        oc_tokens: list of Angel token strings for OC panel rows (not in selected).
        """
        mapping = {}
        if not self.dhan_logged_in:
            return mapping

        # Parse scrip master CSV (use pre-downloaded file if available)
        if not getattr(self, "_dhan_scrip_loaded", False):
            try:
                import csv as _csv
                csv_path = getattr(self, "_dhan_scrip_master_path", None)
                if csv_path and os.path.exists(csv_path):
                    self.log("🔄 Dhan: loading scrip master from cache...")
                    open_fn = lambda: open(csv_path, encoding="utf-8")
                else:
                    import requests as _req, io as _io
                    self.log("🔄 Dhan: downloading scrip master...")
                    r = _req.get(
                        "https://images.dhan.co/api-data/api-scrip-master.csv",
                        timeout=20)
                    r.raise_for_status()
                    _text = r.text
                    open_fn = lambda: _io.StringIO(_text)
                if not hasattr(self, "_dhan_scrip_cache"):
                    self._dhan_scrip_cache = {}
                count = 0
                with open_fn() as _f:
                    for row in _csv.DictReader(_f):
                        if (row.get("SEM_SEGMENT") not in ("D", "B")
                                or row.get("SEM_INSTRUMENT_NAME") not in ("OPTIDX", "OPTFUT", "OPTSTK")):
                            continue
                        sid    = row.get("SEM_SMST_SECURITY_ID", "").strip()
                        sym    = row.get("SEM_TRADING_SYMBOL", "").strip()
                        expiry = row.get("SEM_EXPIRY_DATE", "").strip()
                        strike = row.get("SEM_STRIKE_PRICE", "").strip()
                        opt_t  = row.get("SEM_OPTION_TYPE", "").strip()
                        if not (sid and sym and expiry and strike and opt_t):
                            continue
                        root = sym.split("-")[0]
                        date_str = expiry[:10]
                        try:
                            strike_int = int(float(strike))
                        except ValueError:
                            continue
                        self._dhan_scrip_cache[(root, date_str, strike_int, opt_t)] = sid
                        count += 1
                self._dhan_scrip_loaded = True
                self.log(f"📡 Dhan scrip master loaded: {count} F&O option entries")
            except Exception as e:
                self.log(f"⚠️  Dhan token map error: {e}")
                return mapping

        from app.order_manager import OrderManagerMixin as _OM

        # Build full list: trade tokens + OC tokens
        items = [(angel_token, tradingsymbol)
                 for _, _, angel_token, tradingsymbol in selected]

        if oc_tokens:
            trade_angel_tokens = {t for _, _, t, _ in selected}
            for row in self.oc_data:
                for tok_key, sym_key in [("ce_token", "ce_sym"), ("pe_token", "pe_sym")]:
                    at  = row.get(tok_key, "")
                    sym = row.get(sym_key, "")
                    if at and sym and at in oc_tokens and at not in trade_angel_tokens:
                        items.append((at, sym))

        total = len(items)
        for angel_token, tradingsymbol in items:
            key = _OM._parse_angel_option_symbol(tradingsymbol)
            if key is None:
                self.log(f"⚠️  Dhan: cannot parse '{tradingsymbol}'")
                continue
            did = self._dhan_scrip_cache.get(key)
            if did:
                mapping[did] = angel_token
                self._dhan_token_cache[angel_token] = did
            else:
                self.log(f"⚠️  Dhan: no match for {tradingsymbol} (key={key})")

        self.log(f"📡 Dhan token map: {len(mapping)}/{total} tokens mapped")
        return mapping

    # ==========================================================
    # PRE-CONNECT DATA FEED  (independent of Start Bot)
    # ==========================================================
    def connect_data_feed(self):
        """Called by the top-bar Connect button. Routes to the selected data feed broker."""
        feed = self.data_feed_broker_var.get()
        self.log(f"🔄 Connecting {feed} data feed...")
        self.feed_indicator.config(fg="orange")

        if feed == "Angel One":
            if not getattr(self, "angel_logged_in", False):
                self.log("❌ Angel One not logged in — login first")
                self.feed_indicator.config(fg="red")
                return
            self.selected_feed = "Angel One"
            self.feed_indicator.config(fg="green")
            self.log("✅ Angel One data feed selected (WebSocket connects on Start Bot)")
        elif feed == "Kotak Neo":
            if not getattr(self, "kotak_logged_in", False):
                self.log("❌ Kotak Neo not logged in — login first")
                self.feed_indicator.config(fg="red")
                return
            self.selected_feed = "Kotak Neo"
            self.feed_indicator.config(fg="orange")
            self.log("✅ Kotak Neo selected — connecting OC feed...")
            # Delegate to the same pre-bot WS path used by OC reloads;
            # avoids having two separate KDE instances competing for the NeoAPI client.
            self.root.after(0, self._start_pre_bot_oc_ws)
        elif feed == "Dhan HQ":
            if not getattr(self, "dhan_logged_in", False):
                self.log("❌ Dhan HQ not logged in — login first")
                self.feed_indicator.config(fg="red")
                return
            self.feed_indicator.config(fg="orange")
            threading.Thread(target=self._connect_dhan_feed,
                             daemon=True, name="dhan-preconnect").start()
        else:
            self.log(f"⚠️  Unknown data feed selection: {feed}")
            self.feed_indicator.config(fg="red")

    def _connect_dhan_feed(self):
        """Background thread: build Dhan token map from OC data and start WebSocket."""
        try:
            # Stop any previously pre-connected Dhan engine
            prev = getattr(self, "active_feed_engine", None)
            if prev is not None:
                try:
                    prev.stop()
                except Exception:
                    pass
                self.active_feed_engine = None

            # Build token map from current OC panel rows (no strike selection yet)
            oc_tokens = [row[k] for row in self.oc_data
                         for k in ("ce_token", "pe_token") if row.get(k)]
            dhan_map = self._build_dhan_token_map([], oc_tokens)

            from engines.dhan_data_engine import DhanDataEngine
            index = self.index_var.get().upper()
            dhan_exch_const = (DhanDataEngine.BSE_FNO
                               if index in ("SENSEX", "BANKEX")
                               else DhanDataEngine.NSE_FNO)
            exch_map = {did: dhan_exch_const for did in dhan_map}

            dde = DhanDataEngine(self.dhan_context, dhan_map)
            dde._app_ref = self

            def _on_tick(token, ltp, cum_vol, now):
                self.update_oc_ltp(token, ltp)
                # Update strike panel label and state if this token was added from OC
                if token in self.strike_ltp_labels:
                    lbl = self.strike_ltp_labels[token]
                    _buf = getattr(self, "_lbl_pending", None)
                    if _buf is not None:
                        _buf[f"sl_{token}"] = (lbl, str(round(ltp, 2)))
                    else:
                        self.root.after(0, lambda l=lbl, v=ltp: l.config(text=str(round(v, 2))))
                with self.lock:
                    st = self.strike_state.get(token)
                    if st:
                        st["ltp"] = ltp

            dde.on_tick_cb = _on_tick
            if dhan_map:
                dde.subscribe(list(dhan_map.keys()), exch_map=exch_map)
            else:
                self.log("⚠️  Dhan: no OC tokens mapped — refresh OC then reconnect")

            self.active_feed_engine = dde
            self.selected_feed = "Dhan HQ"
            self.root.after(0, lambda: self.feed_indicator.config(fg="green"))
            self.log(f"✅ Dhan HQ data feed connected ({len(dhan_map)} OC tokens)")
        except Exception as e:
            self.log(f"❌ Dhan pre-connect failed: {e}")
            self.root.after(0, lambda: self.feed_indicator.config(fg="red"))

    def _stop_all_pipelines(self):
        """Null out callbacks on all live pipeline objects so stale instances can't place orders."""
        for pl in list(getattr(self, "_broker_pipelines", {}).values()):
            qfe = pl.get("qfe")
            if qfe:
                qfe.on_signal = None
            ce = pl.get("ce")
            if ce:
                ce.on_candle_close = None
            for eng_key in ("oce", "kde", "dde"):
                eng = pl.get(eng_key)
                if eng:
                    eng.app_ref = None  # disables process_tick_entry even if library reconnects
                    if hasattr(eng, "stop"):
                        try:
                            eng.stop()
                        except Exception:
                            pass
            tee = pl.get("tee")
            if tee and hasattr(tee, "stop"):
                try:
                    tee.stop()
                except Exception:
                    pass
        self._broker_pipelines = {}

        # Stop every OCE ever created (including those that reconnected via library retry
        # after being removed from _broker_pipelines, causing orphaned retry loops).
        for _oce in list(getattr(self, "_all_oces", [])):
            _oce.app_ref = None
            if hasattr(_oce, "stop"):
                try:
                    _oce.stop()
                except Exception:
                    pass
        self._all_oces = []
        self._pre_bot_oce = None   # stopped above (if it existed) — clear so a
                                    # later call to _start_pre_bot_oc_ws() reconnects

    def _start_broker_pipelines(self, selected, exch, lot_size, seed_data, oc_tokens):
        """
        Start ONE data pipeline based on the Data Feed dropdown selection.
        The selected broker's WebSocket drives ticks → candles → OC display → signals.
        Orders are placed via place_order() to ALL checked + logged-in brokers.
        """
        feed = self.data_feed_broker_var.get()     # "Angel One" | "Kotak Neo" | "Dhan HQ"
        self.oc_feed_var.set(feed)                  # keep oc_feed_var in sync
        self._stop_all_pipelines()  # tear down any previously running pipeline

        # Stop pre-bot OC WebSocket engine before bot creates its own pipeline
        _pre_oc = getattr(self, "_oc_engine", None)
        if _pre_oc:
            try:
                _pre_oc.stop()
            except Exception:
                pass
            self._oc_engine = None

        trade_tokens = [t for _, _, t, _ in selected]
        broker_ss    = {t: self._init_strike_state(t, s, o, sym, exch, lot_size)
                        for s, o, t, sym in selected}

        # Unify state: replace self.strike_state with broker_ss so that
        # open_trade(), QFE, add_trade_row(), and tick SL callbacks all
        # read/write the same dict.  Carry forward any ltp already set
        # by a pre-connect tick or the OC panel seed.
        with self.lock:
            for token, bss in broker_ss.items():
                existing = self.strike_state.get(token)
                if existing and existing.get("ltp"):
                    bss["ltp"] = existing["ltp"]
            # Preserve OC-panel tokens that aren't strategy tokens —
            # they may have open trades placed before the bot started.
            # Without this, strike_state.get(token) returns None for OC
            # trades, causing P&L to stay at 0 and close buttons to fail.
            for token, oc_st in list(self.strike_state.items()):
                if token not in broker_ss:
                    broker_ss[token] = oc_st
            self.strike_state = broker_ss

        def _register_tokens(ce):
            for token, data in self.strike_state.items():
                sd = seed_data.get(token, {})
                ce.register_token(
                    token,
                    lot_size=data["lot_size"],
                    seed_df=sd.get("df"),
                    seed_last_cum_vol=sd.get("last_cum_vol", 0),
                    seed_current_candle=sd.get("current_candle"),
                    seed_last_candle_minute=sd.get("last_candle_minute"),
                )

        # ── Shared tick handler factory ──────────────────────────
        # Each feed engine calls this with its own ce/tee references.
        def _make_tick_handler(ce, tee):
            def _on_tick(token, ltp, cum_vol, now):
                self._record_tick(token, ltp, cum_vol, now)
                ce._on_tick(token, ltp, cum_vol, now)
                self.update_oc_ltp(token, ltp)
                if token in self.strike_ltp_labels:
                    lbl = self.strike_ltp_labels[token]
                    _buf = getattr(self, "_lbl_pending", None)
                    if _buf is not None:
                        _buf[f"sl_{token}"] = (lbl, str(round(ltp, 2)))
                    else:
                        self.root.after(0, lambda l=lbl, v=ltp: l.config(text=str(round(v, 2))))
                with self.lock:
                    st = self.strike_state.get(token)
                    if st:
                        st["ltp"] = ltp
                        if (not st["trade_open"]
                                and not st["entry_taken_today"]
                                and not st["order_in_progress"]):
                            if self.entry_band_hit(ltp):
                                st["entry_band_triggered"] = True
                st_shared = self.strike_state.get(token)
                if st_shared and st_shared["trade_open"]:
                    tee.on_tick(token, ltp)
            return _on_tick

        # ── Route to the broker whose WebSocket was selected ──
        if feed == "Angel One":
            if not self.angel_logged_in:
                self.log("❌ Data Feed is Angel One but Angel not logged in")
                self.is_running = False
                return
            oce_exch_map  = {"NFO": 2, "BFO": 4, "MCX": 5}
            token_list_ws = [{"exchangeType": oce_exch_map.get(exch, 2),
                               "tokens": trade_tokens + oc_tokens}]

            _canary_token, _canary_exch = self.get_spot_token()
            _canary_exch_type = {"NSE": 1, "BSE": 3, "MCX": 5}.get(_canary_exch, 1)

            oce_a = OptionChainEngine(
                self.jwt_token, config.API_KEY,
                self.client_code, self.feed_token)
            oce_a.app_ref = self
            self._all_oces.append(oce_a)
            oce_a.subscribe(token_list_ws,
                            canary_token=_canary_token,
                            canary_exchange_type=_canary_exch_type)

            ce_a  = CandleEngine(oce_a,
                                 interval=self.live_interval_var.get(),
                                 ema_period=self.ema_period_var.get(),
                                 rsi_period=self.rsi_period_var.get())
            _register_tokens(ce_a)
            for token in self.strike_state:
                print(f"[VolumeSync/Angel] {token}: seed = {seed_data.get(token,{}).get('last_cum_vol',0):,}")

            qfe_a = QuantFilterEngine(self)
            tee_a = TradeExecutionEngine(self)

            def _on_candle_a(token, df, _tee=tee_a, _qfe=qfe_a, _oce=oce_a):
                _qfe.process(token, df)
                ltp = _oce.get_ltp(token)
                st  = self.strike_state.get(token)
                if ltp and st and st["trade_open"]:
                    _tee.on_tick(token, ltp)

            ce_a.on_candle_close = _on_candle_a
            qfe_a.on_signal = lambda tok, ltp, df: self._on_signal_broker(
                tok, ltp, df, broker="angel")
            oce_a.on_tick_cb = _make_tick_handler(ce_a, tee_a)
            self._broker_pipelines["angel"] = {
                "oce": oce_a, "ce": ce_a, "qfe": qfe_a, "tee": tee_a,
                "strike_state": broker_ss,
            }
            self.log("🟢 Angel One pipeline started (data feed)")

        elif feed == "Kotak Neo":
            if not self.kotak_logged_in:
                self.log("❌ Data Feed is Kotak Neo but Kotak not logged in")
                self.is_running = False
                return
            from engines.kotak_data_engine import KotakDataEngine as _KDE
            index    = self.index_var.get().upper()
            exch_seg = {"SENSEX": "bse_fo", "CRUDEOIL": "mcx_fo"}.get(index, "nse_fo")
            kotak_map = self._build_kotak_token_map(selected, oc_tokens)
            if not kotak_map:
                self.log("⚠️  Kotak token map empty — check scrip master / expiry")

            kde = _KDE(self.kotak, kotak_map, exchange_segment=exch_seg)
            kde.app_ref = self
            kde.subscribe(list(kotak_map.keys()))

            ce_k  = CandleEngine(kde,
                                 interval=self.live_interval_var.get(),
                                 ema_period=self.ema_period_var.get(),
                                 rsi_period=self.rsi_period_var.get())
            _register_tokens(ce_k)

            qfe_k = QuantFilterEngine(self)
            tee_k = TradeExecutionEngine(self)

            def _on_candle_k(token, df, _tee=tee_k, _qfe=qfe_k, _kde=kde):
                _qfe.process(token, df)
                ltp = _kde.get_ltp(token)
                st  = self.strike_state.get(token)
                if ltp and st and st["trade_open"]:
                    _tee.on_tick(token, ltp)

            ce_k.on_candle_close = _on_candle_k
            qfe_k.on_signal = lambda tok, ltp, df: self._on_signal_broker(
                tok, ltp, df, broker="kotak")
            kde.on_tick_cb = _make_tick_handler(ce_k, tee_k)
            self._broker_pipelines["kotak"] = {
                "kde": kde, "ce": ce_k, "qfe": qfe_k, "tee": tee_k,
                "strike_state": broker_ss,
            }
            self.log(f"🟢 Kotak Neo pipeline started (data feed, {len(kotak_map)} tokens)")

        elif feed == "Dhan HQ":
            if not self.dhan_logged_in:
                self.log("❌ Data Feed is Dhan HQ but Dhan not logged in")
                self.is_running = False
                return
            from engines.dhan_data_engine import DhanDataEngine as _DDE
            index = self.index_var.get().upper()
            dhan_exch_const = (_DDE.BSE_FNO if index in ("SENSEX", "BANKEX")
                               else _DDE.NSE_FNO)
            dhan_map = self._build_dhan_token_map(selected, oc_tokens)
            exch_map_dhan = {did: dhan_exch_const for did in dhan_map}
            if not dhan_map:
                self.log("⚠️  Dhan token map empty — check scrip master / expiry")

            dde = _DDE(self.dhan_context, dhan_map)
            dde._app_ref = self
            dde.subscribe(list(dhan_map.keys()), exch_map=exch_map_dhan)

            ce_d  = CandleEngine(dde,
                                 interval=self.live_interval_var.get(),
                                 ema_period=self.ema_period_var.get(),
                                 rsi_period=self.rsi_period_var.get())
            _register_tokens(ce_d)

            qfe_d = QuantFilterEngine(self)
            tee_d = TradeExecutionEngine(self)

            def _on_candle_d(token, df, _tee=tee_d, _qfe=qfe_d, _dde=dde):
                _qfe.process(token, df)
                ltp = _dde.get_ltp(token)
                st  = self.strike_state.get(token)
                if ltp and st and st["trade_open"]:
                    _tee.on_tick(token, ltp)

            ce_d.on_candle_close = _on_candle_d
            qfe_d.on_signal = lambda tok, ltp, df: self._on_signal_broker(
                tok, ltp, df, broker="dhan")
            dde.on_tick_cb = _make_tick_handler(ce_d, tee_d)
            self._broker_pipelines["dhan"] = {
                "dde": dde, "ce": ce_d, "qfe": qfe_d, "tee": tee_d,
                "strike_state": broker_ss,
            }
            self.log(f"🟢 Dhan HQ pipeline started (data feed, {len(dhan_map)} tokens)")

        else:
            self.log(f"❌ Unknown Data Feed selection: '{feed}' — cannot start pipeline")
            self.is_running = False
            return

        # ── Backward-compat references ────────────────────────
        first     = next(iter(self._broker_pipelines.values()))
        self.oce  = first.get("oce") or first.get("kde") or first.get("dde")
        self.ce   = first.get("ce")
        self.qfe  = first.get("qfe")
        self.tee  = first.get("tee")
        self.log(f"📡 Data feed: {feed} | Orders → all checked brokers")

    def _update_ltp_ui(self, token, ltp):
        """Thread-safe update of Strike LTP label + OC LTP label."""
        if token in self.strike_ltp_labels:
            lbl = self.strike_ltp_labels[token]
            self.root.after(0, lambda l=lbl, v=ltp: l.config(text=str(v)))
        self.update_oc_ltp(token, ltp)

    def _on_signal_broker(self, token, ltp, df, broker):
        """Called by a broker-specific QFE when entry conditions are met."""
        # Manual entry mode — suppress all automatic entries
        if self.trade_exec_mode_var.get() == "Manual":
            return
        # self.strike_state IS broker_ss (unified in _start_broker_pipelines),
        # so no swap needed — just guard against duplicate entry
        st = self.strike_state.get(token)
        if not st or st["trade_open"] or st["entry_taken_today"]:
            return
        self.open_trade(token, ltp, broker=broker)

    def _save_live_trades(self):
        file_name = f"Strategy2_Trades_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        file_path = os.path.join(self.output_folder.get(), file_name)
        try:
            with pd.ExcelWriter(file_path, engine="xlsxwriter") as writer:
                wb      = writer.book
                hdr_fmt = wb.add_format({"bold": True, "bg_color": "#1e3a5f",
                                          "font_color": "white", "border": 1})
                key_fmt = wb.add_format({"bold": True, "bg_color": "#2b2b2b",
                                          "font_color": "#00e676", "border": 1})
                val_fmt = wb.add_format({"bg_color": "#1e1e1e",
                                          "font_color": "white", "border": 1})
                ws = wb.add_worksheet("Summary")
                writer.sheets["Summary"] = ws
                ws.set_column(0, 0, 28)
                ws.set_column(1, 1, 30)
                gexp_on     = self.gexp_override_var.get()
                gexp_method = self.gexp_method_var.get()
                params = [
                    ("Trade Date",    dt.datetime.now().strftime("%d %b %Y")),
                    ("Trade Mode",    self.trade_mode_var.get()),
                    ("Index",         self.index_var.get()),
                    ("Spot Time",     self.spot_entry.get_value()),
                    ("Candle Interval", self.live_interval_var.get()),
                    ("Entry Method",  self.entry_mode_var.get()),
                    ("Entry Price",   self.numeric_entries["Entry Price"].get_value() or "—"),
                    ("Tolerance",     self.numeric_entries["Tolerance"].get_value() or "—"),
                    # Day Profile values (Config Panel Order Params) — what
                    # open_trade()/open_short_trade() actually applied,
                    # not the independent Strategy Configuration fields.
                    ("SL Points",     self.cfg_sl_pts_var.get() or "—"),
                    ("Target Points", self.cfg_target_pts_var.get() or "—"),
                    ("--- TSL ---",   ""),
                    ("TSL Enabled",   self.enable_tsl_var.get()),
                    ("TSL Step",      self.tsl_step_var.get()),
                    ("--- GExp ---",  ""),
                    ("GExp Override", gexp_on),
                    ("GExp Method",   "Approach 1" if gexp_method == "approach1"
                                      else "Approach 2" if gexp_on else "—"),
                    ("GExp SL %",     f"{self.gexp_sl_pct_var.get()}%" if gexp_on else "—"),
                    ("GExp R:R",      self.gexp_rr_var.get() if (gexp_on and gexp_method == "approach1") else "—"),
                    ("GExp TSL Step", self.gexp_tsl_step_var.get() if (gexp_on and gexp_method == "approach2") else "—"),
                    ("--- Stats ---", ""),
                    ("Total Trades",  len(self.completed_trades)),
                    ("Total P&L",     round(sum(t.get("P&L", 0) for t in self.completed_trades), 2)),
                ]
                ws.write(0, 0, "Parameter", hdr_fmt)
                ws.write(0, 1, "Value",     hdr_fmt)
                for r, (k, v) in enumerate(params, start=1):
                    if str(k).startswith("---"):
                        ws.write(r, 0, k, hdr_fmt)
                        ws.write(r, 1, "", hdr_fmt)
                    else:
                        ws.write(r, 0, k, key_fmt)
                        ws.write(r, 1, str(v), val_fmt)

                df_trades = pd.DataFrame(self.completed_trades)
                df_trades.to_excel(writer, sheet_name="Trades", index=False)

                # Candle data sheets
                if self.ce:
                    for token, st in self.strike_state.items():
                        df = self.ce.get_candles(token)
                        if df is None or df.empty:
                            continue
                        symbol = f"{st['strike']}{st['type']}"
                        out    = df.copy()
                        out["time"] = pd.to_datetime(out["time"])
                        if out["time"].dt.tz is not None:
                            out["time"] = out["time"].dt.tz_localize(None)
                        sheet = f"{symbol}_{token}"[:31]
                        out.to_excel(writer, sheet_name=sheet, index=False)

                # Tick data info sheet (actual tick CSVs/xlsx live in tick_data/ folder)
                if self.save_ticks_var.get():
                    tick_info_rows = []
                    tick_dir = self._get_tick_folder()
                    for token, st in self.strike_state.items():
                        symbol   = f"{st.get('strike','')}{st.get('type','')}"
                        date_str = dt.datetime.now().strftime("%Y%m%d")
                        ext      = "csv" if self.tick_save_format_var.get() == "CSV" else "xlsx"
                        fname    = f"{symbol}_{token}_ticks_{date_str}.{ext}"
                        fpath    = os.path.join(tick_dir, fname)
                        exists   = os.path.exists(fpath)
                        rows_saved = 0
                        if exists and ext == "csv":
                            try:
                                rows_saved = sum(1 for _ in open(fpath)) - 1
                            except Exception:
                                pass
                        tick_info_rows.append({
                            "Token":    token,
                            "Symbol":   symbol,
                            "File":     fname,
                            "Saved":    exists,
                            "Rows":     rows_saved,
                            "Folder":   tick_dir,
                        })
                    if tick_info_rows:
                        df_ti = pd.DataFrame(tick_info_rows)
                        df_ti.to_excel(writer, sheet_name="TickData_Index",
                                       index=False)
                        wsti = writer.sheets["TickData_Index"]
                        wsti.set_column("A:B", 14)
                        wsti.set_column("C:C", 45)
                        wsti.set_column("F:F", 50)

            print("Trades saved:", file_path)
            self.status_var.set(f"Trades Saved: {file_name}")
        except Exception as e:
            print("Save error:", e)
            self.status_var.set(f"Save error: {e}")

    # ==========================================================
    # BACKTEST  (all logic preserved from v8)
