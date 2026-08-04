import threading
import datetime as dt


class TradeExecutionEngine:
    """
    Receives signal from QuantFilterEngine.
    Manages open trades: SL, Target, TSL.
    Paper / Live order placement via Angel REST API.
    Updates GUI.
    """

    def __init__(self, app_ref):
        self.app  = app_ref
        self.lock = threading.Lock()

    # ----------------------------------------------------------
    def on_signal(self, token, ltp, df):
        """Called by QuantFilterEngine when entry conditions met."""
        self.app.open_trade(token, ltp)

    # ----------------------------------------------------------
    def on_tick(self, token, ltp):
        """
        Called every tick for tokens with open positions.
        Manages TSL, SL, Target checks.
        """
        try:
            app = self.app
            st  = app.strike_state.get(token)
            if not st or not st["trade_open"]:
                return

            direction = st.get("direction", "LONG")
            is_short  = direction == "SHORT"

            # ── UI update (FIX 3: all widget updates via root.after — thread safe) ──
            row = app._active_row(token)
            if row is not None:
                if row["status"] == "CLOSED":
                    return
                if row["status"] == "RUNNING":
                    labels = row["labels"]
                    entry  = row["entry_price"]
                    if is_short:
                        pnl = round((entry - ltp) * st["lot_size"], 2)
                    else:
                        pnl = round((ltp - entry) * st["lot_size"], 2)
                    fg_pnl = "#4caf50" if pnl >= 0 else "#ff5252"
                    # Buffer into _pnl_pending — flushed every 150 ms by
                    # _ui_flush to avoid flooding the Tkinter message queue.
                    _pbuf = getattr(app, "_pnl_pending", None)
                    if _pbuf is not None:
                        _pbuf[token] = (labels, ltp, pnl, fg_pnl)
                    else:
                        app.root.after(0, lambda ls=labels, v=ltp, p=pnl, fg=fg_pnl: (
                            ls[5].config(text=str(v)),
                            ls[8].config(text=f"{p:.2f}", fg=fg),
                        ))

            if is_short:
                # ── SHORT trailing stop — mirrors Normal TSL, trailing the
                # stop DOWN as price falls. GExp/VWAP-adaptive trailing are
                # long-only strategy modes and don't apply to a naked short.
                if app.enable_tsl_var.get():
                    if ltp < st["lowest_price"]:
                        st["lowest_price"] = ltp
                        new_sl = st["lowest_price"] + app.tsl_step_var.get()
                        if new_sl < st["sl"]:
                            st["sl"] = new_sl
                            self._update_sl_label(token, st["sl"])

                # ── SL Hit (stop sits ABOVE entry for a short) ───────
                if ltp >= st["sl"]:
                    self._exit_trade(token, ltp, "SL_HIT")
                    return

                # ── Target Hit (target sits BELOW entry for a short) ─
                for tgt in st["targets"]:
                    if ltp <= tgt and tgt not in st["targets_hit"]:
                        st["targets_hit"].append(tgt)
                        self._exit_trade(token, ltp, "TARGET_HIT")
                        return

            else:
                # ── GExp Approach 2 TSL ──────────────────────────────
                if (st.get("gexp_override")
                        and st.get("gexp_method") == "approach2"):
                    gexp_step = st.get("gexp_tsl_step",
                                       app.gexp_tsl_step_var.get())
                    if ltp > st["highest_price"]:
                        st["highest_price"] = ltp
                        new_sl = round(ltp - gexp_step, 2)
                        if new_sl > st["sl"]:
                            st["sl"] = new_sl
                            self._update_sl_label(token, st["sl"])

                # ── Normal TSL ───────────────────────────────────────
                elif app.enable_tsl_var.get():
                    if ltp > st["highest_price"]:
                        st["highest_price"] = ltp
                        new_sl = st["highest_price"] - app.tsl_step_var.get()
                        if new_sl > st["sl"]:
                            st["sl"] = new_sl
                            self._update_sl_label(token, st["sl"])

                # ── VWAP Band Step-up Trailing ───────────────────────
                elif (getattr(app, "candle_sl_mode_var", None)
                        and app.candle_sl_mode_var.get() == "vwap_adaptive"):
                    df_vb = app.ce.get_candles(token) if getattr(app, "ce", None) else None
                    if df_vb is not None and len(df_vb) > 0:
                        import math
                        lv     = df_vb.iloc[-1]
                        vwap   = float(lv.get("vwap",        float("nan")))
                        lower1 = float(lv.get("vwap_lower1", float("nan")))
                        upper1 = float(lv.get("vwap_upper1", float("nan")))
                        if not any(math.isnan(v) for v in [vwap, lower1, upper1]):
                            if ltp >= upper1:
                                new_sl = round(vwap, 2)
                            elif ltp >= vwap:
                                new_sl = round(lower1, 2)
                            else:
                                new_sl = st["sl"]
                            if new_sl > st["sl"]:
                                st["sl"] = new_sl
                                self._update_sl_label(token, st["sl"])

                # ── SL Hit ───────────────────────────────────────────
                if ltp <= st["sl"]:
                    self._exit_trade(token, ltp, "SL_HIT")
                    return

                # ── Target Hit ───────────────────────────────────────
                for tgt in st["targets"]:
                    if ltp >= tgt and tgt not in st["targets_hit"]:
                        st["targets_hit"].append(tgt)
                        self._exit_trade(token, ltp, "TARGET_HIT")
                        return

            # ── Running P&L summary ──────────────────────────────
            self._update_running_pnl()

        except Exception as e:
            print("TradeExecutionEngine.on_tick error:", e)

    # ----------------------------------------------------------
    def _update_sl_label(self, token, sl_val):
        app = self.app
        row = app._active_row(token)
        if row is not None:
            # labels[6] is now a tk.Entry — update via its StringVar
            sl_var = row.get("sl_var")
            if sl_var:
                app.root.after(0, lambda v=sl_val: sl_var.set(str(round(v, 2))))

    # ----------------------------------------------------------
    def _exit_trade(self, token, ltp, reason):
        app = self.app

        # Guard: mark trade closed BEFORE the HTTP call so concurrent ticks
        # that arrive during HTTP latency cannot fire a second SELL.
        with self.lock:
            st = app.strike_state.get(token)
            if not st or not st.get("trade_open"):
                return
            st["trade_open"] = False

        is_short   = st.get("direction") == "SHORT"
        close_side = "BUY" if is_short else "SELL"
        app._place_order_chunked(token, close_side)

        exit_time = dt.datetime.now()

        if token in app.running_positions:
            pos = app.running_positions[token]
            if is_short:
                pnl = round((pos["EntryPrice"] - ltp) * pos["Qty"], 2)
            else:
                pnl = round((ltp - pos["EntryPrice"]) * pos["Qty"], 2)
            app.cumulative_pnl += pnl

            if reason == "SL_HIT":
                if is_short and ltp < pos["EntryPrice"]:
                    reason = "TSL_EXIT"
                elif not is_short and ltp > pos["EntryPrice"]:
                    reason = "TSL_EXIT"

            # Slippage: difference between intended signal price and actual fill
            intended_px = pos.get("IntendedEntryPrice", pos["EntryPrice"])
            slippage    = round(abs(pos["EntryPrice"] - intended_px), 2)

            record = {
                "Date"         : pos["Date"],
                "Token"        : pos["Token"],
                "Symbol"       : pos["Symbol"],
                "SL_Points"    : pos["SL_Points"],
                "Target"       : pos["Target_Points"],
                "EntryTime"    : pos["EntryTime"],
                "EntryPrice"   : pos["EntryPrice"],
                "Qty"          : pos["Qty"],
                "StopNumeric"  : (pos["EntryPrice"] + pos["SL_Points"]) if is_short
                                  else (pos["EntryPrice"] - pos["SL_Points"]),
                "TargetPrice"  : (pos["EntryPrice"] - pos["Target_Points"]) if is_short
                                  else (pos["EntryPrice"] + pos["Target_Points"]),
                "ExitTime"     : exit_time,
                "ExitPrice"    : ltp,
                "P&L"          : pnl,
                "Slippage"     : slippage,
                "EMA"          : pos["EMA"],
                "RSI"          : pos["RSI"],
                "VWAP"         : pos["VWAP"],
                "VWAP_SD1_UP"  : pos["VWAP_SD1_UP"],
                "VWAP_SD1_DOWN": pos["VWAP_SD1_DOWN"],
                "EntryVolume"  : pos["EntryVolume"],
                "Reason"       : reason,
                "Cumulative P&L": app.cumulative_pnl,
            }
            app.completed_trades.append(record)
            del app.running_positions[token]
            app._save_state()

        # Update GUI row
        row_data = app._active_row(token)
        if row_data is not None:
            labels   = row_data["labels"]
            entry    = row_data["entry_price"]
            st       = app.strike_state.get(token)
            lot_size = st["lot_size"] if st else 1
            if is_short:
                safe_pnl = round((entry - ltp) * lot_size, 2)
            else:
                safe_pnl = round((ltp - entry) * lot_size, 2)

            fg   = "#00e676" if reason == "TARGET_HIT" else "#ff5252"
            bg   = "#1f3a1f" if reason == "TARGET_HIT" else "#3a1f1f"
            text = "Closed"

            apply_btn = row_data.get("apply_btn")

            def _disp(widget, orig, is_text=False):
                # These are dark-theme literals applied directly (not via
                # the theme system's bg/fg walk), so in light mode they'd
                # otherwise stick at their dark literal value forever —
                # a near-black row that swallows its own text. Run them
                # through the same inversion live, and re-cache the
                # "original" so a later theme toggle still restores
                # correctly from here.
                attr = f"_theme_orig_{'fg' if is_text else 'bg'}"
                try:
                    setattr(widget, attr, orig)
                except Exception:
                    pass
                if getattr(app, "current_theme", "dark") == "light":
                    try:
                        return app._invert_color(widget, orig, is_text=is_text)
                    except Exception:
                        return orig
                return orig

            def _upd(ls=labels, p=safe_pnl, f=fg, b=bg, t=text,
                     ab=apply_btn, rd=row_data):
                ls[5].config(text=str(ltp))
                ls[8].config(text=str(p), fg=_disp(ls[8], f, is_text=True))
                ls[9].config(text=t)
                # Apply bg to all widgets (Label and Entry both support bg)
                for l in ls:
                    try:
                        l.config(bg=_disp(l, b, is_text=False))
                    except Exception:
                        pass
                # Freeze editable Qty/SL/Target entries on close
                for idx in (3, 6, 7):
                    if len(ls) > idx:
                        try:
                            ls[idx].config(
                                state="disabled",
                                disabledbackground=_disp(ls[idx], b, is_text=False),
                                disabledforeground=_disp(ls[idx], "#888888", is_text=True))
                        except Exception:
                            pass
                # Disable Apply button on close
                if ab:
                    try:
                        ab.config(state="disabled")
                    except Exception:
                        pass
                # Disable Roll and Close buttons — no operations on a closed row
                for _btn in rd.get("roll1_btns", ()):
                    try: _btn.config(state="disabled", bg="#2a2a2a")
                    except Exception: pass
                for _btn in rd.get("roll2_btns", ()):
                    try: _btn.config(state="disabled", bg="#2a2a2a")
                    except Exception: pass
                for _btn in rd.get("close_btns", {}).values():
                    try: _btn.config(state="disabled", bg="#555555")
                    except Exception: pass
                # Move the dimmed row into the Closed section and shrink the panel
                app._move_row_to_closed(rd)
                app._update_trade_panel_height()

            app.root.after(0, _upd)
            # Refresh Running P&L summary and hedge button after _upd has applied the closed state
            app.root.after(100, self._update_running_pnl)
            app.root.after(150, app.update_hedge_btn)
            row_data["status"] = "CLOSED"
            # Free the token so a future entry starts a fresh live row.
            app._active_row_id.pop(token, None)

        # FIX 3b: status_var.set also needs root.after when called from background thread
        app.root.after(0, lambda: app.status_var.set(f"{token} {reason} @ {ltp}"))

    # ----------------------------------------------------------
    def _update_running_pnl(self):
        app   = self.app
        app.enforce_risk_limits()
        total = 0
        for _rid, row in app.trade_rows.items():
            if row["status"] == "RUNNING":
                try:
                    total += float(row["labels"][8].cget("text"))
                except Exception:
                    pass
        color = "#4caf50" if total >= 0 else "#ff5252"
        app.root.after(0, lambda: app.pnl_summary_label.config(
            text=f"Running P&L: {round(total, 2)}", fg=color))

        # Risk status label (total PnL vs active limit + HALTED flag).
        # Show when master "Risk Limits" is ON OR any individual limit is checked.
        if getattr(app, "risk_status_label", None) is not None:
            any_limit_on = (app.risk_max_loss_on_var.get() or
                            app.risk_profit_on_var.get() or
                            app.risk_max_trades_on_var.get())
            master_on = app.risk_enabled_var.get()
            if master_on or any_limit_on:
                tot = app._compute_total_pnl()
                if app._risk_halted:
                    rtxt, rfg = f"HALTED: {app._risk_halt_reason}", "#ff5252"
                else:
                    parts = [f"P&L {tot}"]
                    if app.risk_max_loss_on_var.get():
                        loss_limit = abs(app.risk_max_loss_var.get())
                        trail_on   = getattr(app, "risk_trail_loss_var", None)
                        peak       = getattr(app, "_risk_peak_pnl", 0.0)
                        if trail_on and trail_on.get():
                            floor = round(peak - loss_limit, 2) if peak > 0 else -loss_limit
                            parts.append(f"trail-floor {floor} (peak {peak:.0f})")
                        else:
                            parts.append(f"loss-lim -{loss_limit}")
                    if app.risk_profit_on_var.get():
                        parts.append(f"tgt {abs(app.risk_profit_target_var.get())}")
                    if app.risk_max_trades_on_var.get():
                        parts.append(f"trades {app.trades_today}/{app.risk_max_trades_var.get()}")
                    rtxt, rfg = " | ".join(parts), "#aaaaaa"
            else:
                rtxt, rfg = "", "#888888"
            app.root.after(0, lambda t=rtxt, f=rfg: app.risk_status_label.config(text=t, fg=f))
