import threading
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import time
import re
import requests

import pandas as pd
import tkinter as tk
from tkinter import messagebox

import config


class OptionChainMixin:
    """Instrument helpers, option chain panel, token mapping, LTP polling."""

    def get_lot_size(self):
        idx = self.index_var.get().upper()
        return {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}.get(idx, 1)

    def is_market_open(self):
        now = dt.datetime.now().time()
        return dt.time(9, 15) <= now <= dt.time(15, 30)

    def update_expiry_list(self):
        if not self.instrument_master:
            return
        index = self.index_var.get().upper()
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

        def parse_expiry(e):
            for fmt in ("%d%b%Y", "%Y-%m-%d"):
                try:
                    return dt.datetime.strptime(e, fmt)
                except Exception:
                    pass
            return dt.datetime.max

        expiries = sorted(expiries_raw, key=parse_expiry)
        self.expiry_combo["values"] = expiries
        if expiries:
            self.expiry_var.set(expiries[0])
            # Rebuild option chain with the new default expiry
            self.root.after(100, self.refresh_option_chain)

    def get_market_tokens(self):
        tokens = {}
        crude_contracts = []
        for s in self.instrument_master:
            name = s.get("name", "").upper()
            exch = s.get("exch_seg", "")
            inst = s.get("instrumenttype", "")
            expiry = s.get("expiry")
            if name in ["NIFTY", "BANKNIFTY"] and exch == "NSE" and not expiry:
                tokens[name] = {"token": s.get("token"), "exchange": exch}
            if name == "SENSEX" and exch == "BSE" and not expiry:
                tokens["SENSEX"] = {"token": s.get("token"), "exchange": "BSE"}
            if name == "CRUDEOIL" and exch == "MCX" and inst == "FUTCOM":
                crude_contracts.append(s)
        nearest = self.get_nearest_crude_token()
        if nearest:
            tokens["CRUDEOIL"] = {"token": nearest["token"], "exchange": "MCX"}
        print("Market Tokens:", tokens)
        return tokens

    def get_nearest_crude_token(self):
        crude = [s for s in self.instrument_master
                 if s.get("name", "").upper() == "CRUDEOIL"
                 and s.get("exch_seg") == "MCX"
                 and s.get("instrumenttype", "") == "FUTCOM"
                 and "CRUDEOILM" not in s.get("symbol", "")]
        if not crude:
            return None
        crude.sort(key=lambda x: dt.datetime.strptime(x["expiry"], "%d%b%Y"))
        return crude[0]

    def get_spot_token(self):
        index = self.index_var.get().upper()
        for s in self.instrument_master:
            name   = s.get("name", "").upper()
            exch   = s.get("exch_seg", "")
            expiry = s.get("expiry")
            if index in ["NIFTY", "BANKNIFTY"]:
                if name == index and exch == "NSE" and not expiry:
                    return s.get("token"), "NSE"
            if index == "SENSEX":
                if name == "SENSEX" and exch == "BSE" and not expiry:
                    return s.get("token"), "BSE"
            if index == "CRUDEOIL":
                nearest = self.get_nearest_crude_token()
                if nearest:
                    return nearest["token"], "MCX"
        return None, None

    def _align_spot_to_candle_close(self, spot_time, interval_str):
        """
        Aligns a user-specified spot time to the CLOSE of the candle
        that contains that time for the given interval.

        Rules:
          interval=1min  spot=12:00 → candle 12:00 closes at 12:01 → use close of 12:00 candle
          interval=3min  spot=12:01 → candle boundary floor(12:01 / 3)*3 = 12:00 → use 12:00 candle close
          interval=3min  spot=12:04 → floor(12:04 / 3)*3 = 12:03 → use 12:03 candle close
          interval=5min  spot=09:22 → floor(09:22 / 5)*5 = 09:20 → use 09:20 candle close
        """
        total_minutes = spot_time.hour * 60 + spot_time.minute
        if interval_str == "1min":
            # For 1-min candles: candle that OPENED at spot_time closes 1 min later
            # We want the close price OF that candle — which equals the close
            # of the candle whose open == spot_time. No shift needed; we fetch
            # up-to spot_time and take the last complete candle.
            candle_open_minutes = total_minutes  # keep as-is
        elif interval_str == "3min":
            candle_open_minutes = (total_minutes // 3) * 3
        elif interval_str == "5min":
            candle_open_minutes = (total_minutes // 5) * 5
        else:
            candle_open_minutes = total_minutes

        aligned_hour   = candle_open_minutes // 60
        aligned_minute = candle_open_minutes % 60
        return dt.time(aligned_hour, aligned_minute)

    def get_spot_price_at_time(self, spot_time):
        """
        Fetch the CLOSE price of the candle whose open == aligned_candle_time.
        The alignment ensures we always read a fully-closed candle regardless
        of what spot_time the user entered.
        """
        token, exch = self.get_spot_token()
        if not token or not self.smart:
            # No Angel session — fall back directly to live WS cache
            index  = self.index_var.get().upper()
            cached = getattr(self, "market_ltp_cache", {}).get(index)
            return cached or None
        try:
            interval_str    = self.live_interval_var.get()   # "1min"/"3min"/"5min"
            aligned_time    = self._align_spot_to_candle_close(spot_time, interval_str)

            interval_map    = {"1min": "ONE_MINUTE", "3min": "THREE_MINUTE",
                               "5min": "FIVE_MINUTE"}
            api_interval    = interval_map.get(interval_str, "ONE_MINUTE")

            now             = dt.datetime.now()
            candle_open_dt  = now.replace(hour=aligned_time.hour,
                                          minute=aligned_time.minute,
                                          second=0, microsecond=0)
            # Fetch a window that covers the candle: from 2 bars before to 1 bar after
            fetch_from = candle_open_dt - dt.timedelta(minutes=5)
            fetch_to   = candle_open_dt + dt.timedelta(minutes=10)

            params = {
                "exchange":    exch,
                "symboltoken": token,
                "interval":    api_interval,
                "fromdate":    fetch_from.strftime("%Y-%m-%d %H:%M"),
                "todate":      fetch_to.strftime("%Y-%m-%d %H:%M"),
            }
            resp = self.smart.getCandleData(params)
            if resp and resp.get("status"):
                data = resp.get("data", [])
                if data:
                    # Find the candle whose open time == aligned candle_open_dt
                    for row in reversed(data):
                        try:
                            row_dt = pd.to_datetime(row[0])
                            if row_dt.tzinfo is not None:
                                row_dt = row_dt.tz_convert("Asia/Kolkata").tz_localize(None)
                            else:
                                row_dt += pd.Timedelta(hours=5, minutes=30)
                            row_dt_naive = row_dt.replace(second=0, microsecond=0)
                            if row_dt_naive == candle_open_dt:
                                close_price = float(row[4])
                                print(f"[SpotAlign] interval={interval_str} "
                                      f"user_spot={spot_time} → "
                                      f"candle_open={aligned_time} → close={close_price}")
                                return close_price
                        except Exception:
                            pass
                    # Fallback: last available candle close
                    return float(data[-1][4])
        except Exception as e:
            print("Spot fetch error:", e)
        # Fallback: use live market WebSocket LTP (already running before Start Bot)
        index  = self.index_var.get().upper()
        cached = getattr(self, "market_ltp_cache", {}).get(index)
        if cached:
            print(f"[SpotFallback] REST failed → using live WS LTP: {cached}")
            return cached
        return None

    def calculate_atm(self, spot):
        index = self.index_var.get().upper()
        step  = {"SENSEX": 100, "NIFTY": 50,
                 "BANKNIFTY": 100, "CRUDEOIL": 50}.get(index, 50)
        atm   = round(spot / step) * step
        return int(atm), step

    def is_institutional_round(self, atm):
        index = self.index_var.get().upper()
        mod   = {"SENSEX": 500, "NIFTY": 100, "BANKNIFTY": 500}.get(index, 1)
        return atm % mod == 0

    def generate_strikes(self, atm, step):
        mode    = self.strike_mode_var.get()
        index   = self.index_var.get().upper()
        strikes = []
        if mode == "ROUND":
            if self.is_institutional_round(atm):
                strikes += [(atm, "CE"), (atm, "PE")]
            return strikes
        if mode == "LEGACY":
            lmap = {"NIFTY":     {"ATM":0,"+1":50, "-1":-50,"+2":100,"-2":-100},
                    "BANKNIFTY": {"ATM":0,"+1":100,"-1":-100,"+2":200,"-2":-200},
                    "SENSEX":    {"ATM":0,"+1":100,"-1":-100,"+2":200,"-2":-200}}
            step_levels = lmap.get(index, {"ATM": 0})
            for level, gap in step_levels.items():
                if not self.legacy_gap_vars.get(level, tk.BooleanVar()).get():
                    continue
                sp = atm + gap
                if sp % step == 0:
                    strikes += [(sp, "CE"), (sp, "PE")]
            return strikes
        if mode == "RELATIVE":
            for level, types in self.directional_vars.items():
                for opt_type, var in types.items():
                    if not var.get():
                        continue
                    if level == "ATM":
                        sp = atm
                    else:
                        direction = 1 if level.startswith("+") else -1
                        gap = int(level.replace("+", "").replace("-", ""))
                        sp  = atm + direction * gap
                    if sp % step == 0:
                        strikes.append((sp, opt_type))
            return strikes
        if mode == "CUSTOM_RANGE":
            from_offset = self.custom_range_from_var.get()
            to_offset   = self.custom_range_to_var.get()
            include_ce  = self.custom_range_ce_var.get()
            include_pe  = self.custom_range_pe_var.get()
            lo = min(from_offset, to_offset)
            hi = max(from_offset, to_offset)
            offset = lo
            while offset <= hi:
                sp = atm + offset
                if sp % step == 0:
                    if include_ce:
                        strikes.append((sp, "CE"))
                    if include_pe:
                        strikes.append((sp, "PE"))
                offset += step
            return strikes
        return strikes

    def map_strikes_to_tokens(self, strikes):
        expiry = self.expiry_var.get()
        index  = self.index_var.get().upper()
        selected = []

        # Collect expiries available for this index so we can warn on mismatch
        available_expiries = sorted({
            s.get("expiry", "") for s in self.instrument_master
            if s.get("name", "").upper() == index
            and "OPT" in s.get("instrumenttype", "")
        })

        for strike, opt_type in strikes:
            for s in self.instrument_master:
                exch = s.get("exch_seg", "")
                if (s.get("name", "").upper() == index
                        and s.get("expiry") == expiry
                        and "OPT" in s.get("instrumenttype", "")):
                    if index in ["NIFTY", "BANKNIFTY"] and exch != "NFO":
                        continue
                    if index == "SENSEX" and exch != "BFO":
                        continue
                    raw_strike     = float(s.get("strike", 0))
                    display_strike = raw_strike / 100
                    if int(display_strike) == int(strike):
                        sym = s.get("symbol", "")
                        if opt_type == "CE" and sym.endswith("CE"):
                            selected.append((strike, opt_type,
                                             s.get("token"), sym))
                        if opt_type == "PE" and sym.endswith("PE"):
                            selected.append((strike, opt_type,
                                             s.get("token"), sym))

        if not selected and strikes:
            if not available_expiries:
                print(f"[TokenMap] ❌ No {index} OPT instruments found in master "
                      f"— reload instrument master (Settings → Refresh Master)")
            elif expiry not in available_expiries:
                print(f"[TokenMap] ❌ Expiry mismatch — UI has '{expiry}' but "
                      f"{index} expiries in master: {available_expiries[:5]}")
                print(f"[TokenMap]    Fix: select a valid expiry from the "
                      f"Expiry dropdown and refresh the Option Chain.")
            else:
                print(f"[TokenMap] ❌ Expiry '{expiry}' exists but no strikes "
                      f"matched — check strike step/mode settings.")

        return selected

    # ==========================================================
    # OPTION CHAIN PANEL  — build / refresh / select / add
    # ==========================================================

    def refresh_option_chain(self):
        """
        Build or rebuild the option chain rows from instrument_master.
        Shows ATM±10 strikes (21 rows) around ATM, or nothing if ATM unknown.
        Safe to call before login (uses master data only).
        """
        if not self.instrument_master:
            self.log("⚠️  Load instrument master first (click 'Load Master')")
            return

        index  = self.index_var.get().upper()
        expiry = self.expiry_var.get()
        if not expiry:
            self.log("⚠️  Select an expiry first")
            return

        # Determine exchange filter
        exch_filter = {"NIFTY": "NFO", "BANKNIFTY": "NFO",
                       "SENSEX": "BFO", "CRUDEOIL": "MCX"}.get(index, "NFO")

        # Collect all CE + PE entries for this index / expiry
        ce_map = {}   # strike_int → {token, symbol}
        pe_map = {}
        for s in self.instrument_master:
            if (s.get("name", "").upper() != index):
                continue
            if s.get("expiry") != expiry:
                continue
            if "OPT" not in s.get("instrumenttype", ""):
                continue
            if s.get("exch_seg", "") != exch_filter:
                continue
            raw_strike = float(s.get("strike", 0))
            disp_strike = int(raw_strike / 100)
            sym = s.get("symbol", "")
            tok = s.get("token", "")
            if sym.endswith("CE"):
                ce_map[disp_strike] = {"token": tok, "symbol": sym}
            elif sym.endswith("PE"):
                pe_map[disp_strike] = {"token": tok, "symbol": sym}

        all_strikes = sorted(set(list(ce_map.keys()) + list(pe_map.keys())))
        if not all_strikes:
            self.log(f"⚠️  No options found for {index} expiry {expiry}")
            return

        # Try to find ATM from market LTP label or last known spot
        atm = None
        try:
            ltp_txt = self.market_ltp_labels.get(index)
            if ltp_txt:
                raw = ltp_txt.cget("text")
                if raw not in ("--", ""):
                    spot = float(raw)
                    _, step = self.calculate_atm(spot)
                    atm, _ = self.calculate_atm(spot)
        except Exception:
            pass

        # Filter strikes symmetrically around ATM — count controlled by the
        # Strikes dropdown (5/10/15/20/25/30) instead of a fixed ATM±10.
        n = max(1, self.oc_strike_count_var.get())
        half = n // 2
        step = None
        if atm:
            _, step = self.calculate_atm(atm)
            wanted = {atm + (i * step) for i in range(-half, n - half)}
            display_strikes = sorted(s for s in all_strikes if s in wanted)
            if not display_strikes:            # fallback: closest n
                close_strikes = sorted(all_strikes, key=lambda x: abs(x - atm))
                display_strikes = sorted(close_strikes[:n])
        else:
            # ATM unknown (no Angel market feed) — show middle N strikes so
            # the chain is populated even when only Kotak/Dhan is logged in.
            mid = len(all_strikes) // 2
            display_strikes = all_strikes[max(0, mid - half): mid + (n - half)]

        # Build oc_data list
        self.oc_data = []
        for sp in display_strikes:
            ce_info = ce_map.get(sp, {})
            pe_info = pe_map.get(sp, {})
            self.oc_data.append({
                "strike"  : sp,
                "ce_token": ce_info.get("token", ""),
                "ce_sym"  : ce_info.get("symbol", ""),
                "pe_token": pe_info.get("token", ""),
                "pe_sym"  : pe_info.get("symbol", ""),
            })

        # Render rows
        self._render_oc_rows(atm, step)
        self.log(f"📊 Option chain loaded: {index} {expiry} "
                 f"({len(display_strikes)} strikes)")

        # ── Instant paint from tick_store (if WebSocket already has data) ──
        # Gives immediate LTPs on re-render without waiting for REST.
        # Check primary engine, supplemental OC engine, and the pre-bot OC WS.
        for _eng in [self.oce, getattr(self, "_oc_engine", None),
                     getattr(self, "_pre_bot_oce", None)]:
            if not _eng:
                continue
            for row in self.oc_data:
                for token in (row["ce_token"], row["pe_token"]):
                    if token:
                        cached = _eng.tick_store.get(token)
                        if cached and cached.get("ltp", 0) > 0:
                            self.update_oc_ltp(token, cached["ltp"])

        # ── Subscribe OC tokens to live WebSocket if bot is running ──
        # This ensures tick-by-tick LTP updates in the option chain panel.
        if self.is_running:
            exch_type_map = {"NFO": 2, "BFO": 4, "MCX": 5}
            exch_filter = {"NIFTY": "NFO", "BANKNIFTY": "NFO",
                           "SENSEX": "BFO", "CRUDEOIL": "MCX"}.get(index, "NFO")
            oc_tokens = [row[k] for row in self.oc_data
                         for k in ("ce_token", "pe_token") if row.get(k)]
            # Angel feed: add new tokens to primary WS
            if self.oce and hasattr(self.oce, 'sws') and self.oce.sws:
                _tlist = getattr(self.oce, '_token_list', None)
                known = set(_tlist[0]["tokens"]) if _tlist else set()
                new_tokens = [t for t in oc_tokens if t not in known]
                if new_tokens:
                    new_sub = [{"exchangeType": exch_type_map.get(exch_filter, 2),
                                 "tokens": new_tokens}]
                    try:
                        self.oce.sws.subscribe("optionchain_oc", 3, new_sub)
                        if _tlist:
                            _tlist[0]["tokens"].extend(new_tokens)
                        self.log(f"📡 OC subscribed live: {len(new_tokens)} new tokens")
                    except Exception as e:
                        self.log(f"⚠️  OC live subscription error: {e}")
            # Dhan/Kotak feed: add new tokens to supplemental Angel OC engine
            oc_eng = getattr(self, "_oc_engine", None)
            if oc_eng and hasattr(oc_eng, 'sws') and oc_eng.sws:
                _tlist2 = getattr(oc_eng, '_token_list', None)
                known2 = set(_tlist2[0]["tokens"]) if _tlist2 else set()
                new_tokens2 = [t for t in oc_tokens if t not in known2]
                if new_tokens2:
                    new_sub2 = [{"exchangeType": exch_type_map.get(exch_filter, 2),
                                  "tokens": new_tokens2}]
                    try:
                        oc_eng.sws.subscribe("optionchain_oc_refresh", 3, new_sub2)
                        if _tlist2:
                            _tlist2[0]["tokens"].extend(new_tokens2)
                        self.log(f"📡 OC supplemental WS subscribed: {len(new_tokens2)} new tokens")
                    except Exception as e:
                        self.log(f"⚠️  OC supplemental subscription error: {e}")

        # ── Pre-bot: keep the chain ticking live via WebSocket ───────────────
        # Connects once per login session; later calls just add new tokens
        # to the existing connection (no teardown/recreate — that pattern is
        # what caused reconnect storms/REST throttling previously).
        if self.is_logged_in and not self.is_running:
            self._start_pre_bot_oc_ws()

        # Immediately fetch LTPs in background (works on holidays via candle fallback,
        # and covers the brief window before the pre-bot WS delivers its first ticks)
        if self.is_logged_in and not self.is_running:
            threading.Thread(target=self.fetch_oc_ltps_rest,
                             daemon=True, name="oc-ltp-init").start()

    def _render_oc_rows(self, atm=None, step=None):
        """Destroy and rebuild all chain rows inside oc_inner."""
        for w in self.oc_inner.winfo_children():
            w.destroy()
        self.oc_row_frames.clear()
        self.oc_ltp_labels.clear()

        if not self.oc_data:
            tk.Label(self.oc_inner, text="No data",
                     bg="#1a1a2e", fg="#555577",
                     font=("Segoe UI", 8, "italic")).pack(pady=10)
            return

        side_filter = self.oc_side_filter_var.get()
        show_ce = side_filter != "PUT_ONLY"
        show_pe = side_filter != "CALL_ONLY"

        # Configure column weights via a grid container — collapse the
        # hidden side's column when Call-only / Put-only is selected.
        self.oc_inner.grid_columnconfigure(0, weight=2 if show_ce else 0)
        self.oc_inner.grid_columnconfigure(1, weight=3)
        self.oc_inner.grid_columnconfigure(2, weight=2 if show_pe else 0)
        if hasattr(self, "oc_col_hdr_labels"):
            for key, visible in (("CE", show_ce), ("PE", show_pe)):
                lbl = self.oc_col_hdr_labels.get(key)
                if lbl:
                    if visible:
                        lbl.grid()
                    else:
                        lbl.grid_remove()

        for row_idx, row in enumerate(self.oc_data):
            sp         = row["strike"]
            ce_token   = row["ce_token"]
            pe_token   = row["pe_token"]
            is_atm_row = (atm is not None and sp == atm)

            # Alternating row background
            row_bg = "#1e2e3e" if row_idx % 2 == 0 else "#1a2535"
            atm_bg = "#2e3a1a"   # greenish tint for ATM row
            cell_bg = atm_bg if is_atm_row else row_bg

            # Distance-from-ATM badge: actual point distance (not step index),
            # e.g. NIFTY ATM+1 strike (50pts away) shows "+50", BANKNIFTY/
            # SENSEX ATM+1 (100pts away) shows "+100". Shown once, on the
            # left side of the panel only (CE cell) — not duplicated on PE.
            dist_txt = ""
            if atm is not None:
                pt_diff = sp - atm
                dist_txt = "ATM" if pt_diff == 0 else f"{pt_diff:+d}"

            # ── CE cell (left) ────────────────────────────
            if show_ce:
                ce_cell = tk.Frame(self.oc_inner, bg=cell_bg, cursor="hand2")
                ce_cell.grid(row=row_idx, column=0, sticky="ew",
                             padx=(1, 0), pady=1)

                if dist_txt:
                    tk.Label(ce_cell, text=dist_txt, bg=cell_bg, fg="#666688",
                             font=("Segoe UI", 7), width=5, anchor="w"
                             ).pack(side="left", padx=(2, 0))

                if ce_token:
                    btn_b = tk.Button(ce_cell, text="B", width=2, bg="#00695c",
                                      fg="white", font=("Segoe UI", 7, "bold"),
                                      bd=0, cursor="hand2",
                                      command=lambda sp=sp, t=ce_token, sym=row["ce_sym"]:
                                      self._oc_quick_trade(sp, "CE", t, sym, "BUY"))
                    btn_b.pack(side="left", padx=(2, 0))
                    self._bind_hover(btn_b, "#00897b", "#00695c")

                if ce_token:
                    btn_s = tk.Button(ce_cell, text="S", width=2, bg="#b71c1c",
                                      fg="white", font=("Segoe UI", 7, "bold"),
                                      bd=0, cursor="hand2",
                                      command=lambda sp=sp, t=ce_token, sym=row["ce_sym"]:
                                      self._oc_quick_trade(sp, "CE", t, sym, "SELL"))
                    btn_s.pack(side="right", padx=(0, 2))
                    self._bind_hover(btn_s, "#d32f2f", "#b71c1c")

                ce_ltp_lbl = tk.Label(
                    ce_cell, text="--", bg=cell_bg, fg="#00e676",
                    font=("Segoe UI", 8), anchor="e", width=8)
                ce_ltp_lbl.pack(side="right", padx=4, pady=2)

                if ce_token:
                    self.oc_ltp_labels[ce_token] = ce_ltp_lbl
                    ce_cell.bind("<Button-1>",
                                 lambda e, sp=sp, t=ce_token,
                                 sym=row["ce_sym"]: self._oc_select(sp, "CE", t, sym))
                    ce_ltp_lbl.bind("<Button-1>",
                                    lambda e, sp=sp, t=ce_token,
                                    sym=row["ce_sym"]: self._oc_select(sp, "CE", t, sym))
            else:
                ce_cell = None

            # ── Strike cell (middle) ─────────────────────
            strike_fg = "#ffd740" if is_atm_row else "#cccccc"
            strike_font = ("Segoe UI", 8, "bold") if is_atm_row else ("Segoe UI", 8)
            strike_cell = tk.Frame(self.oc_inner, bg=cell_bg)
            strike_cell.grid(row=row_idx, column=1, sticky="ew",
                             padx=1, pady=1)
            atm_tag = "  ATM" if is_atm_row else ""
            tk.Label(strike_cell,
                     text=f"{sp:,}{atm_tag}",
                     bg=cell_bg,
                     fg=strike_fg, font=strike_font,
                     anchor="center").pack(fill="x", pady=2)

            # ── PE cell (right) ──────────────────────────
            if show_pe:
                pe_cell = tk.Frame(self.oc_inner, bg=cell_bg, cursor="hand2")
                pe_cell.grid(row=row_idx, column=2, sticky="ew",
                             padx=(0, 1), pady=1)

                if pe_token:
                    btn_b = tk.Button(pe_cell, text="B", width=2, bg="#00695c",
                                      fg="white", font=("Segoe UI", 7, "bold"),
                                      bd=0, cursor="hand2",
                                      command=lambda sp=sp, t=pe_token, sym=row["pe_sym"]:
                                      self._oc_quick_trade(sp, "PE", t, sym, "BUY"))
                    btn_b.pack(side="left", padx=(2, 0))
                    self._bind_hover(btn_b, "#00897b", "#00695c")

                pe_ltp_lbl = tk.Label(
                    pe_cell, text="--", bg=cell_bg, fg="#ff5252",
                    font=("Segoe UI", 8), anchor="w", width=8)
                pe_ltp_lbl.pack(side="left", padx=4, pady=2)

                if pe_token:
                    btn_s = tk.Button(pe_cell, text="S", width=2, bg="#b71c1c",
                                      fg="white", font=("Segoe UI", 7, "bold"),
                                      bd=0, cursor="hand2",
                                      command=lambda sp=sp, t=pe_token, sym=row["pe_sym"]:
                                      self._oc_quick_trade(sp, "PE", t, sym, "SELL"))
                    btn_s.pack(side="right", padx=(0, 2))
                    self._bind_hover(btn_s, "#d32f2f", "#b71c1c")

                if pe_token:
                    self.oc_ltp_labels[pe_token] = pe_ltp_lbl
                    pe_cell.bind("<Button-1>",
                                 lambda e, sp=sp, t=pe_token,
                                 sym=row["pe_sym"]: self._oc_select(sp, "PE", t, sym))
                    pe_ltp_lbl.bind("<Button-1>",
                                    lambda e, sp=sp, t=pe_token,
                                    sym=row["pe_sym"]: self._oc_select(sp, "PE", t, sym))
            else:
                pe_cell = None

            self.oc_row_frames[sp] = (ce_cell, strike_cell, pe_cell)

        # Freshly-built rows always use dark-theme literal colors — repaint
        # them to match if the app is currently in light mode.
        self._theme_repaint_subtree(self.oc_inner)

        # Scroll to ATM row if known
        if atm and atm in self.oc_row_frames:
            # Flush pending layout so bbox("all") reflects the new rows
            self.oc_inner.update_idletasks()
            self.oc_canvas.update_idletasks()
            self.oc_canvas.configure(
                scrollregion=self.oc_canvas.bbox("all"))
            total   = len(self.oc_data)
            atm_idx = next((i for i, r in enumerate(self.oc_data)
                            if r["strike"] == atm), 0)
            frac = atm_idx / max(total - 1, 1)
            # Centre ATM in the visible area rather than placing it near top
            self.oc_canvas.yview_moveto(max(0.0, frac - 0.35))

    def _on_oc_side_filter_change(self):
        """Call/Put-only radio changed — re-render with the new filter."""
        self.refresh_option_chain()

    def _bind_hover(self, widget, hover_bg, normal_bg):
        widget.bind("<Enter>", lambda e: widget.config(bg=hover_bg))
        widget.bind("<Leave>", lambda e: widget.config(bg=normal_bg))

    def _oc_quick_trade(self, strike, opt_type, token, symbol, side, is_hedge=False):
        """B/S buttons on an Option Chain row — selects the strike, ensures
        it's tracked in strike_state, and punches the trade directly via the
        same codepath the old Strike LTP panel buttons used."""
        self._oc_select(strike, opt_type, token, symbol)
        self.oc_action_var.set(side)

        if token not in self.strike_state:
            lots = max(1, self.oc_lots_var.get())
            index    = self.index_var.get().upper()
            exch_map = {"NIFTY": "NFO", "BANKNIFTY": "NFO",
                        "SENSEX": "BFO", "CRUDEOIL": "MCX"}
            exchange = exch_map.get(index, "NFO")
            lot_size = self.get_lot_size() * lots
            with self.lock:
                self.strike_state[token] = {
                    "exchange"          : exchange,
                    "tradingsymbol"     : symbol,
                    "lot_size"          : lot_size,
                    "strike"            : strike,
                    "type"              : opt_type,
                    "trade_open"        : False,
                    "direction"         : None,
                    "entry_taken_today" : False,
                    "order_in_progress" : False,
                    "entry_price"       : None,
                    "highest_price"     : 0,
                    "lowest_price"      : 0,
                    "sl"                : None,
                    "targets"           : [],
                    "targets_hit"       : [],
                    "entry_band_triggered": False,
                    "gexp_override"     : False,
                    "gexp_method"       : "none",
                    "gexp_tsl_step"     : 0,
                    "ltp"               : None,
                    "oc_action"         : side,
                }

        self.manual_punch_trade(token, side, transaction_type_label=side)

        # Clear the row highlight so the option chain looks clean after placing
        if not is_hedge:
            self._oc_clear_selection()

        # ── Hedge auto-pairing: a naked SELL gets an OTM-direction hedge BUY ──
        if side == "SELL" and not is_hedge and self.cfg_hedge_enable_var.get():
            offset = self.cfg_hedge_offset_var.get()
            hedge_strike = strike + offset if opt_type == "CE" else strike - offset
            hedge_row = next((r for r in self.oc_data if r["strike"] == hedge_strike), None)
            if hedge_row:
                h_token = hedge_row["ce_token"] if opt_type == "CE" else hedge_row["pe_token"]
                h_sym   = hedge_row["ce_sym"] if opt_type == "CE" else hedge_row["pe_sym"]
                if h_token:
                    import uuid
                    group_id = uuid.uuid4().hex[:8]
                    with self.lock:
                        if token in self.strike_state:
                            self.strike_state[token]["hedge_group_id"] = group_id
                    self._oc_quick_trade(hedge_strike, opt_type, h_token, h_sym,
                                         "BUY", is_hedge=True)
                    with self.lock:
                        if h_token in self.strike_state:
                            self.strike_state[h_token]["hedge_group_id"] = group_id
            else:
                self.log(f"⚠️  Hedge strike {hedge_strike} not found in current chain — hedge skipped")

    def _oc_paint_bg(self, widget, dark_literal):
        """Apply a dark-theme literal bg to widget, inverted live if the app
        is currently in light theme. _oc_select paints cell highlights with
        hardcoded dark literals straight via .config(bg=...) — bypassing
        the bg/fg theme-inversion walk entirely, so in light mode the cell
        stayed on its dark literal forever (a near-black bar swallowing its
        own text). Re-caching _theme_orig_bg here also keeps a later theme
        toggle correct from this point on."""
        try:
            widget._theme_orig_bg = dark_literal
            disp_bg = dark_literal
            if getattr(self, "current_theme", "dark") == "light":
                disp_bg = self._invert_color(widget, dark_literal, is_text=False)
            widget.config(bg=disp_bg)
        except Exception:
            pass

    def _oc_select(self, strike, opt_type, token, symbol):
        """Called when a CE or PE cell is clicked. Highlights the selection."""
        # Clear previous highlight
        if self.oc_selected_strike in self.oc_row_frames:
            ce_c, st_c, pe_c = self.oc_row_frames[self.oc_selected_strike]
            prev_atm = (self.oc_selected_strike ==
                        getattr(self, "_oc_atm_strike", None))
            restore_bg = "#2e3a1a" if prev_atm else "#1e2e3e"
            for cell in (ce_c, st_c, pe_c):
                self._oc_paint_bg(cell, restore_bg)
                try:
                    for child in cell.winfo_children():
                        self._oc_paint_bg(child, restore_bg)
                except Exception:
                    pass

        # Apply highlight to clicked side
        self.oc_selected_token  = token
        self.oc_selected_type   = opt_type
        self.oc_selected_strike = strike

        if strike in self.oc_row_frames:
            ce_c, st_c, pe_c = self.oc_row_frames[strike]
            hl_bg = "#1a3a28" if opt_type == "CE" else "#3a1a1a"
            target_cell = ce_c if opt_type == "CE" else pe_c
            self._oc_paint_bg(target_cell, hl_bg)
            try:
                for child in target_cell.winfo_children():
                    self._oc_paint_bg(child, hl_bg)
            except Exception:
                pass

        # Update selection label
        action = self.oc_action_var.get()
        self.oc_sel_label.config(
            text=f"Selected: {strike} {opt_type}  ({symbol})",
            fg="#ffd740")

    def _oc_clear_selection(self):
        """Restore the previously-highlighted row to its normal background so
        the option chain shows no selection after an order is placed."""
        if self.oc_selected_strike in self.oc_row_frames:
            ce_c, st_c, pe_c = self.oc_row_frames[self.oc_selected_strike]
            was_atm = (self.oc_selected_strike ==
                       getattr(self, "_oc_atm_strike", None))
            restore_bg = "#2e3a1a" if was_atm else "#1e2e3e"
            for cell in (ce_c, st_c, pe_c):
                if cell is None:
                    continue
                self._oc_paint_bg(cell, restore_bg)
                try:
                    for child in cell.winfo_children():
                        self._oc_paint_bg(child, restore_bg)
                except Exception:
                    pass
        self.oc_selected_strike = None
        self.oc_selected_token  = None
        self.oc_selected_type   = None

    def add_oc_strike(self):
        """
        Add the currently selected Option Chain strike to the bot.
        Works both BEFORE and DURING a live/paper session.
        """
        if not self.oc_selected_token:
            messagebox.showwarning("Option Chain",
                                   "Click a CE or PE cell first to select a strike.")
            return

        token      = self.oc_selected_token
        opt_type   = self.oc_selected_type
        strike     = self.oc_selected_strike
        lots       = max(1, self.oc_lots_var.get())
        action     = self.oc_action_var.get()

        # Find the matching row in oc_data for trading symbol
        sym = ""
        for row in self.oc_data:
            if opt_type == "CE" and row["ce_token"] == token:
                sym = row["ce_sym"]
                break
            if opt_type == "PE" and row["pe_token"] == token:
                sym = row["pe_sym"]
                break

        if not sym:
            messagebox.showerror("Option Chain",
                                 f"Could not find symbol for token {token}.")
            return

        index    = self.index_var.get().upper()
        exch_map = {"NIFTY": "NFO", "BANKNIFTY": "NFO",
                    "SENSEX": "BFO", "CRUDEOIL": "MCX"}
        exchange = exch_map.get(index, "NFO")
        lot_size = self.get_lot_size() * lots

        # Guard against duplicate token
        if token in self.strike_state:
            messagebox.showinfo("Option Chain",
                                f"{strike} {opt_type} is already in the strike panel.")
            return

        # ── Add to strike_state ───────────────────────────────
        with self.lock:
            self.strike_state[token] = {
                "exchange"          : exchange,
                "tradingsymbol"     : sym,
                "lot_size"          : lot_size,
                "strike"            : strike,
                "type"              : opt_type,
                "trade_open"        : False,
                "direction"         : None,
                "entry_taken_today" : False,
                "order_in_progress" : False,
                "entry_price"       : None,
                "highest_price"     : 0,
                "lowest_price"      : 0,
                "sl"                : None,
                "targets"           : [],
                "targets_hit"       : [],
                "entry_band_triggered": False,
                "gexp_override"     : False,
                "gexp_method"       : "none",
                "gexp_tsl_step"     : 0,
                "ltp"               : None,
                "oc_action"         : action,   # BUY or SELL — recorded for reference
            }

        # ── Add LTP row to Strike Panel ───────────────────────
        self._add_strike_ltp_row(token, strike, opt_type, lot_size)

        # ── If bot is running: hook into live engines ─────────
        if self.is_running and self.oce and self.ce:
            # Register with CandleEngine
            self.ce.register_token(token, lot_size=lot_size)

            # Angel WebSocket feed — subscribe new token directly
            if hasattr(self.oce, 'sws') and self.oce.sws:
                exch_type_map = {"NFO": 2, "BFO": 4, "MCX": 5}
                new_sub = [{"exchangeType": exch_type_map.get(exchange, 2),
                            "tokens": [token]}]
                try:
                    self.oce.sws.subscribe("optionchain_add", 3, new_sub)
                    self.log(f"📡 Subscribed live: {strike} {opt_type} ({token})")
                except Exception as e:
                    self.log(f"⚠️  Live subscription error for {token}: {e}")

            # Kotak feed — add new symbol to KotakDataEngine and subscribe
            elif hasattr(self.oce, 'kotak_to_angel'):
                try:
                    kotak_sym = self._angel_to_kotak_symbol(sym)
                    if kotak_sym and kotak_sym not in self.oce.kotak_to_angel:
                        self.oce.kotak_to_angel[kotak_sym] = token
                        exch_seg = getattr(self.oce, 'exchange_segment', 'nse_fo')
                        instruments = [{"instrument_token": kotak_sym,
                                        "exchange_segment": exch_seg}]
                        self.oce.client.subscribe(
                            instrument_tokens=instruments,
                            isIndex=False, isDepth=False)
                        self.log(f"📡 Kotak subscribed live: {strike} {opt_type}")
                    elif kotak_sym:
                        self.log(f"📡 Kotak already subscribed: {strike} {opt_type}")
                except Exception as e:
                    self.log(f"⚠️  Kotak live sub error: {e}")

            # Dhan/Kotak feed: subscribe new token to supplemental Angel OC engine
            oc_eng = getattr(self, '_oc_engine', None)
            if oc_eng and hasattr(oc_eng, 'sws') and oc_eng.sws:
                exch_type_map = {"NFO": 2, "BFO": 4, "MCX": 5}
                new_sub = [{"exchangeType": exch_type_map.get(exchange, 2),
                            "tokens": [token]}]
                try:
                    oc_eng.sws.subscribe("strike_add_oc", 3, new_sub)
                    self.log(f"📡 OC engine subscribed: {strike} {opt_type} ({token})")
                except Exception as e:
                    self.log(f"⚠️  OC engine sub error for {token}: {e}")

        # Seed LTP immediately from best available cache so panel shows value at once
        seed_ltp = None
        oc_eng = getattr(self, '_oc_engine', None)
        if oc_eng and hasattr(oc_eng, 'tick_store'):
            cached = oc_eng.tick_store.get(token)
            if cached and cached.get("ltp", 0) > 0:
                seed_ltp = cached["ltp"]
        if not seed_ltp and self.oce:
            seed_ltp = self.oce.get_ltp(token)
        if seed_ltp and seed_ltp > 0:
            with self.lock:
                self.strike_state[token]["ltp"] = seed_ltp
            lbl = self.strike_ltp_labels.get(token)
            if lbl:
                _buf = getattr(self, "_lbl_pending", None)
                if _buf is not None:
                    _buf[f"sl_{token}"] = (lbl, str(round(seed_ltp, 2)))
                else:
                    self.root.after(0, lambda l=lbl, v=seed_ltp: l.config(text=str(round(v, 2))))

        self.log(f"✅ Added from chain: {strike} {opt_type}  "
                 f"sym={sym}  lots={lots}  action={action}")
        self.status_var.set(
            f"Added {strike} {opt_type} ({action}) to bot ✅")

    def _add_strike_ltp_row(self, token, strike, opt_type, lot_size):
        """Strike LTP panel UI removed (item 14) — trades now go straight from
        the Option Chain B/S buttons (see _oc_quick_trade). Kept as a no-op
        since bot_lifecycle/multi_strategy_runner still call it on restore."""
        pass

    def update_oc_ltp(self, token, ltp):
        """
        Called from the tick callback to update a single LTP label in
        the option chain panel.  Writes into the shared _lbl_pending
        buffer; the 150 ms _ui_flush loop applies it on the main thread.
        """
        lbl = self.oc_ltp_labels.get(token)
        if lbl:
            buf = getattr(self, "_lbl_pending", None)
            if buf is not None:
                buf[f"oc_{token}"] = (lbl, f"{ltp:.2f}")
            else:
                self.root.after(0, lambda l=lbl, v=ltp: l.winfo_exists() and l.config(text=f"{v:.2f}"))

    def _fetch_oc_ltps_kotak_rest(self):
        """Kotak REST LTP snapshot — called when Angel One is not logged in.
        Uses NeoAPI quotes() to fetch current LTPs for all visible OC tokens."""
        try:
            if not getattr(self, "_kotak_scrip_cache", None):
                return
            index    = self.index_var.get().upper()
            exch_seg = {"SENSEX": "bse_fo", "CRUDEOIL": "mcx_fo"}.get(index, "nse_fo")
            oc_tokens = [row[k] for row in self.oc_data
                         for k in ("ce_token", "pe_token") if row.get(k)]
            kotak_map = self._build_kotak_token_map([], oc_tokens)
            if not kotak_map:
                return

            instruments = [
                {"instrument_token": sym, "exchange_segment": exch_seg}
                for sym in kotak_map.keys()
            ]
            result = self.kotak.quotes(
                instrument_tokens=instruments,
                quote_type="ltp",
                isIndex=False,
                isDepth=False,
            )
            # NeoAPI returns {"data": [{"instrument_token": sym, "last_traded_price": xxx}, ...]}
            data = result if isinstance(result, list) else (result or {}).get("data", [])
            if not data:
                return
            # Build kotak_sym → ltp lookup
            sym_to_ltp = {}
            for item in data:
                sym = str(item.get("instrument_token") or item.get("tk") or "")
                ltp = 0.0
                for key in ("last_traded_price", "ltp", "LTP", "last_price"):
                    v = item.get(key)
                    if v:
                        try:
                            ltp = float(v)
                            break
                        except (ValueError, TypeError):
                            pass
                if sym and ltp > 0:
                    sym_to_ltp[sym] = ltp

            # Map back to angel tokens and update OC labels
            for kotak_sym, angel_tok in kotak_map.items():
                ltp = sym_to_ltp.get(kotak_sym, 0.0)
                if ltp <= 0:
                    continue
                self.update_oc_ltp(angel_tok, ltp)
                with self.lock:
                    st = self.strike_state.get(angel_tok)
                    if st:
                        st["ltp"] = ltp
                lbl = self.oc_ltp_labels.get(angel_tok)
                if lbl:
                    v = ltp
                    self.root.after(0, lambda l=lbl, v=v:
                                    l.winfo_exists() and l.config(text=f"{v:.2f}"))
        except Exception as e:
            print(f"Kotak REST LTP snapshot error: {e}")

    def fetch_oc_ltps_rest(self):
        """
        Fetch LTPs for all visible OC tokens.

        Strategy (handles live sessions, after-hours AND market holidays):
          0. Check tick_store first — if WebSocket already has data, use it
             immediately and skip REST for those tokens.
          1. Batch call getMarketData("LTP") — one API call for up to 50 tokens.
             Returns last traded price even when market is closed.
          2. Any token that comes back with ltp == 0 (truly never traded today
             or data gap) falls back to getCandleData to get the most recent
             available close price from the last few trading days.
             Candle fallbacks run in PARALLEL (up to 8 concurrent) for speed.
          3. Labels are updated on the GUI thread via root.after.
        """
        if not self.is_logged_in or not self.oc_data:
            return
        # Kotak-only session: use Kotak REST quotes for the snapshot, then return.
        if not self.smart and getattr(self, "kotak_logged_in", False):
            threading.Thread(target=self._fetch_oc_ltps_kotak_rest,
                             daemon=True, name="kotak-ltp-snap").start()
            return
        # REST LTP fetch requires Angel One session; skip gracefully for other non-Angel
        if not self.smart:
            return
        if self.is_running:
            # Allow REST fallback if live feed has been frozen > 30 s.
            # For Dhan/Kotak feeds, use the supplemental Angel OC engine's
            # last_tick_time (it serves OC tokens), not the primary engine.
            oc_eng = getattr(self, "_oc_engine", None) or getattr(self, "oce", None)
            last_tick = getattr(oc_eng, "last_tick_time", None)
            if last_tick is None or (time.time() - last_tick) < 30:
                return   # live WebSocket is healthy — skip REST

        index    = self.index_var.get().upper()
        exch_map = {"NIFTY": "NFO", "BANKNIFTY": "NFO",
                    "SENSEX": "BFO", "CRUDEOIL": "MCX"}
        exchange = exch_map.get(index, "NFO")

        # Build full token list and token→label mapping
        token_lbl   = {}   # {token: tk.Label}
        token_sym   = {}   # {token: tradingsymbol}  — needed for candle fallback
        all_tokens  = []

        for row in self.oc_data:
            for token, sym in [(row["ce_token"], row["ce_sym"]),
                               (row["pe_token"], row["pe_sym"])]:
                if token and sym and token in self.oc_ltp_labels:
                    token_lbl[token]  = self.oc_ltp_labels[token]
                    token_sym[token]  = sym
                    all_tokens.append(token)

        if not all_tokens:
            return

        ltp_map = {}   # {token: float}

        def _sync_strike_ltp(tok, val):
            """Keep strike_state in sync so manual trade buttons (which read
            state['ltp']) never go stale just because the OC label was
            updated via REST while the state dict wasn't."""
            st = self.strike_state.get(tok)
            if st:
                st["ltp"] = val

        # ── Step 0: Check tick_store for already-cached WS data ─────────
        # Paint known values instantly; only REST-fetch what we don't have.
        if self.oce:
            for token in all_tokens:
                cached = self.oce.tick_store.get(token)
                if cached and cached.get("ltp", 0) > 0:
                    ltp_map[token] = cached["ltp"]
                    _sync_strike_ltp(token, cached["ltp"])
                    lbl = token_lbl.get(token)
                    if lbl:
                        v = cached["ltp"]
                        self.root.after(
                            0, lambda l=lbl, v=v: l.winfo_exists() and l.config(text=f"{v:.2f}"))

        # Tokens still needing REST fetch
        rest_tokens = [t for t in all_tokens if t not in ltp_map]
        if not rest_tokens:
            return  # tick_store had everything — done instantly

        # ── Step 1: Batch getMarketData (chunks of 50) ──────────────────────
        chunk_size = 50
        for i in range(0, len(rest_tokens), chunk_size):
            chunk = rest_tokens[i: i + chunk_size]
            try:
                resp = self.smart.getMarketData("LTP", {exchange: chunk})
                if resp and resp.get("status"):
                    for item in resp.get("data", {}).get("fetched", []):
                        tok = str(item.get("symbolToken", ""))
                        ltp = float(item.get("ltp", 0) or 0)
                        if tok:
                            ltp_map[tok] = ltp
            except Exception as e:
                print(f"getMarketData error (chunk {i}): {e}")
            time.sleep(0.02)

        # Paint REST results immediately (don't wait for candle fallback)
        for token in rest_tokens:
            ltp_val = ltp_map.get(token, 0)
            if ltp_val > 0:
                _sync_strike_ltp(token, ltp_val)
                lbl = token_lbl.get(token)
                if lbl:
                    self.root.after(
                        0, lambda l=lbl, v=ltp_val: l.winfo_exists() and l.config(text=f"{v:.2f}"))

        # ── Step 2: Candle fallback for tokens STILL at ltp == 0 ────────
        # Run all fallbacks in parallel — vastly faster than sequential calls.
        zero_tokens = [t for t in rest_tokens if ltp_map.get(t, 0) == 0]

        if zero_tokens:
            # Circuit breaker: if we hit a rate limit recently, skip candle
            # fallback entirely for 5 minutes to avoid an infinite error loop.
            _rate_limited_until = getattr(self, "_candle_rl_until", 0)
            if time.time() < _rate_limited_until:
                return

            now     = dt.datetime.now()
            from_dt = now - dt.timedelta(days=7)
            from_str = from_dt.strftime("%Y-%m-%d %H:%M")
            to_str   = now.strftime("%Y-%m-%d %H:%M")
            _rate_hit = False   # track if any call in this batch gets rate-limited

            def _candle_fetch(token):
                nonlocal _rate_hit
                sym = token_sym.get(token, "")
                if not sym:
                    return token, 0.0
                try:
                    params = {
                        "exchange"   : exchange,
                        "symboltoken": token,
                        "interval"   : "ONE_DAY",
                        "fromdate"   : from_str,
                        "todate"     : to_str,
                    }
                    resp = self.smart.getCandleData(params)
                    if resp and resp.get("status"):
                        candles = resp.get("data", [])
                        if candles:
                            last_close = float(candles[-1][4])
                            if last_close > 0:
                                return token, last_close
                except Exception as e:
                    err_s = str(e).lower()
                    if "access rate" in err_s or "access denied" in err_s:
                        _rate_hit = True   # signal circuit breaker
                    else:
                        print(f"Candle fallback error ({token}): {e}")
                return token, 0.0

            # Run up to 2 candle calls concurrently (avoid rate limiting)
            with ThreadPoolExecutor(max_workers=2) as pool:
                for token, price in pool.map(_candle_fetch, zero_tokens):
                    if price > 0:
                        ltp_map[token] = price
                        _sync_strike_ltp(token, price)
                        lbl = token_lbl.get(token)
                        if lbl:
                            self.root.after(
                                0, lambda l=lbl, v=price: l.config(
                                    text=f"{v:.2f}"))

            # If rate-limited this batch, pause candle fallback for 60 seconds
            if _rate_hit:
                self._candle_rl_until = time.time() + 60
                print("[OC] Angel rate limit hit — candle fallback paused 60s")

    def _resolve_live_ltp(self, token):
        """
        Best-effort live LTP lookup, checking every source before giving up:
        cached strike_state, every tick_store (primary/supplemental/pre-bot
        engines), then a one-shot REST call. Used by both the chain's B/S
        quick-trade buttons and the inline Manual Trade panel so neither
        fails just because strike_state['ltp'] hasn't been refreshed yet.
        """
        st  = self.strike_state.get(token)
        ltp = st.get("ltp") if st else None
        if ltp and ltp > 0:
            return ltp

        for eng in (self.oce, getattr(self, "_oc_engine", None),
                    getattr(self, "_pre_bot_oce", None)):
            if eng and hasattr(eng, "tick_store"):
                cached = eng.tick_store.get(token)
                if cached and cached.get("ltp", 0) > 0:
                    ltp = cached["ltp"]
                    if st:
                        st["ltp"] = ltp
                    return ltp

        # Last resort: one-shot Angel REST getMarketData.
        # BFO (SENSEX): getMarketData returns the index price, not the
        # option LTP — skip it there.
        exch = {"NIFTY": "NFO", "BANKNIFTY": "NFO",
                "SENSEX": "BFO", "CRUDEOIL": "MCX"}.get(
                    self.index_var.get().upper(), "NFO")
        if self.smart and exch != "BFO":
            try:
                resp = self.smart.getMarketData("LTP", {exch: [token]})
                if resp and resp.get("status"):
                    items = resp.get("data", {}).get("fetched", [])
                    if items:
                        ltp = float(items[0].get("ltp", 0) or 0)
                        if ltp > 0:
                            if st:
                                st["ltp"] = ltp
                            self.log(f"📡 REST LTP for {token}: {ltp:.2f}")
                            return ltp
            except Exception as e:
                self.log(f"⚠️  REST LTP fetch failed: {e}")

        return ltp or 0

    def _get_last_close_single(self, exchange, token):
        """
        Convenience helper — returns last available close for a single token.
        Used by add_oc_strike to immediately show a price on the newly added row.
        """
        try:
            now     = dt.datetime.now()
            from_dt = now - dt.timedelta(days=7)
            params  = {
                "exchange"   : exchange,
                "symboltoken": token,
                "interval"   : "ONE_DAY",
                "fromdate"   : from_dt.strftime("%Y-%m-%d %H:%M"),
                "todate"     : now.strftime("%Y-%m-%d %H:%M"),
            }
            resp = self.smart.getCandleData(params)
            if resp and resp.get("status"):
                candles = resp.get("data", [])
                if candles:
                    return float(candles[-1][4])
        except Exception:
            pass
        return None

    def start_oc_ltp_poll(self):
        """
        Start a periodic background thread that refreshes OC LTPs via REST.
        • Before bot starts  → polls every 10 s (REST batch + candle fallback)
        • Once bot is running → thread exits (WebSocket takes over completely)

        If oc_data is empty on the first call (loadmaster not done yet),
        retries every 2 s until data is available, then switches to 10 s cadence.
        """
        def _loop():
            # Wait until oc_data is populated (loadmaster may not have run yet)
            while self.is_logged_in:
                if self.oc_data:
                    break
                if self.is_running:
                    break   # bot started before chain loaded — WebSocket will cover it
                time.sleep(2)

            # First real fetch
            self.fetch_oc_ltps_rest()

            # Periodic refresh every 10 s while idle or when live feed is frozen
            while self.is_logged_in:
                time.sleep(10)
                self.fetch_oc_ltps_rest()
                # Exit loop only when bot stops (is_running flips False → REST takes over)
                # fetch_oc_ltps_rest() self-guards: skips when feed is healthy

        threading.Thread(target=_loop, daemon=True,
                         name="oc-ltp-poll").start()

    def _start_pre_bot_oc_ws(self):
        """
        Opens a lightweight WebSocket (using the selected Data Feed broker) so
        the Option Chain panel gets tick-by-tick LTPs before Start Bot is clicked.

        Angel: connects once per session and incrementally adds tokens (stable).
        Kotak/Dhan: restarts with the full current OC token set on each call
        (their SDKs don't have the same reconnect-storm issue as Angel's).
        The engine is torn down once the bot starts; the live pipeline's own
        WebSocket takes over (see _stop_all_pipelines).
        """
        if not self.is_logged_in or self.is_running:
            return

        feed = self.data_feed_broker_var.get()

        oc_tokens = [row[k] for row in self.oc_data
                     for k in ("ce_token", "pe_token") if row.get(k)]
        if not oc_tokens:
            return

        index = self.index_var.get().upper()
        exch_filter = {"NIFTY": "NFO", "BANKNIFTY": "NFO",
                       "SENSEX": "BFO", "CRUDEOIL": "MCX"}.get(index, "NFO")
        exch_type_map = {"NFO": 2, "BFO": 4, "MCX": 5}

        def _make_prebot_on_tick():
            def _on_tick(token, ltp, cum_vol, now):
                self.update_oc_ltp(token, ltp)
                with self.lock:
                    st = self.strike_state.get(token)
                    if st:
                        st["ltp"] = ltp
                if token in self.strike_ltp_labels:
                    lbl = self.strike_ltp_labels[token]
                    _buf = getattr(self, "_lbl_pending", None)
                    if _buf is not None:
                        _buf[f"sl_{token}"] = (lbl, str(round(ltp, 2)))
                    else:
                        self.root.after(0, lambda l=lbl, v=ltp: l.config(text=str(round(v, 2))))
                # Direct trade-row P&L update — works even before the bot starts
                # (self.tee may not exist yet for manual/pre-bot trades).
                row_id   = self._active_row_id.get(token)
                row_data = self.trade_rows.get(row_id) if row_id is not None else None

                # Diagnostic: first 3 ticks for tokens that have an active trade row.
                # Visible in the Logs panel — helps confirm ticks arrive and tokens match.
                _tdc = getattr(self, "_prebot_tick_dbg", {})
                if token in self._active_row_id and _tdc.get(token, 0) < 3:
                    _tdc[token] = _tdc.get(token, 0) + 1
                    self._prebot_tick_dbg = _tdc
                    _st_dbg   = self.strike_state.get(token, {})
                    _row_ok   = row_data is not None
                    _stat_ok  = row_data.get("status") == "RUNNING" if _row_ok else False
                    _open_ok  = _st_dbg.get("trade_open", False)
                    _lbuf = getattr(self, "_log_pending", None)
                    _dmsg = (f"🔍 PnL-tick#{_tdc[token]} tok={token} "
                             f"ltp={round(ltp,1)} row={'✓' if _row_ok else '✗'} "
                             f"status={'RUN' if _stat_ok else '-'} open={_open_ok}")
                    if _lbuf is not None:
                        _lbuf.append(f"{_dmsg}\n")
                    else:
                        print(_dmsg)

                if row_data and row_data.get("status") == "RUNNING":
                    _st = self.strike_state.get(token, {})
                    if _st.get("trade_open"):
                        entry    = row_data.get("entry_price", 0)
                        lot_size = _st.get("lot_size", 1)
                        is_short = _st.get("direction") == "SHORT"
                        pnl = round((entry - ltp) * lot_size, 2) if is_short \
                              else round((ltp - entry) * lot_size, 2)
                        fg_pnl = "#4caf50" if pnl >= 0 else "#ff5252"
                        _pbuf = getattr(self, "_pnl_pending", None)
                        if _pbuf is not None:
                            _pbuf[token] = (row_data["labels"], ltp, pnl, fg_pnl)
                        else:
                            _ls = row_data["labels"]
                            self.root.after(0, lambda ls=_ls, v=ltp, p=pnl, fg=fg_pnl: (
                                ls[5].config(text=str(round(v, 2))),
                                ls[8].config(text=f"{p:.2f}", fg=fg),
                            ))
                # Also forward to the live TEE (bot mode — avoids double-update when
                # tee.on_tick() fires for the same token via the live pipeline).
                _tee = getattr(self, "tee", None)
                if _tee:
                    _st = self.strike_state.get(token)
                    if _st and _st.get("trade_open"):
                        _tee.on_tick(token, ltp)
            return _on_tick

        # ── Angel One pre-bot WS ──────────────────────────────────
        if feed == "Angel One":
            if not self.angel_logged_in:
                return
            engine = getattr(self, "_pre_bot_oce", None)
            if engine is None:
                from engines.option_chain_engine import OptionChainEngine
                engine = OptionChainEngine(self.jwt_token, config.API_KEY,
                                           self.client_code, self.feed_token)
                engine.on_tick_cb = _make_prebot_on_tick()
                try:
                    engine.subscribe([{"exchangeType": exch_type_map.get(exch_filter, 2),
                                        "tokens": oc_tokens}])
                    self._pre_bot_oce = engine
                    self._all_oces.append(engine)
                    self.log(f"📡 Pre-bot Angel WS connected ({len(oc_tokens)} tokens)")
                except Exception as e:
                    self.log(f"⚠️  Pre-bot Angel WS connect failed: {e}")
                return
            # Already connected — add any new tokens
            if hasattr(engine, "sws") and engine.sws:
                tlist = getattr(engine, "_token_list", None)
                known = set(tlist[0]["tokens"]) if tlist else set()
                new_tokens = [t for t in oc_tokens if t not in known]
                if new_tokens:
                    new_sub = [{"exchangeType": exch_type_map.get(exch_filter, 2),
                                "tokens": new_tokens}]
                    try:
                        engine.sws.subscribe("prebot_oc", 3, new_sub)
                        if tlist:
                            tlist[0]["tokens"].extend(new_tokens)
                        self.log(f"📡 Pre-bot Angel WS: +{len(new_tokens)} new tokens")
                    except Exception as e:
                        self.log(f"⚠️  Pre-bot Angel WS subscribe error: {e}")

        # ── Kotak Neo pre-bot WS ──────────────────────────────────
        elif feed == "Kotak Neo":
            if not self.kotak_logged_in:
                return
            exch_seg  = {"SENSEX": "bse_fo", "CRUDEOIL": "mcx_fo"}.get(index, "nse_fo")
            kotak_map = self._build_kotak_token_map([], oc_tokens)
            if not kotak_map:
                self.log("⚠️  Kotak pre-bot: no OC tokens mapped — check scrip master / wait for cache")
                return
            from engines.kotak_data_engine import KotakDataEngine as _KDE
            old = getattr(self, "_pre_bot_oce", None)

            if old is not None and isinstance(old, _KDE) and old.running:
                old_set = set(old._kotak_symbols or [])
                new_set = set(kotak_map.keys())
                if old_set == new_set:
                    # Same tokens already live — nothing to do; prevents reconnect storm.
                    return
                # Token set changed (index/expiry switch): update in-place, no teardown.
                old.kotak_to_angel = kotak_map
                old.exchange_segment = exch_seg
                old.on_tick_cb = _make_prebot_on_tick()
                old.app_ref = self
                old.resubscribe(kotak_map)
                self.log(f"📡 Kotak pre-bot: resubscribed {len(kotak_map)} tokens")
                # Instant REST snapshot while WS reconnects (1 s delay lets resubscribe settle)
                self.root.after(1000, lambda: threading.Thread(
                    target=self._fetch_oc_ltps_kotak_rest,
                    daemon=True, name="kotak-ltp-snap").start())
            else:
                # First connect — create a fresh KDE.
                if old is not None:
                    try:
                        old.stop()
                    except Exception:
                        pass
                kde = _KDE(self.kotak, kotak_map, exchange_segment=exch_seg)
                kde.on_tick_cb = _make_prebot_on_tick()
                kde.app_ref = self   # enables Tkinter log panel output from KDE
                kde.subscribe(list(kotak_map.keys()))
                self._pre_bot_oce = kde
                self.active_feed_engine = kde
                self.root.after(0, lambda: self.feed_indicator.config(fg="green"))
                self.log(f"📡 Pre-bot Kotak WS connected ({len(kotak_map)} OC tokens)")
                # Instant REST snapshot so OC shows prices before first WS tick
                threading.Thread(target=self._fetch_oc_ltps_kotak_rest,
                                 daemon=True, name="kotak-ltp-snap").start()

        # ── Dhan HQ pre-bot WS ────────────────────────────────────
        elif feed == "Dhan HQ":
            if not self.dhan_logged_in:
                return
            # Stop old engine if any
            old = getattr(self, "_pre_bot_oce", None)
            if old is not None:
                try:
                    old.stop()
                except Exception:
                    pass
                self._pre_bot_oce = None
            from engines.dhan_data_engine import DhanDataEngine as _DDE
            dhan_exch_const = (_DDE.BSE_FNO if index in ("SENSEX", "BANKEX")
                               else _DDE.NSE_FNO)
            dhan_map = self._build_dhan_token_map([], oc_tokens)
            if not dhan_map:
                self.log("⚠️  Dhan pre-bot: no OC tokens mapped — check scrip master")
                return
            exch_map_dhan = {did: dhan_exch_const for did in dhan_map}
            dde = _DDE(self.dhan_context, dhan_map)
            dde.on_tick_cb = _make_prebot_on_tick()
            try:
                dde.subscribe(list(dhan_map.keys()), exch_map=exch_map_dhan)
                self._pre_bot_oce = dde
                self.log(f"📡 Pre-bot Dhan WS connected ({len(dhan_map)} OC tokens)")
            except Exception as e:
                self.log(f"⚠️  Pre-bot Dhan WS connect failed: {e}")

    # ==========================================================
    # MANUAL TRADE FROM OPTION CHAIN PANEL
    # ==========================================================
    def place_manual_trade(self):
        """
        Place a manual trade using the currently selected OC strike
        and the inline Manual Trade controls (order type, SL, target).
        """
        token = getattr(self, "oc_selected_token", None)
        if not token:
            self.log("⚠️  No strike selected in option chain")
            return

        st = self.strike_state.get(token)
        if not st:
            # Auto-add the strike silently so we have state
            self.add_oc_strike()
            st = self.strike_state.get(token)
            if not st:
                self.log("⚠️  Could not initialise strike state for manual trade")
                return

        if st.get("trade_open") or st.get("entry_taken_today"):
            self.log(f"⚠️  Trade already open or taken today for {token}")
            return

        order_type  = self.manual_order_type_var.get()
        limit_price = self.manual_limit_price_var.get()
        sl_pts      = self.manual_sl_pts_var.get()
        tgt_pts     = self.manual_tgt_pts_var.get()

        # Determine entry price
        if order_type == "LIMIT" and limit_price > 0:
            entry_price = limit_price
        else:
            ltp = self._resolve_live_ltp(token)
            if not ltp or ltp <= 0:
                self.log(f"⚠️  No LTP available for {token} — cannot place trade")
                return
            entry_price = ltp

        # Set override slots so open_trade() uses manual SL/Target
        self._manual_sl_override  = sl_pts
        self._manual_tgt_override = tgt_pts
        self._manual_order_type   = order_type
        self._manual_limit_price  = limit_price

        import threading
        threading.Thread(
            target=lambda: self.open_trade(token, entry_price),
            daemon=True, name="manual-oc-trade"
        ).start()

        # Clear overrides after a brief delay so open_trade() has started
        def _clear():
            import time; time.sleep(0.5)
            self._manual_sl_override  = None
            self._manual_tgt_override = None
        threading.Thread(target=_clear, daemon=True).start()

        self.log(f"🚀 Manual trade submitted: {st.get('tradingsymbol', token)} "
                 f"@ {entry_price}  SL={sl_pts}pts  Tgt={tgt_pts}pts  [{order_type}]")

