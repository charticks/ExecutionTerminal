"""SL/Target derivation — ported from legacy/app/order_manager.py's
`_get_candle_sl_target` and `open_trade`'s GExp block, for the modes that
are just an ABSOLUTE PRICE computed once at entry (prev-candle OHLC field,
swing-low, plain percent, GExp approach-1's fixed R:R target).

Design Decision A (see the plan doc): every mode here resolves to a
points-offset from entry — exactly what LiveManager's existing rule schema
(`slMode: "points" | "percent"`) already understands. There is no new rule
type and no change to LiveManager: a strategy's `rule` dict looks identical
to one a manually-placed order carries.

Two findings from reading `open_trade`'s real math (order_manager.py:1011-1025)
worth recording here, since they are not obvious from the legacy field names:
- `pct_entry` mode is already just a percent-offset under the hood:
  `sl = price * (1 - sl_pct/100)` is IDENTICAL to LiveManager's own
  `slMode: "percent"` formula for both legs — no new SL mode needed,
  `build_rule`'s existing percent pass-through covers it exactly.
- GExp approach-1 LOOKS like the same shortcut (`target = price +
  (price - sl) * rr` "reduces to" a target percent of `sl_pct * rr`) but
  is NOT — legacy rounds `sl` to 2dp before computing `risk = price - sl`,
  so the percent-mode shortcut and the literal formula diverge by a couple
  of paise. Caught by the end-to-end test, not by inspection. Use
  `gexp_approach1_rule` below, which reproduces the literal arithmetic —
  do not "simplify" it back to a percent pass-through.
- GExp approach-2 and VWAP-adaptive trailing are the only genuinely
  CONTINUOUS (tick-driven) modes — a strategy calls `StrategyContext.
  set_risk` on each relevant tick/candle-close for these, the same API
  manual SL-editing already uses. `gexp_approach2_trail_sl` below is the
  small, stateless piece of that math; the tick-loop itself lives in
  `quant_preset.py`, not here. VWAP-adaptive is unused by any of the 24
  current presets and stays deferred (also: legacy gives it TWO targets,
  which LiveManager's single-`targetVal` schema cannot express as-is —
  a real limitation to revisit only if a future preset needs it).
"""
from __future__ import annotations

from typing import Any

import pandas as pd

MIN_PRICE = 0.05   # a price can never be zero or negative — matches paper_engine.MIN_PRICE


def _clip(price: float) -> float:
    return round(max(MIN_PRICE, price), 2)


def points_offset(entry: float, price: float, side: str, kind: str) -> float | None:
    """The positive points-offset LiveManager's `slVal`/`targetVal` want, or
    None if `price` is on the wrong side of entry to serve as `kind`
    ("sl" or "target").

    Legacy validates this before ever sending an order ("validate-then-buy",
    order_manager.py:1041-1053) — a derived stop above entry on a BUY is not
    a smaller mistake than no stop at all, it is a stop pointed the wrong
    way, and LiveManager would arm it exactly as configured. Returning None
    here is what lets the caller fall back to "no SL/Target for this leg"
    rather than invent one on the wrong side.
    """
    if side == "BUY":
        valid = price < entry if kind == "sl" else price > entry
    else:
        valid = price > entry if kind == "sl" else price < entry
    if not valid:
        return None
    return round(abs(entry - price), 2)


def prev_ohlc_price(candles: pd.DataFrame | None, field: str) -> float | None:
    """A field (open/high/low/close) of the most recently closed candle —
    the caller passes the frame as of the candle that just triggered entry,
    so `iloc[-1]` IS that "previous" candle relative to the trade about to
    be placed."""
    if candles is None or candles.empty or field not in candles.columns:
        return None
    value = candles.iloc[-1][field]
    return None if pd.isna(value) else float(value)


def swing_low_price(candles: pd.DataFrame | None, lookback: int) -> float | None:
    """The lowest LOW over the last `lookback` closed candles — legacy has
    no equivalent "swing high" for a target, only this, for SL."""
    if candles is None or candles.empty:
        return None
    window = candles.tail(max(1, lookback))
    if window.empty or window["low"].isna().all():
        return None
    return float(window["low"].min())


