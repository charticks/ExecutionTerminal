import os
import re
import time
import threading
import datetime as dt

import pandas as pd
import tkinter as tk
from tkinter import filedialog, messagebox


class BacktestMixin:
    """All backtest methods."""

    def toggle_backtest_panel(self):
        if self.bt_frame is None:
            return
        if self.trade_mode_var.get() == "Backtest":
            self.bt_frame.pack(after=self.mode_frame_ref, pady=10, fill="x")
        else:
            self.bt_frame.pack_forget()

    def _read_file_to_df(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext == ".csv":
            return pd.read_csv(path)
        elif ext in [".xlsx", ".xls"]:
            xl     = pd.ExcelFile(path)
            sheets = xl.sheet_names
            if len(sheets) == 1:
                return xl.parse(sheets[0])
            sheet_win = tk.Toplevel(self.root)
            sheet_win.title("Select Sheet")
            sheet_win.geometry("320x260")
            sheet_win.configure(bg="#1e1e1e")
            sheet_win.grab_set()
            tk.Label(sheet_win, text="Select Sheet to Load:",
                     bg="#1e1e1e", fg="white",
                     font=("Segoe UI", 11, "bold")).pack(pady=10)
            lb = tk.Listbox(sheet_win,
                            listvariable=tk.StringVar(value=sheets),
                            selectmode="single", bg="#2b2b2b", fg="white",
                            font=("Segoe UI", 10),
                            height=min(len(sheets), 8))
            lb.pack(fill="x", padx=15)
            lb.select_set(0)
            result = {"sheet": None}
            def confirm():
                sel = lb.curselection()
                result["sheet"] = sheets[sel[0]] if sel else sheets[0]
                sheet_win.destroy()
            tk.Button(sheet_win, text="Load Selected Sheet",
                      bg="#00bcd4", fg="white",
                      command=confirm).pack(pady=12)
            self.root.wait_window(sheet_win)
            sheet = result["sheet"] or sheets[0]
            return xl.parse(sheet)
        else:
            raise ValueError(f"Unsupported format: {ext}")

    def _normalize_df_columns(self, df):
        df = df.copy()
        df.columns = [c.lower().strip() for c in df.columns]
        if "date" in df.columns and "time" in df.columns:
            df["datetime"] = pd.to_datetime(
                df["date"].astype(str).str.strip() + " " +
                df["time"].astype(str).str.strip(),
                dayfirst=True, errors="coerce")
            df.drop(columns=["date", "time"], inplace=True)
        else:
            for col in ["datetime","timestamp","dt","date time",
                        "date/time","candle_time"]:
                if col in df.columns:
                    df.rename(columns={col: "datetime"}, inplace=True)
                    break
            if "datetime" not in df.columns:
                raise ValueError("No datetime column found.")
            df["datetime"] = pd.to_datetime(
                df["datetime"], dayfirst=True, errors="coerce")
        df = df.dropna(subset=["datetime"])
        return df.sort_values("datetime").reset_index(drop=True)

    def load_bt_index_file(self):
        path = filedialog.askopenfilename(
            title="Select Index Data File",
            filetypes=[("All Supported","*.csv *.xlsx *.xls"),
                       ("CSV","*.csv"),("Excel","*.xlsx *.xls"),
                       ("All","*.*")])
        if not path:
            return
        try:
            raw_df = self._read_file_to_df(path)
            df     = self._normalize_df_columns(raw_df)
            df["date"] = df["datetime"].dt.date
            required = ["open","high","low","close"]
            for col in required:
                if col not in df.columns:
                    messagebox.showerror("Error",
                        f"Index file missing column: '{col}'")
                    return
            if "volume" not in df.columns:
                df["volume"] = 1
            for col in ["open","high","low","close","volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["open","high","low","close"])
            self.bt_index_df = df
            fname = os.path.basename(path)
            self.bt_index_label.config(
                text=f"{fname}  ({len(df):,} rows)", fg="#00e676")
            self.log(f"Index data loaded: {len(df):,} rows | "
                     f"{df['datetime'].min().date()} → "
                     f"{df['datetime'].max().date()}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load index:\n{e}")

    def load_bt_options_folder(self):
        folder = filedialog.askdirectory(
            title="Select Options Data Folder")
        if folder:
            self.bt_options_folder_var.set(folder)
            total_xlsx = sum(
                1 for _, _, files in os.walk(folder)
                for f in files if f.lower().endswith((".csv", ".xlsx", ".xls"))
            )
            self.bt_options_label.config(
                text=f"{os.path.basename(folder)}  ({total_xlsx} files)",
                fg="#00e676")
            self.log(f"Options folder: {folder} | {total_xlsx} files (incl. subfolders)")

    def start_backtest(self):
        if self.bt_index_df is None:
            messagebox.showerror("Error", "Load Index CSV first")
            return
        if not self.bt_options_folder_var.get():
            messagebox.showerror("Error", "Select Options Data Folder first")
            return
        if not self.spot_entry.get_value():
            messagebox.showerror("Error", "Spot Time is required (e.g. 09:20)")
            return
        is_market   = self.entry_mode_var.get() == "MARKET"
        gexp_on     = self.gexp_override_var.get()
        skip_fields = set()
        if is_market:
            skip_fields |= {"Entry Price","Tolerance"}
        if gexp_on:
            skip_fields |= {"SL Points","Target Points"}
        for name, entry in self.numeric_entries.items():
            if name in skip_fields:
                continue
            if not entry.get_value():
                messagebox.showerror("Error", f"{name} is required")
                return
        self.bt_results    = []
        self.completed_trades = []
        self.cumulative_pnl   = 0
        self.is_running       = True
        self.status_var.set("Backtest Running...")
        self.start_indicator.config(fg="#ffd740")
        threading.Thread(target=self._run_backtest_thread,
                         daemon=True).start()

    def _run_backtest_thread(self):
        try:
            from_date = pd.to_datetime(self.bt_from_date_var.get()).date()
            to_date   = pd.to_datetime(self.bt_to_date_var.get()).date()
            all_dates = sorted([
                d for d in self.bt_index_df["date"].unique()
                if from_date <= d <= to_date])
            total = len(all_dates)
            if total == 0:
                self.log("No trading days found in selected date range")
                self.is_running = False
                return
            self.log(f"Starting backtest: {total} trading days | "
                     f"{from_date} → {to_date}")
            self.bt_progress_bar["maximum"] = total
            for i, date in enumerate(all_dates):
                if not self.is_running:
                    self.log("Backtest stopped by user")
                    break
                date_str = date.strftime("%Y-%m-%d")
                _msg = f"[{i+1}/{total}] Processing {date_str}..."
                _val = i + 1
                self.root.after(0, lambda m=_msg, v=_val: (
                    self.bt_progress_var.set(m),
                    self.bt_progress_bar.config(value=v),
                ))
                try:
                    self._simulate_day(date)
                except Exception as e:
                    self.log(f"  Warning: Error on {date_str}: {e}")
                    continue
            self._save_bt_results()
        except Exception as e:
            self.log(f"Backtest thread error: {e}")
            import traceback
            self.log(traceback.format_exc())
        finally:
            self.is_running = False
            self.start_indicator.config(fg="red")
            self.bt_progress_var.set("Backtest Complete")

    def _simulate_day(self, date):
        date_str      = date.strftime("%Y-%m-%d")
        spot_time_str = self.spot_entry.get_value()
        try:
            spot_time = dt.datetime.strptime(spot_time_str, "%H:%M").time()
        except Exception:
            self.log(f"  Invalid spot time: {spot_time_str}")
            return
        spot_dt   = dt.datetime.combine(date, spot_time)
        day_index = self.bt_index_df[self.bt_index_df["date"] == date].copy()
        if day_index.empty:
            return

        # ── Resolve spot / trigger based on detection mode ───
        spot_detect_mode = self.spot_detect_mode_var.get()

        if spot_detect_mode == "TIME":
            spot_candles = day_index[day_index["datetime"] <= spot_dt]
            if spot_candles.empty:
                spot_candles = day_index.head(1)
            spot = float(spot_candles.iloc[-1]["close"])

        elif spot_detect_mode == "EMA_CROSS":
            fast = self.ema_fast_var.get()
            slow = self.ema_slow_var.get()
            day_index_sorted = day_index.sort_values("datetime").reset_index(drop=True)
            warmup = max(fast, slow) + 3
            spot   = None
            for bi in range(warmup, len(day_index_sorted)):
                row_time = day_index_sorted.iloc[bi]["datetime"]
                if row_time <= spot_dt:
                    continue
                df_slice = day_index_sorted.iloc[:bi+1].copy()
                df_slice["close"] = pd.to_numeric(df_slice["close"], errors="coerce")
                if self.check_ema_cross_signal(df_slice):
                    spot    = float(df_slice.iloc[-1]["close"])
                    spot_dt = row_time
                    self.log(f"  {date_str}: EMA Cross @ "
                             f"{row_time.strftime('%H:%M')} | Spot={spot}")
                    break
            if spot is None:
                self.log(f"  {date_str}: No EMA cross — skipping day")
                return

        elif spot_detect_mode == "PDH_PDL":
            pdh, pdl = self.compute_pdh_pdl(date)
            day_index_sorted = day_index.sort_values("datetime").reset_index(drop=True)
            spot = None
            for bi in range(2, len(day_index_sorted)):
                row_time = day_index_sorted.iloc[bi]["datetime"]
                if row_time <= spot_dt:
                    continue
                df_slice = day_index_sorted.iloc[:bi+1].copy()
                if self.check_pdh_pdl_touch(df_slice, pdh, pdl):
                    spot    = float(df_slice.iloc[-1]["close"])
                    spot_dt = row_time
                    self.log(f"  {date_str}: PDH/PDL touch @ "
                             f"{row_time.strftime('%H:%M')} | "
                             f"PDH={pdh} PDL={pdl} | Spot={spot}")
                    break
            if spot is None:
                self.log(f"  {date_str}: No PDH/PDL touch — skipping day")
                return
        else:
            spot_candles = day_index[day_index["datetime"] <= spot_dt]
            if spot_candles.empty:
                spot_candles = day_index.head(1)
            spot = float(spot_candles.iloc[-1]["close"])

        atm, step      = self.calculate_atm(spot)
        wanted_strikes = self.generate_strikes(atm, step)
        if not wanted_strikes:
            self.log(f"  {date_str}: No strikes generated")
            return
        day_folder = self._get_day_folder(date)
        if not day_folder:
            self.log(f"  {date_str}: Folder not found")
            return
        try:
            all_files = [f for f in os.listdir(day_folder)
                         if f.lower().endswith((".csv",".xlsx",".xls"))]
        except Exception as e:
            self.log(f"  {date_str}: Cannot read folder — {e}")
            return
        if not all_files:
            self.log(f"  {date_str}: No data files")
            return

        # Detect consolidated ALL-strike files (new NIFTY data format)
        ce_all = [f for f in all_files
                  if re.search(r'_CE_ALL\.xlsx$', f, re.IGNORECASE)]
        pe_all = [f for f in all_files
                  if re.search(r'_PE_ALL\.xlsx$', f, re.IGNORECASE)]

        if ce_all and pe_all:
            ce_path = os.path.join(day_folder, ce_all[0])
            pe_path = os.path.join(day_folder, pe_all[0])
            try:
                strike_candles, no_volume_files = self._load_consolidated_options(
                    ce_path, pe_path, wanted_strikes, date
                )
            except Exception as e:
                self.log(f"  {date_str}: Consolidated load error — {e}")
                return
        else:
            # Existing per-strike file loading
            file_map = {}
            for fname in all_files:
                s, t = self._parse_strike_from_filename(fname)
                if s is not None:
                    file_map[(s, t)] = os.path.join(day_folder, fname)
            strike_candles  = {}
            no_volume_files = []
            for strike_price, opt_type in wanted_strikes:
                key      = (int(strike_price), opt_type)
                filepath = file_map.get(key)
                if filepath is None:
                    continue
                try:
                    df_raw, vol_available, needs_calc = self._load_strike_file(filepath)
                except Exception as e:
                    self.log(f"  {date_str}: Load error "
                             f"{os.path.basename(filepath)} — {e}")
                    continue
                if len(df_raw) < 5:
                    continue
                df_full = self._apply_indicators_bt(df_raw, needs_calc)
                label   = f"{strike_price}{opt_type}"
                strike_candles[label] = {
                    "strike":           strike_price,
                    "type":             opt_type,
                    "candles":          df_full,
                    "volume_available": vol_available,
                    "source_file":      os.path.basename(filepath),
                }
                if not vol_available:
                    no_volume_files.append(os.path.basename(filepath))
        if not strike_candles:
            self.log(f"  {date_str}: No matching strike files found")
            return
        vol_note = (f" | No volume: {', '.join(no_volume_files)}"
                    if no_volume_files else "")
        self.log(f"  {date_str}: Spot={spot} ATM={atm} | "
                 f"{len(strike_candles)} strikes loaded{vol_note}")
        for key, data in strike_candles.items():
            df_c = data["candles"]
            at_spot = df_c[df_c["datetime"] <= pd.Timestamp(spot_dt)]
            if not at_spot.empty:
                ltp_at_spot = round(float(at_spot.iloc[-1]["close"]), 2)
                self.log(f"    {key} LTP at spot_time: {ltp_at_spot}")

        # ── Run strategies ────────────────────────────────────
        if self.strategies:
            for strat_params in self.strategies:
                self._replay_bars(date_str, strike_candles, spot_dt,
                                  no_volume_files=no_volume_files,
                                  spot=spot,
                                  strategy_params=strat_params)
        else:
            self._replay_bars(date_str, strike_candles, spot_dt,
                              no_volume_files=no_volume_files, spot=spot)

    def _get_day_folder(self, date):
        """
        Locate the options data folder for a given date.
        Tries the candle interval from bt_interval_var first (e.g. "1min" or "3min"),
        then falls back to the flat day folder (no interval sub-dir) so the
        function works with both structured and flat folder layouts.
        """
        folder = self.bt_options_folder_var.get()
        if not folder:
            return None
        year_str  = date.strftime("%Y")
        month_str = date.strftime("%Y-%m")
        day_str   = date.strftime("%Y-%m-%d")
        interval  = self.bt_interval_var.get()   # "1min" or "3min"

        day_str_alpha = date.strftime("%d-%b-%Y").upper()  # "01-JAN-2026"

        # Candidate paths: structured and flat, YYYY-MM-DD and DD-MMM-YYYY formats
        candidates = [
            os.path.join(folder, year_str, month_str, day_str, interval),
            os.path.join(folder, year_str, month_str, day_str),
            os.path.join(folder, day_str, interval),
            os.path.join(folder, day_str),
            os.path.join(folder, year_str, month_str, day_str_alpha),
            os.path.join(folder, year_str, month_str, day_str_alpha, interval),
            os.path.join(folder, day_str_alpha),
        ]
        for path in candidates:
            if os.path.isdir(path):
                return path
        return None

    def _parse_strike_from_filename(self, filename):
        name    = os.path.splitext(filename)[0]
        matches = re.findall(r'(\d+)\s*(CE|PE)', name, re.IGNORECASE)
        if not matches:
            return None, None
        strike_str, opt_type = matches[-1]
        return int(strike_str), opt_type.upper()

    _STRIKE_COL_MAP = {
        "ema20":         "ema",
        "rsi":           "rsi",
        "vwap":          "vwap",
        "vwapsd1 upper": "vwap_upper1",
        "vwapsd1 lower": "vwap_lower1",
        "vwapsd2 upper": "vwap_upper2",
        "vwapsd2 lower": "vwap_lower2",
        "volume":        "volume",
    }

    def _load_strike_file(self, filepath):
        raw_df = self._read_file_to_df(filepath)
        df     = self._normalize_df_columns(raw_df)
        rename_map = {}
        for src, dst in self._STRIKE_COL_MAP.items():
            if src in df.columns and dst not in df.columns:
                rename_map[src] = dst
        if rename_map:
            df.rename(columns=rename_map, inplace=True)
        num_cols = ["open","high","low","close","volume",
                    "ema","rsi","vwap",
                    "vwap_upper1","vwap_lower1",
                    "vwap_upper2","vwap_lower2"]
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["open","high","low","close"])
        volume_available = ("volume" in df.columns
                            and df["volume"].notna().any())
        if not volume_available and "volume" not in df.columns:
            df["volume"] = 1
        needs_calc = [col for col in
                      ["ema","rsi","vwap","vwap_upper1","vwap_lower1",
                       "vwap_upper2","vwap_lower2"]
                      if col not in df.columns or df[col].isna().all()]
        return df, volume_available, needs_calc

    def _apply_indicators_bt(self, df, needs_calc=None):
        if needs_calc is None:
            needs_calc = ["ema","rsi","vwap","vwap_upper1","vwap_lower1",
                          "vwap_upper2","vwap_lower2"]
        if not needs_calc:
            return df
        df = df.copy().reset_index(drop=True)
        if "ema" in needs_calc:
            df["ema"] = df["close"].ewm(
                span=self.ema_period_var.get(), adjust=False).mean()
        if "rsi" in needs_calc:
            df["rsi"] = self.compute_rsi(df["close"],
                                         self.rsi_period_var.get())
        vwap_needed = any(c in needs_calc for c in
                          ["vwap","vwap_upper1","vwap_lower1",
                           "vwap_upper2","vwap_lower2"])
        if vwap_needed:
            df["typical_price"] = (df["high"]+df["low"]+df["close"])/3
            df["pv"]            = df["typical_price"]*df["volume"]
            df["cum_pv"]        = df["pv"].cumsum()
            df["cum_vol"]       = df["volume"].cumsum()
            df["vwap"]          = (df["cum_pv"] /
                                   df["cum_vol"].replace(0, float("nan"))
                                   ).ffill()
            roll_std = (df["typical_price"]-df["vwap"]).rolling(
                30, min_periods=5).std()
            df["vwap_upper1"] = df["vwap"] + roll_std
            df["vwap_lower1"] = df["vwap"] - roll_std
            df["vwap_upper2"] = df["vwap"] + 2*roll_std
            df["vwap_lower2"] = df["vwap"] - 2*roll_std
            for c in ["vwap_upper1","vwap_lower1",
                      "vwap_upper2","vwap_lower2"]:
                df[c] = df[c].bfill().ffill()
        return df

    def _load_consolidated_options(self, ce_path, pe_path, wanted_strikes, date):
        """
        Load NIFTY[DATE]_CE_ALL.xlsx / _PE_ALL.xlsx (all strikes in one file,
        each row identified by spot_atm == strike price) and return a
        strike_candles dict in the same format _simulate_day() expects.
        """
        ce_df = pd.read_excel(ce_path)
        pe_df = pd.read_excel(pe_path)

        def _prep(df):
            df = df.copy()
            df.columns = [c.lower().strip() for c in df.columns]
            df["datetime"] = pd.to_datetime(
                df["date"].astype(str).str.strip() + " " +
                df["time"].astype(str).str.strip(),
                dayfirst=True, errors="coerce"
            )
            df = df.dropna(subset=["datetime"])
            df = df[df["datetime"].dt.date == date]
            df = df.sort_values("datetime").reset_index(drop=True)
            for col in ["open", "high", "low", "close", "volume"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df["spot_atm"] = pd.to_numeric(df["spot_atm"], errors="coerce")
            return df

        ce_df = _prep(ce_df)
        pe_df = _prep(pe_df)

        strike_candles  = {}
        no_volume_files = []

        for strike_price, opt_type in wanted_strikes:
            strike_int = int(strike_price)
            src = ce_df if opt_type == "CE" else pe_df
            df  = src[src["spot_atm"] == strike_int].copy()
            df  = df.dropna(subset=["open", "high", "low", "close"])

            if len(df) < 5:
                continue

            vol_available = (
                "volume" in df.columns
                and df["volume"].notna().any()
                and df["volume"].sum() > 0
            )
            if not vol_available:
                df["volume"] = 1

            needs_calc = [
                col for col in
                ["ema", "rsi", "vwap", "vwap_upper1", "vwap_lower1"]
                if col not in df.columns or df[col].isna().all()
            ]
            df_full = self._apply_indicators_bt(
                df.reset_index(drop=True), needs_calc)

            label = f"{strike_price}{opt_type}"
            fname = os.path.basename(ce_path if opt_type == "CE" else pe_path)
            strike_candles[label] = {
                "strike":           strike_price,
                "type":             opt_type,
                "candles":          df_full,
                "volume_available": vol_available,
                "source_file":      fname,
            }
            if not vol_available:
                no_volume_files.append(fname)

        return strike_candles, no_volume_files

    def _replay_bars(self, date_str, strike_candles, spot_dt,
                     no_volume_files=None, spot=None, strategy_params=None):
        """
        Replay option bars for a single day.
        strategy_params: if provided (from Strategy Builder), overrides live GUI params.
        """
        if no_volume_files is None:
            no_volume_files = []

        # ── Resolve parameters: strategy_params overrides GUI ─
        p = strategy_params  # shorthand; None = use live GUI

        def _float(val, fallback=0):
            try:
                return float(val)
            except (TypeError, ValueError):
                return fallback

        strat_label = p.get("label", "default") if p else "default"

        try:
            sl_points = _float(p["sl_points"]) if p else _float(
                self.numeric_entries["SL Points"].get_value())
        except Exception:
            sl_points = 0
        try:
            target_points = _float(p["target_points"]) if p else _float(
                self.numeric_entries["Target Points"].get_value())
        except Exception:
            target_points = 0

        entry_mode = p["entry_mode"] if p else self.entry_mode_var.get()

        entry_price_input = None
        tolerance         = None
        if entry_mode == "PRICE_BAND":
            try:
                entry_price_input = _float(p["entry_price"]) if p else _float(
                    self.numeric_entries["Entry Price"].get_value())
                tolerance = _float(p["tolerance"]) if p else _float(
                    self.numeric_entries["Tolerance"].get_value())
            except Exception:
                pass

        # Filter flags — use strategy_params if available
        def _bflag(key, tk_var):
            if p:
                return bool(p.get(key, False))
            return tk_var.get()

        use_quant      = _bflag("enable_quant",    self.enable_quant_var)
        f_ema          = _bflag("filter_ema",       self.filter_ema_var)
        f_rsi          = _bflag("filter_rsi",       self.filter_rsi_var)
        f_range        = _bflag("filter_range",     self.filter_range_var)
        f_volume       = _bflag("filter_volume",    self.filter_volume_var)
        f_vwap         = _bflag("filter_vwap",      self.filter_vwap_var)
        f_gamma_trap   = _bflag("filter_gamma_trap",self.filter_gamma_trap_var)
        f_gamma_exp    = _bflag("filter_gamma_exp", self.filter_gamma_expansion_var)
        rsi_min        = int(p.get("rsi_min_threshold", self.rsi_min_threshold_var.get())) if p else self.rsi_min_threshold_var.get()
        f_multi_bar    = _bflag("filter_multi_bar",  self.filter_multi_bar_var)
        f_time_window   = _bflag("filter_time_window",   self.filter_time_window_var)
        f_consolidation = _bflag("filter_consolidation", self.filter_consolidation_var)
        f_vol_ratio     = _bflag("filter_vol_ratio",     self.filter_vol_ratio_var)
        f_body_quality  = _bflag("filter_body_quality",  self.filter_body_quality_var)
        vol_follow_through = _bflag("vol_follow_through", self.vol_follow_through_var)
        use_tsl        = _bflag("enable_tsl",       self.enable_tsl_var)
        gexp_override  = _bflag("gexp_override",    self.gexp_override_var)
        gexp_method    = p["gexp_method"]   if p else self.gexp_method_var.get()
        gexp_sl_pct    = _float(p["gexp_sl_pct"])   if p else self.gexp_sl_pct_var.get()
        gexp_rr        = _float(p["gexp_rr"])        if p else self.gexp_rr_var.get()
        gexp_tsl_step  = _float(p["gexp_tsl_step"]) if p else self.gexp_tsl_step_var.get()
        tsl_step       = _float(p["tsl_step"])       if p else self.tsl_step_var.get()

        lot_size = self.get_lot_size()
        bt_state = {}
        for key, data in strike_candles.items():
            bt_state[key] = {
                "strike":               data["strike"],
                "type":                 data["type"],
                "candles":              data["candles"],
                "volume_available":     data.get("volume_available", True),
                "source_file":          data.get("source_file",""),
                "trade_open":           False,
                "entry_taken_today":    False,
                "entry_band_triggered": False,
                "entry_price":          None,
                "entry_time":           None,
                "sl":                   None,
                "highest_price":        0,
                "targets":              [],
                "targets_hit":          [],
                "entry_indicators":     {},
                "gexp_override":        False,
                "gexp_method":          "none",
                "gexp_tsl_step":        0,
            }
        WARMUP   = 10
        max_bars = max(len(v["candles"]) for v in bt_state.values())
        for bar_idx in range(WARMUP, max_bars):
            if not self.is_running:
                break
            for key, state in bt_state.items():
                df       = state["candles"]
                if bar_idx >= len(df):
                    continue
                df_slice = df.iloc[:bar_idx+1].copy()
                bar_row  = df_slice.iloc[-1]
                ltp      = float(bar_row["close"])
                c_open   = float(bar_row["open"])
                c_high   = float(bar_row["high"])
                c_low    = float(bar_row["low"])
                bar_time = bar_row["datetime"]
                if pd.Timestamp(bar_time) <= pd.Timestamp(spot_dt):
                    continue
                if not state["trade_open"] and not state["entry_taken_today"]:
                    if len(df_slice) < WARMUP:
                        continue
                    if not all(c in df_slice.columns
                               for c in ["ema","rsi","vwap"]):
                        continue

                    # ── Quant filters ─────────────────────────
                    gamma_ok = True
                    if f_gamma_exp:
                        gamma_ok = self.gamma_expansion_detector(df_slice)
                    if use_quant:
                        skip_vol = not state["volume_available"]
                        last_s   = df_slice.iloc[-1]
                        recent_s = df_slice.tail(5)
                        filter_ok = True
                        if f_ema:
                            cs = df_slice["close"]
                            p1 = int(p.get("ema_f1", self.ema_f1_var.get())) if p else self.ema_f1_var.get()
                            p2 = int(p.get("ema_f2", self.ema_f2_var.get())) if p else self.ema_f2_var.get()
                            p3 = int(p.get("ema_f3", self.ema_f3_var.get())) if p else self.ema_f3_var.get()
                            ema1_val = cs.ewm(span=p1, adjust=False).mean().iloc[-1] if p1 > 0 else None
                            ema2_val = cs.ewm(span=p2, adjust=False).mean().iloc[-1] if p2 > 0 else None
                            ema3_val = cs.ewm(span=p3, adjust=False).mean().iloc[-1] if p3 > 0 else None
                            if ema1_val is not None and last_s["close"] < ema1_val:
                                filter_ok = False
                            if filter_ok and ema2_val is not None and last_s["close"] < ema2_val:
                                filter_ok = False
                            if filter_ok and ema3_val is not None and last_s["close"] < ema3_val:
                                filter_ok = False
                            if filter_ok:
                                f_ema_align = bool(p.get("ema_alignment", self.ema_alignment_var.get())) if p else self.ema_alignment_var.get()
                                if f_ema_align:
                                    if ema1_val is not None and ema2_val is not None and ema1_val <= ema2_val:
                                        filter_ok = False
                                    if filter_ok and ema2_val is not None and ema3_val is not None and ema2_val <= ema3_val:
                                        filter_ok = False
                        if filter_ok and f_rsi and last_s["rsi"] < rsi_min:
                            filter_ok = False
                        if filter_ok and f_range:
                            rng = recent_s["high"].max() - recent_s["low"].min()
                            if rng > recent_s["close"].mean() * 0.8:
                                filter_ok = False
                        if filter_ok and f_volume and not skip_vol:
                            vmean = recent_s["volume"].mean()
                            if last_s["volume"] > vmean * 2:
                                filter_ok = False
                        if filter_ok and f_gamma_trap:
                            body = abs(last_s["close"] - last_s["open"])
                            rng2 = last_s["high"] - last_s["low"]
                            if rng2 > 0 and body / rng2 < 0.35:
                                filter_ok = False
                        if filter_ok and f_vwap:
                            bw = df_slice["vwap_upper1"] - df_slice["vwap_lower1"]
                            if bw.tail(5).mean() > last_s["close"] * 0.12:
                                filter_ok = False
                            elif not self.vwap_band_touch_ok(
                                    last_s, self.vwap_band_level_var.get(),
                                    self.vwap_band_tol_var.get()):
                                filter_ok = False
                        if filter_ok and f_multi_bar:
                            opt_type = state.get("type", "CE")
                            if not self.multi_bar_momentum_filter(df_slice, opt_type):
                                filter_ok = False
                        if filter_ok and f_time_window:
                            if not self.time_window_filter(bar_time):
                                filter_ok = False
                        if filter_ok and f_consolidation:
                            if not self.consolidation_filter(df_slice):
                                filter_ok = False
                        if filter_ok and f_vol_ratio:
                            if not self.volume_surge_filter(df_slice):
                                filter_ok = False
                            if (filter_ok and vol_follow_through
                                    and bar_idx + 1 < len(df)):
                                next_bar = df.iloc[bar_idx + 1]
                                recent_v = df_slice.iloc[-6:-1]
                                vmean_v  = recent_v["volume"].mean()
                                if (vmean_v <= 0
                                        or next_bar["volume"] < vmean_v * 1.5):
                                    filter_ok = False
                        if filter_ok and f_body_quality:
                            if not self.body_quality_filter(df_slice):
                                filter_ok = False
                        if not (filter_ok and gamma_ok):
                            continue

                    # ── Entry signal ──────────────────────────
                    entry_signal       = False
                    actual_entry_price = ltp
                    if entry_mode == "MARKET":
                        entry_signal = True
                    elif entry_mode == "PRICE_BAND":
                        if entry_price_input is not None and tolerance is not None:
                            el = entry_price_input - tolerance
                            eh = entry_price_input + tolerance
                            if c_high >= el and c_low <= eh:
                                actual_entry_price = (
                                    el if c_open < el else
                                    eh if c_open > eh else c_open)
                                entry_signal = True
                    elif entry_mode == "VWAP_RECLAIM":
                        ep = self.vwap_reclaim_entry(df_slice)
                        if ep and ltp >= ep:
                            entry_signal       = True
                            actual_entry_price = ep

                    if entry_signal:
                        if gexp_override:
                            gexp_gamma_ok  = self.gamma_expansion_detector(df_slice)
                            use_gexp_trade = gexp_gamma_ok
                        else:
                            use_gexp_trade = False
                        if use_gexp_trade:
                            if gexp_method == "approach1":
                                sl_pct_val = gexp_sl_pct / 100
                                sl         = round(actual_entry_price*(1-sl_pct_val),2)
                                risk       = actual_entry_price - sl
                                targets    = [round(actual_entry_price + risk*gexp_rr, 2)]
                            else:
                                sl      = round(actual_entry_price - gexp_tsl_step, 2)
                                targets = []
                        else:
                            sl      = actual_entry_price - sl_points
                            targets = [actual_entry_price + target_points]
                        state.update({
                            "trade_open":        True,
                            "entry_taken_today": True,
                            "entry_price":       actual_entry_price,
                            "entry_time":        bar_time,
                            "sl":                sl,
                            "highest_price":     actual_entry_price,
                            "targets":           targets,
                            "targets_hit":       [],
                            "gexp_override":     use_gexp_trade,
                            "gexp_method":       gexp_method if use_gexp_trade else "none",
                            "gexp_tsl_step":     gexp_tsl_step if use_gexp_trade else 0,
                            "strategy_label":    strat_label,
                        })
                        last = df_slice.iloc[-1]
                        state["entry_indicators"] = {
                            "ema":         last.get("ema"),
                            "rsi":         last.get("rsi"),
                            "vwap":        last.get("vwap"),
                            "vwap_upper1": last.get("vwap_upper1"),
                            "vwap_lower1": last.get("vwap_lower1"),
                            "volume":      last.get("volume"),
                        }
                elif state["trade_open"]:
                    # GExp A2 TSL
                    if (state.get("gexp_override")
                            and state.get("gexp_method") == "approach2"):
                        gs = state.get("gexp_tsl_step", gexp_tsl_step)
                        if c_high > state["highest_price"]:
                            state["highest_price"] = c_high
                            new_sl = round(c_high - gs, 2)
                            if new_sl > state["sl"]:
                                state["sl"] = new_sl
                    elif use_tsl:
                        if ltp > state["highest_price"]:
                            state["highest_price"] = ltp
                            new_sl = ltp - tsl_step
                            if new_sl > state["sl"]:
                                state["sl"] = new_sl
                    # Target first
                    target_hit = False
                    for tgt in state["targets"]:
                        if c_high >= tgt and tgt not in state["targets_hit"]:
                            state["targets_hit"].append(tgt)
                            reason = ("GEXP_A1_TARGET"
                                      if state.get("gexp_override")
                                      else "TARGET_HIT")
                            pnl = round((tgt-state["entry_price"])*lot_size,2)
                            self._record_bt_trade(date_str, key, state,
                                                  tgt, bar_time, pnl,
                                                  reason, spot=spot)
                            state["trade_open"] = False
                            target_hit = True
                            break
                    if target_hit:
                        continue
                    # SL
                    if c_low <= state["sl"]:
                        sl_price = state["sl"]
                        if (state.get("gexp_override")
                                and state.get("gexp_method") == "approach2"):
                            reason = "GEXP_A2_TSL"
                        elif state.get("gexp_override"):
                            reason = "GEXP_A1_SL"
                        else:
                            reason = ("TSL_EXIT"
                                      if sl_price > state["entry_price"]
                                      else "SL_HIT")
                        pnl = round((sl_price-state["entry_price"])*lot_size,2)
                        self._record_bt_trade(date_str, key, state,
                                              sl_price, bar_time, pnl,
                                              reason, spot=spot)
                        state["trade_open"] = False
        # EOD close
        for key, state in bt_state.items():
            if state["trade_open"]:
                df  = state["candles"]
                ep  = float(df.iloc[-1]["close"]) if len(df) > 0 else state["entry_price"]
                bt  = df.iloc[-1]["datetime"]     if len(df) > 0 else spot_dt
                pnl = round((ep-state["entry_price"])*lot_size,2)
                self._record_bt_trade(date_str, key, state, ep, bt,
                                      pnl, "EOD_EXIT", spot=spot)
                state["trade_open"] = False

    def _record_bt_trade(self, date_str, key, state, exit_price,
                         exit_time, pnl, reason, spot=None):
        ind    = state.get("entry_indicators", {})
        record = {
            "Date":              date_str,
            "Strategy":          state.get("strategy_label", "default"),
            "Spot_Detect_Mode":  self.spot_detect_mode_var.get(),
            "Spot":              spot,
            "Index":             self.index_var.get(),
            "Strike":            state["strike"],
            "Type":              state["type"],
            "EntryTime":         state.get("entry_time",""),
            "EntryPrice":        round(state["entry_price"],2),
            "ExitTime":          exit_time,
            "ExitPrice":         round(exit_price,2),
            "SL":                round(state["sl"],2) if state["sl"] else None,
            "Target":            round(state["targets"][0],2) if state["targets"] else None,
            "P&L":               pnl,
            "Reason":            reason,
            "Lot Size":          self.get_lot_size(),
            "EMA":               round(ind["ema"],4)         if ind.get("ema")         is not None else None,
            "RSI":               round(ind["rsi"],2)         if ind.get("rsi")         is not None else None,
            "VWAP":              round(ind["vwap"],2)        if ind.get("vwap")        is not None else None,
            "VWAP_SD1_UP":       round(ind["vwap_upper1"],2) if ind.get("vwap_upper1") is not None else None,
            "VWAP_SD1_DOWN":     round(ind["vwap_lower1"],2) if ind.get("vwap_lower1") is not None else None,
            "EntryVolume":       ind.get("volume"),
            "Filter_EMA":        state.get("strat_f_ema",        self.filter_ema_var.get()),
            "Filter_RSI":        state.get("strat_f_rsi",        self.filter_rsi_var.get()),
            "Filter_Range":      state.get("strat_f_range",      self.filter_range_var.get()),
            "Filter_Volume":     state.get("strat_f_volume",     self.filter_volume_var.get()),
            "Filter_VWAP":       state.get("strat_f_vwap",       self.filter_vwap_var.get()),
            "Filter_GammaTrap":  state.get("strat_f_gamma_trap", self.filter_gamma_trap_var.get()),
            "Filter_GammaExp":   state.get("strat_f_gamma_exp",  self.filter_gamma_expansion_var.get()),
            "Filter_RSI_Min":    state.get("strat_rsi_min",       self.rsi_min_threshold_var.get()),
            "Filter_MultiBar":   state.get("strat_f_multi_bar",   self.filter_multi_bar_var.get()),
            "Filter_TimeWindow":   self.filter_time_window_var.get(),
            "Filter_Consolidation": self.filter_consolidation_var.get(),
            "Filter_VolRatio":     self.filter_vol_ratio_var.get(),
            "Filter_BodyQuality":  self.filter_body_quality_var.get(),
            "TimeWindow_Start":    self.time_window_start_var.get(),
            "TimeWindow_End":      self.time_window_end_var.get(),
            "Entry_Mode":        state.get("strat_entry_mode",   self.entry_mode_var.get()),
            "TSL_On":            self.enable_tsl_var.get(),
            "GExp_Override":     state.get("gexp_override",False),
            "GExp_Method":       state.get("gexp_method","none"),
            "GExp_SL_Pct":       self.gexp_sl_pct_var.get() if state.get("gexp_override") else None,
            "GExp_RR":           self.gexp_rr_var.get()     if (state.get("gexp_override") and state.get("gexp_method")=="approach1") else None,
            "GExp_TSL_Step":     state.get("gexp_tsl_step") if (state.get("gexp_override") and state.get("gexp_method")=="approach2") else None,
            "Volume_Available":  state.get("volume_available",True),
            "Source_File":       state.get("source_file",""),
        }
        self.bt_results.append(record)
        icon = "WIN" if pnl > 0 else "LOSS"
        self.log(f"    [{icon}] {key} | {reason} | "
                 f"Entry:{state['entry_price']} → Exit:{exit_price} | P&L:{pnl}"
                 + (" [no vol]" if not state.get("volume_available",True) else ""))

    def _save_bt_results(self):
        if not self.bt_results:
            self.log("No trades recorded in backtest")
            self.bt_progress_var.set("No Trades Found")
            return
        df    = pd.DataFrame(self.bt_results)
        total = len(df)
        wins  = int((df["P&L"] > 0).sum())
        losses= int((df["P&L"] <= 0).sum())
        wr    = round(wins/total*100,2) if total > 0 else 0
        tot_pnl  = round(df["P&L"].sum(),2)
        avg_win  = round(df.loc[df["P&L"]>0,"P&L"].mean(),2) if wins   > 0 else 0
        avg_los  = round(df.loc[df["P&L"]<0,"P&L"].mean(),2) if losses > 0 else 0
        max_win  = round(df["P&L"].max(),2)
        max_los  = round(df["P&L"].min(),2)
        pf_denom = abs(df.loc[df["P&L"]<0,"P&L"].sum())
        profit_factor = round(df.loc[df["P&L"]>0,"P&L"].sum()/pf_denom,2) \
                        if losses > 0 and pf_denom != 0 else float("inf")
        reason_stats = df.groupby("Reason").agg(
            Trades=("P&L","count"),
            Wins=("P&L", lambda x:(x>0).sum()),
            Total_PnL=("P&L","sum"),
        ).reset_index()
        reason_stats["Win Rate (%)"] = (
            reason_stats["Wins"]/reason_stats["Trades"]*100).round(2)
        daily = df.groupby("Date").agg(
            Trades=("P&L","count"),
            Wins=("P&L", lambda x:(x>0).sum()),
            Daily_PnL=("P&L","sum"),
        ).reset_index()
        daily["Win Rate (%)"]    = (daily["Wins"]/daily["Trades"]*100).round(2)
        daily["Cumulative P&L"]  = daily["Daily_PnL"].cumsum().round(2)
        type_stats = df.groupby("Type").agg(
            Trades=("P&L","count"),
            Wins=("P&L", lambda x:(x>0).sum()),
            Total_PnL=("P&L","sum"),
        ).reset_index()
        type_stats["Win Rate (%)"] = (
            type_stats["Wins"]/type_stats["Trades"]*100).round(2)
        gexp_on     = self.gexp_override_var.get()
        gexp_method = self.gexp_method_var.get()
        num_strategies = len(self.strategies)
        summary = {
            "Metric": [
                "Total Trades","Wins","Losses","Win Rate (%)",
                "Total P&L","Avg Win","Avg Loss",
                "Max Win","Max Loss","Profit Factor",
                "--- Run Config ---",
                "Index","Spot Detect Mode","Spot Time",
                "EMA Fast","EMA Slow","PDH/PDL Target",
                "Candle Interval (Spot TF)","Strategies Saved",
                "--- Trade Config ---",
                "Entry Mode","Quant Filter On",
                "EMA Filter","RSI Filter","Range Filter",
                "Volume Filter","VWAP Filter","Gamma Trap Filter",
                "Gamma Expansion Filter","RSI Min Threshold","Multi-Bar Momentum",
                "Time Window Filter","Time Window Range",
                "Consolidation Filter","Volume Surge Filter","Body Quality Filter",
                "Vol Follow-through",
                "TSL On","TSL Step",
                "From Date","To Date",
                "Entry Price","Tolerance","SL Points","Target Points",
                "Options Interval",
                "--- GExp Override ---",
                "GExp Override On","GExp Method",
                "GExp SL %","GExp R:R Ratio","GExp TSL Step",
            ],
            "Value": [
                total,wins,losses,wr,
                tot_pnl,avg_win,avg_los,
                max_win,max_los,profit_factor,
                "",
                self.index_var.get(),
                self.spot_detect_mode_var.get(),
                self.spot_entry.get_value(),
                self.ema_fast_var.get(),
                self.ema_slow_var.get(),
                self.pdh_pdl_target_var.get(),
                self.spot_candle_tf_var.get(),
                num_strategies if num_strategies > 0 else "using live params",
                "",
                self.entry_mode_var.get(),
                self.enable_quant_var.get(),
                self.filter_ema_var.get(),self.filter_rsi_var.get(),
                self.filter_range_var.get(),self.filter_volume_var.get(),
                self.filter_vwap_var.get(),self.filter_gamma_trap_var.get(),
                self.filter_gamma_expansion_var.get(),
                f"RSI Min={self.rsi_min_threshold_var.get()}",
                self.filter_multi_bar_var.get(),
                self.filter_time_window_var.get(),
                f"{self.time_window_start_var.get()}–{self.time_window_end_var.get()}"
                    + (" +EOD" if self.time_window_eod_var.get() else ""),
                self.filter_consolidation_var.get(),
                self.filter_vol_ratio_var.get(),
                self.filter_body_quality_var.get(),
                self.vol_follow_through_var.get(),
                self.enable_tsl_var.get(),self.tsl_step_var.get(),
                self.bt_from_date_var.get(),self.bt_to_date_var.get(),
                self.numeric_entries["Entry Price"].get_value() or "—",
                self.numeric_entries["Tolerance"].get_value() or "—",
                self.numeric_entries["SL Points"].get_value() or "—",
                self.numeric_entries["Target Points"].get_value() or "—",
                self.bt_interval_var.get(),
                "",
                gexp_on,
                "Approach 1 (% SL + R:R)" if gexp_method=="approach1"
                else "Approach 2 (TSL uncapped)" if gexp_on else "—",
                f"{self.gexp_sl_pct_var.get()}%" if gexp_on else "—",
                self.gexp_rr_var.get() if (gexp_on and gexp_method=="approach1") else "—",
                self.gexp_tsl_step_var.get() if (gexp_on and gexp_method=="approach2") else "—",
            ]
        }
        file_name = (f"Backtest_{self.index_var.get()}_"
                     f"{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        file_path = os.path.join(self.output_folder.get(), file_name)
        try:
            with pd.ExcelWriter(file_path, engine="xlsxwriter") as writer:
                wb         = writer.book
                green_fmt  = wb.add_format({"bg_color":"#1f3a1f","font_color":"#00e676","bold":True})
                red_fmt    = wb.add_format({"bg_color":"#3a1f1f","font_color":"#ff5252","bold":True})
                header_fmt = wb.add_format({"bold":True,"bg_color":"#2e3b4e","font_color":"white","border":1})
                title_fmt  = wb.add_format({"bold":True,"font_size":14,"font_color":"#00bcd4"})
                df_summary = pd.DataFrame(summary)
                df_summary.to_excel(writer, sheet_name="Summary", index=False)
                ws = writer.sheets["Summary"]
                ws.set_column("A:A",22)
                ws.set_column("B:B",20)
                start_row = len(df_summary)+3
                ws.write(start_row, 0, "Exit Reason Breakdown", title_fmt)
                reason_stats.to_excel(writer, sheet_name="Summary",
                                       startrow=start_row+1, index=False)
                start_row2 = start_row+len(reason_stats)+4
                ws.write(start_row2, 0, "CE vs PE Performance", title_fmt)
                type_stats.to_excel(writer, sheet_name="Summary",
                                     startrow=start_row2+1, index=False)
                df.to_excel(writer, sheet_name="All Trades", index=False)
                ws2 = writer.sheets["All Trades"]
                ws2.set_column("A:A",12)
                ws2.set_column("B:B",14)
                ws2.set_column("F:G",12)
                pnl_col_idx = df.columns.get_loc("P&L")
                for row_num, pnl_val in enumerate(df["P&L"], start=1):
                    fmt = green_fmt if pnl_val > 0 else red_fmt
                    ws2.write(row_num, pnl_col_idx, pnl_val, fmt)
                daily.to_excel(writer, sheet_name="Daily P&L", index=False)
                ws3 = writer.sheets["Daily P&L"]
                ws3.set_column("A:A",13)
                ws3.set_column("D:E",15)
                chart = wb.add_chart({"type":"line"})
                chart.add_series({
                    "name":       "Cumulative P&L",
                    "categories": ["Daily P&L",1,0,len(daily),0],
                    "values":     ["Daily P&L",1,4,len(daily),4],
                    "line":       {"color":"#00e676","width":2},
                })
                chart.set_title({"name":"Cumulative P&L Curve"})
                chart.set_x_axis({"name":"Date"})
                chart.set_y_axis({"name":"P&L"})
                chart.set_style(10)
                ws3.insert_chart("G2", chart,
                                 {"x_scale":2.2,"y_scale":1.4})
                no_vol_df = (df[df["Volume_Available"]==False][[
                    "Date","Strike","Type","Source_File"
                ]].drop_duplicates()
                             if "Volume_Available" in df.columns
                             else pd.DataFrame())
                if not no_vol_df.empty:
                    no_vol_df.to_excel(writer,
                                       sheet_name="No Volume Files",
                                       index=False)
                    ws4 = writer.sheets["No Volume Files"]
                    ws4.set_column("A:A",13)
                    ws4.set_column("B:B",14)
                    ws4.set_column("C:C",60)

                # ── Strategy Comparison sheet (if multi-strategy) ──
                if "Strategy" in df.columns and df["Strategy"].nunique() > 1:
                    strat_stats = df.groupby("Strategy").agg(
                        Trades      =("P&L","count"),
                        Wins        =("P&L", lambda x:(x>0).sum()),
                        Total_PnL   =("P&L","sum"),
                        Avg_Win     =("P&L", lambda x: x[x>0].mean() if (x>0).any() else 0),
                        Avg_Loss    =("P&L", lambda x: x[x<=0].mean() if (x<=0).any() else 0),
                        Max_Win     =("P&L","max"),
                        Max_Loss    =("P&L","min"),
                    ).reset_index()
                    strat_stats["Win_Rate_%"] = (
                        strat_stats["Wins"]/strat_stats["Trades"]*100).round(2)
                    strat_stats["Total_PnL"]  = strat_stats["Total_PnL"].round(2)
                    strat_stats.to_excel(writer,
                                         sheet_name="Strategy Comparison",
                                         index=False)
                    ws5 = writer.sheets["Strategy Comparison"]
                    ws5.set_column("A:A", 25)
                    ws5.set_column("B:I", 14)

                    # Per-strategy per-entry-mode breakdown
                    if "Entry_Mode" in df.columns:
                        strat_entry = df.groupby(["Strategy","Entry_Mode"]).agg(
                            Trades   =("P&L","count"),
                            Wins     =("P&L", lambda x:(x>0).sum()),
                            Total_PnL=("P&L","sum"),
                        ).reset_index()
                        strat_entry["Win_Rate_%"] = (
                            strat_entry["Wins"]/strat_entry["Trades"]*100).round(2)
                        start_se = len(strat_stats) + 4
                        ws5.write(start_se, 0,
                                  "Strategy x Entry Mode Breakdown", title_fmt)
                        strat_entry.to_excel(writer,
                                              sheet_name="Strategy Comparison",
                                              startrow=start_se+1, index=False)
            self.log(f"\n{'='*50}")
            self.log(f"BACKTEST RESULTS — {self.index_var.get()}")
            self.log(f"{'='*50}")
            self.log(f"Total Trades : {total}")
            self.log(f"Win Rate     : {wr}%  ({wins}W / {losses}L)")
            self.log(f"Total P&L    : {tot_pnl}")
            self.log(f"Avg Win      : {avg_win}  |  Avg Loss: {avg_los}")
            self.log(f"Profit Factor: {profit_factor}")
            self.log(f"Saved → {file_path}")
            self.log(f"{'='*50}\n")
            self.status_var.set(
                f"Backtest Done | WR:{wr}% | P&L:{tot_pnl} | Saved:{file_name}")
            messagebox.showinfo("Backtest Complete",
                f"Index    : {self.index_var.get()}\n"
                f"Trades   : {total}\n"
                f"Win Rate : {wr}%  ({wins}W / {losses}L)\n"
                f"Total P&L: {tot_pnl}\n"
                f"Profit Factor: {profit_factor}\n\n"
                f"Saved → {file_name}")
        except Exception as e:
            self.log(f"Save error: {e}")
            messagebox.showerror("Save Error", str(e))
