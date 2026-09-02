"""Regression tests for the legacy strategy port — filters, entry modes,
strike selection, and the new risk_rules.py additions.

    python sidecar/tests/test_strategy_legacy_port.py

Pure-function tests only — no broker, no manager, no candle store. Each
function here is a near-verbatim port of specific legacy source (see each
module's own docstring for exact file:line provenance); these tests feed the
same shapes of data the legacy code would see and assert the same outputs,
the most direct evidence available that the port did not change behaviour.
"""
import datetime as dt
import os
import sys
import tempfile

import pandas as pd

SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CHARTICKS_DATA_DIR"] = tempfile.mkdtemp(prefix="charticks-legacyport-")
os.environ["CHARTICKS_LOG_DIR"] = os.environ["CHARTICKS_DATA_DIR"]
sys.path.insert(0, SIDECAR)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <-- ' + str(detail)}")


def section(title):
    print(f"\n{title}")


from services.strategy_engine import filters, entry_modes, strike_selection, risk_rules  # noqa: E402


def candles(rows: list[dict]) -> pd.DataFrame:
    """Build a minimal candle frame with the columns CandleEngine's real
    indicator computation would have produced — tests supply only the
    columns each filter under test actually reads."""
    return pd.DataFrame(rows)


# ═════════════════════════════════════════════════════════════════════════
# [1] filters.py — pre-filters (time/session/OI/spread gates)
# ═════════════════════════════════════════════════════════════════════════
section("[1] Pre-filters — time window, session blocks, OI, spread")

p = {"filter_time_window": True, "time_window_start": "09:45", "time_window_end": "14:00"}
check("inside the configured window passes",
      filters.time_window_ok(dt.datetime(2026, 9, 1, 10, 0), p))
check("before the window fails",
      not filters.time_window_ok(dt.datetime(2026, 9, 1, 9, 30), p))
check("after the window fails (no EOD extension configured)",
      not filters.time_window_ok(dt.datetime(2026, 9, 1, 14, 30), p))
p_eod = {**p, "time_window_eod": True}
check("the EOD extension (14:30-15:00) passes when enabled",
      filters.time_window_ok(dt.datetime(2026, 9, 1, 14, 45), p_eod))
check("disabled filter always passes", filters.time_window_ok(dt.datetime(2026, 9, 1, 3, 0), {}))

check("09:20 is inside the market-open session block",
      not filters.session_block_ok(dt.datetime(2026, 9, 1, 9, 20), {"filter_session_blocks": True}))
check("09:35 is outside every session block",
      filters.session_block_ok(dt.datetime(2026, 9, 1, 9, 35), {"filter_session_blocks": True}))
check("13:05 is inside the lunch session block",
      not filters.session_block_ok(dt.datetime(2026, 9, 1, 13, 5), {"filter_session_blocks": True}))

check("OI above the minimum passes",
      filters.oi_ok(60000, {"filter_oi": True, "oi_min": 50000}))
check("OI below the minimum fails",
      not filters.oi_ok(10000, {"filter_oi": True, "oi_min": 50000}))
check("missing OI (None) fails rather than passing silently",
      not filters.oi_ok(None, {"filter_oi": True, "oi_min": 50000}))

check("a tight spread passes",
      filters.spread_ok(99.5, 100.0, {"filter_spread_guard": True, "max_spread_pct": 2.0}))
check("a wide spread fails",
      not filters.spread_ok(90.0, 100.0, {"filter_spread_guard": True, "max_spread_pct": 2.0}))
check("no depth data never blocks (nothing to judge)",
      filters.spread_ok(0, 0, {"filter_spread_guard": True, "max_spread_pct": 2.0}))

ok, reason = filters.pre_filters(
    dt.datetime(2026, 9, 1, 10, 0), {"oi": 60000, "bid": 99, "ask": 100},
    {"filter_time_window": True, "time_window_start": "09:45", "time_window_end": "14:00",
     "filter_oi": True, "oi_min": 50000, "filter_spread_guard": True, "max_spread_pct": 2.0})
check("pre_filters combines all four gates and passes when all are satisfied", ok, reason)


# ═════════════════════════════════════════════════════════════════════════
# [2] filters.py — individual quant filters
# ═════════════════════════════════════════════════════════════════════════
section("[2] Individual quant filters")

df_ema = candles([{"close": 100, "open": 99, "high": 101, "low": 98,
                   "volume": 1000, "ema": 95, "rsi": 60, "vwap": 99,
                   "vwap_upper1": 101, "vwap_lower1": 97, "atr": 1.0}] * 10)
check("run_filters passes when nothing is enabled (enable_quant off short-circuits everything)",
      filters.run_filters(df_ema, {"enable_quant": False, "filter_ema": True}, "CE"))
check("EMA filter passes when close is above ema1/ema2",
      filters.run_filters(df_ema, {"enable_quant": True, "filter_ema": True,
                                   "ema_f1": 20, "ema_f2": 50, "ema_f3": 0}, "CE"))