def gexp_approach1_rule(price: float, sl_pct: float, rr: float) -> dict:
    """GExp approach-1's fixed SL%/R:R, ported LITERALLY from
    order_manager.py:1013-1018 — including its intermediate rounding step.

    Earlier draft of this function took a shortcut: since `sl = price*(1-p)`
    is algebraically the same shape as LiveManager's own percent mode, and
    `target = price + (price-sl)*rr` reduces to a target percent of
    `sl_pct*rr`, it seemed unnecessary to special-case this. It was wrong by
    up to a couple of paise per trade — legacy ROUNDS `sl` to 2dp BEFORE
    computing `risk = price - sl`, and that rounding step means the
    percent-mode shortcut and legacy's actual arithmetic are close but not
    identical. Caught by the end-to-end test asserting against the literal
    formula, not the shortcut. Kept as a lesson in this docstring: a
    "this reduces to X" simplification needs the actual arithmetic checked,
    not just the algebra.
    """
    sl_price = round(price * (1 - sl_pct / 100), 2)
    risk = price - sl_price
    target_price = round(price + risk * rr, 2)
    rule: dict[str, Any] = {"slEnabled": False, "targetEnabled": False}
    sl_offset = points_offset(price, sl_price, "BUY", "sl")
    if sl_offset is not None:
        rule["slEnabled"] = True
        rule["slMode"], rule["slVal"] = "points", sl_offset
    target_offset = points_offset(price, target_price, "BUY", "target")
    if target_offset is not None:
        rule["targetEnabled"] = True
        rule["targetMode"], rule["targetVal"] = "points", target_offset
    return rule


def gexp_approach2_trail_sl(highest_price: float, gexp_tsl_step: float) -> float:
    """GExp approach-2's continuous trail: stop = highest price seen since
    entry, minus a fixed step — ported from trade_execution_engine.py:91-96.
    The caller updates `highest_price` on every new high and calls this
    (then `StrategyContext.set_risk`) each time; `live_book.set_risk`'s own
    "trailing only ever tightens" isn't enforced here on purpose — that
    contract belongs to `move_stop`, not this pure formula."""
    return round(highest_price - gexp_tsl_step, 2)


def build_rule(*, side: str, entry: float,
               sl_mode: str = "points", sl_val: float = 0.0,
               target_mode: str = "points", target_val: float = 0.0,
               candles: pd.DataFrame | None = None,
               sl_field: str = "low", target_field: str = "high",
               swing_lookback: int = 5,
               trail: dict | None = None) -> dict:
    """Assemble a LiveManager-compatible rule dict from a strategy preset's
    SL/Target configuration.

    `sl_mode`/`target_mode` (mirrors the legacy preset's `candle_sl_mode`):
      "points"     — sl_val/target_val used directly as a points offset.
      "percent"    — sl_val/target_val used directly as LiveManager's own
                     percent mode (no candle needed — passed straight through).
      "prev_ohlc"  — pinned to `sl_field`/`target_field` of the candle that
                     just closed.
      "swing_low"  — SL pinned to the lowest low over `swing_lookback`
                     candles (target has no equivalent — legacy has none
                     either, so `target_mode="swing_low"` is not supported).

    A mode that cannot be resolved (no candle data yet, the derived price
    lands on the wrong side of entry) leaves that leg disabled rather than
    guessing — "no stop" is a state LiveManager already understands and
    reports (`NO_RULE`); a wrong one is not.
    """
    rule: dict[str, Any] = {"slEnabled": False, "targetEnabled": False}
    if trail:
        rule["trail"] = trail

    if sl_mode == "points":
        rule["slEnabled"] = sl_val > 0
        rule["slMode"], rule["slVal"] = "points", sl_val
    elif sl_mode == "percent":
        rule["slEnabled"] = sl_val > 0
        rule["slMode"], rule["slVal"] = "percent", sl_val
    elif sl_mode in ("prev_ohlc", "swing_low"):
        price = (prev_ohlc_price(candles, sl_field) if sl_mode == "prev_ohlc"
                else swing_low_price(candles, swing_lookback))
        offset = None if price is None else points_offset(entry, _clip(price), side, "sl")
        if offset is not None:
            rule["slEnabled"] = True
            rule["slMode"], rule["slVal"] = "points", offset

    if target_mode == "points":
        rule["targetEnabled"] = target_val > 0
        rule["targetMode"], rule["targetVal"] = "points", target_val
    elif target_mode == "percent":
        rule["targetEnabled"] = target_val > 0
        rule["targetMode"], rule["targetVal"] = "percent", target_val
    elif target_mode == "prev_ohlc":
        price = prev_ohlc_price(candles, target_field)
        offset = (None if price is None
                  else points_offset(entry, _clip(price), side, "target"))
        if offset is not None:
            rule["targetEnabled"] = True
            rule["targetMode"], rule["targetVal"] = "points", offset

    return rule
