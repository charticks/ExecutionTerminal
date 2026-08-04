import json
import os
from tkinter import messagebox, simpledialog

STRATEGIES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "strategies"
)

_EMPTY_LABEL = "── no presets saved ──"


class StrategyManagerMixin:
    """Save / load named strategy presets to/from disk as JSON files."""

    # ── Var map ────────────────────────────────────────────────────

    def _strategy_var_map(self):
        """Return {key: tk_var} for every strategy-relevant variable."""
        return {
            # Index / lots / interval
            "index":                  self.index_var,
            "lots":                   self.lots_var,
            "live_interval":          self.live_interval_var,
            "trade_exec_mode":        self.trade_exec_mode_var,
            # Spot detection
            "spot_detect_mode":       self.spot_detect_mode_var,
            "ema_fast":               self.ema_fast_var,
            "ema_slow":               self.ema_slow_var,
            "spot_candle_tf":         self.spot_candle_tf_var,
            "pdh_pdl_target":         self.pdh_pdl_target_var,
            "pdh_value":              self.pdh_value_var,
            "pdl_value":              self.pdl_value_var,
            # Entry method
            "entry_mode":             self.entry_mode_var,
            # Candle SL / Target
            "candle_sl_mode":         self.candle_sl_mode_var,
            "sl_candle_tf":           self.sl_candle_tf_var,
            "sl_ohlc_field":          self.sl_ohlc_field_var,
            "target_ohlc_field":      self.target_ohlc_field_var,
            "sl_pct_entry":           self.sl_pct_entry_var,
            "target_pct_entry":       self.target_pct_entry_var,
            "swing_lookback":         self.swing_lookback_var,
            # Trade management
            "enable_tsl":             self.enable_tsl_var,
            "tsl_step":               self.tsl_step_var,
            "gexp_override":          self.gexp_override_var,
            "gexp_method":            self.gexp_method_var,
            "gexp_sl_pct":            self.gexp_sl_pct_var,
            "gexp_rr":                self.gexp_rr_var,
            "gexp_tsl_step":          self.gexp_tsl_step_var,
            # Strike selection
            "strike_mode":            self.strike_mode_var,
            "use_strongest_strike":   self.use_strongest_strike_var,
            "custom_range_from":      self.custom_range_from_var,
            "custom_range_to":        self.custom_range_to_var,
            "custom_range_ce":        self.custom_range_ce_var,
            "custom_range_pe":        self.custom_range_pe_var,
            # Quant filters — Phase 1
            "enable_quant":           self.enable_quant_var,
            "filter_ema":             self.filter_ema_var,
            "ema_f1":                 self.ema_f1_var,
            "ema_f2":                 self.ema_f2_var,
            "ema_f3":                 self.ema_f3_var,
            "ema_alignment":          self.ema_alignment_var,
            "filter_rsi":             self.filter_rsi_var,
            "rsi_min_threshold":      self.rsi_min_threshold_var,
            "filter_range":           self.filter_range_var,
            "filter_volume":          self.filter_volume_var,
            "filter_vwap":            self.filter_vwap_var,
            "filter_gamma_trap":      self.filter_gamma_trap_var,
            "filter_gamma_expansion": self.filter_gamma_expansion_var,
            "filter_multi_bar":       self.filter_multi_bar_var,
            # Quant filters — Phase 2
            "filter_time_window":     self.filter_time_window_var,
            "time_window_start":      self.time_window_start_var,
            "time_window_end":        self.time_window_end_var,
            "time_window_eod":        self.time_window_eod_var,
            "filter_consolidation":   self.filter_consolidation_var,
            "consol_lookback":        self.consol_lookback_var,
            "consol_atr_ratio":       self.consol_atr_ratio_var,
            "filter_vol_ratio":       self.filter_vol_ratio_var,
            "vol_ratio_min":          self.vol_ratio_min_var,
            "vol_follow_through":     self.vol_follow_through_var,
            "filter_body_quality":    self.filter_body_quality_var,
            "body_quality_min":       self.body_quality_min_var,
            # Quant filters — Phase 3
            "filter_adx":             self.filter_adx_var,
            "adx_min":                self.adx_min_var,
            "filter_supertrend":      self.filter_supertrend_var,
            "filter_session_blocks":  self.filter_session_blocks_var,
            "filter_oi":              self.filter_oi_var,
            "oi_min":                 self.oi_min_var,
            "filter_spread_guard":    self.filter_spread_guard_var,
            "max_spread_pct":         self.max_spread_pct_var,
            # Indicators
            "ema_period":             self.ema_period_var,
            "rsi_period":             self.rsi_period_var,
            # VWAP Band Touch sub-controls
            "vwap_band_level":        self.vwap_band_level_var,
            "vwap_band_tol":          self.vwap_band_tol_var,
            # SL / Target mode
            "sl_tgt_type":            self.sl_tgt_type_var,
        }

    # ── Collect / Apply ────────────────────────────────────────────

    def _collect_strategy_params(self):
        """Snapshot all current UI parameters into a serialisable dict."""
        params = {}
        for key, var in self._strategy_var_map().items():
            try:
                params[key] = var.get()
            except Exception:
                pass

        # Directional strike checkboxes
        params["directional_vars"] = {
            level: {"CE": d["CE"].get(), "PE": d["PE"].get()}
            for level, d in self.directional_vars.items()
        }

        # Legacy gap checkboxes
        params["legacy_gap_vars"] = {
            k: v.get() for k, v in self.legacy_gap_vars.items()
        }

        # Numeric entry fields (PlaceholderEntry)
        params["numeric_entries"] = {
            k: (e.get_value() or "") for k, e in self.numeric_entries.items()
        }

        # Spot time (PlaceholderEntry)
        params["spot_time"] = self.spot_entry.get_value() or self.spot_entry.get()

        return params

    def _apply_strategy_params(self, params):
        """Push a loaded params dict into every UI variable and refresh layouts."""
        for key, var in self._strategy_var_map().items():
            if key in params:
                try:
                    var.set(params[key])
                except Exception:
                    pass

        # Directional strike checkboxes
        if "directional_vars" in params:
            for level, d in params["directional_vars"].items():
                if level in self.directional_vars:
                    self.directional_vars[level]["CE"].set(d.get("CE", False))
                    self.directional_vars[level]["PE"].set(d.get("PE", False))

        # Legacy gap checkboxes
        if "legacy_gap_vars" in params:
            for k, v in params["legacy_gap_vars"].items():
                if k in self.legacy_gap_vars:
                    self.legacy_gap_vars[k].set(v)

        # Numeric entry fields
        if "numeric_entries" in params:
            for k, v in params["numeric_entries"].items():
                if k in self.numeric_entries:
                    e = self.numeric_entries[k]
                    e.delete(0, "end")
                    if v:
                        e.insert(0, str(v))
                        e.config(fg="#ffffff")
                    else:
                        e._put_placeholder()

        # Spot time
        if params.get("spot_time"):
            self.spot_entry.delete(0, "end")
            self.spot_entry.insert(0, params["spot_time"])
            self.spot_entry.config(fg="#ffffff")

        # Refresh all conditional UI panels
        self._on_entry_mode_change()
        self._on_candle_sl_mode_change()
        self._toggle_spot_detect_ui()
        self._toggle_quant_filters_ui()
        self._toggle_gexp_override_ui()
        self._toggle_vwap_band_ui()
        self._on_sl_tgt_type_change()
        self.update_gap_controls()

    # ── File I/O ───────────────────────────────────────────────────

    def save_strategy_to_file(self, name):
        """Serialise current params to strategies/<name>.json and return the path."""
        os.makedirs(STRATEGIES_DIR, exist_ok=True)
        path = os.path.join(STRATEGIES_DIR, f"{name}.json")
        params = self._collect_strategy_params()
        params["_name"] = name
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(params, fh, indent=2)
        return path

    def load_strategy_from_file(self, name):
        """Load strategies/<name>.json and apply all parameters to the UI."""
        path = os.path.join(STRATEGIES_DIR, f"{name}.json")
        if not os.path.exists(path):
            raise FileNotFoundError(f"Preset '{name}' not found at {path}")
        with open(path, encoding="utf-8") as fh:
            params = json.load(fh)
        self._apply_strategy_params(params)
        return params

    def list_strategy_files(self):
        """Return a sorted list of saved preset names (no .json extension)."""
        if not os.path.exists(STRATEGIES_DIR):
            return []
        return sorted(
            f[:-5] for f in os.listdir(STRATEGIES_DIR) if f.endswith(".json")
        )

    def delete_strategy_file(self, name):
        """Remove strategies/<name>.json from disk."""
        path = os.path.join(STRATEGIES_DIR, f"{name}.json")
        if os.path.exists(path):
            os.remove(path)

    # ── GUI actions ────────────────────────────────────────────────

    def save_preset_dialog(self):
        """Prompt for a name and save current UI settings as a preset."""
        name = simpledialog.askstring(
            "Save Strategy Preset",
            "Enter a name for this preset:",
            parent=self.root,
        )
        if not name:
            return
        name = name.strip().replace("/", "_").replace("\\", "_").replace(":", "_")
        if not name:
            return
        try:
            path = self.save_strategy_to_file(name)
            self._refresh_preset_dropdown(select=name)
            self.log(f"[Preset] Saved '{name}' → {path}")
            messagebox.showinfo(
                "Preset Saved", f"'{name}' saved successfully.", parent=self.root
            )
        except Exception as exc:
            messagebox.showerror("Save Error", str(exc), parent=self.root)

    def update_preset_action(self):
        """Overwrite the currently selected preset with the current UI settings."""
        name = self.strategy_select_var.get()
        if not name or name == _EMPTY_LABEL:
            messagebox.showwarning(
                "No Preset Selected", "Select a preset to update.", parent=self.root
            )
            return
        if not messagebox.askyesno(
            "Confirm Update",
            f"Overwrite preset '{name}' with the current UI settings?",
            parent=self.root,
        ):
            return
        try:
            path = self.save_strategy_to_file(name)
            self._refresh_preset_dropdown(select=name)
            self.log(f"[Preset] Updated '{name}' → {path}")
            messagebox.showinfo(
                "Preset Updated", f"'{name}' updated successfully.", parent=self.root
            )
        except Exception as exc:
            messagebox.showerror("Update Error", str(exc), parent=self.root)

    def load_preset_action(self):
        """Load and apply the preset currently selected in the dropdown."""
        name = self.strategy_select_var.get()
        if not name or name == _EMPTY_LABEL:
            messagebox.showwarning(
                "No Preset Selected", "Select a preset first.", parent=self.root
            )
            return
        try:
            self.load_strategy_from_file(name)
            self.log(f"[Preset] Loaded '{name}'")
            messagebox.showinfo(
                "Preset Loaded",
                f"'{name}' loaded successfully.",
                parent=self.root,
            )
        except Exception as exc:
            messagebox.showerror("Load Error", str(exc), parent=self.root)

    def delete_preset_action(self):
        """Delete the currently selected preset from disk after confirmation."""
        name = self.strategy_select_var.get()
        if not name or name == _EMPTY_LABEL:
            messagebox.showwarning(
                "No Preset Selected", "Select a preset to delete.", parent=self.root
            )
            return
        if not messagebox.askyesno(
            "Confirm Delete", f"Delete preset '{name}'?", parent=self.root
        ):
            return
        self.delete_strategy_file(name)
        self._refresh_preset_dropdown()
        self.log(f"[Preset] Deleted '{name}'")

    def _refresh_preset_dropdown(self, select=None):
        """Rebuild the dropdown values from disk and optionally select a name."""
        names = self.list_strategy_files()
        if names:
            self.preset_combo["values"] = names
            chosen = select if (select and select in names) else names[0]
            self.strategy_select_var.set(chosen)
        else:
            self.preset_combo["values"] = [_EMPTY_LABEL]
            self.strategy_select_var.set(_EMPTY_LABEL)
