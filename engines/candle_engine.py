import threading
import datetime as dt

import pandas as pd


class CandleEngine:
    """
    Reads tick_store every tick (via on_tick_cb).
    Builds OHLCV candles per token for the chosen interval.
    On candle close: computes EMA, RSI, VWAP, Bands.
    Fires on_candle_close(token, df) → QuantFilterEngine.
    Zero REST calls.
    """

    def __init__(self, option_chain_engine, interval="3min",
                 ema_period=20, rsi_period=14):
        self.oce          = option_chain_engine
        self.interval     = interval           # "1min" / "3min" / "5min"
        self.ema_period   = ema_period
        self.rsi_period   = rsi_period

        # {token: { candles: DataFrame,
        #           current_candle: dict,
        #           last_candle_minute: datetime,
        #           last_cum_vol: int,
        #           lot_size: int }}
        self.state        = {}
        self.lock         = threading.Lock()

        # Callback fired on candle close
        self.on_candle_close = None   # fn(token, df)

        # NOTE: on_tick_cb is NOT set here.
        # _run_all_engines wires oce.on_tick_cb = on_tick_live which
        # calls both ce._on_tick AND ltp/trade logic in one place.
        # Setting it here would be overwritten later and break the chain.

    # ----------------------------------------------------------
    def register_token(self, token, lot_size, seed_df=None,
                       seed_last_cum_vol=0, seed_current_candle=None,
                       seed_last_candle_minute=None):
        """
        Called at startup for each token.
        seed_df: historical candles DataFrame (from REST at startup only).
        """
        with self.lock:
            self.state[token] = {
                "candles"            : seed_df.copy() if seed_df is not None
                                       else pd.DataFrame(),
                "current_candle"     : seed_current_candle,
                "last_candle_minute" : seed_last_candle_minute,
                "last_cum_vol"       : seed_last_cum_vol,
                "lot_size"           : lot_size,
            }

    def try_late_seed(self, token, seed_df, last_cum_vol,
                      seed_current_candle, seed_last_candle_minute):
        """Inject historical seed data after bot has already started.
        Prepends seed candles ahead of any live candles already accumulated.
        Only applies when seed provides more history than current live data.
        """
        if seed_df is None or seed_df.empty:
            return
        with self.lock:
            st = self.state.get(token)
            if st is None:
                return
            live_df = st["candles"]
            if len(seed_df) <= len(live_df):
                return  # live data already as good or better
            n_live = len(live_df)
            n_keep = max(0, 600 - n_live)
            if not live_df.empty:
                combined = pd.concat([seed_df.tail(n_keep), live_df], ignore_index=True)
            else:
                combined = seed_df.tail(600).reset_index(drop=True)
            st["candles"] = combined
            if n_live == 0:   # no live candles yet — also update volume baseline
                st["last_cum_vol"]       = last_cum_vol
                st["current_candle"]     = seed_current_candle
                st["last_candle_minute"] = seed_last_candle_minute

    def remove_token(self, token):
        with self.lock:
            self.state.pop(token, None)

    def get_candles(self, token):
        with self.lock:
            st = self.state.get(token)
            if st is None:
                return None
            return st["candles"].copy() if not st["candles"].empty else None

    # ----------------------------------------------------------
    def _candle_boundary(self, now):
        """Return the floored candle-start datetime for current time."""
        interval = self.interval
        if interval == "3min":
            total = now.hour * 60 + now.minute
            snapped = (total // 3) * 3
            return now.replace(hour=snapped // 60,
                               minute=snapped % 60,
                               second=0, microsecond=0)
        elif interval == "5min":
            total = now.hour * 60 + now.minute
            snapped = (total // 5) * 5
            return now.replace(hour=snapped // 60,
                               minute=snapped % 60,
                               second=0, microsecond=0)
        else:  # 1min
            return now.replace(second=0, microsecond=0)

    # ----------------------------------------------------------
    def _on_tick(self, token, ltp, cum_vol, now):
        """Called by OptionChainEngine on every tick."""
        candle_to_close = None

        with self.lock:
            st = self.state.get(token)
            if st is None:
                return

            minute       = self._candle_boundary(now)
            last_minute  = st["last_candle_minute"]
            candle       = st["current_candle"]
            lot_size     = st["lot_size"]
            last_cum_vol = st["last_cum_vol"]

            # Compute volume delta
            if cum_vol is not None and cum_vol >= last_cum_vol:
                vol_delta = max(int(round(
                    (cum_vol - last_cum_vol) / lot_size)), 0)
                st["last_cum_vol"] = cum_vol
            else:
                vol_delta = 0

            # New candle boundary
            if last_minute != minute:
                if candle:
                    candle_to_close = candle.copy()

                st["current_candle"] = {
                    "time"   : minute,
                    "open"   : ltp,
                    "high"   : ltp,
                    "low"    : ltp,
                    "close"  : ltp,
                    "volume" : vol_delta,
                }
                st["last_candle_minute"] = minute

            else:
                if candle:
                    candle["high"]    = max(candle["high"], ltp)
                    candle["low"]     = min(candle["low"],  ltp)
                    candle["close"]   = ltp
                    candle["volume"] += vol_delta
                else:
                    st["current_candle"] = {
                        "time"   : minute,
                        "open"   : ltp,
                        "high"   : ltp,
                        "low"    : ltp,
                        "close"  : ltp,
                        "volume" : vol_delta,
                    }
                    st["last_candle_minute"] = minute

        # Process closed candle OUTSIDE lock
        if candle_to_close:
            self._close_candle(token, candle_to_close)

    # ----------------------------------------------------------
    def _close_candle(self, token, candle):
        """Append candle to df, compute indicators, fire callback."""
        with self.lock:
            st = self.state.get(token)
            if st is None:
                return

            df = st["candles"]
            new_row = pd.DataFrame([candle])

            if df.empty:
                df = new_row.copy()
            else:
                df = pd.concat([df, new_row], ignore_index=True)

            df = df.tail(600).reset_index(drop=True)

            # ── Indicators ──────────────────────────────────────
            df = self._compute_indicators(df)

            st["candles"] = df

        # Fire callback outside lock
        if self.on_candle_close:
            try:
                self.on_candle_close(token, df.copy())
            except Exception as e:
                print("CandleEngine callback error:", e)

    # ----------------------------------------------------------
    def _compute_indicators(self, df):
        """Compute EMA, RSI, VWAP, Bands, ATR, ADX, Supertrend on full DataFrame."""
        # EMA
        df["ema"] = df["close"].ewm(
            span=self.ema_period, adjust=False).mean()

        # RSI — Wilder's EWM
        delta    = df["close"].diff()
        gain     = delta.clip(lower=0)
        loss     = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / self.rsi_period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / self.rsi_period, adjust=False).mean()
        rs       = avg_gain / avg_loss.replace(0, float("nan"))
        df["rsi"] = 100 - (100 / (1 + rs))
        df["rsi"] = df["rsi"].fillna(50)

        # VWAP (resets each day — filter to today only)
        today = dt.datetime.now().date()
        df["time"] = pd.to_datetime(df["time"])
        today_mask = df["time"].dt.date == today

        df["typical_price"] = (df["high"] + df["low"] + df["close"]) / 3
        df["pv"]            = df["typical_price"] * df["volume"]

        # Reset cumulative sums at day boundary
        df["cum_pv"]  = df["pv"].where(today_mask).fillna(0).cumsum()
        df["cum_vol"] = df["volume"].where(today_mask).fillna(0).cumsum()
        df["vwap"]    = (df["cum_pv"] /
                         df["cum_vol"].replace(0, float("nan"))).ffill()

        # VWAP Bands
        roll_std = (df["typical_price"] - df["vwap"]).rolling(
            30, min_periods=1).std()
        df["std"]          = roll_std
        df["vwap_upper1"]  = df["vwap"] + roll_std
        df["vwap_lower1"]  = df["vwap"] - roll_std
        df["vwap_upper2"]  = df["vwap"] + 2 * roll_std
        df["vwap_lower2"]  = df["vwap"] - 2 * roll_std

        # ATR (Wilder's, period=14)
        df = self._add_atr(df, period=14)

        # ADX (Wilder's, period=14)
        df = self._add_adx(df, period=14)

        # Supertrend (period=10, multiplier=3)
        df = self._add_supertrend(df, period=10, multiplier=3)

        df.bfill(inplace=True)
        return df

    @staticmethod
    def _add_atr(df, period=14):
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"]  - prev_close).abs(),
        ], axis=1).max(axis=1)
        df["atr"] = tr.ewm(alpha=1 / period, adjust=False).mean()
        return df

    @staticmethod
    def _add_adx(df, period=14):
        prev_high  = df["high"].shift(1)
        prev_low   = df["low"].shift(1)
        up_move    = df["high"] - prev_high
        down_move  = prev_low  - df["low"]

        plus_dm  = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

        atr = df.get("atr")
        if atr is None:
            prev_close = df["close"].shift(1)
            tr  = pd.concat([
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"]  - prev_close).abs(),
            ], axis=1).max(axis=1)
            atr = tr.ewm(alpha=1 / period, adjust=False).mean()

        safe_atr = atr.replace(0, float("nan"))
        plus_di  = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean()  / safe_atr
        minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / safe_atr
        dx = (100 * (plus_di - minus_di).abs()
              / (plus_di + minus_di).replace(0, float("nan")))
        df["adx"]      = dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0)
        df["plus_di"]  = plus_di.fillna(0)
        df["minus_di"] = minus_di.fillna(0)
        return df

    @staticmethod
    def _add_supertrend(df, period=10, multiplier=3):
        """Add supertrend_dir column: 1 = bullish, -1 = bearish."""
        if "atr" not in df.columns:
            df = CandleEngine._add_atr(df, period)
        hl2   = (df["high"] + df["low"]) / 2
        upper = (hl2 + multiplier * df["atr"]).values
        lower = (hl2 - multiplier * df["atr"]).values
        close = df["close"].values
        n     = len(close)

        direction  = [1] * n
        final_band = lower.copy()

        for i in range(1, n):
            if direction[i - 1] == 1:
                cur = max(lower[i], final_band[i - 1])
                if close[i] < cur:
                    direction[i]  = -1
                    final_band[i] = upper[i]
                else:
                    direction[i]  = 1
                    final_band[i] = cur
            else:
                cur = min(upper[i], final_band[i - 1])
                if close[i] > cur:
                    direction[i]  = 1
                    final_band[i] = lower[i]
                else:
                    direction[i]  = -1
                    final_band[i] = cur

        df["supertrend_dir"]  = direction
        df["supertrend_band"] = final_band
        return df

    def update_interval(self, interval):
        self.interval = interval

    def update_periods(self, ema_period, rsi_period):
        self.ema_period = ema_period
        self.rsi_period = rsi_period
