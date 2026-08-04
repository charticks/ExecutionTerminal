import datetime as dt

import pandas as pd


class SignalFilterMixin:
    """Spot detection, entry band, VWAP reclaim, and quant filter methods."""

    def compute_pdh_pdl(self, date=None):
        """
        Compute Previous Day High and Previous Day Low from the
        loaded index dataframe (bt_index_df) or from live index candles.

        For live mode, uses self.bt_index_df if loaded, else returns
        the manual override values from pdh_value_var / pdl_value_var.
        Returns (pdh, pdl) tuple. Either can be None.
        """
        # Manual override
        manual_pdh = self.pdh_value_var.get()
        manual_pdl = self.pdl_value_var.get()
        if manual_pdh > 0 or manual_pdl > 0:
            return (manual_pdh or None, manual_pdl or None)

        if self.bt_index_df is None:
            return None, None
        try:
            if date is None:
                date = dt.datetime.now().date()
            prev_data = self.bt_index_df[
                self.bt_index_df["date"] < date
            ]
            if prev_data.empty:
                return None, None
            prev_date = prev_data["date"].max()
            prev_day  = prev_data[prev_data["date"] == prev_date]
            pdh = float(prev_day["high"].max())
            pdl = float(prev_day["low"].min())
            return pdh, pdl
        except Exception as e:
            print("compute_pdh_pdl error:", e)
            return None, None

    def check_ema_cross_signal(self, df):
        """
        Returns True when the fast EMA has just crossed ABOVE the slow EMA
        on the most recent closed candle (golden cross = bullish signal).
        Requires at least 3 candles after warmup.
        """
        fast = self.ema_fast_var.get()
        slow = self.ema_slow_var.get()
        if df is None or len(df) < max(fast, slow) + 2:
            return False
        try:
            df = df.copy()
            df["ema_fast"] = df["close"].ewm(span=fast, adjust=False).mean()
            df["ema_slow"] = df["close"].ewm(span=slow, adjust=False).mean()
            prev = df.iloc[-2]
            last = df.iloc[-1]
            # Golden cross: fast was below slow, now above
            cross_up = (prev["ema_fast"] <= prev["ema_slow"] and
                        last["ema_fast"] >  last["ema_slow"])
            return cross_up
        except Exception as e:
            print("check_ema_cross_signal error:", e)
            return False

    def check_pdh_pdl_touch(self, df, pdh, pdl):
        """
        Returns True when the last closed candle's high/low touched or
        exceeded PDH or PDL based on pdh_pdl_target_var setting.
        """
        if df is None or len(df) < 2:
            return False
        last   = df.iloc[-1]
        target = self.pdh_pdl_target_var.get()
        hit    = False
        if pdh and target in ("PDH", "BOTH"):
            if last["high"] >= pdh:
                hit = True
        if pdl and target in ("PDL", "BOTH"):
            if last["low"] <= pdl:
                hit = True
        return hit

    def entry_band_hit(self, ltp):
        try:
            ep_raw  = self.numeric_entries["Entry Price"].get_value()
            tol_raw = self.numeric_entries["Tolerance"].get_value()
            if ep_raw is None or tol_raw is None:
                return False
            entry_price = float(ep_raw)
            tolerance   = float(tol_raw)
        except (TypeError, ValueError):
            return False
        return (entry_price - tolerance) <= ltp <= (entry_price + tolerance)

    def vwap_reclaim_entry(self, df):
        if df is None or len(df) < 3:
            return None
        prev = df.iloc[-2]
        last = df.iloc[-1]
        if pd.isna(prev.get("vwap_lower1")) or pd.isna(last.get("vwap_lower1")):
            return None
        if (prev["close"] < prev["vwap_lower1"]
                and last["close"] > last["vwap_lower1"]):
            body      = abs(last["close"] - last["open"])
            prev_body = abs(prev["close"] - prev["open"])
            if body > prev_body:
                return last["high"]
        return None

    def vwap_squeeze_break_entry(self, df):
        if df is None or len(df) < 3:
            return None
        n_var = getattr(self, "squeeze_lookback_var", None)
        n     = int(n_var.get()) if n_var else 10
        if len(df) < n + 1:
            return None
        import math
        last   = df.iloc[-1]
        prev   = df.iloc[-2]
        window = df.iloc[-(n + 1):-1]
        for col in ("std", "atr", "vwap_upper1"):
            if math.isnan(float(last.get(col, float("nan")))):
                return None
        # Squeeze: both std and ATR below their N-bar average
        if float(last["std"]) >= window["std"].mean():
            return None
        if float(last["atr"]) >= window["atr"].mean():
            return None
        # Fresh breakout above upper1 (previous close was below)
        upper1_now  = float(last["vwap_upper1"])
        upper1_prev = float(prev.get("vwap_upper1", upper1_now))
        if last["close"] <= upper1_now or prev["close"] > upper1_prev:
            return None
        return float(last["high"])

    def compute_rsi(self, series, period):
        delta    = series.diff()
        gain     = delta.clip(lower=0)
        loss     = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
        rs       = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def vwap_band_touch_ok(self, last_row, level, tol_pct):
        """True if price (close) is at/above the selected VWAP band within tolerance.

        The selected level is the ONLY threshold (no lower-1 floor): selecting a
        lower band genuinely allows entries down to that band. Shared by the live
        filter (QuantFilterEngine) and the backtest so they behave identically.
        """
        import math
        tol   = (tol_pct or 0) / 100.0
        price = float(last_row.get("close", float("nan")))
        l1 = last_row.get("vwap_lower1", float("nan")); l2 = last_row.get("vwap_lower2", float("nan"))
        u1 = last_row.get("vwap_upper1", float("nan")); u2 = last_row.get("vwap_upper2", float("nan"))
        vw = last_row.get("vwap",        float("nan"))
        if any(math.isnan(v) for v in [price, l1, l2, u1, u2, vw]):
            return False
        band_map = {"lower2": l2, "lower1": l1, "vwap": vw, "upper1": u1, "upper2": u2}
        if level in band_map:
            return price >= band_map[level] * (1 - tol)
        if level == "either":
            return price >= u1 * (1 - tol) or price >= u2 * (1 - tol)
        return price >= u1 * (1 - tol)   # fallback to upper1

    def quant_filter(self, df, skip_volume=False):
        last   = df.iloc[-1]
        recent = df.tail(5)
        if self.filter_ema_var.get():
            if last["close"] < last["ema"]:
                return False
        if self.filter_rsi_var.get():
            rsi_min = self.rsi_min_threshold_var.get()
            if last["rsi"] < rsi_min:
                return False
        if self.filter_range_var.get():
            rng = recent["high"].max() - recent["low"].min()
            if rng > recent["close"].mean() * 0.8:
                return False
        if self.filter_volume_var.get() and not skip_volume:
            vmean = recent["volume"].mean()
            if last["volume"] > vmean * 2:
                return False
        if self.filter_gamma_trap_var.get():
            body       = abs(last["close"] - last["open"])
            range_size = last["high"] - last["low"]
            if range_size > 0 and body / range_size < 0.35:
                return False
        if self.filter_vwap_var.get():
            level_s = getattr(self, "vwap_band_level_var", None)
            tol_var = getattr(self, "vwap_band_tol_var",   None)
            level   = level_s.get() if level_s else "upper1"
            tol     = tol_var.get() if tol_var else 2.0
            if not self.vwap_band_touch_ok(last, level, tol):
                return False
        if self.filter_multi_bar_var.get():
            direction = getattr(self, "_quant_filter_direction", "CE")
            if not self.multi_bar_momentum_filter(df, direction):
                return False
        if self.filter_consolidation_var.get():
            if not self.consolidation_filter(df):
                return False
        if self.filter_vol_ratio_var.get():
            if not self.volume_surge_filter(df):
                return False
        if self.filter_body_quality_var.get():
            if not self.body_quality_filter(df):
                return False
        return True

    def multi_bar_momentum_filter(self, df, direction="CE"):
        """Returns True if 3 of the last 5 candles align with trade direction."""
        if df is None or len(df) < 5:
            return False
        recent = df.tail(5)
        aligned = (recent["close"] > recent["open"]).sum()
        return aligned >= 3

    def time_window_filter(self, bar_time):
        """True if bar_time is inside the configured entry window (or EOD extension)."""
        try:
            start = dt.datetime.strptime(
                self.time_window_start_var.get(), "%H:%M").time()
            end = dt.datetime.strptime(
                self.time_window_end_var.get(), "%H:%M").time()
        except Exception:
            return True
        if isinstance(bar_time, dt.time):
            bt = bar_time
        else:
            try:
                bt = pd.Timestamp(bar_time).time()
            except Exception:
                return True
        if start <= bt <= end:
            return True
        if self.time_window_eod_var.get():
            if dt.time(14, 30) <= bt <= dt.time(15, 0):
                return True
        return False

    def consolidation_filter(self, df):
        """True if the N bars BEFORE the current bar formed a tight range.
        Uses ATR-relative tightness when the ATR column is available (preferred),
        otherwise falls back to range-as-%-of-close."""
        n = self.consol_lookback_var.get()
        if df is None or len(df) < n + 2:
            return False
        ratio_max = self.consol_atr_ratio_var.get()
        prior = df.iloc[-(n + 1):-1]
        rng   = prior["high"].max() - prior["low"].min()

        # ATR-based tightness (accurate): range < ratio_max × ATR
        if "atr" in df.columns:
            atr_val = float(df.iloc[-1]["atr"])
            if atr_val > 0:
                return rng < ratio_max * atr_val

        # Fallback: range as % of mean close
        mean_close = prior["close"].mean()
        if mean_close <= 0:
            return False
        return (rng / mean_close) * 100 < ratio_max

    def volume_surge_filter(self, df):
        """Current bar vol > min_mult * 5-bar mean (follow-through handled by caller)."""
        if df is None or len(df) < 6:
            return False
        min_mult = self.vol_ratio_min_var.get()
        last = df.iloc[-1]
        recent = df.iloc[-6:-1]
        vmean = recent["volume"].mean()
        if vmean <= 0:
            return False
        if last["volume"] < vmean * min_mult:
            return False
        return True

    def body_quality_filter(self, df):
        """Ignition candle body must be >= configured % of total range."""
        if df is None or len(df) < 1:
            return False
        last = df.iloc[-1]
        rng = last["high"] - last["low"]
        if rng <= 0:
            return False
        body = abs(last["close"] - last["open"])
        return (body / rng) >= self.body_quality_min_var.get()

    def gamma_expansion_detector(self, df):
        if df is None or len(df) < 20:
            return False
        last   = df.iloc[-1]
        prev   = df.iloc[-2]
        recent = df.tail(5)
        if abs(last["close"] - last["open"]) < abs(prev["close"] - prev["open"]) * 1.3:
            return False
        vol_mean = recent["volume"].mean()
        if vol_mean == 0 or last["volume"] < vol_mean * 1.5:
            return False
        if last["close"] < last["vwap"]:
            return False
        if (last["high"] - last["low"]) < (recent["high"].max() - recent["low"].min()) * 0.25:
            return False
        return True

    def strike_momentum_score(self, df):
        if df is None or len(df) < 5:
            return 0
        last     = df.iloc[-1]
        prev     = df.iloc[-2]
        pm       = abs(last["close"] - prev["close"])
        vol_mean = df["volume"].tail(5).mean()
        vs       = last["volume"] / vol_mean if vol_mean > 0 else 0
        vd       = abs(last["close"] - last["vwap"])
        return pm * 2 + vs + vd

    def get_strongest_strike(self):
        best_token = None
        best_score = 0
        with self.lock:
            tokens = list(self.strike_state.keys())
        for token in tokens:
            if not self.ce:
                continue
            df = self.ce.get_candles(token)
            if df is None or len(df) < 20:
                continue
            score = self.strike_momentum_score(df)
            if score > best_score:
                best_score = score
                best_token = token
        return best_token
