import threading
import time
import os
import datetime as _dt
try:
    import winsound
except ImportError:
    winsound = None

import tkinter as tk

from app.gui_builder import GUIBuilderMixin
from app.login import LoginMixin
from app.option_chain import OptionChainMixin
from app.signal_filters import SignalFilterMixin
from app.order_manager import OrderManagerMixin
from app.bot_lifecycle import BotLifecycleMixin
from app.tick_logger import TickLoggerMixin
from app.backtest import BacktestMixin
from app.strategy_manager import StrategyManagerMixin
from app.multi_strategy_runner import MultiStrategyRunnerMixin


class Strategy2LiveBotApp(
    GUIBuilderMixin,
    LoginMixin,
    OptionChainMixin,
    SignalFilterMixin,
    OrderManagerMixin,
    BotLifecycleMixin,
    TickLoggerMixin,
    BacktestMixin,
    StrategyManagerMixin,
    MultiStrategyRunnerMixin,
):
    def __init__(self, root):
        self.root = root
        root.title("Charticks - Execution Terminal")
        root.update_idletasks()
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        w  = min(1440, max(1220, int(sw * 0.92)))
        h  = min(960,  max(750,  int(sh * 0.92)))
        x  = (sw - w) // 2
        y  = max(0, (sh - h) // 2 - 20)
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.minsize(1100, 680)

        # ── Core state ────────────────────────────────────────
        self.smart        = None
        self.is_logged_in = False
        self.is_running   = False

        self.instrument_master = []
        self.selected_strikes  = []
        self.strike_state      = {}   # {token: state_dict}

        self.jwt_token   = None
        self.feed_token  = None
        self.client_code = None

        # ── Engine references (created on start) ─────────────
        self.oce  = None   # OptionChainEngine (Angel primary)
        self.ce   = None   # CandleEngine (Angel primary)
        self.qfe  = None   # QuantFilterEngine (Angel primary)
        self._oc_engine = None  # Supplemental Angel OC WebSocket (Kotak/Dhan feed mode)
        self._pre_bot_oce = None  # Pre-bot Angel OC WebSocket (live OC ticks before Start Bot)
        self.tee  = None   # TradeExecutionEngine (Angel primary)
        self._all_oces: list = []  # registry of every OCE created; stopped in _stop_all_pipelines

        # ── Parallel broker pipelines ─────────────────────────
        # Each broker has its own CE / QFE / TEE + position state
        self._broker_pipelines   = {}   # {"angel": {...}, "kotak": {...}}
        self.angel_position_state = {}  # {token: position_dict} Angel-only trades
        self.kotak_position_state = {}  # {token: position_dict} Kotak-only trades

        # ── Market WebSocket (separate — for index LTP) ───────
        self.sws_market        = None
        self.market_ws_running    = False   # True while a tick stream is live (legacy flag)
        self.market_ws_should_run = False   # intent: keep the stream alive + auto-reconnect
        self.market_ws_connected  = False   # True between on_open and on_close
        self._market_generation        = 0      # bumps each (re)connect; guards stale callbacks
        self._market_reconnect_attempt = 0      # exponential-backoff counter
        self._market_reconnect_lock    = threading.Lock()
        self._market_rest_fallback_started = False
        self.market_token_map  = {}   # populated by start_market_ltp_stream
        self.market_change_labels = {}  # sym → (pts_lbl, pct_lbl, arrow_lbl); populated by build_gui
        self.market_ltp_cache  = {}   # {index_name: float_ltp}  — live WS fallback for spot fetch

        self.lock = threading.Lock()

        # ── Trade state ───────────────────────────────────────
        self.trade_log         = []
        self.running_positions = {}
        self.completed_trades  = []
        self.cumulative_pnl    = 0
        self.trades_today      = 0
        self._risk_halted      = False    # set True once any active limit is breached
        self._risk_halt_reason = ""
        self._risk_base_pnl    = 0.0     # snapshot on Apply; limits measure from here
        self._risk_peak_pnl    = 0.0     # highest PnL since last Apply (trail tracking)

        # ── Tick data capture ─────────────────────────────────
        self.tick_log           = {}          # {token: [tick_dict, ...]}
        self.tick_lock          = threading.Lock()
        self._tick_flush_stop   = threading.Event()
        self._tick_flush_thread = None

        # ── Adaptive ATM ──────────────────────────────────────
        self.last_adaptive_atm  = None
        self.adaptive_threshold = 100
        self.last_tick_time     = time.time()

        # ── Lots (before entering trade) ──────────────────────
        self.lots_var           = tk.IntVar(value=1)

        # ── Option Chain panel state ───────────────────────────
        self.oc_data            = []
        self.oc_selected_token  = None
        self.oc_selected_type   = None
        self.oc_selected_strike = None
        self.oc_lots_var        = tk.IntVar(value=1)
        self.oc_action_var      = tk.StringVar(value="BUY")
        self.oc_row_frames      = {}
        self.oc_ltp_labels      = {}
        self.oc_strike_count_var = tk.IntVar(value=10)
        self.oc_side_filter_var = tk.StringVar(value="BOTH")

        # ── Per-row lots in Strike LTP panel ──────────────────
        self.strike_row_lots_vars = {}   # {token: tk.IntVar}

        # ── Backtest state ────────────────────────────────────
        self.bt_index_df           = None
        self.bt_index_file_var     = tk.StringVar(value="")
        self.bt_options_folder_var = tk.StringVar(value="")
        self.bt_from_date_var      = tk.StringVar(value="2025-01-01")
        self.bt_to_date_var        = tk.StringVar(value="2025-12-31")
        self.bt_interval_var       = tk.StringVar(value="1min")
        self.bt_progress_var       = tk.StringVar(value="")
        self.bt_results            = []
        self.bt_frame              = None

        # ── GUI variables ─────────────────────────────────────
        self.index_var        = tk.StringVar(value="SENSEX")
        self.expiry_var       = tk.StringVar()
        self.enable_quant_var = tk.BooleanVar(value=False)
        self.ema_period_var   = tk.IntVar(value=20)
        self.rsi_period_var   = tk.IntVar(value=14)
        self.entry_mode_var   = tk.StringVar(value="PRICE_BAND")
        self.live_interval_var = tk.StringVar(value="3min")
        # Candle-based SL/Target (PRICE_BAND mode)
        self.candle_sl_mode_var    = tk.StringVar(value="points")  # "points"|"prev_ohlc"|"pct_entry"|"swing_low"
        self.sl_candle_tf_var      = tk.StringVar(value="5min")    # "1min"|"3min"|"5min"|"15min"
        self.sl_ohlc_field_var     = tk.StringVar(value="low")     # OHLC field for SL
        self.target_ohlc_field_var = tk.StringVar(value="high")    # OHLC field for Target
        self.sl_pct_entry_var      = tk.DoubleVar(value=2.0)       # SL % (pct_entry mode)
        self.target_pct_entry_var  = tk.DoubleVar(value=4.0)       # Target % (pct_entry mode)
        self.swing_lookback_var    = tk.IntVar(value=5)            # candle lookback (swing_low mode)
        self.trade_mode_var   = tk.StringVar(value="Paper")
        self.enable_tsl_var   = tk.BooleanVar(value=False)
        self.tsl_step_var     = tk.DoubleVar(value=10)
        self.status_var       = tk.StringVar(value="Idle")

        # ── Risk Management (kill switch) ──────────────────────
        self.risk_enabled_var        = tk.BooleanVar(value=False)   # master on/off
        self.risk_max_loss_on_var    = tk.BooleanVar(value=True)
        self.risk_max_loss_var       = tk.DoubleVar(value=5000.0)   # rupees (abs)
        self.risk_max_trades_on_var  = tk.BooleanVar(value=False)
        self.risk_max_trades_var     = tk.IntVar(value=10)
        self.risk_profit_on_var      = tk.BooleanVar(value=False)
        self.risk_profit_target_var  = tk.DoubleVar(value=10000.0)  # rupees
        self.risk_action_block_var   = tk.BooleanVar(value=True)    # block new entries
        self.risk_action_sqoff_var   = tk.BooleanVar(value=False)   # square off all
        self.risk_trail_loss_var     = tk.BooleanVar(value=False)   # trail max-loss floor
        self.roll_pos1_var          = tk.BooleanVar(value=False)    # show Roll 1 (1-step) buttons in trade panel
        self.roll_pos2_var          = tk.BooleanVar(value=False)    # show Roll 2 (2-step) buttons in trade panel
        self.strike_mode_var  = tk.StringVar(value="RELATIVE")
        self.use_strongest_strike_var = tk.BooleanVar(value=False)

        # ── Manual Trade Entry Controls ───────────────────────
        self.manual_order_type_var  = tk.StringVar(value="MARKET")
        self.manual_limit_price_var = tk.DoubleVar(value=0.0)
        self.manual_sl_pts_var      = tk.DoubleVar(value=50.0)
        self.manual_tgt_pts_var     = tk.DoubleVar(value=100.0)

        # ── Manual trade override slots (set by place_manual_trade) ──
        self._manual_sl_override    = None
        self._manual_tgt_override   = None
        self._manual_order_type     = None
        self._manual_limit_price    = None

        # ── Custom Range Strike Mode ──────────────────────────
        self.custom_range_from_var = tk.IntVar(value=-200)
        self.custom_range_to_var   = tk.IntVar(value=0)
        self.custom_range_ce_var   = tk.BooleanVar(value=True)
        self.custom_range_pe_var   = tk.BooleanVar(value=True)

        # ── Broker Selection (Live Trading) ───────────────────
        self.use_angel_var   = tk.BooleanVar(value=True)
        self.use_kotak_var   = tk.BooleanVar(value=False)
        self.use_dhan_var    = tk.BooleanVar(value=False)
        self.angel_logged_in = False
        self.kotak_logged_in = False
        self.dhan_logged_in  = False
        self.angel_api_ok    = True    # False = API unreachable; gates new order entry
        self._angel_sub_sessions = []   # list of SmartConnect instances
        self._kotak_sub_sessions = []   # list of NeoAPI instances
        self._dhan_sub_sessions  = []   # list of dhanhq instances
        self.dhan            = None    # dhanhq client instance
        self.dhan_context    = None    # DhanContext (for MarketFeed)
        self.dhan_position_state = {}
        self._dhan_token_cache   = {}  # {angel_token: dhan_security_id}

        # Which broker drives the live data pipeline (ticks → candles → OC display)
        # Selected via top-bar Data Feed dropdown; orders still go to all checked brokers
        self.data_feed_broker_var = tk.StringVar(value="Angel One")

        # oc_feed_var kept for backward compat — synced to data_feed_broker_var at bot start
        self.oc_feed_var = tk.StringVar(value="Angel One")

        # Pre-connect feed state (set by Connect button, reused by start_bot)
        self.selected_feed      = "Angel One"   # name of currently connected feed broker
        self.active_feed_engine = None      # pre-connected DhanDataEngine / KotakDataEngine
        self._dhan_scrip_master_path = None # path to cached Dhan scrip master CSV

        # Auto = signals fire trades automatically
        # Manual = WebSocket + candles run, but entries only via "Place Trade Now"
        self.trade_exec_mode_var = tk.StringVar(value="Auto")

        self.filter_ema_var             = tk.BooleanVar(value=False)
        self.ema_f1_var                 = tk.IntVar(value=20)
        self.ema_f2_var                 = tk.IntVar(value=50)
        self.ema_f3_var                 = tk.IntVar(value=0)
        self.ema_alignment_var          = tk.BooleanVar(value=False)
        self.ema_crossover_var          = tk.BooleanVar(value=False)
        self.ema_proximity_var          = tk.BooleanVar(value=False)
        self.ema_proximity_tol_var      = tk.DoubleVar(value=2.0)
        self.filter_rsi_var             = tk.BooleanVar(value=False)
        self.filter_range_var           = tk.BooleanVar(value=False)
        self.filter_volume_var          = tk.BooleanVar(value=False)
        self.filter_vwap_var            = tk.BooleanVar(value=False)
        self.filter_gamma_trap_var      = tk.BooleanVar(value=False)
        self.filter_gamma_expansion_var = tk.BooleanVar(value=False)
        self.rsi_min_threshold_var      = tk.IntVar(value=50)
        self.filter_multi_bar_var       = tk.BooleanVar(value=False)

        # ── Phase 2 pattern filters ───────────────────────────
        self.filter_time_window_var  = tk.BooleanVar(value=False)
        self.time_window_start_var   = tk.StringVar(value="11:30")
        self.time_window_end_var     = tk.StringVar(value="14:30")
        self.time_window_eod_var     = tk.BooleanVar(value=True)

        self.filter_consolidation_var = tk.BooleanVar(value=False)
        self.consol_lookback_var      = tk.IntVar(value=7)
        self.consol_atr_ratio_var     = tk.DoubleVar(value=0.6)

        self.filter_vol_ratio_var     = tk.BooleanVar(value=False)
        self.vol_ratio_min_var        = tk.DoubleVar(value=2.0)
        self.vol_follow_through_var   = tk.BooleanVar(value=True)

        self.filter_body_quality_var  = tk.BooleanVar(value=False)
        self.body_quality_min_var     = tk.DoubleVar(value=0.6)

        # ── Advanced quant filters (Phase 3) ──────────────────
        self.filter_adx_var           = tk.BooleanVar(value=False)
        self.adx_min_var              = tk.IntVar(value=20)

        self.filter_supertrend_var    = tk.BooleanVar(value=False)

        self.filter_session_blocks_var = tk.BooleanVar(value=True)

        self.filter_oi_var            = tk.BooleanVar(value=False)
        self.oi_min_var               = tk.IntVar(value=50000)

        self.filter_spread_guard_var  = tk.BooleanVar(value=False)
        self.max_spread_pct_var       = tk.DoubleVar(value=2.0)

        # ── VWAP Band Touch sub-controls ──────────────────────
        self.vwap_band_level_var      = tk.StringVar(value="upper1")   # lower2 | lower1 | vwap | upper1 | upper2 | either
        self.vwap_band_tol_var        = tk.DoubleVar(value=2.0)         # tolerance %
        self.squeeze_lookback_var     = tk.IntVar(value=10)             # bars for VWAP_SQUEEZE_BREAK detection

        # ── SL / Target mode ──────────────────────────────────
        self.sl_tgt_type_var          = tk.StringVar(value="pts")       # pts | pct

        # ── Order lifecycle tracking ───────────────────────────
        self.order_registry           = {}   # {token: {order_id, side, qty, status}}

        # ── Spot Detection Mode ───────────────────────────────
        self.spot_detect_mode_var   = tk.StringVar(value="TIME")
        self.ema_fast_var           = tk.IntVar(value=20)
        self.ema_slow_var           = tk.IntVar(value=50)
        self.spot_candle_tf_var     = tk.StringVar(value="3min")
        self.pdh_pdl_target_var     = tk.StringVar(value="BOTH")
        self.pdh_value_var          = tk.DoubleVar(value=0.0)
        self.pdl_value_var          = tk.DoubleVar(value=0.0)

        # ── Multi-Strategy parameters ─────────────────────────
        self.strategies          = []
        self.active_strategy_idx = tk.IntVar(value=0)

        self.gexp_override_var  = tk.BooleanVar(value=False)
        self.gexp_method_var    = tk.StringVar(value="approach1")
        self.gexp_sl_pct_var    = tk.DoubleVar(value=30)
        self.gexp_rr_var        = tk.DoubleVar(value=3)
        self.gexp_tsl_step_var  = tk.DoubleVar(value=20)

        # ── Tick save options ─────────────────────────────────
        self.save_ticks_var       = tk.BooleanVar(value=False)
        self.tick_save_format_var = tk.StringVar(value="CSV")
        self.tick_flush_mins_var  = tk.IntVar(value=15)

        default_path = r"D:\live trade result"
        if not os.path.exists(default_path):
            os.makedirs(default_path)
        self.output_folder = tk.StringVar(value=default_path)

        # ── Connection indicators (temp; replaced in build_gui) ─
        self.login_indicator  = tk.Label(root, text="●", fg="red",
                                         bg="#1e1e1e", font=("Segoe UI", 12))
        self.master_indicator = tk.Label(root, text="●", fg="red",
                                         bg="#1e1e1e", font=("Segoe UI", 12))
        self.start_indicator  = tk.Label(root, text="●", fg="red",
                                         bg="#1e1e1e", font=("Segoe UI", 12))

        # ── Strategy preset selector (used by StrategyManagerMixin) ──
        self.strategy_select_var = tk.StringVar(value="── no presets saved ──")

        # ── Multi-strategy run queue ───────────────────────────────────
        self.preset_run_queue = []   # list of preset names to run simultaneously

        # ── Config Panel (additive — Order Params / Hedges / Risk / Profile) ──
        self.cfg_max_qty_per_order_var = tk.IntVar(value=0)      # 0 = no cap
        self.cfg_max_price_var         = tk.DoubleVar(value=0.0) # 0 = no cap
        self.cfg_wait_var              = tk.IntVar(value=0)      # seconds between orders
        self.cfg_max_adj_var           = tk.IntVar(value=0)      # max SL/Tgt adjustments
        self.cfg_default_qty_var       = tk.IntVar(value=1)
        self.cfg_limit_order_pct_var   = tk.DoubleVar(value=1.0)
        self.cfg_order_type_var        = tk.StringVar(value="MARKET")
        self.cfg_sl_pts_var            = tk.DoubleVar(value=50.0)
        self.cfg_target_pts_var        = tk.DoubleVar(value=100.0)

        self.cfg_hedge_enable_var    = tk.BooleanVar(value=False)
        self.cfg_hedge_offset_var    = tk.IntVar(value=100)
        self.cfg_hedge_retry_var     = tk.BooleanVar(value=False)
        self.cfg_exit_hedges_var     = tk.BooleanVar(value=True)
        self.cfg_broker_select_var   = tk.StringVar(value="Angel One")

        # Same canonical Variables the Active Trades "Risk Limits" row
        # enforces (risk_max_loss_var / risk_max_trades_var, defined above)
        # — shared rather than duplicated so the Config panel's values and
        # the enforced values can never drift out of sync.
        self.cfg_max_loss_var  = self.risk_max_loss_var
        self.cfg_max_order_var = self.risk_max_trades_var

        self.cfg_profile_day_var = tk.StringVar(value="Mon")

        self.current_theme = "light"   # default to light theme on startup
        self.build_gui()
        self.root.after(100, self.apply_theme)  # apply after all widgets are mapped

        # ── Market open/close bell notifications (item 8) ──────────────
        self._bell_fired_date = {"open": None, "close": None}
        self.root.after(1000, self._check_market_bell)

        # Save open-position state on window close (crash-recovery foundation).
        try:
            self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)
        except Exception:
            pass

    def _check_market_bell(self):
        """Poll once a second for 09:15:00 (open) / 15:30:00 (close) and ring
        a bell exactly once per event per day. Re-schedules itself."""
        now = _dt.datetime.now()
        today = now.date()
        if (now.hour, now.minute) == (9, 15) and self._bell_fired_date["open"] != today:
            self._bell_fired_date["open"] = today
            self._ring_bell()
            self.status_var.set("🔔 Market Open — 09:15")
        elif (now.hour, now.minute) == (15, 30) and self._bell_fired_date["close"] != today:
            self._bell_fired_date["close"] = today
            self._ring_bell()
            self.status_var.set("🔔 Market Close — 15:30")
        self.root.after(1000, self._check_market_bell)

    def _ring_bell(self, _n=3):
        """Chain a few short beeps via root.after (non-blocking — avoids
        freezing the Tk mainloop with time.sleep)."""
        if winsound is None or _n <= 0:
            return
        try:
            winsound.Beep(1000, 150)
        except Exception:
            pass
        self.root.after(180, lambda: self._ring_bell(_n - 1))

    def _on_window_close(self):
        """Persist state before the window is destroyed. Positions stay live at
        the broker and are reconciled on next launch."""
        try:
            self._save_state()
        except Exception:
            pass
        self.root.destroy()
