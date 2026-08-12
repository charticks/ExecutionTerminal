import types
import threading


class _Val:
    """Duck-types tk.Variable for frozen strategy context parameters."""
    def __init__(self, v):
        self._v = v

    def get(self):
        return self._v

    def set(self, v):
        self._v = v


class _EntryProxy:
    """Duck-types PlaceholderEntry widgets for strategy context."""
    def __init__(self, value=""):
        self._v = str(value) if value else ""

    def get(self):
        return self._v

    def get_value(self):
        return self._v if self._v else None

    def delete(self, *a):
        self._v = ""

    def insert(self, idx, val):
        self._v = str(val)

    def config(self, **kw):
        pass


# Mapping: preset JSON key → app attribute name (all *_var attributes)
_KEY_TO_ATTR = {
    "index":                  "index_var",
    "lots":                   "lots_var",
    "live_interval":          "live_interval_var",
    "trade_exec_mode":        "trade_exec_mode_var",
    "spot_detect_mode":       "spot_detect_mode_var",
    "ema_fast":               "ema_fast_var",
    "ema_slow":               "ema_slow_var",
    "spot_candle_tf":         "spot_candle_tf_var",
    "pdh_pdl_target":         "pdh_pdl_target_var",
    "pdh_value":              "pdh_value_var",
    "pdl_value":              "pdl_value_var",
    "entry_mode":             "entry_mode_var",
    "candle_sl_mode":         "candle_sl_mode_var",
    "sl_candle_tf":           "sl_candle_tf_var",
    "sl_ohlc_field":          "sl_ohlc_field_var",
    "target_ohlc_field":      "target_ohlc_field_var",
    "sl_pct_entry":           "sl_pct_entry_var",
    "target_pct_entry":       "target_pct_entry_var",
    "swing_lookback":         "swing_lookback_var",
    "enable_tsl":             "enable_tsl_var",
    "tsl_step":               "tsl_step_var",
    "gexp_override":          "gexp_override_var",
    "gexp_method":            "gexp_method_var",
    "gexp_sl_pct":            "gexp_sl_pct_var",
    "gexp_rr":                "gexp_rr_var",
    "gexp_tsl_step":          "gexp_tsl_step_var",
    "strike_mode":            "strike_mode_var",
    "use_strongest_strike":   "use_strongest_strike_var",
    "custom_range_from":      "custom_range_from_var",
    "custom_range_to":        "custom_range_to_var",
    "custom_range_ce":        "custom_range_ce_var",
    "custom_range_pe":        "custom_range_pe_var",
    "enable_quant":           "enable_quant_var",
    "filter_ema":             "filter_ema_var",
    "ema_f1":                 "ema_f1_var",
    "ema_f2":                 "ema_f2_var",
    "ema_f3":                 "ema_f3_var",
    "ema_alignment":          "ema_alignment_var",
    "filter_rsi":             "filter_rsi_var",
    "rsi_min_threshold":      "rsi_min_threshold_var",
    "filter_range":           "filter_range_var",
    "filter_volume":          "filter_volume_var",
    "filter_vwap":            "filter_vwap_var",
    "filter_gamma_trap":      "filter_gamma_trap_var",
    "filter_gamma_expansion": "filter_gamma_expansion_var",
    "filter_multi_bar":       "filter_multi_bar_var",
    "filter_time_window":     "filter_time_window_var",
    "time_window_start":      "time_window_start_var",
    "time_window_end":        "time_window_end_var",
    "time_window_eod":        "time_window_eod_var",
    "filter_consolidation":   "filter_consolidation_var",
    "consol_lookback":        "consol_lookback_var",
    "consol_atr_ratio":       "consol_atr_ratio_var",
    "filter_vol_ratio":       "filter_vol_ratio_var",
    "vol_ratio_min":          "vol_ratio_min_var",
    "vol_follow_through":     "vol_follow_through_var",
    "filter_body_quality":    "filter_body_quality_var",
    "body_quality_min":       "body_quality_min_var",
    "filter_adx":             "filter_adx_var",
    "adx_min":                "adx_min_var",
    "filter_supertrend":      "filter_supertrend_var",
    "filter_session_blocks":  "filter_session_blocks_var",
    "filter_oi":              "filter_oi_var",
    "oi_min":                 "oi_min_var",
    "filter_spread_guard":    "filter_spread_guard_var",
    "max_spread_pct":         "max_spread_pct_var",
    "ema_period":             "ema_period_var",
    "rsi_period":             "rsi_period_var",
}


