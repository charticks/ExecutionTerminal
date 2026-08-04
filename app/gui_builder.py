import os
import colorsys
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, font
import datetime as dt
import time

from widgets import PlaceholderEntry
from tkcalendar import DateEntry


class GUIBuilderMixin:
    """GUI layout and helper methods for Strategy2LiveBotApp."""

    _TICKER_SYM_DISPLAY = {
        "NIFTY":     ("NIFTY 50",    "INDEX"),
        "BANKNIFTY": ("NIFTY BANK",  "INDEX"),
        "SENSEX":    ("SENSEX",      "INDEX"),
        "CRUDEOIL":  ("CRUDEOIL",    "FUTURES"),
    }

    def _on_start_stop_click(self):
        if self.is_running:
            self.stop_bot()
        else:
            self.start_bot()
        self._refresh_start_stop_btn()

    def _refresh_start_stop_btn(self):
        if not hasattr(self, "start_stop_btn"):
            return
        if self.is_running:
            self.start_stop_btn.config(text="Stop Bot", bg="#b71c1c")
        else:
            self.start_stop_btn.config(text="Start Bot", bg="#00c853")

    def toggle_theme(self):
        """Flip dark/light and repaint the whole widget tree."""
        self.current_theme = "light" if getattr(self, "current_theme", "light") == "dark" else "dark"
        self.theme_toggle_btn.config(text="☀️" if self.current_theme == "light" else "🌙")
        self.apply_theme()

    def _invert_color(self, widget, color, is_text=False):
        """Light theme is strict grayscale by design — no accent hue (green/
        red/gold/cyan etc.) survives into it, only black/grey/white. Text
        always goes to a firm dark gray (not pure black, for a slightly
        softer look) regardless of its original hue; backgrounds/accents
        get a plain lightness flip with saturation forced to 0, so every
        color collapses to its grayscale equivalent."""
        try:
            r16, g16, b16 = widget.winfo_rgb(color)
            r, g, b = r16 / 65535, g16 / 65535, b16 / 65535
            h, l, s = colorsys.rgb_to_hls(r, g, b)
            l2 = 0.15 if is_text else 1.0 - l
            r2, g2, b2 = colorsys.hls_to_rgb(h, l2, 0.0)
            return "#%02x%02x%02x" % (round(r2 * 255), round(g2 * 255), round(b2 * 255))
        except Exception:
            return color

    def _invert_widget_tree(self, root_widget):
        """Recursively repaint bg/fg for root_widget and all its descendants,
        always computing from each widget's ORIGINAL (dark-theme) color —
        cached on first visit as widget._theme_orig_bg/fg — rather than from
        whatever color it currently displays. This makes repeated toggling
        perfectly stable (no drift/collapse-to-gray from repeated rounding),
        since "go dark" is just "restore the cached original" and "go light"
        is always computed from that same fixed original."""
        text_opts = {"fg", "activeforeground", "disabledforeground"}
        all_opts  = ("bg", "fg", "selectcolor", "activebackground",
                     "activeforeground", "disabledforeground",
                     "highlightbackground")

        def repaint(widget):
            # _theme_no_invert: keep ALL original colors in both themes (e.g. Square Off All).
            if getattr(widget, "_theme_no_invert", False):
                try:
                    children = widget.winfo_children()
                except Exception:
                    children = []
                for child in children:
                    try:
                        repaint(child)
                    except Exception:
                        pass
                return
            # _theme_keep_fg: invert bg normally but preserve original fg color.
            # Used for accent indicators (e.g. red ▲ unhedged warning) that must
            # stay their accent color even in the grayscale light theme.
            keep_fg = getattr(widget, "_theme_keep_fg", False)
            for opt in all_opts:
                try:
                    cur = widget.cget(opt)
                except Exception:
                    continue
                if not cur:
                    continue
                attr = f"_theme_orig_{opt}"
                if not hasattr(widget, attr):
                    setattr(widget, attr, cur)
                orig = getattr(widget, attr)
                if self.current_theme == "light":
                    if keep_fg and opt in text_opts:
                        new_color = orig   # preserve original fg color (e.g. red)
                    else:
                        new_color = self._invert_color(widget, orig, is_text=(opt in text_opts))
                else:
                    new_color = orig
                try:
                    widget.config(**{opt: new_color})
                except Exception:
                    pass
            try:
                children = widget.winfo_children()
            except Exception:
                children = []
            for child in children:
                # Per-child, not per-loop: one widget raising mid-recursion
                # must not abort painting of its remaining siblings, or
                # everything after it in the tree silently keeps the old
                # (invisible-on-the-new-background) colors.
                try:
                    repaint(child)
                except Exception:
                    pass
        repaint(root_widget)

    def _apply_ttk_theme_colors(self):
        """ttk widgets (Combobox, the tkcalendar DateEntry, Progressbar...)
        don't expose plain bg/fg options — Tk's bg/fg-inversion walk in
        _invert_widget_tree can't touch them (cget('bg')/cget('fg') raise
        and get skipped), so they always render with their native light
        field/white background — invisible in light mode against light
        theme text, and equally a mismatched white box in dark mode against
        the dark text it then needs to show. Style them explicitly instead,
        once per theme toggle, since style changes apply instantly to every
        existing and future widget using that style."""
        if not hasattr(self, "_ttk_style"):
            self._ttk_style = ttk.Style()
        style = self._ttk_style
        if self.current_theme == "light":
            field_bg, fg, select_bg = "#f0f0f0", "#000000", "#c0c0c0"
        else:
            field_bg, fg, select_bg = "#1e1e1e", "#ffffff", "#37474f"
        for style_name in ("TCombobox", "TSpinbox", "TEntry"):
            style.configure(style_name, fieldbackground=field_bg,
                             foreground=fg, background=field_bg,
                             selectbackground=select_bg, selectforeground=fg)
            style.map(style_name,
                      fieldbackground=[("readonly", field_bg), ("disabled", field_bg)],
                      foreground=[("readonly", fg), ("disabled", fg)])

    def apply_theme(self):
        """Repaint the whole widget tree (and the marquee canvas text) by
        inverting every bg/fg's lightness — see _invert_color for why this
        keeps contrast (and therefore readability) intact in light mode."""
        self._invert_widget_tree(self.root)
        self._apply_ttk_theme_colors()
        if getattr(self, "ticker_canvas", None) is not None:
            try:
                if not hasattr(self, "_ticker_text_orig_fill"):
                    self._ticker_text_orig_fill = self.ticker_canvas.itemcget(
                        self._ticker_text_id, "fill")
                new_fill = (self._invert_color(self.ticker_canvas,
                                               self._ticker_text_orig_fill, is_text=True)
                           if self.current_theme == "light" else self._ticker_text_orig_fill)
                self.ticker_canvas.itemconfig(self._ticker_text_id, fill=new_fill)
            except Exception:
                pass

    def _theme_repaint_subtree(self, widget):
        """Call after building dynamic content (option chain rows, trade
        rows) that's always created with dark-theme literal colors — if the
        app is currently in light mode, invert this subtree to match."""
        if getattr(self, "current_theme", "dark") == "light":
            self._invert_widget_tree(widget)

    def _build_market_ticker(self, parent):
        """Horizontally-scrolling Market Ticker marquee — packed at the very
        top of the app (above the scrollable canvas) so it's always visible."""
        self.ticker_canvas = tk.Canvas(parent, bg="#2e3b4e", height=32,
                                       highlightthickness=0)
        self.ticker_canvas.pack(side="top", fill="x")
        self._ticker_text_id = self.ticker_canvas.create_text(
            0, 16, text="Market Ticker", anchor="w",
            fill="#ffffff", font=("Segoe UI", 11, "bold"))
        self._ticker_speed = 2
        self._ticker_after_id = None
        self._ticker_tick()

    def _refresh_ticker_text(self):
        """Rebuild the marquee string from the (unpacked) market LTP labels.
        Called from the 150ms _ui_flush loop — cheap text-only update."""
        parts = []
        for sym, (disp_name, disp_tag) in self._TICKER_SYM_DISPLAY.items():
            ltp_lbl = self.market_ltp_labels.get(sym)
            if not ltp_lbl:
                continue
            pts_lbl, pct_lbl, arrow_lbl = self.market_change_labels.get(sym, (None, None, None))
            ltp = ltp_lbl.cget("text")
            pts = pts_lbl.cget("text") if pts_lbl else ""
            pct = pct_lbl.cget("text") if pct_lbl else ""
            arrow = arrow_lbl.cget("text") if arrow_lbl else ""
            parts.append(f"{disp_name}  {ltp}  {pts} {pct} {arrow}")
        text = "      |      ".join(parts) if parts else "Market Ticker — waiting for data..."
        self.ticker_canvas.itemconfig(self._ticker_text_id, text=text)

    def _ticker_tick(self):
        """Animate the marquee one frame left, wrapping when it scrolls off."""
        try:
            self.ticker_canvas.move(self._ticker_text_id, -self._ticker_speed, 0)
            bbox = self.ticker_canvas.bbox(self._ticker_text_id)
            if bbox and bbox[2] < 0:
                canvas_w = self.ticker_canvas.winfo_width()
                self.ticker_canvas.coords(self._ticker_text_id, canvas_w, 16)
        except Exception:
            pass
        self._ticker_after_id = self.root.after(40, self._ticker_tick)

    def _build_config_panel(self, parent):
        """Collapsible Config panel — Order Params / Hedges / Risk Controls /
        Profile. Additive: does not move or remove any existing scattered
        controls elsewhere in the app."""
        outer = tk.Frame(parent, bg="#23303f", padx=10, pady=8,
                         highlightbackground="#37474f", highlightthickness=1)
        outer.pack(pady=6, fill="x")

        header = tk.Frame(outer, bg="#23303f")
        header.pack(fill="x")
        self._config_caret_btn = tk.Button(
            header, text="▸ Config", bg="#23303f", fg="#00bcd4",
            font=("Segoe UI", 10, "bold"), bd=0, relief="flat",
            activebackground="#23303f", cursor="hand2",
            command=self._toggle_config_panel)
        self._config_caret_btn.pack(side="left")

        body = tk.Frame(outer, bg="#23303f")
        # Collapsed by default — user expands when needed
        self.config_body_frame = body

        # ── PROFILE ────────────────────────────────────────────
        prof_row = tk.Frame(body, bg="#23303f")
        prof_row.pack(fill="x", pady=(0, 8))
        tk.Label(prof_row, text="PROFILE", bg="#23303f", fg="#888888",
                 font=("Segoe UI", 8, "bold")).pack(side="left")
        ttk.Combobox(prof_row, textvariable=self.cfg_profile_day_var,
                     values=["Mon", "Tue", "Wed", "Thu", "Fri", "Other"],
                     state="readonly", width=8).pack(side="left", padx=8)
        tk.Label(prof_row, text="Index:", bg="#23303f", fg="#888888",
                 font=("Segoe UI", 8)).pack(side="left")
        ttk.Combobox(prof_row, textvariable=self.index_var,
                     values=["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL"],
                     state="readonly", width=10).pack(side="left", padx=(4, 8))
        tk.Button(prof_row, text="Load", bg="#1565c0", fg="white",
                  font=("Segoe UI", 9, "bold"),
                  command=self.load_profile_config).pack(side="left", padx=2)
        tk.Button(prof_row, text="Save", bg="#2e7d32", fg="white",
                  font=("Segoe UI", 9, "bold"),
                  command=self.save_profile_config).pack(side="left", padx=2)
        self.master_btn = tk.Button(
            prof_row, text="🔄 Reload Master Data",
            bg="#37474f", fg="white",
            command=self.load_master_threaded)
        self.master_btn.pack(side="left", padx=(16, 0))

        cols = tk.Frame(body, bg="#23303f")
        cols.pack(fill="x")

        # ── ORDER PARAMS ───────────────────────────────────────
        op = tk.Frame(cols, bg="#23303f", padx=8)
        op.pack(side="left", anchor="n")
        tk.Label(op, text="ORDER PARAMS", bg="#23303f", fg="#888888",
                 font=("Segoe UI", 8, "bold")).grid(row=0, column=0, columnspan=4, sticky="w")
        op_fields = [
            ("MaxQty/Order", self.cfg_max_qty_per_order_var),
            ("MaxPrice",     self.cfg_max_price_var),
            ("Wait",         self.cfg_wait_var),
        ]
        for i, (lbl, var) in enumerate(op_fields):
            tk.Label(op, text=lbl, bg="#23303f", fg="white",
                     font=("Segoe UI", 8)).grid(row=1, column=i, sticky="w", padx=2)
        for i, (lbl, var) in enumerate(op_fields):
            tk.Entry(op, textvariable=var, width=8,
                     bg="#1e1e1e", fg="white").grid(row=2, column=i, padx=2, pady=(0, 6))

        op_fields2 = [
            ("MaxAdj",              self.cfg_max_adj_var),
            ("DefaultQty",          self.cfg_default_qty_var),
            ("Entry Price\nOffset(%)", self.cfg_limit_order_pct_var),
        ]
        for i, (lbl, var) in enumerate(op_fields2):
            tk.Label(op, text=lbl, bg="#23303f", fg="white",
                     font=("Segoe UI", 8)).grid(row=3, column=i, sticky="w", padx=2)
        for i, (lbl, var) in enumerate(op_fields2):
            tk.Entry(op, textvariable=var, width=8,
                     bg="#1e1e1e", fg="white").grid(row=4, column=i, padx=2, pady=(0, 6))

        ot_row = tk.Frame(op, bg="#23303f")
        ot_row.grid(row=5, column=0, columnspan=4, sticky="w", pady=(2, 0))
        tk.Radiobutton(ot_row, text="Market", variable=self.cfg_order_type_var,
                       value="MARKET", bg="#23303f", fg="white",
                       selectcolor="#23303f").pack(side="left")
        tk.Radiobutton(ot_row, text="Limit", variable=self.cfg_order_type_var,
                       value="LIMIT", bg="#23303f", fg="#ffd740",
                       selectcolor="#23303f").pack(side="left", padx=(6, 0))
        tk.Label(ot_row, text="SL pts", bg="#23303f", fg="#ff5252",
                 font=("Segoe UI", 8)).pack(side="left", padx=(10, 2))
        tk.Entry(ot_row, textvariable=self.cfg_sl_pts_var, width=6,
                 bg="#1e1e1e", fg="#ff5252").pack(side="left")
        tk.Label(ot_row, text="Target pts", bg="#23303f", fg="#00e676",
                 font=("Segoe UI", 8)).pack(side="left", padx=(10, 2))
        tk.Entry(ot_row, textvariable=self.cfg_target_pts_var, width=6,
                 bg="#1e1e1e", fg="#00e676").pack(side="left")

        # ── HEDGES ─────────────────────────────────────────────
        hg = tk.Frame(cols, bg="#23303f", padx=16)
        hg.pack(side="left", anchor="n")
        tk.Label(hg, text="HEDGES", bg="#23303f", fg="#888888",
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        hg_row = tk.Frame(hg, bg="#23303f")
        hg_row.pack(anchor="w", pady=(2, 0))
        tk.Checkbutton(hg_row, text="Enable Add", variable=self.cfg_hedge_enable_var,
                       bg="#23303f", fg="white", selectcolor="#23303f").pack(side="left")
        self.hedge_offset_combo = ttk.Combobox(
            hg_row, textvariable=self.cfg_hedge_offset_var,
            state="readonly", width=6)
        self.hedge_offset_combo.pack(side="left", padx=6)
        tk.Checkbutton(hg_row, text="Retry", variable=self.cfg_hedge_retry_var,
                       bg="#23303f", fg="white", selectcolor="#23303f").pack(side="left", padx=(6, 0))
        self._update_hedge_offset_choices()


        # Entry Mode + Trade Mode share this column's remaining space —
        # both moved here from their old standalone panels (see item 5/7).
        em_row = tk.Frame(hg, bg="#23303f")
        em_row.pack(anchor="w", pady=(10, 0))
        tk.Label(em_row, text="Entry Mode:", bg="#23303f", fg="#00e676",
                 font=("Segoe UI", 8, "bold")).pack(side="left")
        tk.Radiobutton(em_row, text="Auto",
                       variable=self.trade_exec_mode_var, value="Auto",
                       bg="#23303f", fg="#00e676", selectcolor="#23303f",
                       font=("Segoe UI", 8, "bold")).pack(side="left", padx=(6, 0))
        tk.Radiobutton(em_row, text="Manual",
                       variable=self.trade_exec_mode_var, value="Manual",
                       bg="#23303f", fg="#ffd740", selectcolor="#23303f",
                       font=("Segoe UI", 8, "bold")).pack(side="left", padx=(6, 0))

        tm_row = tk.Frame(hg, bg="#23303f")
        tm_row.pack(anchor="w", pady=(4, 0))
        tk.Label(tm_row, text="Trade Mode:", bg="#23303f", fg="#00e676",
                 font=("Segoe UI", 8, "bold")).pack(side="left")
        for txt, val, fg_c in [("Paper", "Paper", "white"), ("Live", "Live", "#ff5252")]:
            tk.Radiobutton(tm_row, text=txt, variable=self.trade_mode_var, value=val,
                           bg="#23303f", fg=fg_c, selectcolor="#23303f",
                           font=("Segoe UI", 8, "bold"),
                           command=self._on_broker_toggle).pack(side="left", padx=(6, 0))

        # ── RISK CONTROLS ──────────────────────────────────────
        rc = tk.Frame(cols, bg="#23303f", padx=16)
        rc.pack(side="left", anchor="n")
        tk.Label(rc, text="RISK CONTROLS", bg="#23303f", fg="#888888",
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        rc_row1 = tk.Frame(rc, bg="#23303f")
        rc_row1.pack(anchor="w", pady=(2, 0))
        tk.Label(rc_row1, text="Max Loss", bg="#23303f", fg="white",
                 font=("Segoe UI", 8)).pack(side="left")
        tk.Entry(rc_row1, textvariable=self.cfg_max_loss_var, width=8,
                 bg="#1e1e1e", fg="white").pack(side="left", padx=4)
        rc_row2 = tk.Frame(rc, bg="#23303f")
        rc_row2.pack(anchor="w", pady=(4, 0))
        tk.Label(rc_row2, text="Max Order", bg="#23303f", fg="white",
                 font=("Segoe UI", 8)).pack(side="left")
        tk.Entry(rc_row2, textvariable=self.cfg_max_order_var, width=8,
                 bg="#1e1e1e", fg="white").pack(side="left", padx=4)

    def _toggle_log_panel(self):
        if self.log_body_frame.winfo_ismapped():
            self.log_body_frame.pack_forget()
            self._log_caret_btn.config(text="▸ Logs")
        else:
            self.log_body_frame.pack(fill="x", pady=(8, 0))
            self._log_caret_btn.config(text="▾ Logs")

    def _toggle_strategy_config_panel(self):
        if self.strategy_config_body_frame.winfo_ismapped():
            self.strategy_config_body_frame.pack_forget()
            self._strat_cfg_caret_btn.config(text="▸ Strategy Configuration")
        else:
            self.strategy_config_body_frame.pack(fill="x", pady=(8, 0))
            self._strat_cfg_caret_btn.config(text="▾ Strategy Configuration")

    def _toggle_config_panel(self):
        if self.config_body_frame.winfo_ismapped():
            self.config_body_frame.pack_forget()
            self._config_caret_btn.config(text="▸ Config")
        else:
            self.config_body_frame.pack(fill="x", pady=(8, 0))
            self._config_caret_btn.config(text="▾ Config")

    def _refresh_roll_columns(self):
        """Show/hide Roll 1 and Roll 2 sub-frames in every active trade row and
        update the matching header labels so they always stay in sync."""
        r1_on = getattr(self, "roll_pos1_var", None) and self.roll_pos1_var.get()
        r2_on = getattr(self, "roll_pos2_var", None) and self.roll_pos2_var.get()

        # ── Header labels (created in build_gui_panels) ──────────────────────
        _hdr1 = getattr(self, "_roll_hdr_lbl1", None)
        _hdr2 = getattr(self, "_roll_hdr_lbl2", None)
        _hdr0 = getattr(self, "_roll_hdr_lbl0", None)  # "Roll" fallback
        if _hdr1:
            try:
                if r1_on:
                    _hdr1.pack(side="left", fill="both", expand=True, padx=(0, 2))
                else:
                    _hdr1.pack_forget()
            except Exception:
                pass
        if _hdr2:
            try:
                if r2_on:
                    _hdr2.pack(side="left", fill="both", expand=True)
                else:
                    _hdr2.pack_forget()
            except Exception:
                pass
        if _hdr0:
            try:
                if not r1_on and not r2_on:
                    _hdr0.pack(fill="both", expand=True)
                else:
                    _hdr0.pack_forget()
            except Exception:
                pass

        # ── Trade row sub-frames ──────────────────────────────────────────────
        for _rid, row_data in self.trade_rows.items():
            if row_data.get("status") != "RUNNING":
                continue
            r1_sub = row_data.get("roll1_sub")
            r2_sub = row_data.get("roll2_sub")
            if r1_sub:
                try:
                    if r1_on:
                        r1_sub.pack(side="left", fill="both", expand=True, padx=(0, 2))
                    else:
                        r1_sub.pack_forget()
                except Exception:
                    pass
            if r2_sub:
                try:
                    if r2_on:
                        r2_sub.pack(side="left", fill="both", expand=True)
                    else:
                        r2_sub.pack_forget()
                except Exception:
                    pass

    def update_hedge_btn(self):
        """Refresh Exit Hedges button: green+enabled when hedge legs are open,
        grey+disabled once all hedges are closed."""
        running_rows = [r for r in self.trade_rows.values() if r.get("status") == "RUNNING"]
        has_hedges = any(
            self.strike_state.get(r["token"], {}).get("hedge_group_id")
            for r in running_rows
        )
        btn = getattr(self, "_exit_hedges_btn", None)
        if btn:
            if has_hedges:
                btn.config(bg="#1b5e20", fg="white", state="normal")
            else:
                btn.config(bg="#424242", fg="#aaaaaa", state="disabled")

    def _close_all_hedges(self):
        """Close all open trade rows that are hedge legs (have hedge_group_id)."""
        for row_data in list(self.trade_rows.values()):
            if row_data.get("status") != "RUNNING":
                continue
            token = row_data["token"]
            st = self.strike_state.get(token, {})
            if st.get("hedge_group_id") and st.get("trade_open"):
                self.manual_close_trade(token)
        self.root.after(200, self.update_hedge_btn)

    def _refresh_live_prices(self):
        """Force a REST LTP snapshot + reconnect pre-bot WS to fix stale prices."""
        import threading as _thr
        # REST snapshot (works for Angel and Kotak sessions)
        _thr.Thread(target=self.fetch_oc_ltps_rest,
                    daemon=True, name="ltp-refresh").start()
        if getattr(self, "kotak_logged_in", False):
            _thr.Thread(target=self._fetch_oc_ltps_kotak_rest,
                        daemon=True, name="kotak-ltp-refresh").start()
        # Re-subscribe pre-bot WS if the feed engine exists
        engine = getattr(self, "_pre_bot_oce", None) or getattr(self, "active_feed_engine", None)
        if engine is not None:
            kotak_map = getattr(engine, "kotak_to_angel", None) or getattr(engine, "_kotak_symbols", None)
            if kotak_map is not None:
                syms = list(kotak_map.keys()) if isinstance(kotak_map, dict) else kotak_map
                if syms:
                    try:
                        engine.resubscribe(engine.kotak_to_angel if isinstance(kotak_map, dict) else {s: s for s in syms})
                    except AttributeError:
                        pass  # Dhan/Angel engines don't need manual resubscribe
        self.log("🔄 Prices refreshed")

    def _update_hedge_offset_choices(self, *_):
        """Hedge offset choices depend on the selected index — NIFTY steps by
        50 up to 500; BANKNIFTY/SENSEX step by 100 up to 1000."""
        index = self.index_var.get().upper()
        if index == "NIFTY":
            choices = list(range(50, 501, 50))
        else:
            choices = list(range(100, 1001, 100))
        self.hedge_offset_combo.config(values=choices)
        if self.cfg_hedge_offset_var.get() not in choices:
            self.cfg_hedge_offset_var.set(choices[0])

    def save_profile_config(self):
        import json
        day   = self.cfg_profile_day_var.get()
        index = self.index_var.get().upper()
        base_dir = os.path.dirname(os.path.abspath(__file__))
        folder = os.path.join(base_dir, "data_cache", "profiles")
        os.makedirs(folder, exist_ok=True)
        data = {
            "max_qty_per_order": self.cfg_max_qty_per_order_var.get(),
            "max_price":         self.cfg_max_price_var.get(),
            "wait":              self.cfg_wait_var.get(),
            "max_adj":           self.cfg_max_adj_var.get(),
            "default_qty":       self.cfg_default_qty_var.get(),
            "limit_order_pct":   self.cfg_limit_order_pct_var.get(),
            "order_type":        self.cfg_order_type_var.get(),
            "sl_pts":            self.cfg_sl_pts_var.get(),
            "target_pts":        self.cfg_target_pts_var.get(),
            "hedge_enable":      self.cfg_hedge_enable_var.get(),
            "hedge_offset":      self.cfg_hedge_offset_var.get(),
            "hedge_retry":       self.cfg_hedge_retry_var.get(),
            "exit_hedges":       self.cfg_exit_hedges_var.get(),
            "max_loss":          self.cfg_max_loss_var.get(),
            "max_order":         self.cfg_max_order_var.get(),
        }
        filename = f"{day}_{index}.json"
        with open(os.path.join(folder, filename), "w") as fh:
            json.dump(data, fh, indent=2)
        self.status_var.set(f"Config profile '{day} {index}' saved ✅")

    def load_profile_config(self):
        import json
        day   = self.cfg_profile_day_var.get()
        index = self.index_var.get().upper()
        base_dir = os.path.dirname(os.path.abspath(__file__))
        filename = f"{day}_{index}.json"
        path = os.path.join(base_dir, "data_cache", "profiles", filename)
        # Fallback: try old single-key file name for backward compat
        if not os.path.exists(path):
            old_path = os.path.join(base_dir, "data_cache", "profiles", f"{day}.json")
            if os.path.exists(old_path):
                path = old_path
            else:
                messagebox.showinfo("Config Profile",
                                    f"No saved profile for '{day} {index}'.")
                return
        with open(path) as fh:
            data = json.load(fh)
        self.cfg_max_qty_per_order_var.set(data.get("max_qty_per_order", 0))
        self.cfg_max_price_var.set(data.get("max_price", 0.0))
        self.cfg_wait_var.set(data.get("wait", 0))
        self.cfg_max_adj_var.set(data.get("max_adj", 0))
        self.cfg_default_qty_var.set(data.get("default_qty", 1))
        self.cfg_limit_order_pct_var.set(data.get("limit_order_pct", 1.0))
        self.cfg_order_type_var.set(data.get("order_type", "MARKET"))
        self.cfg_sl_pts_var.set(data.get("sl_pts", 50.0))
        self.cfg_target_pts_var.set(data.get("target_pts", 100.0))
        self.cfg_hedge_enable_var.set(data.get("hedge_enable", False))
        self.cfg_hedge_offset_var.set(data.get("hedge_offset", 100))
        self.cfg_hedge_retry_var.set(data.get("hedge_retry", False))
        self.cfg_exit_hedges_var.set(data.get("exit_hedges", True))
        # Hedge offset choices depend on the selected Index (see
        # _update_hedge_offset_choices) — revalidate now so an offset saved
        # under a different index doesn't silently stick as an invalid value.
        self._update_hedge_offset_choices()
        # cfg_max_loss_var/cfg_max_order_var ARE risk_max_loss_var/
        # risk_max_trades_var (shared Variables — see bot_app.py), so this
        # sets the real Active-Trades risk limits directly.
        self.cfg_max_loss_var.set(data.get("max_loss", 5000.0))
        self.cfg_max_order_var.set(data.get("max_order", 10))
        # DefaultQty governs the Option Chain lots spinbox used by every
        # new strike added from the chain (manual punch + add_oc_strike).
        self.oc_lots_var.set(max(1, self.cfg_default_qty_var.get()))
        self.status_var.set(f"Config profile '{day} {index}' loaded ✅")

    def build_gui(self):
        self.root.configure(bg="#1e1e1e")

        # ── UI update throttle buffers ────────────────────────────
        # All per-tick label/log/PnL updates write into these dicts
        # instead of calling root.after(0, ...) directly.  A single
        # 150 ms flush loop drains them on the main thread, capping
        # Tkinter message-queue depth regardless of tick frequency.
        self._lbl_pending = {}   # key → (widget, text)   label text updates
        self._log_pending = []   # list[str]               log lines
        self._pnl_pending = {}   # token → (labels, ltp, pnl, fg)

        def _ui_flush():
            # 1. Label updates (LTPs for OC, strike panel, market)
            for _k, (lbl, txt) in list(self._lbl_pending.items()):
                try:
                    lbl.config(text=txt)
                except Exception:
                    pass
            self._lbl_pending.clear()

            # 2. Log messages (batched into one insert)
            if self._log_pending:
                batch = "".join(self._log_pending)
                self._log_pending.clear()
                try:
                    self.log_box.insert("end", batch)
                    self.log_box.see("end")
                    # Cap log at 500 lines to prevent unbounded memory growth
                    end_line = int(self.log_box.index("end-1c").split(".")[0])
                    if end_line > 500:
                        self.log_box.delete("1.0", f"{end_line - 500}.0")
                except Exception:
                    pass

            # 3. Trade P&L updates
            had_pnl = bool(self._pnl_pending)
            for _tok, (ls, ltp_v, pnl_v, fg) in list(self._pnl_pending.items()):
                try:
                    ls[5].config(text=str(ltp_v))
                    ls[8].config(text=f"{pnl_v:.2f}", fg=fg)
                except Exception:
                    pass
            self._pnl_pending.clear()

            # 3b. Recompute Running P&L summary whenever any trade row was updated.
            # This keeps the header label in sync even in pre-bot / manual mode
            # where the TEE is not running _update_running_pnl() on each tick.
            # Also enforces risk limits in all modes (not just bot/TEE mode).
            if had_pnl:
                try:
                    total = 0.0
                    for _r, _rd in self.trade_rows.items():
                        if _rd.get("status") == "RUNNING":
                            try:
                                total += float(_rd["labels"][8].cget("text"))
                            except Exception:
                                pass
                    color = "#4caf50" if total >= 0 else "#ff5252"
                    self.pnl_summary_label.config(
                        text=f"Running P&L: {round(total, 2)}", fg=color)
                except Exception:
                    pass
                try:
                    # Enforce risk limits in pre-bot/manual mode too (TEE may not exist).
                    if not getattr(self, "tee", None):
                        self.enforce_risk_limits()
                except Exception:
                    pass

            # 4. Periodic risk-limit check every ~2 s in pre-bot/manual mode.
            # When ticks are stale (no LTP updates), had_pnl stays False and the
            # risk check above never fires — this fallback ensures limits still
            # trigger even when P&L rows haven't refreshed recently.
            _flush_tick = getattr(self, "_ui_flush_tick", 0) + 1
            self._ui_flush_tick = _flush_tick
            if _flush_tick % 14 == 0:   # 14 × 150 ms ≈ 2.1 s
                try:
                    if not getattr(self, "tee", None):
                        self.enforce_risk_limits()
                except Exception:
                    pass

            # 5. Market Ticker marquee text (content only — position is
            # animated separately by _ticker_tick)
            try:
                self._refresh_ticker_text()
            except Exception:
                pass

            self.root.after(150, _ui_flush)

        self.root.after(150, _ui_flush)

        # ── Market Ticker marquee — always visible strip at the very top ──
        self._build_market_ticker(self.root)

        # Main canvas with both vertical and horizontal scrollbars
        canvas    = tk.Canvas(self.root, bg="#1e1e1e", highlightthickness=0)
        self.main_canvas = canvas
        v_scroll  = tk.Scrollbar(self.root, orient="vertical",
                                 command=canvas.yview)
        h_scroll  = tk.Scrollbar(self.root, orient="horizontal",
                                 command=canvas.xview)
        self.scroll_frame = tk.Frame(canvas, bg="#1e1e1e")

        self.scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(
                scrollregion=canvas.bbox("all")))

        self._main_canvas_window = canvas.create_window(
            (0, 0), window=self.scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=v_scroll.set,
                         xscrollcommand=h_scroll.set)
        # Pack order: scrollbars first so canvas fills remaining space
        v_scroll.pack(side="right", fill="y")
        h_scroll.pack(side="bottom", fill="x")
        canvas.pack(fill="both", expand=True)

        # Stretch scroll_frame to the canvas width so panels reflow instead
        # of needing a horizontal scrollbar on narrower screens.
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfig(self._main_canvas_window, width=e.width))

        # ── Smart mousewheel dispatcher ──────────────────────────
        # Tracks which scrollable canvas the mouse is currently over.
        # When hovering a sub-panel (strike / trade), wheel scrolls that
        # panel only; everywhere else scrolls the main canvas.
        _scroll_target = [canvas]   # mutable single-element list

        def _set_target(c):
            return (lambda e: _scroll_target.__setitem__(0, c))

        def _reset_target(e):
            _scroll_target[0] = canvas

        def _bind_panel_scroll(widget, panel_canvas):
            """Redirect mousewheel to panel_canvas while mouse is inside widget."""
            widget.bind("<Enter>", _set_target(panel_canvas), add="+")
            widget.bind("<Leave>", _reset_target,              add="+")

        self._bind_panel_scroll = _bind_panel_scroll   # store for later use

        def _mw(e):
            _scroll_target[0].yview_scroll(int(-1 * (e.delta / 120)), "units")
        def _su(e):
            _scroll_target[0].yview_scroll(-1, "units")
        def _sd(e):
            _scroll_target[0].yview_scroll(1, "units")

        canvas.bind_all("<MouseWheel>", _mw)
        canvas.bind_all("<Button-4>",   _su)
        canvas.bind_all("<Button-5>",   _sd)

        main = self.scroll_frame

        content_frame = tk.Frame(main, bg="#1e1e1e")
        content_frame.pack(fill="both", expand=True)

        left_frame = tk.Frame(content_frame, bg="#1e1e1e")
        left_frame.pack(side="left", fill="both", expand=True, padx=20)

        right_frame = tk.Frame(content_frame, bg="#1e1e1e")
        right_frame.pack(side="right", fill="both", expand=True, padx=20)
        # NOTE: deliberately NOT calling pack_propagate(False) here. With it
        # disabled (and no explicit height set), right_frame's height freezes
        # at its near-zero size *before* any children are packed, so
        # content_frame's height ends up driven entirely by left_frame —
        # right_frame (Strategy Configuration + Option Chain) then gets
        # squeezed/clipped to whatever height left_frame happens to have,
        # independent of how much content it actually needs. Letting it
        # auto-size (propagate stays True) means right_frame is always as
        # tall as its own content, so Config (left) and Strategy
        # Configuration (right) can expand/collapse independently without
        # clipping each other. Individual widgets inside already constrain
        # their own widths (fixed-width comboboxes/entries), so right_frame's
        # natural width stays reasonable without an explicit cap.
        # right_frame now holds just Strategy Configuration (collapsible,
        # natural height) above Option Chain (fills the rest) — Active
        # Trades and Logs moved to full-width panels below content_frame.

        # ── Login + Load Master ──────────────────────────────
        top_frame = tk.Frame(left_frame, bg="#1e1e1e")
        top_frame.pack(pady=5, anchor="w")

        self.login_btn = tk.Button(top_frame, text="Login",
                                   bg="#00c853", fg="white",
                                   command=self.login)
        self.login_btn.pack(side="left", padx=5)

        self.login_indicator = tk.Label(top_frame, text="●",
                                        fg="red", bg="#1e1e1e")
        self.login_indicator.pack(side="left")

        # ── Broker selector dropdown (always visible) ────────
        tk.Label(top_frame, text="Broker:", bg="#1e1e1e", fg="#888888",
                 font=("Segoe UI", 8)).pack(side="left", padx=(10, 2))
        _broker_combo = ttk.Combobox(
            top_frame, textvariable=self.cfg_broker_select_var,
            values=["Angel One", "Kotak Neo", "Dhan HQ",
                    "Angel + Kotak", "Angel + Dhan", "All"],
            state="readonly", width=13, font=("Segoe UI", 8))
        _broker_combo.pack(side="left", padx=(0, 6))

        def _on_broker_dropdown(*_):
            sel = self.cfg_broker_select_var.get()
            self.use_angel_var.set("Angel" in sel or sel == "All")
            self.use_kotak_var.set("Kotak" in sel or sel == "All")
            self.use_dhan_var.set("Dhan" in sel or sel == "All")
            self._on_broker_toggle()

        self.cfg_broker_select_var.trace_add("write", _on_broker_dropdown)

        # Load Master now runs automatically right after a successful login
        # (see login.py login()). master_btn/master_indicator remain here as
        # an at-a-glance status dot + manual re-load action moved into the
        # Config panel (see _build_config_panel → "Reload Master Data").
        self.master_indicator = tk.Label(top_frame, text="●",
                                         fg="red", bg="#1e1e1e")
        # master_indicator kept (load_master sets its fg) but not packed —
        # second dot next to Login removed per user request.

        # Data Feed: selectable dropdown — which broker's WebSocket provides live ticks
        tk.Label(top_frame, text="Data Feed:",
                 bg="#1e1e1e", fg="#888888",
                 font=("Segoe UI", 8)).pack(side="left", padx=(4, 2))
        _feed_combo = ttk.Combobox(
            top_frame, textvariable=self.data_feed_broker_var,
            values=["Angel One", "Kotak Neo", "Dhan HQ"],
            state="readonly", width=10, font=("Segoe UI", 8))
        _feed_combo.pack(side="left", padx=(0, 4))
        self.connect_btn = tk.Button(
            top_frame, text="Connect",
            command=self.connect_data_feed,
            bg="#37474f", fg="white",
            font=("Segoe UI", 8))
        self.connect_btn.pack(side="left", padx=(4, 0))
        self.feed_indicator = tk.Label(top_frame, text="●",
                                       fg="grey", bg="#1e1e1e")
        self.feed_indicator.pack(side="left", padx=(2, 8))

        self.progress = ttk.Progressbar(top_frame, mode="indeterminate",
                                        length=60)
        self.progress.pack(side="left", padx=4)

        self.api_status_label = tk.Label(
            top_frame, text="API: OK", fg="#00e676",
            bg="#1e1e1e", font=("Segoe UI", 8, "bold"))
        self.api_status_label.pack(side="left", padx=(8, 0))

        self.start_stop_btn = tk.Button(
            top_frame, text="Start Bot", bg="#00c853", fg="white",
            font=("Segoe UI", 9, "bold"), command=self._on_start_stop_click)
        self.start_stop_btn.pack(side="left", padx=(10, 0))

        self.theme_toggle_btn = tk.Button(
            top_frame, text="☀️", font=("Segoe UI", 9),
            bg="#37474f", fg="white",
            command=self.toggle_theme)
        self.theme_toggle_btn.pack(side="left", padx=(12, 0))

        # ── Config Panel (collapsible) ────────────────────────
        self._build_config_panel(left_frame)

        # (Index/Expiry moved into the Option Chain panel header; Entry Mode
        # and Trade Mode moved into the Config panel's Hedges column — see
        # _build_config_panel and oc_header_row below.)
        self.index_var.trace_add(
            "write",
            lambda *_a: self.root.after(50, self._on_index_var_write)
        )

        # ── Strategy Configuration (collapsible) — lives in the RIGHT
        # column, directly above Option Chain (per item 1) ────
        # Wraps Trade Setup / Spot Detection Mode / Entry Method /
        # Trade Management / Strike Selection Mode / Directional Strike
        # Selection / Strategy Builder under one collapsible panel — each
        # section keeps its own heading inside.
        strat_cfg_outer = tk.Frame(right_frame, bg="#23303f", padx=10, pady=4,
                                   highlightbackground="#37474f", highlightthickness=1)
        strat_cfg_outer.pack(pady=(0, 3), fill="x")
        strat_cfg_header = tk.Frame(strat_cfg_outer, bg="#23303f")
        strat_cfg_header.pack(fill="x")
        self._strat_cfg_caret_btn = tk.Button(
            strat_cfg_header, text="▸ Strategy Configuration",
            bg="#23303f", fg="#00bcd4", font=("Segoe UI", 10, "bold"),
            bd=0, relief="flat", activebackground="#23303f", cursor="hand2",
            command=self._toggle_strategy_config_panel)
        self._strat_cfg_caret_btn.pack(side="left")
        strategy_config_body = tk.Frame(strat_cfg_outer, bg="#1e1e1e")
        # Collapsed by default — not packed; user expands when needed
        self.strategy_config_body_frame = strategy_config_body
        _outer_left_frame = left_frame   # restored after the wrapped sections
        left_frame = strategy_config_body   # re-target subsequent sections

        # ── Input fields ─────────────────────────────────────
        input_frame = tk.Frame(left_frame, bg="#2b2b2b", padx=15, pady=15)
        input_frame.pack(pady=10, fill="x")

        tk.Label(input_frame, text="⚙️  Trade Setup",
                 bg="#2b2b2b", fg="#ffd740",
                 font=("Segoe UI", 10, "bold")).grid(
                     row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))

        dt_row = tk.Frame(input_frame, bg="#2b2b2b")
        dt_row.grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 6))
        tk.Label(dt_row, text="📅", bg="#2b2b2b", fg="#00e676",
                 font=("Segoe UI", 9)).pack(side="left")
        self.datetime_label = tk.Label(dt_row, text="", bg="#2b2b2b",
                                       fg="#00e676",
                                       font=("Segoe UI", 9, "bold"))
        self.datetime_label.pack(side="left", padx=4)
        self._update_clock()

        tk.Label(input_frame, text="Spot Time",
                 bg="#2b2b2b", fg="white").grid(row=2, column=0, sticky="w")
        self.spot_entry = PlaceholderEntry(input_frame, "HH:MM",
                                           width=12, bg="#1e1e1e")
        self.spot_entry.grid(row=2, column=1, padx=10, pady=5)
        current_time = dt.datetime.now().strftime("%H:%M")
        self.spot_entry.delete(0, "end")
        self.spot_entry.insert(0, current_time)
        self.spot_entry.config(fg="#ffffff")

        def _spot_auto_colon(event):
            txt = self.spot_entry.get()
            if (len(txt) == 2 and ":" not in txt
                    and event.keysym not in ("BackSpace", "Delete", "colon")):
                self.spot_entry.insert(2, ":")
                self.spot_entry.icursor(3)
        self.spot_entry.bind("<KeyRelease>", _spot_auto_colon)

        tk.Label(input_frame, text="Candle Interval",
                 bg="#2b2b2b", fg="white").grid(row=3, column=0, sticky="w")
        ttk.Combobox(input_frame, textvariable=self.live_interval_var,
                     values=["1min", "3min", "5min"],
                     state="readonly", width=10
                     ).grid(row=3, column=1, padx=10, pady=5, sticky="w")

        self.numeric_entries = {}
        # Entry Price (row 4) and Tolerance (row 5)
        for i, text in enumerate(["Entry Price", "Tolerance"]):
            tk.Label(input_frame, text=text,
                     bg="#2b2b2b", fg="white").grid(
                         row=i + 4, column=0, sticky="w")
            entry = PlaceholderEntry(input_frame, "Enter",
                                     width=12, bg="#1e1e1e")
            entry.grid(row=i + 4, column=1, padx=10, pady=5)
            self.numeric_entries[text] = entry

        # SL (row 6) — label stays "SL Points" for preset backward-compat
        tk.Label(input_frame, text="SL",
                 bg="#2b2b2b", fg="white").grid(row=6, column=0, sticky="w")
        _sl_entry = PlaceholderEntry(input_frame, "Enter", width=12, bg="#1e1e1e")
        _sl_entry.grid(row=6, column=1, padx=10, pady=5)
        self.numeric_entries["SL Points"] = _sl_entry
        self._sl_unit_label = tk.Label(input_frame, text="pts",
            bg="#2b2b2b", fg="#888888", font=("Segoe UI", 8))
        self._sl_unit_label.grid(row=6, column=2, sticky="w")

        # Target (row 7) — label stays "Target Points" for preset backward-compat
        tk.Label(input_frame, text="Target",
                 bg="#2b2b2b", fg="white").grid(row=7, column=0, sticky="w")
        _tgt_entry = PlaceholderEntry(input_frame, "Enter", width=12, bg="#1e1e1e")
        _tgt_entry.grid(row=7, column=1, padx=10, pady=5)
        self.numeric_entries["Target Points"] = _tgt_entry
        self._tgt_unit_label = tk.Label(input_frame, text="pts",
            bg="#2b2b2b", fg="#888888", font=("Segoe UI", 8))
        self._tgt_unit_label.grid(row=7, column=2, sticky="w")

        # SL/Target mode selector (row 8)
        mode_row = tk.Frame(input_frame, bg="#2b2b2b")
        mode_row.grid(row=8, column=0, columnspan=3, sticky="w", pady=(0, 4))
        tk.Label(mode_row, text="SL/Tgt:", bg="#2b2b2b", fg="#00e676",
                 font=("Segoe UI", 8, "bold")).pack(side="left")
        tk.Radiobutton(mode_row, text="Points", variable=self.sl_tgt_type_var, value="pts",
                       command=self._on_sl_tgt_type_change,
                       bg="#2b2b2b", fg="white", selectcolor="#2b2b2b",
                       font=("Segoe UI", 9)).pack(side="left", padx=(8, 4))
        tk.Radiobutton(mode_row, text="Percent (%)", variable=self.sl_tgt_type_var, value="pct",
                       command=self._on_sl_tgt_type_change,
                       bg="#2b2b2b", fg="#ffd740", selectcolor="#2b2b2b",
                       font=("Segoe UI", 9)).pack(side="left")

        # ── Lots before trade entry ───────────────────────────
        tk.Label(input_frame, text="Lots",
                 bg="#2b2b2b", fg="#00e676",
                 font=("Segoe UI", 9, "bold")).grid(row=9, column=0, sticky="w")
        lot_spin_row = tk.Frame(input_frame, bg="#2b2b2b")
        lot_spin_row.grid(row=9, column=1, columnspan=2, sticky="w", padx=10, pady=5)
        tk.Spinbox(lot_spin_row, textvariable=self.lots_var,
                   from_=1, to=200, width=5,
                   bg="#1e1e1e", fg="white",
                   buttonbackground="#37474f",
                   font=("Segoe UI", 9)).pack(side="left")
        self.lot_size_info_label = tk.Label(
            lot_spin_row, text="",
            bg="#2b2b2b", fg="#888888",
            font=("Segoe UI", 8))
        self.lot_size_info_label.pack(side="left", padx=8)
        self._update_lot_info_label()

        # ── Spot Detection Mode ───────────────────────────────
        sdm_frame = tk.Frame(left_frame, bg="#2b2b2b", padx=15, pady=12)
        sdm_frame.pack(pady=5, fill="x")
        tk.Label(sdm_frame, text="Spot Detection Mode",
                 bg="#2b2b2b", fg="#00e676",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Label(sdm_frame,
                 text="How the bot detects the spot price / trigger moment",
                 bg="#2b2b2b", fg="#888888",
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 6))

        for txt, val in [
            ("Fixed Time  (candle-close aligned)", "TIME"),
            ("EMA Cross  (fast EMA crosses slow EMA)",  "EMA_CROSS"),
            ("PDH / PDL Touch  (candle reaches prev day H/L)", "PDH_PDL"),
        ]:
            tk.Radiobutton(sdm_frame, text=txt,
                           variable=self.spot_detect_mode_var, value=val,
                           command=self._toggle_spot_detect_ui,
                           bg="#2b2b2b", fg="white",
                           selectcolor="#2b2b2b").pack(anchor="w")

        # ── EMA Cross sub-panel ───────────────────────────────
        self.ema_cross_frame = tk.Frame(sdm_frame, bg="#1e2e1e",
                                        padx=10, pady=8,
                                        highlightbackground="#00e676",
                                        highlightthickness=1)
        self.ema_cross_frame.pack(anchor="w", fill="x", pady=4)
        tk.Label(self.ema_cross_frame,
                 text="EMA Cross Settings",
                 bg="#1e2e1e", fg="#00e676",
                 font=("Segoe UI", 9, "bold")).grid(
                     row=0, column=0, columnspan=4, sticky="w")
        tk.Label(self.ema_cross_frame, text="Fast EMA:",
                 bg="#1e2e1e", fg="white").grid(row=1, column=0, sticky="w", pady=3)
        tk.Entry(self.ema_cross_frame, textvariable=self.ema_fast_var,
                 width=6, bg="#1e1e1e", fg="white").grid(row=1, column=1, padx=5)
        tk.Label(self.ema_cross_frame, text="Slow EMA:",
                 bg="#1e2e1e", fg="white").grid(row=1, column=2, sticky="w", padx=(10,0))
        tk.Entry(self.ema_cross_frame, textvariable=self.ema_slow_var,
                 width=6, bg="#1e1e1e", fg="white").grid(row=1, column=3, padx=5)
        tk.Label(self.ema_cross_frame,
                 text="Candle Interval used for EMA calculation:",
                 bg="#1e2e1e", fg="#aaaaaa",
                 font=("Segoe UI", 8)).grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Combobox(self.ema_cross_frame,
                     textvariable=self.spot_candle_tf_var,
                     values=["1min", "3min", "5min", "15min"],
                     state="readonly", width=8).grid(row=2, column=2, columnspan=2,
                                                      padx=5, sticky="w")

        # ── PDH/PDL sub-panel ─────────────────────────────────
        self.pdh_pdl_frame = tk.Frame(sdm_frame, bg="#1e1e2e",
                                      padx=10, pady=8,
                                      highlightbackground="#ffd740",
                                      highlightthickness=1)
        self.pdh_pdl_frame.pack(anchor="w", fill="x", pady=4)
        tk.Label(self.pdh_pdl_frame,
                 text="PDH / PDL Settings",
                 bg="#1e1e2e", fg="#ffd740",
                 font=("Segoe UI", 9, "bold")).grid(
                     row=0, column=0, columnspan=4, sticky="w")
        tk.Label(self.pdh_pdl_frame, text="Detect on:",
                 bg="#1e1e2e", fg="white").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Combobox(self.pdh_pdl_frame,
                     textvariable=self.pdh_pdl_target_var,
                     values=["PDH", "PDL", "BOTH"],
                     state="readonly", width=8).grid(row=1, column=1, padx=5)
        tk.Label(self.pdh_pdl_frame, text="Timeframe:",
                 bg="#1e1e2e", fg="white").grid(row=1, column=2, sticky="w", padx=(10,0))
        ttk.Combobox(self.pdh_pdl_frame,
                     textvariable=self.spot_candle_tf_var,
                     values=["1min", "3min", "5min", "15min"],
                     state="readonly", width=8).grid(row=1, column=3, padx=5)
        tk.Label(self.pdh_pdl_frame,
                 text="Manual PDH override (0 = auto from index data):",
                 bg="#1e1e2e", fg="#aaaaaa",
                 font=("Segoe UI", 8)).grid(row=2, column=0, columnspan=2, sticky="w")
        pdh_row = tk.Frame(self.pdh_pdl_frame, bg="#1e1e2e")
        pdh_row.grid(row=3, column=0, columnspan=4, sticky="w", pady=2)
        tk.Label(pdh_row, text="PDH:", bg="#1e1e2e", fg="white", width=5).pack(side="left")
        tk.Entry(pdh_row, textvariable=self.pdh_value_var,
                 width=10, bg="#1e1e1e", fg="white").pack(side="left", padx=4)
        tk.Label(pdh_row, text="PDL:", bg="#1e1e2e", fg="white", width=5).pack(side="left", padx=(8,0))
        tk.Entry(pdh_row, textvariable=self.pdl_value_var,
                 width=10, bg="#1e1e1e", fg="white").pack(side="left", padx=4)

        self._toggle_spot_detect_ui()   # set initial visibility

        # ── Entry Method ──────────────────────────────────────
        self.entry_frame = tk.Frame(left_frame, bg="#2b2b2b", padx=15, pady=10)
        entry_frame = self.entry_frame
        entry_frame.pack(pady=5, fill="x")
        tk.Label(entry_frame, text="Entry Method",
                 bg="#2b2b2b", fg="#00e676",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.entry_mode_combo = ttk.Combobox(
            entry_frame, textvariable=self.entry_mode_var,
            values=["MARKET", "PRICE_BAND", "VWAP_RECLAIM", "VWAP_SQUEEZE_BREAK"],
            state="readonly", width=20)
        self.entry_mode_combo.pack(anchor="w", pady=5)
        self.entry_mode_combo.bind("<<ComboboxSelected>>", lambda e: self._on_entry_mode_change())

        # ── Squeeze sub-controls (visible only for VWAP_SQUEEZE_BREAK) ──
        self.squeeze_frame = tk.Frame(entry_frame, bg="#1a2a2a", padx=14, pady=5)
        sq_row = tk.Frame(self.squeeze_frame, bg="#1a2a2a")
        sq_row.pack(anchor="w")
        tk.Label(sq_row, text="Squeeze Lookback:", bg="#1a2a2a",
                 fg="white", font=("Segoe UI", 8)).pack(side="left")
        tk.Spinbox(sq_row, textvariable=self.squeeze_lookback_var,
                   from_=5, to=30, increment=1, width=5,
                   bg="#1e1e1e", fg="white",
                   buttonbackground="#37474f").pack(side="left", padx=5)
        tk.Label(sq_row, text="bars  (std & ATR must be below N-bar avg)",
                 bg="#1a2a2a", fg="#888888", font=("Segoe UI", 8)).pack(side="left")

        # ── Candle SL/Target (visible only for PRICE_BAND) ────
        self.candle_sl_outer = tk.Frame(left_frame, bg="#252525", padx=15, pady=10)
        self.candle_sl_outer.pack(pady=2, fill="x")

        tk.Label(self.candle_sl_outer, text="Candle SL / Target",
                 bg="#252525", fg="#ffd740",
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")

        tk.Label(self.candle_sl_outer, text="Mode",
                 bg="#252525", fg="white").grid(row=1, column=0, sticky="w", pady=(4, 2))
        self.candle_sl_mode_combo = ttk.Combobox(
            self.candle_sl_outer, textvariable=self.candle_sl_mode_var,
            values=["points", "prev_ohlc", "pct_entry", "swing_low", "vwap_adaptive"],
            state="readonly", width=14)
        self.candle_sl_mode_combo.grid(row=1, column=1, padx=8, sticky="w")
        self.candle_sl_mode_combo.bind("<<ComboboxSelected>>", lambda e: self._on_candle_sl_mode_change())

        tk.Label(self.candle_sl_outer, text="Candle TF",
                 bg="#252525", fg="white").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Combobox(self.candle_sl_outer, textvariable=self.sl_candle_tf_var,
                     values=["1min", "3min", "5min", "15min"],
                     state="readonly", width=8
                     ).grid(row=2, column=1, padx=8, sticky="w")

        # Sub-frame: prev_ohlc
        self.candle_sl_ohlc_frame = tk.Frame(self.candle_sl_outer, bg="#252525")
        self.candle_sl_ohlc_frame.grid(row=3, column=0, columnspan=3, sticky="w", pady=2)
        tk.Label(self.candle_sl_ohlc_frame, text="SL Field",
                 bg="#252525", fg="white", width=9, anchor="w").grid(row=0, column=0)
        ttk.Combobox(self.candle_sl_ohlc_frame, textvariable=self.sl_ohlc_field_var,
                     values=["open", "high", "low", "close"],
                     state="readonly", width=7
                     ).grid(row=0, column=1, padx=6)
        tk.Label(self.candle_sl_ohlc_frame, text="Tgt Field",
                 bg="#252525", fg="white", width=9, anchor="w").grid(row=1, column=0, pady=3)
        ttk.Combobox(self.candle_sl_ohlc_frame, textvariable=self.target_ohlc_field_var,
                     values=["open", "high", "low", "close"],
                     state="readonly", width=7
                     ).grid(row=1, column=1, padx=6)

        # Sub-frame: pct_entry
        self.candle_sl_pct_frame = tk.Frame(self.candle_sl_outer, bg="#252525")
        self.candle_sl_pct_frame.grid(row=3, column=0, columnspan=3, sticky="w", pady=2)
        tk.Label(self.candle_sl_pct_frame, text="SL %",
                 bg="#252525", fg="white", width=9, anchor="w").grid(row=0, column=0)
        tk.Entry(self.candle_sl_pct_frame, textvariable=self.sl_pct_entry_var,
                 width=8, bg="#1e1e1e", fg="white").grid(row=0, column=1, padx=6)
        tk.Label(self.candle_sl_pct_frame, text="of entry price",
                 bg="#252525", fg="#888888", font=("Segoe UI", 8)).grid(row=0, column=2)
        tk.Label(self.candle_sl_pct_frame, text="Target %",
                 bg="#252525", fg="white", width=9, anchor="w").grid(row=1, column=0, pady=3)
        tk.Entry(self.candle_sl_pct_frame, textvariable=self.target_pct_entry_var,
                 width=8, bg="#1e1e1e", fg="white").grid(row=1, column=1, padx=6)
        tk.Label(self.candle_sl_pct_frame, text="of entry price",
                 bg="#252525", fg="#888888", font=("Segoe UI", 8)).grid(row=1, column=2)

        # Sub-frame: swing_low
        self.candle_sl_swing_frame = tk.Frame(self.candle_sl_outer, bg="#252525")
        self.candle_sl_swing_frame.grid(row=3, column=0, columnspan=3, sticky="w", pady=2)
        tk.Label(self.candle_sl_swing_frame, text="Lookback",
                 bg="#252525", fg="white", width=9, anchor="w").grid(row=0, column=0)
        tk.Spinbox(self.candle_sl_swing_frame, textvariable=self.swing_lookback_var,
                   from_=1, to=50, width=6,
                   bg="#1e1e1e", fg="white", buttonbackground="#37474f"
                   ).grid(row=0, column=1, padx=6)
        tk.Label(self.candle_sl_swing_frame, text="candles",
                 bg="#252525", fg="#888888", font=("Segoe UI", 8)).grid(row=0, column=2)

        self._on_entry_mode_change()   # set initial visibility

        # ── Trade Management ──────────────────────────────────
        tsl_frame = tk.Frame(left_frame, bg="#2b2b2b", padx=15, pady=15)
        tsl_frame.pack(pady=10, fill="x")
        tk.Label(tsl_frame, text="Trade Management",
                 bg="#2b2b2b", fg="#00e676",
                 font=("Segoe UI", 11, "bold")).pack(anchor="w")
        tk.Checkbutton(tsl_frame, text="Enable Trailing Stop Loss",
                       variable=self.enable_tsl_var,
                       bg="#2b2b2b", fg="white",
                       selectcolor="#2b2b2b").pack(anchor="w", pady=5)
        row = tk.Frame(tsl_frame, bg="#2b2b2b")
        row.pack(anchor="w")
        tk.Label(row, text="TSL Step",
                 bg="#2b2b2b", fg="white").pack(side="left")
        tk.Entry(row, textvariable=self.tsl_step_var,
                 width=10, bg="#1e1e1e",
                 fg="white").pack(side="left", padx=10)

        # ── Strike Selection Mode ─────────────────────────────
        strike_mode_frame = tk.Frame(left_frame, bg="#2b2b2b",
                                     padx=15, pady=15)
        strike_mode_frame.pack(pady=10, fill="x")
        self.strike_panel_container = tk.Frame(left_frame, bg="#1e1e1e")
        self.strike_panel_container.pack(pady=10, fill="x")

        tk.Label(strike_mode_frame, text="Strike Selection Mode",
                 bg="#2b2b2b", fg="#00e676",
                 font=("Segoe UI", 11, "bold")).pack(anchor="w")
        for txt, val in [("Legacy (± Gap)", "LEGACY"),
                         ("ATM Round Only", "ROUND"),
                         ("Relative Offsets (Research)", "RELATIVE"),
                         ("Custom Range", "CUSTOM_RANGE")]:
            tk.Radiobutton(strike_mode_frame, text=txt,
                           variable=self.strike_mode_var, value=val,
                           command=self.update_gap_controls,
                           bg="#2b2b2b", fg="white",
                           selectcolor="#2b2b2b").pack(anchor="w")

        # Directional frame
        self.directional_frame = tk.Frame(self.strike_panel_container,
                                          bg="#2b2b2b", padx=15, pady=15)
        self.directional_frame.pack(pady=10, fill="x")
        tk.Label(self.directional_frame, text="Directional Strike Selection",
                 bg="#2b2b2b", fg="#00e676",
                 font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.directional_vars = {}
        for level in ["ATM", "+50", "-50", "+100", "-100", "+200", "-200"]:
            row = tk.Frame(self.directional_frame, bg="#2b2b2b")
            row.pack(anchor="w")
            tk.Label(row, text=level, width=6,
                     bg="#2b2b2b", fg="white").pack(side="left")
            ce_var = tk.BooleanVar(value=(level == "ATM"))
            pe_var = tk.BooleanVar(value=(level == "ATM"))
            tk.Checkbutton(row, text="CE", variable=ce_var,
                           bg="#2b2b2b", fg="white",
                           selectcolor="#2b2b2b").pack(side="left")
            tk.Checkbutton(row, text="PE", variable=pe_var,
                           bg="#2b2b2b", fg="white",
                           selectcolor="#2b2b2b").pack(side="left")
            self.directional_vars[level] = {"CE": ce_var, "PE": pe_var}

        # Legacy frame
        self.legacy_frame = tk.Frame(self.strike_panel_container,
                                     bg="#2b2b2b", padx=15, pady=15)
        self.legacy_frame.pack(pady=10, fill="x")
        tk.Label(self.legacy_frame, text="Legacy Strike Gaps",
                 bg="#2b2b2b", fg="#00e676",
                 font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.legacy_gap_vars = {}
        for g in ["ATM", "+1", "-1", "+2", "-2"]:
            var = tk.BooleanVar(value=True)
            r   = tk.Frame(self.legacy_frame, bg="#2b2b2b")
            r.pack(anchor="w")
            tk.Checkbutton(r, text=g, variable=var,
                           bg="#2b2b2b", fg="white",
                           selectcolor="#2b2b2b").pack(side="left")
            self.legacy_gap_vars[g] = var

        # Custom Range frame
        self.custom_range_frame = tk.Frame(self.strike_panel_container,
                                           bg="#2b2b2b", padx=15, pady=15)
        self.custom_range_frame.pack(pady=10, fill="x")
        tk.Label(self.custom_range_frame, text="Custom ATM Range",
                 bg="#2b2b2b", fg="#00e676",
                 font=("Segoe UI", 11, "bold")).pack(anchor="w")
        tk.Label(self.custom_range_frame,
                 text="Point offsets relative to ATM  (e.g. From = -200, To = +100)",
                 bg="#2b2b2b", fg="#888888",
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 6))

        _cr_row1 = tk.Frame(self.custom_range_frame, bg="#2b2b2b")
        _cr_row1.pack(anchor="w", pady=2)
        tk.Label(_cr_row1, text="From ATM:", width=10, anchor="w",
                 bg="#2b2b2b", fg="white").pack(side="left")
        tk.Entry(_cr_row1, textvariable=self.custom_range_from_var, width=8,
                 bg="#1e1e1e", fg="white", insertbackground="white").pack(side="left", padx=4)

        _cr_row2 = tk.Frame(self.custom_range_frame, bg="#2b2b2b")
        _cr_row2.pack(anchor="w", pady=2)
        tk.Label(_cr_row2, text="To ATM:", width=10, anchor="w",
                 bg="#2b2b2b", fg="white").pack(side="left")
        tk.Entry(_cr_row2, textvariable=self.custom_range_to_var, width=8,
                 bg="#1e1e1e", fg="white", insertbackground="white").pack(side="left", padx=4)

        _cr_row3 = tk.Frame(self.custom_range_frame, bg="#2b2b2b")
        _cr_row3.pack(anchor="w", pady=(6, 0))
        tk.Checkbutton(_cr_row3, text="CE", variable=self.custom_range_ce_var,
                       bg="#2b2b2b", fg="white",
                       selectcolor="#2b2b2b").pack(side="left")
        tk.Checkbutton(_cr_row3, text="PE", variable=self.custom_range_pe_var,
                       bg="#2b2b2b", fg="white",
                       selectcolor="#2b2b2b").pack(side="left")

        # ── Multi-Strategy Builder (also inside Strategy Configuration) ──
        strat_outer = tk.Frame(left_frame, bg="#1e1e1e")
        strat_outer.pack(pady=5, fill="x")

        strat_header = tk.Frame(strat_outer, bg="#2b2b2b", padx=15, pady=10)
        strat_header.pack(fill="x")
        tk.Label(strat_header, text="📋  Strategy Builder",
                 bg="#2b2b2b", fg="#00bcd4",
                 font=("Segoe UI", 11, "bold")).pack(side="left")
        tk.Button(strat_header, text="+ Save Current as Strategy",
                  bg="#00695c", fg="white",
                  command=self._save_current_as_strategy).pack(side="right", padx=5)
        tk.Button(strat_header, text="🗑 Clear",
                  bg="#b71c1c", fg="white",
                  command=self._clear_all_strategies).pack(side="right", padx=5)

        strat_desc_lbl = tk.Label(
            strat_outer,
            text="Save multiple parameter sets above. "
                 "All saved strategies run simultaneously during backtest.",
            bg="#1e1e1e", fg="#888888",
            font=("Segoe UI", 8), justify="left")
        strat_desc_lbl.pack(anchor="w", fill="x", padx=15, pady=(2, 4))
        # Fixed wraplength clipped the text whenever this panel was narrower
        # than that pixel value — wrap to the label's own current width
        # instead so it always wraps to fit, however wide the panel is.
        strat_desc_lbl.bind(
            "<Configure>",
            lambda e: strat_desc_lbl.config(wraplength=max(1, e.width - 4)))

        self.strat_list_frame = tk.Frame(strat_outer, bg="#1a1a1a",
                                         padx=10, pady=6)
        self.strat_list_frame.pack(fill="x", padx=5)

        left_frame = _outer_left_frame   # sections below are outside Strategy Configuration

        # ── Strategy Presets ─────────────────────────────────
        preset_frame = tk.Frame(left_frame, bg="#1a2a1a", padx=15, pady=10,
                                highlightbackground="#4caf50",
                                highlightthickness=1)
        preset_frame.pack(pady=6, fill="x")

        preset_title_row = tk.Frame(preset_frame, bg="#1a2a1a")
        preset_title_row.pack(fill="x", pady=(0, 6))
        tk.Label(preset_title_row, text="💾  Strategy Presets",
                 bg="#1a2a1a", fg="#81c784",
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Label(preset_title_row,
                 text="Save / load all parameters as a named preset",
                 bg="#1a2a1a", fg="#557755",
                 font=("Segoe UI", 8)).pack(side="left", padx=8)

        preset_load_row = tk.Frame(preset_frame, bg="#1a2a1a")
        preset_load_row.pack(fill="x", pady=(0, 4))

        self.preset_combo = ttk.Combobox(
            preset_load_row, textvariable=self.strategy_select_var,
            state="readonly", width=28, font=("Segoe UI", 9))
        self.preset_combo.pack(side="left")
        self._refresh_preset_dropdown()

        tk.Button(preset_load_row, text="📂 Load",
                  bg="#2e7d32", fg="white",
                  font=("Segoe UI", 9, "bold"),
                  command=self.load_preset_action).pack(side="left", padx=(6, 0))
        tk.Button(preset_load_row, text="🗑 Delete",
                  bg="#b71c1c", fg="white",
                  font=("Segoe UI", 9),
                  command=self.delete_preset_action).pack(side="left", padx=4)
        tk.Button(preset_load_row, text="➕ Add to Queue",
                  bg="#1565c0", fg="white",
                  font=("Segoe UI", 9, "bold"),
                  command=self.add_to_run_queue).pack(side="left", padx=(6, 0))

        preset_save_row = tk.Frame(preset_frame, bg="#1a2a1a")
        preset_save_row.pack(fill="x", pady=(2, 0))
        tk.Button(preset_save_row, text="💾  Save as New Preset",
                  bg="#1b5e20", fg="white",
                  font=("Segoe UI", 9, "bold"),
                  command=self.save_preset_dialog).pack(side="left")
        tk.Button(preset_save_row, text="✏️  Update Selected Preset",
                  bg="#4a2800", fg="#ffcc80",
                  font=("Segoe UI", 9, "bold"),
                  command=self.update_preset_action).pack(side="left", padx=(8, 0))

        # ── Run Queue display ─────────────────────────────────
        queue_frame = tk.Frame(preset_frame, bg="#1a1a2e",
                               highlightbackground="#334477",
                               highlightthickness=1)
        queue_frame.pack(fill="x", pady=(8, 0))

        queue_header = tk.Frame(queue_frame, bg="#1a1a2e")
        queue_header.pack(fill="x", padx=6, pady=(4, 2))
        tk.Label(queue_header, text="🚀  Multi-Strategy Run Queue",
                 bg="#1a1a2e", fg="#88aadd",
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        tk.Button(queue_header, text="Clear All",
                  bg="#1a1a2e", fg="#ff7777",
                  font=("Segoe UI", 8), bd=0,
                  command=self.clear_run_queue).pack(side="right", padx=4)
        tk.Label(queue_header,
                 text="Start Bot runs ALL queued strategies simultaneously",
                 bg="#1a1a2e", fg="#445566",
                 font=("Segoe UI", 7, "italic")).pack(side="left", padx=6)

        self.queue_chips_frame = tk.Frame(queue_frame, bg="#1a1a2e")
        self.queue_chips_frame.pack(fill="x", padx=6, pady=(2, 6))
        self._refresh_queue_display()

        # ── Broker Selection (Live Trading) ──────────────────
        self.broker_frame = tk.Frame(left_frame, bg="#1a1a2e",
                                     padx=15, pady=12,
                                     highlightbackground="#ff9800",
                                     highlightthickness=1)
        # Broker frame starts hidden — shown only when Live mode is selected

        tk.Label(self.broker_frame,
                 text="🏦  Broker Selection  (Live Trading)",
                 bg="#1a1a2e", fg="#ff9800",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Label(self.broker_frame,
                 text="Choose which broker(s) receive live orders. "
                      "Angel One is also the data/WebSocket source.",
                 bg="#1a1a2e", fg="#666688",
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 6))

        broker_row = tk.Frame(self.broker_frame, bg="#1a1a2e")
        broker_row.pack(anchor="w", fill="x")

        # Angel One checkbox
        angel_cb = tk.Checkbutton(
            broker_row,
            text="Angel One",
            variable=self.use_angel_var,
            command=self._on_broker_toggle,
            bg="#1a1a2e", fg="#00e676",
            selectcolor="#1a1a2e",
            font=("Segoe UI", 9, "bold"),
            activebackground="#1a1a2e",
            activeforeground="#00e676",
        )
        angel_cb.pack(side="left", padx=(0, 20))

        # Kotak Neo checkbox
        kotak_cb = tk.Checkbutton(
            broker_row,
            text="Kotak Neo",
            variable=self.use_kotak_var,
            command=self._on_broker_toggle,
            bg="#1a1a2e", fg="#ff9800",
            selectcolor="#1a1a2e",
            font=("Segoe UI", 9, "bold"),
            activebackground="#1a1a2e",
            activeforeground="#ff9800",
        )
        kotak_cb.pack(side="left")

        # Live status indicators per broker
        self.angel_broker_indicator = tk.Label(
            broker_row, text="  ●  Angel: Not Logged In",
            bg="#1a1a2e", fg="#555555", font=("Segoe UI", 8))
        self.angel_broker_indicator.pack(side="left", padx=(18, 0))

        self.kotak_broker_indicator = tk.Label(
            broker_row, text="  ●  Kotak: Not Logged In",
            bg="#1a1a2e", fg="#555555", font=("Segoe UI", 8))
        self.kotak_broker_indicator.pack(side="left", padx=(8, 0))

        # Dhan HQ checkbox
        dhan_cb = tk.Checkbutton(
            broker_row,
            text="Dhan HQ",
            variable=self.use_dhan_var,
            command=self._on_broker_toggle,
            bg="#1a1a2e", fg="#29b6f6",
            selectcolor="#1a1a2e",
            font=("Segoe UI", 9, "bold"),
            activebackground="#1a1a2e",
            activeforeground="#29b6f6",
        )
        dhan_cb.pack(side="left", padx=(8, 0))

        self.dhan_broker_indicator = tk.Label(
            broker_row, text="  ●  Dhan: Not Logged In",
            bg="#1a1a2e", fg="#555555", font=("Segoe UI", 8))
        self.dhan_broker_indicator.pack(side="left", padx=(8, 0))

        self.sub_acct_label = tk.Label(
            self.broker_frame, text="",
            bg="#1a1a2e", fg="#aaaaaa", font=("Segoe UI", 8, "italic"))
        self.sub_acct_label.pack(anchor="w", pady=(4, 0))

        # (Data Feed selector moved to top bar — WebSocket combo + Connect button)

        # Broker selection warning label (shown when none selected)
        self.broker_warn_label = tk.Label(
            self.broker_frame,
            text="⚠️  Select at least one broker for Live trading.",
            bg="#1a1a2e", fg="#ff5252",
            font=("Segoe UI", 8, "italic"))
        # only shown when needed

        # ── Backtest Config Panel ─────────────────────────────
        self.bt_frame = tk.Frame(left_frame, bg="#1a2a1a",
                                 padx=15, pady=15,
                                 highlightbackground="#ffd740",
                                 highlightthickness=1)
        tk.Label(self.bt_frame, text="📊 Backtest Configuration",
                 bg="#1a2a1a", fg="#ffd740",
                 font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 8))
        tk.Label(self.bt_frame,
                 text="Index File: datetime,open,high,low,close,volume"
                      "  (.csv or .xlsx)\n"
                      "Options folder: one file per day (YYYYMMDD.csv/.xlsx)\n"
                      "Options columns: datetime,symbol,open,high,low,close,volume",
                 bg="#1a2a1a", fg="#aaaaaa", font=("Segoe UI", 8),
                 justify="left").pack(anchor="w", pady=(0, 8))

        date_row = tk.Frame(self.bt_frame, bg="#1a2a1a")
        date_row.pack(anchor="w", fill="x", pady=3)
        tk.Label(date_row, text="From Date:", bg="#1a2a1a", fg="white",
                 width=10, anchor="w").pack(side="left")
        DateEntry(date_row, textvariable=self.bt_from_date_var,
                  date_pattern="yyyy-mm-dd", width=12,
                  background="#37474f", foreground="white",
                  borderwidth=2).pack(side="left", padx=5)
        tk.Label(date_row, text="To Date:", bg="#1a2a1a",
                 fg="white").pack(side="left", padx=(10, 0))
        DateEntry(date_row, textvariable=self.bt_to_date_var,
                  date_pattern="yyyy-mm-dd", width=12,
                  background="#37474f", foreground="white",
                  borderwidth=2).pack(side="left", padx=5)

        idx_row = tk.Frame(self.bt_frame, bg="#1a2a1a")
        idx_row.pack(anchor="w", fill="x", pady=3)
        tk.Button(idx_row, text="Load Index CSV / Excel",
                  bg="#37474f", fg="white",
                  command=self.load_bt_index_file).pack(side="left")
        self.bt_index_label = tk.Label(idx_row, text="No file loaded",
                                       bg="#1a2a1a", fg="#aaaaaa",
                                       font=("Segoe UI", 8))
        self.bt_index_label.pack(side="left", padx=8)

        opt_row = tk.Frame(self.bt_frame, bg="#1a2a1a")
        opt_row.pack(anchor="w", fill="x", pady=3)
        tk.Button(opt_row, text="Load Options Folder",
                  bg="#37474f", fg="white",
                  command=self.load_bt_options_folder).pack(side="left")
        self.bt_options_label = tk.Label(opt_row, text="No folder selected",
                                         bg="#1a2a1a", fg="#aaaaaa",
                                         font=("Segoe UI", 8))
        self.bt_options_label.pack(side="left", padx=8)

        intv_row = tk.Frame(self.bt_frame, bg="#1a2a1a")
        intv_row.pack(anchor="w", fill="x", pady=3)
        tk.Label(intv_row, text="Options Interval:",
                 bg="#1a2a1a", fg="white").pack(side="left")
        ttk.Combobox(intv_row, textvariable=self.bt_interval_var,
                     values=["1min", "3min"], state="readonly",
                     width=8).pack(side="left", padx=8)

        self.bt_progress_label = tk.Label(
            self.bt_frame, textvariable=self.bt_progress_var,
            bg="#1a2a1a", fg="#00e676", font=("Segoe UI", 9))
        self.bt_progress_label.pack(anchor="w", pady=4)
        self.bt_progress_bar = ttk.Progressbar(
            self.bt_frame, mode="determinate", length=300)
        self.bt_progress_bar.pack(anchor="w", pady=2)

        # ── Output Folder ─────────────────────────────────────
        folder_frame = tk.Frame(left_frame, bg="#2b2b2b", padx=15, pady=15)
        folder_frame.pack(pady=10, fill="x")
        tk.Label(folder_frame, text="📤  Trade Export",
                 bg="#2b2b2b", fg="#00bcd4",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 6))
        tk.Button(folder_frame, text="Select Output Folder",
                  bg="#37474f", fg="white",
                  command=self.select_output_folder).pack(anchor="w")
        self.folder_label = tk.Label(folder_frame,
                                     text=self.output_folder.get(),
                                     bg="#2b2b2b", fg="#00bcd4")
        self.folder_label.pack(anchor="w")

        # Start/Stop is now a single toggle button next to API status in
        # top_frame (see below). start_indicator is kept (unpacked) since
        # bot_lifecycle.py/backtest.py still set its color on state changes.
        self.start_indicator = tk.Label(self.root, text="●", fg="red")

        # (Logs panel moved to a full-width section below Active Trades —
        # see end of build_gui, after the Active Trades panel is built.)

        # ── Market LTP data store ─────────────────────────────
        # The visible "Market LTP" panel was replaced by the scrolling
        # Market Ticker marquee at the top of the app (item 6). These Label
        # widgets are kept unpacked purely as a data store: their .cget("text")
        # is read by the marquee refresh and by refresh_option_chain()'s ATM
        # lookup, and they're written to the same way as before via login.py.
        self.ltp_font   = ("Segoe UI", 13, "bold")
        self.market_ltp_labels = {}
        self.market_change_labels = {}   # sym → (pts_lbl, pct_lbl, arrow_lbl)
        _ltp_store = tk.Frame(self.root)   # never packed
        for sym in ["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL"]:
            ltp_lbl   = tk.Label(_ltp_store, text="--")
            arrow_lbl = tk.Label(_ltp_store, text="")
            pct_lbl   = tk.Label(_ltp_store, text="")
            pts_lbl   = tk.Label(_ltp_store, text="")
            self.market_ltp_labels[sym] = ltp_lbl
            self.market_change_labels[sym] = (pts_lbl, pct_lbl, arrow_lbl)

        # ── RIGHT — Option Chain ──────────────────────────────
        # Fills all remaining vertical space below Strategy Configuration
        # (which is packed with fill="x" only, so it never competes for it).
        oc_outer = tk.Frame(right_frame, bg="#1a1a2e",
                            padx=10, pady=4,
                            highlightbackground="#ffd740",
                            highlightthickness=1)
        oc_outer.pack(fill="both", expand=True, pady=(0, 4))

        # Header row
        oc_header_row = tk.Frame(oc_outer, bg="#1a1a2e")
        oc_header_row.pack(fill="x", pady=(0, 4))
        tk.Label(oc_header_row, text="📊 Option Chain",
                 bg="#1a1a2e", fg="#ffd740",
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=(0, 4))
        tk.Button(oc_header_row, text="🔄",
                  bg="#37474f", fg="white", width=2,
                  font=("Segoe UI", 9),
                  command=self.refresh_option_chain).pack(side="right")

        # Index + Expiry — moved here from their old standalone panel to
        # use the space between the title and the Feed indicator.
        tk.Label(oc_header_row, text="Index:", bg="#1a1a2e", fg="#888888",
                 font=("Segoe UI", 8)).pack(side="left", padx=(6, 2))
        self.index_combo = ttk.Combobox(
            oc_header_row, textvariable=self.index_var,
            values=["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL"],
            state="readonly", width=9, font=("Segoe UI", 8))
        self.index_combo.pack(side="left")
        self.index_combo.bind("<<ComboboxSelected>>",
            lambda e: [self.update_expiry_list(), self.update_gap_controls(),
                       self._update_lot_info_label(), self._update_hedge_offset_choices()])

        tk.Label(oc_header_row, text="Expiry:", bg="#1a1a2e", fg="#888888",
                 font=("Segoe UI", 8)).pack(side="left", padx=(4, 2))
        self.expiry_combo = ttk.Combobox(
            oc_header_row, textvariable=self.expiry_var,
            width=11, state="readonly", font=("Segoe UI", 8))
        self.expiry_combo.pack(side="left")
        self.expiry_combo.bind("<<ComboboxSelected>>",
            lambda e: self.refresh_option_chain())

        # OC feed label — shows which broker is driving live data (set by Data Feed dropdown)
        self.oc_feed_label = tk.Label(
            oc_header_row, textvariable=self.data_feed_broker_var,
            bg="#1a1a2e", fg="#00e676", font=("Segoe UI", 7, "bold"))
        self.oc_feed_label.pack(side="right", padx=(0, 4))

        # Strikes count + Call/Put-only filter row
        oc_filter_row = tk.Frame(oc_outer, bg="#1a1a2e")
        oc_filter_row.pack(fill="x", pady=(0, 4))
        tk.Label(oc_filter_row, text="Strikes:", bg="#1a1a2e", fg="#888888",
                 font=("Segoe UI", 8)).pack(side="left")
        self.oc_strike_count_combo = ttk.Combobox(
            oc_filter_row, textvariable=self.oc_strike_count_var,
            values=[5, 10, 15, 20, 25, 30], state="readonly", width=4)
        self.oc_strike_count_combo.pack(side="left", padx=(4, 12))
        self.oc_strike_count_combo.bind(
            "<<ComboboxSelected>>", lambda e: self.refresh_option_chain())
        for txt, val in [("Both", "BOTH"), ("Call only", "CALL_ONLY"),
                         ("Put only", "PUT_ONLY")]:
            tk.Radiobutton(oc_filter_row, text=txt,
                           variable=self.oc_side_filter_var, value=val,
                           bg="#1a1a2e", fg="white", selectcolor="#1a1a2e",
                           font=("Segoe UI", 8),
                           command=self._on_oc_side_filter_change).pack(side="left", padx=2)

        # Column headers
        oc_col_hdr = tk.Frame(oc_outer, bg="#263238")
        oc_col_hdr.pack(fill="x")
        oc_col_hdr.grid_columnconfigure(0, weight=2)
        oc_col_hdr.grid_columnconfigure(1, weight=3)
        oc_col_hdr.grid_columnconfigure(2, weight=2)
        self.oc_col_hdr_labels = {}
        for col_i, (key, txt, anchor) in enumerate([
                ("CE", "CE LTP", "e"), ("STRIKE", "Strike", "center"),
                ("PE", "PE LTP", "w")]):
            lbl = tk.Label(oc_col_hdr, text=txt, bg="#263238",
                          fg="#90a4ae", font=("Segoe UI", 8, "bold"),
                          anchor=anchor)
            lbl.grid(row=0, column=col_i, sticky="ew", padx=4, pady=2)
            self.oc_col_hdr_labels[key] = lbl

        # Scrollable chain rows — expands to fill whatever vertical space
        # oc_outer is given by right_frame's grid weights (no fixed height).
        oc_canvas_frame = tk.Frame(oc_outer, bg="#1a1a2e")
        oc_canvas_frame.pack(fill="both", expand=True)
        self.oc_canvas = tk.Canvas(oc_canvas_frame, bg="#1a1a2e",
                                   height=200, highlightthickness=0)
        oc_scroll = tk.Scrollbar(oc_canvas_frame, orient="vertical",
                                 command=self.oc_canvas.yview)
        self.oc_inner = tk.Frame(self.oc_canvas, bg="#1a1a2e")
        _oc_win = self.oc_canvas.create_window((0, 0), window=self.oc_inner, anchor="nw")
        self.oc_inner.bind("<Configure>",
            lambda e: self.oc_canvas.configure(
                scrollregion=self.oc_canvas.bbox("all")))
        # Stretch oc_inner to canvas width so grid columns align with header
        self.oc_canvas.bind("<Configure>",
            lambda e: self.oc_canvas.itemconfig(_oc_win, width=e.width))
        self.oc_canvas.configure(yscrollcommand=oc_scroll.set)
        self.oc_canvas.pack(side="left", fill="both", expand=True)
        oc_scroll.pack(side="right", fill="y")

        # Redirect global mousewheel to OC canvas while hovering
        self._bind_panel_scroll(oc_canvas_frame, self.oc_canvas)

        # Empty state label (shown before Refresh is clicked)
        self.oc_empty_label = tk.Label(
            self.oc_inner,
            text="Load Master then click 🔄 Refresh",
            bg="#1a1a2e", fg="#555577",
            font=("Segoe UI", 8, "italic"))
        self.oc_empty_label.pack(pady=10)

        # Manual trade action bar (BUY/SELL+Lots+Add to Bot, Market/Limit
        # price, SL/Tgt, Place Trade Now) removed — trades are now placed
        # directly from the Option Chain B/S buttons (_oc_quick_trade).
        # oc_sel_label kept as a hidden Label since _oc_select still updates
        # it; oc_lots_var / manual_order_type_var / manual_limit_price_var /
        # manual_sl_pts_var / manual_tgt_pts_var still feed _oc_quick_trade's
        # underlying manual_punch_trade() call.
        self.oc_sel_label = tk.Label(self.root, text="No strike selected")
        self.manual_limit_entry = tk.Entry(self.root, textvariable=self.manual_limit_price_var)

        # Strike LTP panel removed (item 14) — strike_ltp_labels kept as a
        # dict since _add_strike_ltp_row / remove_strike still reference it.
        self.strike_ltp_labels = {}

        # ── Active Trades — full-width panel below BOTH columns ──
        # Pulled out of right_frame (per item 2) so it spans the entire
        # window width instead of being confined to the Option Chain
        # column's fixed width, filling the whole bottom section.
        _sh = self.root.winfo_screenheight()
        self._trade_panel_max_h = max(350, min(700, int(_sh * 0.5)))
        self.trade_frame = tk.Frame(main, bg="#2e3b4e", padx=15, pady=15)
        # fill="x" only (no expand) — the panel's height tracks its actual
        # row count via _update_trade_panel_height() instead of grabbing all
        # leftover vertical space, which previously left a tall blank canvas
        # gap below the (often single) live row.
        self.trade_frame.pack(fill="x", pady=(4, 0))
        tk.Label(self.trade_frame, text="Active Trades",
                 bg="#2e3b4e", fg="#00bcd4",
                 font=("Segoe UI", 9, "bold")).pack(pady=1)
        self.pnl_summary_label = tk.Label(
            self.trade_frame, text="Running P&L: 0.00",
            bg="#2e3b4e", fg="#00e676",
            font=("Segoe UI", 8, "bold"))
        self.pnl_summary_label.pack(pady=1)

        sq_row = tk.Frame(self.trade_frame, bg="#2e3b4e")
        sq_row.pack(fill="x", pady=(0, 2))

        # Refresh LTP button — forces a REST snapshot + WS resubscribe so
        # stale prices (e.g. after alt-tabbing away) are corrected immediately.
        tk.Button(
            sq_row, text="⟳ Refresh",
            bg="#37474f", fg="white",
            font=("Segoe UI", 8, "bold"),
            relief="flat", cursor="hand2",
            command=self._refresh_live_prices
        ).pack(side="left", padx=(4, 0), pady=2)

        _sqoff_btn = tk.Button(
            sq_row, text="⬛  SQUARE OFF ALL",
            bg="#b71c1c", fg="white",
            font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2",
            command=self.square_off_all)
        _sqoff_btn._theme_no_invert = True   # always red regardless of theme
        _sqoff_btn.pack(side="right", padx=5, pady=2)

        # Exit Hedges action button — starts green (sellers always have hedges on entry).
        # Turns grey+disabled once all hedges are closed (update_hedge_btn toggles).
        self._exit_hedges_btn = tk.Button(
            sq_row,
            text="🛡 Exit Hedges",
            bg="#1b5e20", fg="white",
            font=("Segoe UI", 8, "bold"),
            relief="flat", cursor="hand2",
            state="normal",
            command=self._close_all_hedges)
        self._exit_hedges_btn._theme_no_invert = True   # always green regardless of theme
        self._exit_hedges_btn.pack(side="right", padx=(0, 6), pady=2)

        # ── Risk Management (kill switch) ─────────────────────
        risk_frame = tk.Frame(self.trade_frame, bg="#3a2e2e")
        risk_frame.pack(fill="x", pady=(0, 2))
        r1 = tk.Frame(risk_frame, bg="#3a2e2e"); r1.pack(fill="x")
        tk.Checkbutton(r1, text="Risk Limits", variable=self.risk_enabled_var,
                       bg="#3a2e2e", fg="#ff8a80", selectcolor="#3a2e2e",
                       font=("Segoe UI", 8, "bold")).pack(side="left")
        tk.Checkbutton(r1, text="Max Loss", variable=self.risk_max_loss_on_var,
                       bg="#3a2e2e", fg="white", selectcolor="#3a2e2e",
                       font=("Segoe UI", 8)).pack(side="left", padx=(4, 0))
        tk.Entry(r1, textvariable=self.risk_max_loss_var, width=10,
                 bg="#1e1e1e", fg="white", font=("Segoe UI", 8)).pack(side="left")
        tk.Checkbutton(r1, text="Max Trades", variable=self.risk_max_trades_on_var,
                       bg="#3a2e2e", fg="white", selectcolor="#3a2e2e",
                       font=("Segoe UI", 8)).pack(side="left", padx=(4, 0))
        tk.Entry(r1, textvariable=self.risk_max_trades_var, width=6,
                 bg="#1e1e1e", fg="white", font=("Segoe UI", 8)).pack(side="left")
        tk.Checkbutton(r1, text="Profit Tgt", variable=self.risk_profit_on_var,
                       bg="#3a2e2e", fg="white", selectcolor="#3a2e2e",
                       font=("Segoe UI", 8)).pack(side="left", padx=(4, 0))
        tk.Entry(r1, textvariable=self.risk_profit_target_var, width=10,
                 bg="#1e1e1e", fg="white", font=("Segoe UI", 8)).pack(side="left")
        tk.Checkbutton(r1, text="Trail SL", variable=self.risk_trail_loss_var,
                       bg="#3a2e2e", fg="#ffd740", selectcolor="#3a2e2e",
                       font=("Segoe UI", 7)).pack(side="left", padx=(4, 0))
        tk.Checkbutton(r1, text="Roll 1", variable=self.roll_pos1_var,
                       bg="#3a2e2e", fg="#00bcd4", selectcolor="#3a2e2e",
                       font=("Segoe UI", 8, "bold"),
                       command=self._refresh_roll_columns).pack(side="left", padx=(4, 0))
        tk.Checkbutton(r1, text="Roll 2", variable=self.roll_pos2_var,
                       bg="#3a2e2e", fg="#00e5ff", selectcolor="#3a2e2e",
                       font=("Segoe UI", 8, "bold"),
                       command=self._refresh_roll_columns).pack(side="left", padx=(2, 0))

        r2 = tk.Frame(risk_frame, bg="#3a2e2e"); r2.pack(fill="x")
        tk.Label(r2, text="Breach:", bg="#3a2e2e", fg="#aaaaaa",
                 font=("Segoe UI", 7)).pack(side="left")
        tk.Checkbutton(r2, text="Block Entries", variable=self.risk_action_block_var,
                       bg="#3a2e2e", fg="#ffd740", selectcolor="#3a2e2e",
                       font=("Segoe UI", 7)).pack(side="left", padx=2)
        tk.Checkbutton(r2, text="Sq Off All", variable=self.risk_action_sqoff_var,
                       bg="#3a2e2e", fg="#ff5252", selectcolor="#3a2e2e",
                       font=("Segoe UI", 7)).pack(side="left", padx=2)
        tk.Button(r2, text="Apply", bg="#1565c0", fg="white",
                  font=("Segoe UI", 7, "bold"), relief="flat", cursor="hand2",
                  command=self.apply_risk_limits).pack(side="left", padx=(4, 2))
        self.risk_status_label = tk.Label(r2, text="", bg="#3a2e2e", fg="#888888",
                                          font=("Segoe UI", 7))
        self.risk_status_label.pack(side="right")

        # "Apply" column removed (item 1) — SL/Target/Lot edits now commit on
        # Enter/FocusOut with a dirty-state marker instead of a separate button.
        # "Roll" column added (item 4) — Rollup/Rolldown close the current
        # leg and open the next strike up/down (2 steps if Roll Pos 2 is
        # checked in the risk row above, else 1 step).
        columns = ["Time","Type","Strike","Lot ↵","Entry",
                   "LTP","SL ↵","Target ↵","PnL","Status","Adj Lot","Roll","Close"]
        # weight=0 on every real column (item 2) — columns no longer stretch
        # to fill the full window width (which left huge gaps between narrow
        # columns); any leftover width collects in a trailing spacer column
        # instead. Fixed per-column character widths are shared with
        # add_trade_row() in order_manager.py (self._trade_col_widths) so
        # header and data columns line up exactly regardless of cell content.
        weights = [0] * len(columns)
        self._trade_col_widths = [9, 6, 19, 6, 7, 7, 7, 7, 7, 9, 12, 18, 26]

        # Header and canvas share the same trade_container using grid so that
        # the header width exactly matches the canvas content width (col 0),
        # with the scrollbar in col 1 — fixing the column misalignment.
        trade_container = tk.Frame(self.trade_frame, bg="#2e3b4e")
        trade_container.pack(fill="both", expand=True)
        trade_container.grid_columnconfigure(0, weight=1)
        trade_container.grid_columnconfigure(1, weight=0)
        trade_container.grid_rowconfigure(0, weight=0)
        trade_container.grid_rowconfigure(1, weight=1)

        header_frame = tk.Frame(trade_container, bg="#2e3b4e")
        header_frame.grid(row=0, column=0, sticky="ew", pady=1)
        _roll_col = columns.index("Roll")   # column index 11
        for i, col in enumerate(columns):
            header_frame.grid_columnconfigure(i, weight=weights[i],
                                              minsize=self._trade_col_widths[i] * 7)
            if i == _roll_col:
                # Roll column header: three sub-labels inside a Frame.
                # "Roll 1" and "Roll 2" shown/hidden by _refresh_roll_columns;
                # plain "Roll" shown when neither checkbox is ticked.
                roll_hdr_cell = tk.Frame(header_frame, bg="#2e3b4e")
                roll_hdr_cell.grid(row=0, column=i, sticky="nsew", padx=3)
                # Fallback "Roll" label (visible when both checkboxes are off)
                self._roll_hdr_lbl0 = tk.Label(
                    roll_hdr_cell, text="Roll", bg="#2e3b4e", fg="white",
                    font=("Segoe UI", 9, "bold"), anchor="center")
                self._roll_hdr_lbl0.pack(fill="both", expand=True)
                # "Roll 1" header — starts hidden; packed by _refresh_roll_columns
                self._roll_hdr_lbl1 = tk.Label(
                    roll_hdr_cell, text="Roll 1", bg="#2e3b4e", fg="#5ec8e8",
                    font=("Segoe UI", 9, "bold"), anchor="center")
                # "Roll 2" header — starts hidden
                self._roll_hdr_lbl2 = tk.Label(
                    roll_hdr_cell, text="Roll 2", bg="#2e3b4e", fg="#80deea",
                    font=("Segoe UI", 9, "bold"), anchor="center")
            else:
                tk.Label(header_frame, text=col, bg="#2e3b4e", fg="white",
                         font=("Segoe UI", 9, "bold"), width=self._trade_col_widths[i],
                         anchor="center").grid(row=0, column=i,
                                               sticky="nsew", padx=3)
        header_frame.grid_columnconfigure(len(columns), weight=1)   # trailing spacer

        self._trade_row_h = 30   # approx px per row, used by _update_trade_panel_height
        trade_canvas = tk.Canvas(trade_container, bg="#2e3b4e",
                                 highlightthickness=0, height=80)
        trade_canvas.grid(row=1, column=0, sticky="nsew")
        self.trade_canvas = trade_canvas

        trade_scroll = tk.Scrollbar(trade_container,
                                    command=trade_canvas.yview)
        trade_scroll.grid(row=1, column=1, sticky="ns")

        self.trade_inner = tk.Frame(trade_canvas, bg="#2e3b4e")
        cw = trade_canvas.create_window((0, 0), window=self.trade_inner,
                                        anchor="nw")
        self.trade_inner.bind(
            "<Configure>",
            lambda e: trade_canvas.configure(
                scrollregion=trade_canvas.bbox("all")))
        trade_canvas.bind(
            "<Configure>",
            lambda e: trade_canvas.itemconfig(cw, width=e.width))
        trade_canvas.configure(yscrollcommand=trade_scroll.set)

        # Redirect global mousewheel to trade canvas while hovering
        self._bind_panel_scroll(self.trade_frame, trade_canvas)

        # Trade rows are keyed by a unique per-entry row_id (NOT token) so that
        # repeated entries on the same strike each get their own row and none are
        # orphaned. _active_row_id maps token -> row_id of the currently-live row
        # (the one the engine updates / closes).
        self.trade_rows     = {}
        self._row_seq       = 0
        self._active_row_id = {}

        # Divider separating the live (above) and closed (below) trade rows.
        # It is packed (empty) from the start so live rows can be packed
        # `before=` it; its label text is revealed when the first trade closes.
        self._closed_divider = tk.Label(
            self.trade_inner, text="", bg="#2e3b4e", fg="#5f6b7a",
            font=("Segoe UI", 8, "bold"), anchor="center")
        self._closed_divider.pack(fill="x")

        # ── Logs — full-width, collapsible, below Active Trades ──
        log_outer = tk.Frame(main, bg="#23303f", padx=10, pady=8,
                             highlightbackground="#37474f", highlightthickness=1)
        log_outer.pack(fill="x", pady=(6, 10))
        log_header = tk.Frame(log_outer, bg="#23303f")
        log_header.pack(fill="x")
        self._log_caret_btn = tk.Button(
            log_header, text="▸ Logs", bg="#23303f", fg="#00bcd4",
            font=("Segoe UI", 10, "bold"), bd=0, relief="flat",
            activebackground="#23303f", cursor="hand2",
            command=self._toggle_log_panel)
        self._log_caret_btn.pack(side="left")

        log_frame = tk.Frame(log_outer, bg="#1e1e1e")
        self.log_body_frame = log_frame   # hidden until toggled
        self.log_box = tk.Text(log_frame, height=8, bg="#121212",
                               fg="#00e676", wrap="word")
        log_scroll = tk.Scrollbar(log_frame, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=log_scroll.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        self.update_gap_controls()
        self._on_broker_toggle()          # set initial broker indicator colours

    # ==========================================================
    # GUI HELPERS
    # ==========================================================
    def _on_broker_toggle(self, *_):
        """
        Called whenever a broker checkbox changes or trade mode changes.
        Shows/hides broker panel, shows warning when none selected in Live mode,
        and refreshes indicator colours.
        """
        mode = self.trade_mode_var.get()
        # Show broker panel only when Live mode is active
        if mode == "Live":
            self.broker_frame.pack(pady=6, fill="x")
        else:
            self.broker_frame.pack_forget()

        # Warn if Live and no broker selected
        if (mode == "Live"
                and not self.use_angel_var.get()
                and not self.use_kotak_var.get()
                and not self.use_dhan_var.get()):
            self.broker_warn_label.pack(anchor="w", pady=(2, 0))
        else:
            self.broker_warn_label.pack_forget()

        self._refresh_broker_indicators()

    def _refresh_broker_indicators(self):
        """Refresh the small coloured login-status labels next to each checkbox."""
        # Angel One
        if self.angel_logged_in:
            self.angel_broker_indicator.config(
                text="  ●  Angel: Logged In ✓", fg="#00e676")
        else:
            fg = "#aaaaaa" if self.use_angel_var.get() else "#555555"
            self.angel_broker_indicator.config(
                text="  ●  Angel: Not Logged In", fg=fg)
        # Kotak Neo
        if self.kotak_logged_in:
            self.kotak_broker_indicator.config(
                text="  ●  Kotak: Logged In ✓", fg="#ff9800")
        else:
            fg = "#aaaaaa" if self.use_kotak_var.get() else "#555555"
            self.kotak_broker_indicator.config(
                text="  ●  Kotak: Not Logged In", fg=fg)
        # Dhan HQ
        if self.dhan_logged_in:
            self.dhan_broker_indicator.config(
                text="  ●  Dhan: Logged In ✓", fg="#29b6f6")
        else:
            fg = "#aaaaaa" if self.use_dhan_var.get() else "#555555"
            self.dhan_broker_indicator.config(
                text="  ●  Dhan: Not Logged In", fg=fg)

        if hasattr(self, "oc_feed_label"):
            self.oc_feed_label.config(fg="#00e676")

        # Sub-account count summary
        if hasattr(self, "sub_acct_label"):
            n_a = len(getattr(self, "_angel_sub_sessions", []))
            n_k = len(getattr(self, "_kotak_sub_sessions", []))
            n_d = len(getattr(self, "_dhan_sub_sessions",  []))
            parts = ([f"Angel ×{n_a + 1}"] if n_a else []) + \
                    ([f"Kotak ×{n_k + 1}"] if n_k else []) + \
                    ([f"Dhan ×{n_d + 1}"]  if n_d else [])
            self.sub_acct_label.config(
                text=f"Sub-accounts active: {', '.join(parts)}" if parts else "")

    def _set_api_status(self, ok: bool, reason: str = ""):
        """Update the API status label in the top bar and set the angel_api_ok gate flag."""
        self.angel_api_ok = ok
        def _update():
            if hasattr(self, "api_status_label"):
                if ok:
                    self.api_status_label.config(text="API: OK", fg="#00e676")
                else:
                    msg = f"API: DOWN ({reason})" if reason else "API: DOWN"
                    self.api_status_label.config(text=msg, fg="#ff5252")
        self.root.after(0, _update)

    def _update_lot_info_label(self):
        ls = self.get_lot_size()
        try:
            self.lot_size_info_label.config(text=f"(1 lot = {ls} qty)")
        except Exception:
            pass

    def _on_index_var_write(self):
        """Called whenever index_var changes (user selection OR programmatic set).
        Refreshes the expiry dropdown and related controls for the new index."""
        self.update_expiry_list()
        self.update_gap_controls()
        self._update_lot_info_label()

    def _update_clock(self):
        now = dt.datetime.now().strftime("%d %b %Y   %H:%M:%S")
        self.datetime_label.config(text=now)
        self.root.after(1000, self._update_clock)

    def _toggle_spot_detect_ui(self, *_):
        """Show/hide EMA-cross and PDH/PDL sub-panels based on selected mode."""
        mode = self.spot_detect_mode_var.get()
        self.ema_cross_frame.pack_forget()
        self.pdh_pdl_frame.pack_forget()
        if mode == "EMA_CROSS":
            self.ema_cross_frame.pack(anchor="w", fill="x", pady=4)
        elif mode == "PDH_PDL":
            self.pdh_pdl_frame.pack(anchor="w", fill="x", pady=4)

    # ----------------------------------------------------------
    # Strategy builder helpers
    # ----------------------------------------------------------
    def _snapshot_current_params(self, label=None):
        """Capture all current GUI parameters into a dict."""
        idx = len(self.strategies) + 1
        return {
            "label":              label or f"Strategy {idx}",
            "spot_detect_mode":   self.spot_detect_mode_var.get(),
            "spot_time":          self.spot_entry.get_value(),
            "candle_interval":    self.live_interval_var.get(),
            "ema_fast":           self.ema_fast_var.get(),
            "ema_slow":           self.ema_slow_var.get(),
            "spot_candle_tf":     self.spot_candle_tf_var.get(),
            "pdh_pdl_target":     self.pdh_pdl_target_var.get(),
            "pdh_value":          self.pdh_value_var.get(),
            "pdl_value":          self.pdl_value_var.get(),
            "entry_mode":         self.entry_mode_var.get(),
            "entry_price":        self.numeric_entries["Entry Price"].get_value(),
            "tolerance":          self.numeric_entries["Tolerance"].get_value(),
            "sl_points":          self.numeric_entries["SL Points"].get_value(),
            "target_points":      self.numeric_entries["Target Points"].get_value(),
            "enable_quant":       self.enable_quant_var.get(),
            "filter_ema":         self.filter_ema_var.get(),
            "ema_f1":             self.ema_f1_var.get(),
            "ema_f2":             self.ema_f2_var.get(),
            "ema_f3":             self.ema_f3_var.get(),
            "ema_alignment":      self.ema_alignment_var.get(),
            "filter_rsi":         self.filter_rsi_var.get(),
            "filter_range":       self.filter_range_var.get(),
            "filter_volume":      self.filter_volume_var.get(),
            "filter_vwap":        self.filter_vwap_var.get(),
            "filter_gamma_trap":  self.filter_gamma_trap_var.get(),
            "filter_gamma_exp":   self.filter_gamma_expansion_var.get(),
            "rsi_min_threshold":  self.rsi_min_threshold_var.get(),
            "filter_multi_bar":   self.filter_multi_bar_var.get(),
            "filter_time_window": self.filter_time_window_var.get(),
            "time_window_start":  self.time_window_start_var.get(),
            "time_window_end":    self.time_window_end_var.get(),
            "time_window_eod":    self.time_window_eod_var.get(),
            "filter_consolidation": self.filter_consolidation_var.get(),
            "consol_lookback":    self.consol_lookback_var.get(),
            "consol_atr_ratio":   self.consol_atr_ratio_var.get(),
            "filter_vol_ratio":   self.filter_vol_ratio_var.get(),
            "vol_ratio_min":      self.vol_ratio_min_var.get(),
            "vol_follow_through": self.vol_follow_through_var.get(),
            "filter_body_quality": self.filter_body_quality_var.get(),
            "body_quality_min":   self.body_quality_min_var.get(),
            "strike_mode":        self.strike_mode_var.get(),
            "enable_tsl":         self.enable_tsl_var.get(),
            "tsl_step":           self.tsl_step_var.get(),
            "gexp_override":      self.gexp_override_var.get(),
            "gexp_method":        self.gexp_method_var.get(),
            "gexp_sl_pct":        self.gexp_sl_pct_var.get(),
            "gexp_rr":            self.gexp_rr_var.get(),
            "gexp_tsl_step":      self.gexp_tsl_step_var.get(),
        }

    def _save_current_as_strategy(self):
        """Snapshot current params and add to strategy list."""
        snap = self._snapshot_current_params()
        self.strategies.append(snap)
        self._refresh_strategy_list()
        self.log(f"Strategy saved: {snap['label']} "
                 f"(mode={snap['spot_detect_mode']} "
                 f"entry={snap['entry_mode']})")

    def _clear_all_strategies(self):
        self.strategies.clear()
        self._refresh_strategy_list()

    def _refresh_strategy_list(self):
        for w in self.strat_list_frame.winfo_children():
            w.destroy()
        if not self.strategies:
            tk.Label(self.strat_list_frame,
                     text="No strategies saved yet.",
                     bg="#1a1a1a", fg="#555555",
                     font=("Segoe UI", 8)).pack(anchor="w")
            return
        for i, strat in enumerate(self.strategies):
            row = tk.Frame(self.strat_list_frame, bg="#252525",
                           pady=3, padx=6)
            row.pack(fill="x", pady=2)
            summary = (f"#{i+1}  {strat['label']}   "
                       f"| Spot:{strat['spot_detect_mode']}  "
                       f"| Entry:{strat['entry_mode']}  "
                       f"| Interval:{strat['candle_interval']}  "
                       f"| SL:{strat.get('sl_points','—')}  "
                       f"| Tgt:{strat.get('target_points','—')}")
            tk.Label(row, text=summary, bg="#252525",
                     fg="#00e676", font=("Segoe UI", 8),
                     anchor="w").pack(side="left", fill="x", expand=True)
            tk.Button(row, text="❌", bg="#252525", fg="red", bd=0,
                      command=lambda idx=i: self._delete_strategy(idx)
                      ).pack(side="right")

    def _delete_strategy(self, idx):
        if 0 <= idx < len(self.strategies):
            del self.strategies[idx]
        self._refresh_strategy_list()

    def _toggle_tick_ui(self, *_):
        """Tick Data Capture panel removed — kept as a no-op for callers."""
        pass

    def _tick_count_refresh_loop(self):
        """Refresh the buffered-tick count label every 5 s while bot is running."""
        if not self.is_running:
            return
        self._update_tick_count_label()
        self.root.after(5000, self._tick_count_refresh_loop)

    def _toggle_quant_filters_ui(self, *_):
        """Quant Filter UI section removed — kept as a no-op for callers."""
        pass

    def _on_entry_mode_change(self, *_):
        """Show candle SL/Target frame for PRICE_BAND and VWAP_SQUEEZE_BREAK."""
        mode = self.entry_mode_var.get()
        if mode in ("PRICE_BAND", "VWAP_SQUEEZE_BREAK"):
            self.candle_sl_outer.pack(pady=2, fill="x", after=self.entry_frame)
            self._on_candle_sl_mode_change()
        else:
            self.candle_sl_outer.pack_forget()
        if hasattr(self, "squeeze_frame"):
            if mode == "VWAP_SQUEEZE_BREAK":
                self.squeeze_frame.pack(anchor="w", fill="x", padx=4, pady=(0, 4))
            else:
                self.squeeze_frame.pack_forget()

    def _on_candle_sl_mode_change(self, *_):
        """Show the relevant sub-frame for the chosen candle SL mode."""
        self.candle_sl_ohlc_frame.grid_remove()
        self.candle_sl_pct_frame.grid_remove()
        self.candle_sl_swing_frame.grid_remove()
        mode = self.candle_sl_mode_var.get()
        if mode == "prev_ohlc":
            self.candle_sl_ohlc_frame.grid()
        elif mode == "pct_entry":
            self.candle_sl_pct_frame.grid()
        elif mode == "swing_low":
            self.candle_sl_swing_frame.grid()

    # ── Active trade panel height (grows per row, max 10 then scroll) ──
    _TRADE_PANEL_BASE_H = 155   # px: title + PnL + sq-off + risk frame + col header
    _TRADE_ROW_H        = 25    # px per trade row
    _TRADE_MAX_ROWS     = 10

    def _active_row(self, token):
        """Return the row dict of the currently-live trade for `token`, or None.

        Rows are keyed by a unique row_id; _active_row_id points token -> the
        live row_id. Closed rows stay in self.trade_rows but are no longer
        pointed to here, so this only ever returns the row the engine should
        update or close.
        """
        rid = self._active_row_id.get(token)
        return self.trade_rows.get(rid) if rid is not None else None

    def _move_row_to_closed(self, row_data):
        """Repack a finished trade row below the Closed divider and reveal it.

        Layout-only: caller is responsible for dimming / status. Live rows are
        packed `before` the divider, so re-packing without that keyword sends the
        row to the bottom (the closed section).
        """
        try:
            # Reveal the divider label (it is always packed, just empty until now)
            self._closed_divider.config(text="─────  Closed  ─────",
                                        pady=2)
            row_data["row"].pack_forget()
            row_data["row"].pack(fill="x", pady=1)             # append below divider
        except Exception:
            pass

    def _update_trade_panel_height(self):
        """Size the Active Trades scroll canvas to fit the current row
        count (so a single row sits right under the headers instead of
        floating inside a tall, mostly-blank canvas), capped at a generous
        max beyond which the internal scrollbar takes over."""
        try:
            n = len(self.trade_rows)
            row_h = getattr(self, "_trade_row_h", 30)
            max_h = getattr(self, "_trade_panel_max_h", 500)
            h = max(80, min(max_h, (n + 1) * row_h))
            self.trade_canvas.config(height=h)
        except Exception:
            pass

    def _toggle_vwap_band_ui(self, *_):
        """VWAP band sub-panel lived inside the removed Quant Filter section — no-op now."""
        pass

    def _on_sl_tgt_type_change(self, *_):
        unit = "%" if self.sl_tgt_type_var.get() == "pct" else "pts"
        try:
            self._sl_unit_label.config(text=unit)
            self._tgt_unit_label.config(text=unit)
        except Exception:
            pass

    def _toggle_gexp_override_ui(self, *_):
        """Gamma Expansion Override UI removed — kept as a no-op for callers."""
        pass

    def _toggle_limit_entry(self):
        """Enable/disable limit price entry based on order type radio selection."""
        state = "normal" if self.manual_order_type_var.get() == "LIMIT" else "disabled"
        self.manual_limit_entry.config(state=state)

    def clear_strike_panel(self):
        """Strike LTP panel UI removed (item 14) — still clears strike_state
        bookkeeping, which other callers (multi-strategy runner, lifecycle
        reset) rely on."""
        with self.lock:
            self.strike_state.clear()
        self.strike_ltp_labels.clear()
        self.status_var.set("Strike Panel Cleared ✅")

    def remove_strike(self, token, row_widget=None):
        # Remove from shared state first so the background tick thread
        # stops referencing this token before we destroy its widget.
        with self.lock:
            self.strike_state.pop(token, None)
        self.strike_ltp_labels.pop(token, None)
        if self.ce:
            self.ce.remove_token(token)   # stop candle engine BEFORE widget destroy
        if row_widget is not None:
            row_widget.destroy()
        self.status_var.set(f"{token} removed")

    def select_output_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.output_folder.set(folder)
            self.folder_label.config(text=folder)

    def update_gap_controls(self):
        mode = self.strike_mode_var.get()
        self.directional_frame.pack_forget()
        self.legacy_frame.pack_forget()
        self.custom_range_frame.pack_forget()
        if mode == "RELATIVE":
            self.directional_frame.pack(fill="x")
        elif mode == "LEGACY":
            self.legacy_frame.pack(fill="x")
        elif mode == "CUSTOM_RANGE":
            self.custom_range_frame.pack(fill="x")

    def log(self, msg):
        # Buffer the message — flushed to log_box by the 150 ms _ui_flush loop.
        # Falls back to direct insert if called before build_gui initialises the buffer.
        buf = getattr(self, "_log_pending", None)
        if buf is not None:
            buf.append(f"{msg}\n")
        else:
            try:
                self.log_box.insert("end", f"{msg}\n")
                self.log_box.see("end")
            except Exception:
                pass

    def process_tick_entry(self, token, ltp):
        """Real-time entry check (no candle wait)"""
        if not getattr(self, "is_running", False):
            return
        try:
            st = self.strike_state.get(token)
            if not st:
                return

            # Block duplicate trades
            if st["trade_open"] or st["entry_taken_today"] or st["order_in_progress"]:
                return

            # Risk kill switch — refuse new entries when a limit is breached
            if self.risk_entry_blocked():
                return

            # ENTRY MODE LOGIC (REAL-TIME)
            entry_mode = self.entry_mode_var.get()
            entry_signal = False

            if entry_mode == "MARKET":
                entry_signal = True

            elif entry_mode == "PRICE_BAND":
                if self.entry_band_hit(ltp):
                    entry_signal = True

            elif entry_mode == "VWAP_RECLAIM":
                df = self.ce.get_candles(token)
                if df is not None:
                    ep = self.vwap_reclaim_entry(df)
                    if ep and ltp >= ep:
                        entry_signal = True

            # APPLY FILTERS ONLY IF ENABLED
            if self.enable_quant_var.get():
                df = self.ce.get_candles(token)
                if df is None or len(df) < 30:
                    return

                if not self.qfe.check_all_filters(df):
                    return

            # FINAL TRIGGER
            if entry_signal:
                print(f"🚀 REAL-TIME ENTRY TRIGGERED: {token} @ {ltp}")
                self.open_trade(token, ltp)

        except Exception as e:
            print("process_tick_entry error:", e)

