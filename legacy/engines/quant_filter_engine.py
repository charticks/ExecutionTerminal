class QuantFilterEngine:
    """
    Called by CandleEngine on every candle close.
    Applies all configured filters.
    Fires on_signal(token, df) when entry conditions are met.
    """

    def __init__(self, app_ref):
        self.app              = app_ref
        self.on_signal        = None    # fn(token, ltp, df)
        self._token_direction = "CE"    # set from strike_state before check_all_filters

    # ----------------------------------------------------------
    def process(self, token, df):
        """
        Entry point called by CandleEngine on candle close.
        Checks all filters and emits signal if conditions met.
        """
        try:
            import datetime as _dt
            app = self.app
            st  = app.strike_state.get(token)
            if not st:
                return

            # Already in trade or entry taken
            if (st["trade_open"] or st["entry_taken_today"]
                    or st["order_in_progress"]):
                return

            # Risk kill switch — refuse new entries when a limit is breached
            if app.risk_entry_blocked():
                return

            if df is None or len(df) < 5:
                if df is not None:
                    print(f"QFE: {token} — waiting for candles ({len(df)}/5 ready)")
                return

            if not all(c in df.columns for c in ["ema", "rsi", "vwap"]):
                return

            ltp = app.oce.get_ltp(token) if app.oce else None
            if ltp is None:
                # Fallback: use last tick stored in strike_state (set by _on_tick_d/k/oc)
                ltp = st.get("ltp")
            if ltp is None:
                return

            # Phase 2: time-of-day gate (checked before other filters).
            if app.filter_time_window_var.get():
                if not app.time_window_filter(_dt.datetime.now().time()):
                    return

            # Session quality gate — block known noisy market windows
            if getattr(app, "filter_session_blocks_var", None) and app.filter_session_blocks_var.get():
                now_t = _dt.datetime.now().time()
                for blk_s, blk_e in [
                    (_dt.time(9, 15), _dt.time(9, 30)),    # market open chaos
                    (_dt.time(13, 0), _dt.time(13, 15)),   # lunch volume thinning
                ]:
                    if blk_s <= now_t <= blk_e:
                        return

            # OI liquidity gate — skip illiquid strikes
            if getattr(app, "filter_oi_var", None) and app.filter_oi_var.get():
                oi     = st.get("oi", 0)
                oi_min = app.oi_min_var.get() if hasattr(app, "oi_min_var") else 50000
                if oi < oi_min:
                    return

            # Bid-ask spread guard — avoid wide-spread entries
            if getattr(app, "filter_spread_guard_var", None) and app.filter_spread_guard_var.get():
                bid = st.get("bid", 0)
                ask = st.get("ask", 0)
                if bid > 0 and ask > 0:
                    spread_pct = (ask - bid) / ask * 100
                    max_spread = app.max_spread_pct_var.get() if hasattr(app, "max_spread_pct_var") else 2.0
                    if spread_pct > max_spread:
                        return

            # Set direction so multi-bar momentum filter knows CE vs PE
            self._token_direction = st.get("type", "CE")

            # FIX 1 — Quant filter was defined but never called from process().
            # check_all_filters() runs ALL enabled filters including gamma expansion.
            # Without this call, every filter checkbox was silently ignored in live.
            if app.enable_quant_var.get():
                if not self.check_all_filters(df):
                    return

            # ── Entry Mode ───────────────────────────────────────
            entry_mode   = app.entry_mode_var.get()
            entry_signal = False

            if entry_mode == "MARKET":
                entry_signal = True

            elif entry_mode == "PRICE_BAND":
                if not st["entry_band_triggered"]:
                    if app.entry_band_hit(ltp):
                        st["entry_band_triggered"] = True
                if st["entry_band_triggered"]:
                    entry_signal = True

            elif entry_mode == "VWAP_RECLAIM":
                ep = app.vwap_reclaim_entry(df)
                if ep and ltp >= ep:
                    entry_signal = True

            elif entry_mode == "VWAP_SQUEEZE_BREAK":
                ep = app.vwap_squeeze_break_entry(df)
                if ep and ltp >= ep:
                    entry_signal = True

            if entry_signal and self.on_signal:
                self.on_signal(token, ltp, df)

        except Exception as e:
            print("QuantFilterEngine error:", e)

    # ----------------------------------------------------------
    def _gamma_expansion_detector(self, df):
        if df is None or len(df) < 20:
            return False
        last   = df.iloc[-1]
        prev   = df.iloc[-2]
        recent = df.tail(5)

        body_last = abs(last["close"] - last["open"])
        body_prev = abs(prev["close"] - prev["open"])
        if body_last < body_prev * 1.3:
            return False

        vol_mean = recent["volume"].mean()
        if vol_mean == 0 or last["volume"] < vol_mean * 1.5:
            return False

        if last["close"] < last["vwap"]:
            return False

        range_recent = recent["high"].max() - recent["low"].min()
        range_last   = last["high"] - last["low"]
        if range_last < range_recent * 0.25:
            return False

        return True

    def check_all_filters(self, df):
        app = self.app
        last = df.iloc[-1]
        recent = df.tail(5)

        # EMA
        if app.filter_ema_var.get():
            cs = df["close"]
            p1 = app.ema_f1_var.get()
            p2 = app.ema_f2_var.get()
            p3 = app.ema_f3_var.get()
            ema1_val = cs.ewm(span=p1, adjust=False).mean().iloc[-1] if p1 > 0 else None
            ema2_val = cs.ewm(span=p2, adjust=False).mean().iloc[-1] if p2 > 0 else None
            ema3_val = cs.ewm(span=p3, adjust=False).mean().iloc[-1] if p3 > 0 else None
            # Price must be above all active EMAs
            if ema1_val is not None and last["close"] < ema1_val:
                return False
            if ema2_val is not None and last["close"] < ema2_val:
                return False
            if ema3_val is not None and last["close"] < ema3_val:
                return False
            # Proximity: block if price has run too far above EMA1 (avoid chase entries)
            if app.ema_proximity_var.get() and ema1_val is not None:
                tol = app.ema_proximity_tol_var.get() / 100.0
                if last["close"] > ema1_val * (1 + tol):
                    return False
            # EMA stack alignment: fast EMA must be above slow EMA
            if app.ema_alignment_var.get():
                if ema1_val is not None and ema2_val is not None and ema1_val <= ema2_val:
                    return False
                if ema2_val is not None and ema3_val is not None and ema2_val <= ema3_val:
                    return False
            # EMA crossover: EMA1 must have crossed above EMA2 since 09:15 today
            if app.ema_crossover_var.get():
                if p1 <= 0 or p2 <= 0:
                    return False
                import datetime as _dt
                import pandas as _pd
                today       = _dt.datetime.now().date()
                market_open = _dt.time(9, 15)
                if "time" not in df.columns:
                    return False
                times = _pd.to_datetime(df["time"])
                intraday_mask = (times.dt.date == today) & (times.dt.time >= market_open)
                if intraday_mask.sum() < 2:
                    return False
                # Compute EMA on full series (proper warmup), then slice to intraday
                ema1_ser = cs.ewm(span=p1, adjust=False).mean()[intraday_mask].reset_index(drop=True)
                ema2_ser = cs.ewm(span=p2, adjust=False).mean()[intraday_mask].reset_index(drop=True)
                crossed = any(
                    ema1_ser.iloc[i] <= ema2_ser.iloc[i] and
                    ema1_ser.iloc[i + 1] > ema2_ser.iloc[i + 1]
                    for i in range(len(ema1_ser) - 1)
                )
                if not crossed:
                    return False
                if last["close"] < ema1_val:
                    return False
                if ema2_val is not None and last["close"] < ema2_val:
                    return False

        # RSI
        if app.filter_rsi_var.get():
            rsi_min = app.rsi_min_threshold_var.get()
            if last["rsi"] < rsi_min:
                return False

        # RANGE
        if app.filter_range_var.get():
            rng = recent["high"].max() - recent["low"].min()
            if rng > recent["close"].mean() * 0.8:
                return False

        # VOLUME
        if app.filter_volume_var.get():
            vmean = recent["volume"].mean()
            if last["volume"] > vmean * 2:
                return False

        # VWAP
        if app.filter_vwap_var.get():
            band_width = df["vwap_upper1"] - df["vwap_lower1"]
            if band_width.tail(5).mean() > last["close"] * 0.12:
                return False
            _level_v = getattr(app, "vwap_band_level_var", None)
            _tol_v   = getattr(app, "vwap_band_tol_var",   None)
            _level   = _level_v.get() if _level_v else "upper1"
            _tol     = _tol_v.get() if _tol_v else 2.0
            # Selected band is the sole price threshold (no lower-1 floor)
            if not app.vwap_band_touch_ok(last, _level, _tol):
                return False

        # GAMMA TRAP
        if app.filter_gamma_trap_var.get():
            body = abs(last["close"] - last["open"])
            range_size = last["high"] - last["low"]
            if range_size > 0 and body / range_size < 0.35:
                return False

        # GAMMA EXPANSION
        if app.filter_gamma_expansion_var.get():
            if not self._gamma_expansion_detector(df):
                return False

        # MULTI-BAR MOMENTUM
        if app.filter_multi_bar_var.get():
            direction = self._token_direction
            if not app.multi_bar_momentum_filter(df, direction):
                return False

        # ADX — trend strength (requires ATR/ADX columns from CandleEngine)
        if getattr(app, "filter_adx_var", None) and app.filter_adx_var.get():
            adx_min = getattr(app, "adx_min_var", None)
            adx_min = adx_min.get() if adx_min else 20
            adx_val = float(last["adx"]) if "adx" in df.columns else 0.0
            if adx_val < adx_min:
                return False

        # Supertrend direction filter
        if getattr(app, "filter_supertrend_var", None) and app.filter_supertrend_var.get():
            if "supertrend_dir" in df.columns:
                st_dir    = int(last["supertrend_dir"])
                direction = self._token_direction
                if direction == "CE" and st_dir != 1:
                    return False
                if direction == "PE" and st_dir != -1:
                    return False

        # ── Phase 2 pattern filters ──────────────────────────
        if app.filter_consolidation_var.get():
            if not app.consolidation_filter(df):
                return False
        if app.filter_vol_ratio_var.get():
            if not app.volume_surge_filter(df):
                return False
        if app.filter_body_quality_var.get():
            if not app.body_quality_filter(df):
                return False

        return True