class StrategyContext:
    """
    Proxy object that acts as 'app' for QuantFilterEngine, TradeExecutionEngine,
    and the OrderManager methods bound to it.

    Strategy-specific parameters are frozen from the preset's JSON params dict.
    All other attributes (root, smart, broker sessions, log(), oce, etc.) are
    transparently delegated to the real app via __getattr__.
    """

    def __init__(self, app, params: dict, preset_name: str):
        object.__setattr__(self, '_app', app)
        object.__setattr__(self, 'preset_name', preset_name)
        object.__setattr__(self, 'lock', threading.Lock())

        # ── Strategy-specific *_var replacements ──────────────────────────
        for key, attr in _KEY_TO_ATTR.items():
            if key in params:
                object.__setattr__(self, attr, _Val(params[key]))
            else:
                app_var = getattr(app, attr, None)
                if app_var is not None:
                    object.__setattr__(self, attr, _Val(app_var.get()))

        # ── Directional strike checkboxes proxy ───────────────────────────
        dir_params = params.get("directional_vars", {})
        dir_proxy = {}
        for level, d in getattr(app, "directional_vars", {}).items():
            pr = dir_params.get(level, {})
            dir_proxy[level] = {
                "CE": _Val(pr.get("CE", d["CE"].get())),
                "PE": _Val(pr.get("PE", d["PE"].get())),
            }
        object.__setattr__(self, "directional_vars", dir_proxy)

        # ── Legacy gap checkbox proxy ──────────────────────────────────────
        lgv_params = params.get("legacy_gap_vars", {})
        lgv_proxy = {}
        for k, v in getattr(app, "legacy_gap_vars", {}).items():
            lgv_proxy[k] = _Val(lgv_params.get(k, v.get()))
        object.__setattr__(self, "legacy_gap_vars", lgv_proxy)

        # ── Numeric entry fields proxy ─────────────────────────────────────
        ne_params = params.get("numeric_entries", {})
        ne_proxy = {}
        for k in getattr(app, "numeric_entries", {}):
            ne_proxy[k] = _EntryProxy(ne_params.get(k, ""))
        object.__setattr__(self, "numeric_entries", ne_proxy)

        # ── Spot entry proxy ──────────────────────────────────────────────
        object.__setattr__(self, "spot_entry", _EntryProxy(params.get("spot_time", "")))

        # ── Per-strategy trade state ──────────────────────────────────────
        object.__setattr__(self, "strike_state",      {})
        object.__setattr__(self, "running_positions", {})
        object.__setattr__(self, "completed_trades",  [])
        object.__setattr__(self, "cumulative_pnl",    0)

        # ── Manual order override slots ───────────────────────────────────
        object.__setattr__(self, "_manual_sl_override",  None)
        object.__setattr__(self, "_manual_tgt_override", None)
        object.__setattr__(self, "_manual_order_type",   None)
        object.__setattr__(self, "_manual_limit_price",  None)

        # ── CE reference (assigned by MultiStrategyRunnerMixin) ──────────
        object.__setattr__(self, "ce", None)

        # ── Bind mixin methods to this context ────────────────────────────
        self._bind_methods()

    def _bind_methods(self):
        from app.signal_filters import SignalFilterMixin
        from app.order_manager import OrderManagerMixin

        for cls, names in [
            (SignalFilterMixin, (
                "entry_band_hit",
                "time_window_filter",
                "consolidation_filter",
                "volume_surge_filter",
                "body_quality_filter",
                "multi_bar_momentum_filter",
                "gamma_expansion_detector",
            )),
            (OrderManagerMixin, (
                "open_trade",
                "place_order",
                "add_trade_row",
                "_get_candle_sl_target",
                "_with_retry",
                "poll_order_status",
            )),
        ]:
            for name in names:
                fn = getattr(cls, name, None)
                if fn is not None:
                    object.__setattr__(self, name, types.MethodType(fn, self))

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_app"), name)

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
