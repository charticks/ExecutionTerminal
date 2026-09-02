"""Entry-signal filters — ported from
``legacy/engines/quant_filter_engine.py``'s ``QuantFilterEngine.check_all_filters``,
which is the LIVE-authoritative filter chain (confirmed: ``process()`` calls
it, not ``signal_filters.py``'s simpler, superseded ``SignalFilterMixin.
quant_filter``, which nothing in the live path ever called).

Each block below is one filter, named and gated by its own `filter_*`
boolean in `params` — the exact shape legacy already had, just factored into
named functions instead of inline `if app.filter_x_var.get():` blocks. Order
matters: `run_filters` walks them in the SAME short-circuit sequence
`check_all_filters` did, because a later filter's early-return semantics
were never independent of the ones before it in the original code (e.g. ADX
and Supertrend both read `direction`, set once per call).

`pre_filters` covers the gates `process()` applies BEFORE `check_all_filters`
even runs (time window, session blocks, OI, spread guard) — kept separate
because they run regardless of `enable_quant`, on data check_all_filters's
own blocks never see (an option tick's OI/bid/ask, wall-clock time), not the
candle frame.
"""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd

# ── pre-filters (process(), before check_all_filters runs) ─────────────────
# Session-quality blocks are hardcoded in legacy (not preset fields) — ported
# as constants, not params, for that reason.
_SESSION_BLOCKS = (
    (dt.time(9, 15), dt.time(9, 30)),    # market open chaos
    (dt.time(13, 0), dt.time(13, 15)),   # lunch volume thinning
)


def time_window_ok(now: dt.datetime, params: dict) -> bool:
    if not params.get("filter_time_window"):
        return True
    try:
        start = dt.datetime.strptime(params["time_window_start"], "%H:%M").time()
        end = dt.datetime.strptime(params["time_window_end"], "%H:%M").time()
    except (KeyError, ValueError, TypeError):
        return True
    bt = now.time()
    if start <= bt <= end:
        return True
    if params.get("time_window_eod"):
        if dt.time(14, 30) <= bt <= dt.time(15, 0):
            return True
    return False


def session_block_ok(now: dt.datetime, params: dict) -> bool:
    if not params.get("filter_session_blocks"):
        return True
    bt = now.time()
    return not any(s <= bt <= e for s, e in _SESSION_BLOCKS)


def oi_ok(oi: int | None, params: dict) -> bool:
    if not params.get("filter_oi"):
        return True
    oi_min = params.get("oi_min", 50000)
    return (oi or 0) >= oi_min


def spread_ok(bid: float | None, ask: float | None, params: dict) -> bool:
    if not params.get("filter_spread_guard"):
        return True
    if not bid or not ask or bid <= 0 or ask <= 0:
        return True   # no depth to judge — never block for data we don't have
    spread_pct = (ask - bid) / ask * 100
    max_spread = params.get("max_spread_pct", 2.0)
    return spread_pct <= max_spread


def pre_filters(now: dt.datetime, tick: dict, params: dict) -> tuple[bool, str]:
    """(ok, reason) — the four gates process() applies before entry-mode
    evaluation, in the same order legacy checked them."""
    if not time_window_ok(now, params):
        return False, "outside the configured entry time window"
    if not session_block_ok(now, params):
        return False, "inside a blocked session window"
    if not oi_ok(tick.get("oi"), params):
        return False, "open interest below the configured minimum"
    if not spread_ok(tick.get("bid"), tick.get("ask"), params):
        return False, "bid/ask spread exceeds the configured maximum"
    return True, ""