# A flat run (so the EMA settles near 100) then a sharp drop on the LAST
# candle only — the EMA (lagging) stays elevated above the now-dropped
# price. A uniformly-constant series would make EMA == price exactly and
# never trigger "below", which is what the first version of this fixture
# got wrong.
df_below = candles(
    [{"close": 100, "open": 100, "high": 101, "low": 99, "volume": 1000} for _ in range(9)]
    + [{"close": 90, "open": 100, "high": 101, "low": 89, "volume": 1000}])
df_below["ema"] = df_below["close"].ewm(span=20, adjust=False).mean()
check("EMA filter fails when close drops below its own (lagging) EMA",
      not filters.run_filters(df_below, {"enable_quant": True, "filter_ema": True,
                                         "ema_f1": 20, "ema_f2": 0, "ema_f3": 0}, "CE"))

check("RSI filter passes above the threshold",
      filters.run_filters(df_ema, {"enable_quant": True, "filter_rsi": True,
                                   "rsi_min_threshold": 50}, "CE"))
check("RSI filter fails below the threshold",
      not filters.run_filters(df_ema, {"enable_quant": True, "filter_rsi": True,
                                       "rsi_min_threshold": 70}, "CE"))

check("body quality passes a strong-bodied candle",
      filters.body_quality_filter(df_ema, {"body_quality_min": 0.2}))
weak_body = candles([{"close": 100.1, "open": 100.0, "high": 105, "low": 95}])
check("body quality fails a weak-bodied candle in a wide range",
      not filters.body_quality_filter(weak_body, {"body_quality_min": 0.5}))

trend_up = candles([{"close": 100 + i, "open": 99 + i, "volume": 100}
                    for i in range(5)])
check("multi-bar momentum passes when 3+ of 5 candles are bullish",
      filters.multi_bar_momentum_filter(trend_up, "CE"))
trend_down = candles([{"close": 99 - i, "open": 100 - i, "volume": 100}
                      for i in range(5)])
check("multi-bar momentum fails when fewer than 3 of 5 are bullish",
      not filters.multi_bar_momentum_filter(trend_down, "CE"))

df_st = candles([{"close": 100, "open": 99, "high": 101, "low": 98, "volume": 1000,
                  "ema": 95, "rsi": 60, "vwap": 99, "supertrend_dir": 1}])
check("supertrend CE requires an UP direction and passes when it matches",
      filters._supertrend_filter(df_st, {}, "CE"))
check("supertrend PE requires a DOWN direction and fails against an UP one",
      not filters._supertrend_filter(df_st, {}, "PE"))

check("gamma_expansion_detector needs at least 20 candles",
      not filters.gamma_expansion_detector(df_ema))   # only 10 rows


# ═════════════════════════════════════════════════════════════════════════
# [3] entry_modes.py
# ═════════════════════════════════════════════════════════════════════════
section("[3] Entry-trigger modes")

check("MARKET mode always signals",
      entry_modes.evaluate_entry(entry_modes.MARKET, 100.0, df_ema, {}, {}))

pb_params = {"numeric_entries": {"Entry Price": "100", "Tolerance": "2"}}
state = {}
check("PRICE_BAND does not trigger outside tolerance",
      not entry_modes.evaluate_entry(entry_modes.PRICE_BAND, 110.0, df_ema, pb_params, state))
check("PRICE_BAND triggers inside tolerance and LATCHES",
      entry_modes.evaluate_entry(entry_modes.PRICE_BAND, 101.0, df_ema, pb_params, state))
check("...and stays triggered even if price later moves outside the band",
      entry_modes.evaluate_entry(entry_modes.PRICE_BAND, 200.0, df_ema, pb_params, state))

reclaim_df = candles([
    {"close": 94, "open": 93, "high": 95, "low": 92, "vwap_lower1": 95},   # warm-up row (len>=3 required)
    {"close": 90, "open": 92, "high": 93, "low": 89, "vwap_lower1": 95},   # below lower1, small red body
    {"close": 98, "open": 93, "high": 99, "low": 92, "vwap_lower1": 95},   # reclaims, bigger green body
])
check("VWAP_RECLAIM signals on a confirmed reclaim with expanding body",
      entry_modes.evaluate_entry(entry_modes.VWAP_RECLAIM, 99.0, reclaim_df, {}, {}))
check("VWAP_RECLAIM does not signal when ltp is below the reclaim trigger price",
      not entry_modes.evaluate_entry(entry_modes.VWAP_RECLAIM, 90.0, reclaim_df, {}, {}))


# ═════════════════════════════════════════════════════════════════════════
# [4] strike_selection.py
# ═════════════════════════════════════════════════════════════════════════
section("[4] Strike selection")

check("SENSEX ATM rounds to the nearest 100",
      strike_selection.calculate_atm(80234, "SENSEX") == (80200, 100))
