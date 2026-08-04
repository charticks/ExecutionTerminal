import threading
import time
import datetime as dt

import tkinter as tk
from tkinter import messagebox

from engines import CandleEngine, QuantFilterEngine, TradeExecutionEngine
from app.strategy_context import StrategyContext

_STRIKE_SWAP_ATTRS = [
    "index_var", "strike_mode_var", "directional_vars", "legacy_gap_vars",
    "custom_range_from_var", "custom_range_to_var",
    "custom_range_ce_var", "custom_range_pe_var",
    "use_strongest_strike_var",
]


class MultiStrategyRunnerMixin:
    """
    Manages a run-queue of saved strategy presets and starts them all
    simultaneously when the user clicks Start Bot.
    """

    # ── Queue management ──────────────────────────────────────────────

    def add_to_run_queue(self):
        """Add the currently selected preset to the run queue."""
        name = self.strategy_select_var.get()
        if not name or name == "── no presets saved ──":
            messagebox.showwarning("No Preset", "Select a preset first.", parent=self.root)
            return
        if name in self.preset_run_queue:
            messagebox.showinfo("Already Added",
                                f"'{name}' is already in the run queue.",
                                parent=self.root)
            return
        self.preset_run_queue.append(name)
        self._refresh_queue_display()
        self.log(f"[Queue] Added '{name}' ({len(self.preset_run_queue)} in queue)")

    def remove_from_run_queue(self, name):
        if name in self.preset_run_queue:
            self.preset_run_queue.remove(name)
            self._refresh_queue_display()
            self.log(f"[Queue] Removed '{name}'")

    def clear_run_queue(self):
        self.preset_run_queue.clear()
        self._refresh_queue_display()
        self.log("[Queue] Cleared")

    def _refresh_queue_display(self):
        """Rebuild the chip display inside queue_chips_frame."""
        frame = getattr(self, "queue_chips_frame", None)
        if frame is None:
            return
        for w in frame.winfo_children():
            w.destroy()
        if not self.preset_run_queue:
            tk.Label(frame, text="No strategies queued — add presets above",
                     bg="#1a1a2e", fg="#555577",
                     font=("Segoe UI", 8, "italic")).pack(side="left", padx=4)
            return
        for name in self.preset_run_queue:
            chip = tk.Frame(frame, bg="#1a3a6a", bd=1,
                            highlightbackground="#4488cc", highlightthickness=1)
            chip.pack(side="left", padx=3, pady=2)
            tk.Label(chip, text=name, bg="#1a3a6a", fg="#88ccff",
                     font=("Segoe UI", 8, "bold"), padx=4).pack(side="left")
            tk.Button(chip, text="✕", bg="#1a3a6a", fg="#ff6666",
                      font=("Segoe UI", 7), bd=0, padx=2,
                      command=lambda n=name: self.remove_from_run_queue(n)
                      ).pack(side="left")

    # ── Strike generation for a StrategyContext ───────────────────────

    def _generate_strikes_for_ctx(self, ctx, spot):
        """
        Generate strikes using ctx's parameters.
        Temporarily swaps the relevant app vars, calls the existing
        calculate_atm / generate_strikes / map_strikes_to_tokens, then restores.
        Must be called sequentially (not thread-safe w.r.t. other generators).
        """
        saved = {}
        for attr in _STRIKE_SWAP_ATTRS:
            saved[attr] = getattr(self, attr)
        try:
            for attr in _STRIKE_SWAP_ATTRS:
                ctx_val = object.__getattribute__(ctx, attr) if self._ctx_has(ctx, attr) else None
                if ctx_val is not None:
                    setattr(self, attr, ctx_val)
            atm, step = self.calculate_atm(spot)
            strikes   = self.generate_strikes(atm, step)
            selected  = self.map_strikes_to_tokens(strikes)
        finally:
            for attr, v in saved.items():
                setattr(self, attr, v)
        return atm, step, strikes, selected

    @staticmethod
    def _ctx_has(ctx, attr):
        try:
            object.__getattribute__(ctx, attr)
            return True
        except AttributeError:
            return False

    # ── Main multi-strategy startup ───────────────────────────────────

    def _start_multi_strategy_bot(self):
        """
        Orchestrates simultaneous startup of all queued strategy presets.
        Runs in a background thread (started from start_bot).
        """
        try:
            self.log(f"🚀 Multi-strategy mode: {len(self.preset_run_queue)} presets")

            # ── 1. Reset tick log ─────────────────────────────────────
            with self.tick_lock:
                self.tick_log.clear()
            self._tick_flush_stop.clear()

            # ── 2. Spot detection (uses main UI's spot settings) ──────
            spot_text        = self.spot_entry.get_value()
            spot_detect_mode = self.spot_detect_mode_var.get()
            is_manual_mode   = self.trade_exec_mode_var.get() == "Manual"
            spot = None

            if spot_text:
                try:
                    spot_time = dt.datetime.strptime(spot_text, "%H:%M").time()
                except ValueError:
                    spot_time = None
                else:
                    if spot_detect_mode == "TIME":
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
                spot = 0

            if not spot:
                self.root.after(0, lambda: self.status_var.set("Spot fetch failed ❌"))
                self.log("❌ Multi-strategy: spot fetch failed")
                self.is_running = False
                return

            self.log(f"📍 Spot: {spot}")

            # ── 3. Load presets → StrategyContext objects ─────────────
            contexts = []
            for name in list(self.preset_run_queue):
                try:
                    params = self.load_strategy_from_file(name)
                    ctx    = StrategyContext(self, params, name)
                    contexts.append(ctx)
                    self.log(f"  ✅ Loaded: {name}")
                except Exception as e:
                    self.log(f"  ❌ Failed to load '{name}': {e}")

            if not contexts:
                self.log("❌ No valid strategies to run")
                self.is_running = False
                return

            # ── 4. Generate strikes per context ───────────────────────
            index    = self.index_var.get().upper()
            exch_map = {"NIFTY": "NFO", "BANKNIFTY": "NFO",
                        "SENSEX": "BFO", "CRUDEOIL": "MCX"}
            exch     = exch_map.get(index, "NFO")

            valid_plans = []   # [(ctx, selected, lot_size)]
            all_tokens  = []

            for ctx in contexts:
                try:
                    _, _, strikes, selected = self._generate_strikes_for_ctx(ctx, spot)
                    lot_size = self.get_lot_size() * max(1, ctx.lots_var.get())
                    if not selected:
                        self.log(f"  ⚠️  '{ctx.preset_name}': no tokens mapped — skipped")
                        continue
                    valid_plans.append((ctx, selected, lot_size))
                    for _, _, t, _ in selected:
                        if t not in all_tokens:
                            all_tokens.append(t)
                    self.log(f"  ✅ '{ctx.preset_name}': {len(selected)} strikes, lot={lot_size}")
                except Exception as e:
                    self.log(f"  ❌ '{ctx.preset_name}' strike gen failed: {e}")

            if not valid_plans:
                self.log("❌ No strategies have valid strikes")
                self.is_running = False
                return

            # ── 5. Initialise per-context strike_state ─────────────────
            self.root.after(0, self.clear_strike_panel)
            time.sleep(0.05)   # let GUI clear

            for ctx, selected, lot_size in valid_plans:
                for strike, opt_type, token, tradingsymbol in selected:
                    ctx.strike_state[token] = {
                        "exchange":             exch,
                        "tradingsymbol":        tradingsymbol,
                        "lot_size":             lot_size,
                        "strike":               strike,
                        "type":                 opt_type,
                        "trade_open":           False,
                        "entry_taken_today":    False,
                        "order_in_progress":    False,
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
                    }
                # Add strike LTP rows (label prefixed with strategy name)
                prefix = f"[{ctx.preset_name[:9]}] "
                for token, data in ctx.strike_state.items():
                    self.root.after(
                        0,
                        lambda t=token, s=data["strike"], ot=data["type"],
                               ls=data["lot_size"]:
                        self._add_strike_ltp_row(t, s, ot, ls),
                    )

            # ── 6. Seed historical candles ─────────────────────────────
            self.root.after(0, lambda: self.status_var.set("Loading candle data..."))
            _seen_tokens = set()
            combined_selected = []
            for _ctx, _sel, _ in valid_plans:
                for item in _sel:
                    if item[2] not in _seen_tokens:
                        _seen_tokens.add(item[2])
                        combined_selected.append(item)
            seed_lot = valid_plans[0][2]
            seed_data = self._seed_historical_candles(combined_selected, exch, seed_lot)

            # ── 7. Start data feed + per-strategy CE/QFE/TEE ──────────
            # Angel One is the sole market-data feed; orders still fan out to
            # all checked brokers via place_order().
            oc_tokens = [
                row[k] for row in self.oc_data
                for k in ("ce_token", "pe_token")
                if row.get(k) and row[k] not in all_tokens
            ]
            pipeline_list = []   # [(ctx, ce, qfe, tee)]

            self._start_multi_angel(
                valid_plans, seed_data, all_tokens, oc_tokens,
                exch, pipeline_list)

            if not pipeline_list:
                self.log("❌ Multi-strategy: no pipelines started")
                self.is_running = False
                return

            # ── 8. Status log ─────────────────────────────────────────
            names = [ctx.preset_name for ctx, _, _ in valid_plans]
            self.root.after(
                0,
                lambda: self.status_var.set(
                    f"Multi-Strategy Running ✅  ({len(valid_plans)} strategies)"),
            )
            self.log(f"✅ Running strategies: {names}")

            # ── 9. Watchdog ───────────────────────────────────────────
            threading.Thread(target=self._watchdog, daemon=True,
                             name="multi-watchdog").start()

        except Exception as e:
            import traceback
            traceback.print_exc()
            self.root.after(
                0, lambda msg=str(e): self.status_var.set(f"Multi-strategy error: {msg}"))
            self.is_running = False

    # ── Angel multi-strategy pipeline ────────────────────────────────

    def _start_multi_angel(self, valid_plans, seed_data, all_tokens,
                           oc_tokens, exch, pipeline_list):
        if not self.angel_logged_in:
            self.log("❌ Data Feed set to Angel but Angel not logged in")
            return

        import config as _cfg
        from engines import OptionChainEngine

        oce_exch_map  = {"NFO": 2, "BFO": 4, "MCX": 5}
        token_list_ws = [{"exchangeType": oce_exch_map.get(exch, 2),
                          "tokens":       all_tokens + oc_tokens}]

        oce = OptionChainEngine(
            self.jwt_token, _cfg.API_KEY,
            self.client_code, self.feed_token)
        oce.app_ref = self   # must be set before subscribe so on_open can call _set_api_status
        oce.subscribe(token_list_ws)
        self.oce = oce

        for ctx, selected, lot_size in valid_plans:
            ce  = CandleEngine(oce,
                               interval=ctx.live_interval_var.get(),
                               ema_period=ctx.ema_period_var.get(),
                               rsi_period=ctx.rsi_period_var.get())
            ctx.ce = ce
            for token, data in ctx.strike_state.items():
                sd = seed_data.get(token, {})
                ce.register_token(
                    token,
                    lot_size=data["lot_size"],
                    seed_df=sd.get("df"),
                    seed_last_cum_vol=sd.get("last_cum_vol", 0),
                    seed_current_candle=sd.get("current_candle"),
                    seed_last_candle_minute=sd.get("last_candle_minute"),
                )

            qfe = QuantFilterEngine(ctx)
            tee = TradeExecutionEngine(ctx)

            def _make_candle_cb(_qfe, _tee, _oce, _ctx):
                def _cb(token, df):
                    _qfe.process(token, df)
                    ltp = _oce.get_ltp(token)
                    st  = _ctx.strike_state.get(token)
                    if ltp and st and st["trade_open"]:
                        _tee.on_tick(token, ltp)
                return _cb

            ce.on_candle_close  = _make_candle_cb(qfe, tee, oce, ctx)
            qfe.on_signal       = lambda tok, ltp, df, _c=ctx: _c.open_trade(tok, ltp)
            pipeline_list.append((ctx, ce, qfe, tee))
            self.log(f"  🟢 Angel pipeline ready: '{ctx.preset_name}' "
                     f"({ctx.live_interval_var.get()} candles)")

        # Combined tick callback dispatches to all strategy CEs
        def _on_tick_multi(token, ltp, cum_vol, now):
            self._record_tick(token, ltp, cum_vol, now)

            for _ctx, _ce, _qfe, _tee in pipeline_list:
                if token in _ctx.strike_state:
                    _ce._on_tick(token, ltp, cum_vol, now)
                    with _ctx.lock:
                        st = _ctx.strike_state.get(token)
                        if st:
                            st["ltp"] = ltp
                            if (not st["trade_open"]
                                    and not st["entry_taken_today"]
                                    and not st["order_in_progress"]):
                                if _ctx.entry_band_hit(ltp):
                                    st["entry_band_triggered"] = True
                    if _ctx.strike_state.get(token, {}).get("trade_open"):
                        _tee.on_tick(token, ltp)

            # OC panel + Strike LTP labels (shared)
            self.update_oc_ltp(token, ltp)
            if token in self.strike_ltp_labels:
                lbl  = self.strike_ltp_labels[token]
                _buf = getattr(self, "_lbl_pending", None)
                if _buf is not None:
                    _buf[f"sl_{token}"] = (lbl, str(round(ltp, 2)))
                else:
                    self.root.after(
                        0, lambda l=lbl, v=ltp: l.config(text=str(round(v, 2))))

        oce.on_tick_cb = _on_tick_multi
        self._broker_pipelines["multi_angel"] = {
            "oce": oce, "pipelines": pipeline_list,
        }
        self.log("🟢 Angel multi-strategy feed started")

    # ── Kotak multi-strategy pipeline ────────────────────────────────

    def _start_multi_kotak(self, valid_plans, seed_data, all_tokens,
                           oc_tokens, exch, pipeline_list):
        if not self.kotak_logged_in:
            self.log("❌ Data Feed set to Kotak but Kotak not logged in")
            return

        from engines.kotak_data_engine import KotakDataEngine

        _k_exch_seg = {"SENSEX": "bse_fo", "CRUDEOIL": "mcx_fo"}.get(
            self.index_var.get().upper(), "nse_fo")
        kotak_map = self._build_kotak_token_map(
            [item for _, sel, _ in valid_plans for item in sel],
            oc_tokens)
        kde = KotakDataEngine(self.kotak, kotak_map, exchange_segment=_k_exch_seg)

        for ctx, selected, lot_size in valid_plans:
            ce  = CandleEngine(kde,
                               interval=ctx.live_interval_var.get(),
                               ema_period=ctx.ema_period_var.get(),
                               rsi_period=ctx.rsi_period_var.get())
            ctx.ce = ce
            for token, data in ctx.strike_state.items():
                sd = seed_data.get(token, {})
                ce.register_token(
                    token,
                    lot_size=data["lot_size"],
                    seed_df=sd.get("df"),
                    seed_last_cum_vol=sd.get("last_cum_vol", 0),
                    seed_current_candle=sd.get("current_candle"),
                    seed_last_candle_minute=sd.get("last_candle_minute"),
                )

            qfe = QuantFilterEngine(ctx)
            tee = TradeExecutionEngine(ctx)

            def _make_candle_cb(_qfe, _tee, _kde, _ctx):
                def _cb(token, df):
                    _qfe.process(token, df)
                    ltp = _kde.get_ltp(token)
                    st  = _ctx.strike_state.get(token)
                    if ltp and st and st["trade_open"]:
                        _tee.on_tick(token, ltp)
                return _cb

            ce.on_candle_close  = _make_candle_cb(qfe, tee, kde, ctx)
            qfe.on_signal       = lambda tok, ltp, df, _c=ctx: _c.open_trade(tok, ltp)
            pipeline_list.append((ctx, ce, qfe, tee))

        def _on_tick_multi(token, ltp, cum_vol, now):
            self._record_tick(token, ltp, cum_vol, now)
            for _ctx, _ce, _qfe, _tee in pipeline_list:
                if token in _ctx.strike_state:
                    _ce._on_tick(token, ltp, cum_vol, now)
                    with _ctx.lock:
                        st = _ctx.strike_state.get(token)
                        if st:
                            st["ltp"] = ltp
                            if (not st["trade_open"]
                                    and not st["entry_taken_today"]
                                    and not st["order_in_progress"]):
                                if _ctx.entry_band_hit(ltp):
                                    st["entry_band_triggered"] = True
                    if _ctx.strike_state.get(token, {}).get("trade_open"):
                        _tee.on_tick(token, ltp)
            self.update_oc_ltp(token, ltp)
            if token in self.strike_ltp_labels:
                lbl  = self.strike_ltp_labels[token]
                _buf = getattr(self, "_lbl_pending", None)
                if _buf is not None:
                    _buf[f"sl_{token}"] = (lbl, str(round(ltp, 2)))
                else:
                    self.root.after(
                        0, lambda l=lbl, v=ltp: l.config(text=str(round(v, 2))))

        kde.on_tick_cb = _on_tick_multi
        self._broker_pipelines["multi_kotak"] = {
            "kde": kde, "pipelines": pipeline_list,
        }
        self.log("🟢 Kotak multi-strategy feed started")

    # ── Dhan multi-strategy pipeline ─────────────────────────────────

    def _start_multi_dhan(self, valid_plans, seed_data, all_tokens,
                          oc_tokens, exch, pipeline_list):
        if not self.dhan_logged_in:
            self.log("❌ Data Feed set to Dhan but Dhan not logged in")
            return

        from engines.dhan_data_engine import DhanDataEngine

        combined_selected = [item for _, sel, _ in valid_plans for item in sel]
        dhan_map = self._build_dhan_token_map(combined_selected, oc_tokens)
        dde = DhanDataEngine(self.dhan_context, dhan_map)

        for ctx, selected, lot_size in valid_plans:
            ce  = CandleEngine(dde,
                               interval=ctx.live_interval_var.get(),
                               ema_period=ctx.ema_period_var.get(),
                               rsi_period=ctx.rsi_period_var.get())
            ctx.ce = ce
            for token, data in ctx.strike_state.items():
                sd = seed_data.get(token, {})
                ce.register_token(
                    token,
                    lot_size=data["lot_size"],
                    seed_df=sd.get("df"),
                    seed_last_cum_vol=sd.get("last_cum_vol", 0),
                    seed_current_candle=sd.get("current_candle"),
                    seed_last_candle_minute=sd.get("last_candle_minute"),
                )

            qfe = QuantFilterEngine(ctx)
            tee = TradeExecutionEngine(ctx)

            def _make_candle_cb(_qfe, _tee, _dde, _ctx):
                def _cb(token, df):
                    _qfe.process(token, df)
                    ltp = _dde.get_ltp(token)
                    st  = _ctx.strike_state.get(token)
                    if ltp and st and st["trade_open"]:
                        _tee.on_tick(token, ltp)
                return _cb

            ce.on_candle_close  = _make_candle_cb(qfe, tee, dde, ctx)
            qfe.on_signal       = lambda tok, ltp, df, _c=ctx: _c.open_trade(tok, ltp)
            pipeline_list.append((ctx, ce, qfe, tee))

        def _on_tick_multi(token, ltp, cum_vol, now):
            self._record_tick(token, ltp, cum_vol, now)
            for _ctx, _ce, _qfe, _tee in pipeline_list:
                if token in _ctx.strike_state:
                    _ce._on_tick(token, ltp, cum_vol, now)
                    with _ctx.lock:
                        st = _ctx.strike_state.get(token)
                        if st:
                            st["ltp"] = ltp
                            if (not st["trade_open"]
                                    and not st["entry_taken_today"]
                                    and not st["order_in_progress"]):
                                if _ctx.entry_band_hit(ltp):
                                    st["entry_band_triggered"] = True
                    if _ctx.strike_state.get(token, {}).get("trade_open"):
                        _tee.on_tick(token, ltp)
            self.update_oc_ltp(token, ltp)
            if token in self.strike_ltp_labels:
                lbl  = self.strike_ltp_labels[token]
                _buf = getattr(self, "_lbl_pending", None)
                if _buf is not None:
                    _buf[f"sl_{token}"] = (lbl, str(round(ltp, 2)))
                else:
                    self.root.after(
                        0, lambda l=lbl, v=ltp: l.config(text=str(round(v, 2))))

        dde.on_tick_cb = _on_tick_multi
        self._broker_pipelines["multi_dhan"] = {
            "dde": dde, "pipelines": pipeline_list,
        }
        self.log("🟢 Dhan multi-strategy feed started")
