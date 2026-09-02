"""Entry-trigger modes — ported from `legacy/app/signal_filters.py`
(`entry_band_hit`, `vwap_reclaim_entry`, `vwap_squeeze_break_entry`) and
`QuantFilterEngine.process`'s entry-mode dispatch (`quant_filter_engine.py:92-114`).

`evaluate_entry` is the direct port of that dispatch block — same four
modes, same order, same semantics — including PRICE_BAND's one piece of
per-token state (`entry_band_triggered`, "once triggered, stays triggered
until the position opens or the instance restarts") which legacy also kept
on `strike_state`. Here it lives on the caller's own per-token state dict,
passed in and mutated in place, exactly as legacy's `st` was.
"""
from __future__ import annotations

import pandas as pd

MARKET = "MARKET"
PRICE_BAND = "PRICE_BAND"
VWAP_RECLAIM = "VWAP_RECLAIM"
VWAP_SQUEEZE_BREAK = "VWAP_SQUEEZE_BREAK"


def entry_band_hit(ltp: float, params: dict) -> bool:
    """PRICE_BAND mode's trigger: ltp within `numeric_entries.Entry Price`
    +/- `numeric_entries.Tolerance`. Both are strings in the legacy preset
    schema (an empty string means "not configured")."""
    entries = params.get("numeric_entries") or {}
    ep_raw = entries.get("Entry Price")
    tol_raw = entries.get("Tolerance")
    try:
        if ep_raw in (None, ""):
            return False
        if tol_raw in (None, ""):
            return False
        entry_price = float(ep_raw)
        tolerance = float(tol_raw)
    except (TypeError, ValueError):
        return False
    return (entry_price - tolerance) <= ltp <= (entry_price + tolerance)


def vwap_reclaim_entry(df: pd.DataFrame) -> float | None:
    """The trigger price once a reclaim above vwap_lower1 is confirmed by a
    bigger body than the prior candle, or None if not yet triggered."""
    if df is None or len(df) < 3:
        return None
    prev, last = df.iloc[-2], df.iloc[-1]
    if pd.isna(prev.get("vwap_lower1")) or pd.isna(last.get("vwap_lower1")):
        return None
    if prev["close"] < prev["vwap_lower1"] and last["close"] > last["vwap_lower1"]:
        body = abs(last["close"] - last["open"])
        prev_body = abs(prev["close"] - prev["open"])
        if body > prev_body:
            return float(last["high"])
    return None


def vwap_squeeze_break_entry(df: pd.DataFrame, params: dict) -> float | None:
    """A volatility squeeze (std AND atr both below their N-bar average)
    followed by a fresh breakout above vwap_upper1."""
    if df is None or len(df) < 3:
        return None
    n = int(params.get("squeeze_lookback", 10))
    if len(df) < n + 1:
        return None
    last, prev = df.iloc[-1], df.iloc[-2]
    window = df.iloc[-(n + 1):-1]
    for col in ("std", "atr", "vwap_upper1"):
        val = last.get(col, float("nan"))
        if pd.isna(val):
            return None
    if float(last["std"]) >= window["std"].mean():
        return None
    if float(last["atr"]) >= window["atr"].mean():
        return None
    upper1_now = float(last["vwap_upper1"])
    upper1_prev = float(prev.get("vwap_upper1", upper1_now))
    if last["close"] <= upper1_now or prev["close"] > upper1_prev:
        return None
    return float(last["high"])


def evaluate_entry(mode: str, ltp: float, df: pd.DataFrame, params: dict,
                   state: dict) -> bool:
    """The exact `process()` entry-mode dispatch. `state` is the caller's
    per-token state dict — PRICE_BAND mutates `state["entry_band_triggered"]`
    in place, the same "latch, don't re-check every candle" behaviour
    legacy had."""
    if mode == MARKET:
        return True

    if mode == PRICE_BAND:
        if not state.get("entry_band_triggered"):
            if entry_band_hit(ltp, params):
                state["entry_band_triggered"] = True
        return bool(state.get("entry_band_triggered"))

    if mode == VWAP_RECLAIM:
        ep = vwap_reclaim_entry(df)
        return ep is not None and ltp >= ep

    if mode == VWAP_SQUEEZE_BREAK:
        ep = vwap_squeeze_break_entry(df, params)
        return ep is not None and ltp >= ep

    return False