# ── quant filters (check_all_filters) ───────────────────────────────────────
def _ema_filter(df: pd.DataFrame, params: dict) -> bool:
    last = df.iloc[-1]
    cs = df["close"]
    p1, p2, p3 = params.get("ema_f1", 0), params.get("ema_f2", 0), params.get("ema_f3", 0)
    ema1 = cs.ewm(span=p1, adjust=False).mean().iloc[-1] if p1 > 0 else None
    ema2 = cs.ewm(span=p2, adjust=False).mean().iloc[-1] if p2 > 0 else None
    ema3 = cs.ewm(span=p3, adjust=False).mean().iloc[-1] if p3 > 0 else None
    if ema1 is not None and last["close"] < ema1:
        return False
    if ema2 is not None and last["close"] < ema2:
        return False
    if ema3 is not None and last["close"] < ema3:
        return False
    # Proximity: block a chase entry too far above EMA1. Not in the 103-key
    # preset schema (no current preset sets it) — off unless explicitly given.
    if params.get("ema_proximity") and ema1 is not None:
        tol = params.get("ema_proximity_tol", 0) / 100.0
        if last["close"] > ema1 * (1 + tol):
            return False
    if params.get("ema_alignment"):
        if ema1 is not None and ema2 is not None and ema1 <= ema2:
            return False
        if ema2 is not None and ema3 is not None and ema2 <= ema3:
            return False
    # EMA crossover: EMA1 must have crossed above EMA2 since 09:15 today. Also
    # not in the current preset schema — off unless explicitly given.
    if params.get("ema_crossover"):
        if p1 <= 0 or p2 <= 0:
            return False
        today = dt.datetime.now().date()
        market_open = dt.time(9, 15)
        if "time" not in df.columns:
            return False
        times = pd.to_datetime(df["time"])
        mask = (times.dt.date == today) & (times.dt.time >= market_open)
        if mask.sum() < 2:
            return False
        e1 = cs.ewm(span=p1, adjust=False).mean()[mask].reset_index(drop=True)
        e2 = cs.ewm(span=p2, adjust=False).mean()[mask].reset_index(drop=True)
        crossed = any(e1.iloc[i] <= e2.iloc[i] and e1.iloc[i + 1] > e2.iloc[i + 1]
                     for i in range(len(e1) - 1))
        if not crossed:
            return False
        if last["close"] < ema1:
            return False
        if ema2 is not None and last["close"] < ema2:
            return False
    return True


def _rsi_filter(df: pd.DataFrame, params: dict) -> bool:
    return float(df.iloc[-1]["rsi"]) >= params.get("rsi_min_threshold", 0)


def _range_filter(df: pd.DataFrame, params: dict) -> bool:
    recent = df.tail(5)
    rng = recent["high"].max() - recent["low"].min()
    return rng <= recent["close"].mean() * 0.8


def _volume_filter(df: pd.DataFrame, params: dict) -> bool:
    recent = df.tail(5)
    vmean = recent["volume"].mean()
    return df.iloc[-1]["volume"] <= vmean * 2


def vwap_band_touch_ok(last_row, level: str, tol_pct: float) -> bool:
    """True if price (close) is at/above the selected VWAP band within
    tolerance. The selected level is the ONLY threshold (no lower-1 floor).
    Shared by the live filter and (once ported) a backtest, exactly as
    legacy's own comment on this function describes."""
    tol = (tol_pct or 0) / 100.0
    price = float(last_row.get("close", float("nan")))
    l1, l2 = last_row.get("vwap_lower1", float("nan")), last_row.get("vwap_lower2", float("nan"))
    u1, u2 = last_row.get("vwap_upper1", float("nan")), last_row.get("vwap_upper2", float("nan"))
    vw = last_row.get("vwap", float("nan"))
    if any(math.isnan(v) for v in (price, l1, l2, u1, u2, vw)):
        return False
    band_map = {"lower2": l2, "lower1": l1, "vwap": vw, "upper1": u1, "upper2": u2}
    if level in band_map:
        return price >= band_map[level] * (1 - tol)
    if level == "either":
        return price >= u1 * (1 - tol) or price >= u2 * (1 - tol)
    return price >= u1 * (1 - tol)   # fallback to upper1, matching legacy


def _vwap_filter(df: pd.DataFrame, params: dict) -> bool:
    last = df.iloc[-1]
    band_width = df["vwap_upper1"] - df["vwap_lower1"]
    if band_width.tail(5).mean() > last["close"] * 0.12:
        return False
    level = params.get("vwap_band_level", "upper1")
    tol = params.get("vwap_band_tol", 2.0)
    return vwap_band_touch_ok(last, level, tol)


def _gamma_trap_filter(df: pd.DataFrame, params: dict) -> bool:
    last = df.iloc[-1]
    body = abs(last["close"] - last["open"])
    range_size = last["high"] - last["low"]
    return not (range_size > 0 and body / range_size < 0.35)


def gamma_expansion_detector(df: pd.DataFrame) -> bool:
    """Shared by the gamma-expansion FILTER below and (via risk_rules.py)
    GExp SL/Target derivation — one function, ported once."""
    if df is None or len(df) < 20:
        return False
    last, prev, recent = df.iloc[-1], df.iloc[-2], df.tail(5)
    if abs(last["close"] - last["open"]) < abs(prev["close"] - prev["open"]) * 1.3:
        return False
    vol_mean = recent["volume"].mean()
    if vol_mean == 0 or last["volume"] < vol_mean * 1.5:
        return False
    if last["close"] < last["vwap"]:
        return False
    if (last["high"] - last["low"]) < (recent["high"].max() - recent["low"].min()) * 0.25:
        return False
    return True


