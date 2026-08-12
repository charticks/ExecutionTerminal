import os
import threading
import datetime as dt

import pandas as pd
import pytz


class TickLoggerMixin:
    """Tick data capture, buffering, and flush to disk."""

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

            # RAW TICK DATAFRAME
            df_tick = pd.DataFrame(rows, columns=[
                "timestamp", "ltp", "cum_volume", "vol_delta"
            ])

            if df_tick.empty:
                continue

            # BUILD 1-MIN CANDLES
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
                # SAVE TICK DATA
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

                # SAVE 1-MIN CANDLES
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
            self.log(f"Tick flush [{label}]: {total_written:,} rows → {tick_dir}")
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