check("NIFTY ATM rounds to the nearest 50",
      strike_selection.calculate_atm(24523, "NIFTY") == (24500, 50))
check("BANKNIFTY ATM rounds to the nearest 100",
      strike_selection.calculate_atm(51234, "BANKNIFTY") == (51200, 100))

check("a SENSEX strike that is a multiple of 500 is institutional-round",
      strike_selection.is_institutional_round(80500, "SENSEX"))
check("a SENSEX strike that is not a multiple of 500 is not",
      not strike_selection.is_institutional_round(80200, "SENSEX"))

check("ROUND mode fires both legs only on a round ATM",
      strike_selection.generate_strikes(80500, 100, "SENSEX", {"strike_mode": "ROUND"})
      == [(80500, "CE"), (80500, "PE")])
check("ROUND mode fires nothing on a non-round ATM",
      strike_selection.generate_strikes(80200, 100, "SENSEX", {"strike_mode": "ROUND"}) == [])

legacy_params = {"strike_mode": "LEGACY",
                 "legacy_gap_vars": {"ATM": True, "+1": False, "-1": True, "+2": False, "-2": False}}
result = strike_selection.generate_strikes(24500, 50, "NIFTY", legacy_params)
check("LEGACY mode only fires enabled levels (ATM and -1 here)",
      set(result) == {(24500, "CE"), (24500, "PE"), (24450, "CE"), (24450, "PE")}, result)

relative_params = {"strike_mode": "RELATIVE",
                   "directional_vars": {"ATM": {"CE": False, "PE": True},
                                        "+50": {"CE": True, "PE": False}}}
result = strike_selection.generate_strikes(24500, 50, "NIFTY", relative_params)
check("RELATIVE mode fires exactly the enabled (level, side) pairs",
      set(result) == {(24500, "PE"), (24550, "CE")}, result)

custom_params = {"strike_mode": "CUSTOM_RANGE", "custom_range_from": -100,
                 "custom_range_to": 100, "custom_range_ce": False, "custom_range_pe": True}
result = strike_selection.generate_strikes(80500, 100, "SENSEX", custom_params)
check("CUSTOM_RANGE mode covers the whole offset range, side-filtered",
      set(result) == {(80400, "PE"), (80500, "PE"), (80600, "PE")}, result)

# The exact params from a real preset file (SENSEX-S1-BearDaySetup.json).
real_preset_strikes = {"strike_mode": "CUSTOM_RANGE", "custom_range_from": 0,
                       "custom_range_to": 0, "custom_range_ce": False, "custom_range_pe": True}
check("a real preset's own params produce the single PE-only ATM strike it was built for",
      strike_selection.generate_strikes(80500, 100, "SENSEX", real_preset_strikes)
      == [(80500, "PE")])


# ═════════════════════════════════════════════════════════════════════════
# [5] risk_rules.py additions
# ═════════════════════════════════════════════════════════════════════════
section("[5] risk_rules.py — GExp formulas")

check("gexp_approach2_trail_sl is highest_price minus the configured step",
      risk_rules.gexp_approach2_trail_sl(120.0, 15.0) == 105.0)

# GExp approach-1's LITERAL formula (order_manager.py:1013-1018), including
# its intermediate rounding of sl before computing risk — a percent-mode
# shortcut looked algebraically equivalent but was off by a couple of paise
# (caught by the end-to-end test, not by this file); gexp_approach1_rule
# reproduces the exact arithmetic instead.
price, sl_pct, rr = 100.0, 35.0, 3.0
legacy_sl = round(price * (1 - sl_pct / 100), 2)
legacy_risk = price - legacy_sl
legacy_target = round(price + legacy_risk * rr, 2)
rule = risk_rules.gexp_approach1_rule(price, sl_pct, rr)
ported_sl = round(price - rule["slVal"], 2)
ported_target = round(price + rule["targetVal"], 2)
check("gexp_approach1_rule reproduces legacy's exact SL price",
      ported_sl == legacy_sl, (ported_sl, legacy_sl))
check("...and the exact target price, including the intermediate rounding step",
      ported_target == legacy_target, (ported_target, legacy_target))

# A case where the rounding step actually matters — chosen so the percent
# shortcut and the literal formula would have disagreed, proving this isn't
# a vacuous check.
price2, sl_pct2, rr2 = 25.5, 35.0, 3.0
legacy_sl2 = round(price2 * (1 - sl_pct2 / 100), 2)
legacy_target2 = round(price2 + (price2 - legacy_sl2) * rr2, 2)
shortcut_target2 = round(price2 + price2 * (sl_pct2 * rr2) / 100, 2)
check("this test case genuinely distinguishes the two approaches (a real regression case)",
      legacy_target2 != shortcut_target2, (legacy_target2, shortcut_target2))
rule2 = risk_rules.gexp_approach1_rule(price2, sl_pct2, rr2)
check("gexp_approach1_rule matches the literal formula here, not the shortcut",
      round(price2 + rule2["targetVal"], 2) == legacy_target2)


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