def _gamma_expansion_filter(df: pd.DataFrame, params: dict) -> bool:
    return gamma_expansion_detector(df)


def multi_bar_momentum_filter(df: pd.DataFrame, direction: str = "CE") -> bool:
    """True if 3 of the last 5 candles align with the trade direction.
    `direction` is accepted (matching legacy's signature) but the actual
    check is direction-agnostic in legacy too — both CE and PE read the same
    "bullish candle count" — ported as-is, not "fixed"."""
    if df is None or len(df) < 5:
        return False
    recent = df.tail(5)
    aligned = (recent["close"] > recent["open"]).sum()
    return aligned >= 3


def _multi_bar_filter(df: pd.DataFrame, params: dict, direction: str) -> bool:
    return multi_bar_momentum_filter(df, direction)


def _adx_filter(df: pd.DataFrame, params: dict) -> bool:
    adx_min = params.get("adx_min", 20)
    adx_val = float(df.iloc[-1]["adx"]) if "adx" in df.columns else 0.0
    return adx_val >= adx_min


def _supertrend_filter(df: pd.DataFrame, params: dict, direction: str) -> bool:
    if "supertrend_dir" not in df.columns:
        return True   # column not ready yet — do not block on missing data
    st_dir = int(df.iloc[-1]["supertrend_dir"])
    if direction == "CE" and st_dir != 1:
        return False
    if direction == "PE" and st_dir != -1:
        return False
    return True


def consolidation_filter(df: pd.DataFrame, params: dict) -> bool:
    n = params.get("consol_lookback", 7)
    if df is None or len(df) < n + 2:
        return False
    ratio_max = params.get("consol_atr_ratio", 0.6)
    prior = df.iloc[-(n + 1):-1]
    rng = prior["high"].max() - prior["low"].min()
    if "atr" in df.columns:
        atr_val = float(df.iloc[-1]["atr"])
        if atr_val > 0:
            return rng < ratio_max * atr_val
    mean_close = prior["close"].mean()
    if mean_close <= 0:
        return False
    return (rng / mean_close) * 100 < ratio_max


def volume_surge_filter(df: pd.DataFrame, params: dict) -> bool:
    if df is None or len(df) < 6:
        return False
    min_mult = params.get("vol_ratio_min", 1.0)
    last = df.iloc[-1]
    recent = df.iloc[-6:-1]
    vmean = recent["volume"].mean()
    if vmean <= 0:
        return False
    return last["volume"] >= vmean * min_mult


def body_quality_filter(df: pd.DataFrame, params: dict) -> bool:
    if df is None or len(df) < 1:
        return False
    last = df.iloc[-1]
    rng = last["high"] - last["low"]
    if rng <= 0:
        return False
    body = abs(last["close"] - last["open"])
    return (body / rng) >= params.get("body_quality_min", 0.0)


def run_filters(df: pd.DataFrame, params: dict, direction: str = "CE") -> bool:
    """The exact check_all_filters short-circuit chain, same order. `direction`
    is "CE" or "PE" — legacy set this from the position's own strike_state
    (`self._token_direction`) before calling; here it's passed explicitly
    since there's no shared mutable field to set it on."""
    if not params.get("enable_quant"):
        return True

    if params.get("filter_ema") and not _ema_filter(df, params):
        return False
    if params.get("filter_rsi") and not _rsi_filter(df, params):
        return False
    if params.get("filter_range") and not _range_filter(df, params):
        return False
    if params.get("filter_volume") and not _volume_filter(df, params):
        return False
    if params.get("filter_vwap") and not _vwap_filter(df, params):
        return False
    if params.get("filter_gamma_trap") and not _gamma_trap_filter(df, params):
        return False
    if params.get("filter_gamma_expansion") and not _gamma_expansion_filter(df, params):
        return False
    if params.get("filter_multi_bar") and not _multi_bar_filter(df, params, direction):
        return False
    if params.get("filter_adx") and not _adx_filter(df, params):
        return False
    if params.get("filter_supertrend") and not _supertrend_filter(df, params, direction):
        return False
    if params.get("filter_consolidation") and not consolidation_filter(df, params):
        return False
    if params.get("filter_vol_ratio") and not volume_surge_filter(df, params):
        return False
    if params.get("filter_body_quality") and not body_quality_filter(df, params):
        return False
    return True
