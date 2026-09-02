"""Shared, ref-counted candle builder for strategy instances.

Ported from ``legacy/engines/candle_engine.py`` — the bucketing and indicator
math (EMA, RSI, VWAP + bands, ATR, ADX, Supertrend) is unchanged from the
legacy engine; only the ownership model changed. Legacy built one
`CandleEngine` per running preset, so two presets watching the same strike
each paid for their own tick handling, their own indicator recompute, and
their own market-data subscription. Here there is ONE store, keyed by
`(instrument, timeframe)`, ref-counted across every strategy instance that
subscribes to it — two strategies both watching NIFTY 3-minute candles
compute that series once, not twice, and imply one subscription, not two.

`key` is either a canonical `InstrumentKey` (an option contract — ticks come
from `broker_manager.add_option_tick_listener`, and subscribing here also
adds the contract to the STRATEGY market-data subscription source, see
`services/subscriptions.py`) or a plain index symbol string like `"NIFTY"`
(ticks come from `broker_manager.add_index_tick_listener`; index ticks carry
no traded-volume figure, so index candles carry `volume=0` — VWAP/volume
filters are not meaningful on an index series, only EMA/RSI/ADX/Supertrend
are, which is also all legacy ever used index candles for).
"""
from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

import diagnostics
from services.broker_manager import manager
from services.instruments import InstrumentKey
from services.subscriptions import STRATEGY, option_subs

MAX_CANDLES = 600   # matches legacy's cap — a session's worth at 1-minute bars

_STEP_MIN = {"1min": 1, "3min": 3, "5min": 5}


def _candle_boundary(now: dt.datetime, timeframe: str) -> dt.datetime:
    """Floored candle-start time for `now`. Ported from
    CandleEngine._candle_boundary, generalized from three near-identical
    branches to one (1/3/5-minute are all "snap total minutes down to a
    multiple of the step") rather than a behavioural change."""
    step = _STEP_MIN.get(timeframe, 1)
    if step <= 1:
        return now.replace(second=0, microsecond=0)
    total = now.hour * 60 + now.minute
    snapped = (total // step) * step
    return now.replace(hour=snapped // 60, minute=snapped % 60,
                       second=0, microsecond=0)


# ── indicator math, ported verbatim from CandleEngine ──────────────────────
def _compute_indicators(df: pd.DataFrame, ema_period: int = 20,
                        rsi_period: int = 14) -> pd.DataFrame:
    df["ema"] = df["close"].ewm(span=ema_period, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / rsi_period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / rsi_period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    df["rsi"] = 100 - (100 / (1 + rs))
    df["rsi"] = df["rsi"].fillna(50)

    today = dt.datetime.now().date()
    df["time"] = pd.to_datetime(df["time"])
    today_mask = df["time"].dt.date == today

    df["typical_price"] = (df["high"] + df["low"] + df["close"]) / 3
    df["pv"] = df["typical_price"] * df["volume"]

    df["cum_pv"] = df["pv"].where(today_mask).fillna(0).cumsum()
    df["cum_vol"] = df["volume"].where(today_mask).fillna(0).cumsum()
    df["vwap"] = (df["cum_pv"] / df["cum_vol"].replace(0, float("nan"))).ffill()

    roll_std = (df["typical_price"] - df["vwap"]).rolling(30, min_periods=1).std()
    df["std"] = roll_std
    df["vwap_upper1"] = df["vwap"] + roll_std
    df["vwap_lower1"] = df["vwap"] - roll_std
    df["vwap_upper2"] = df["vwap"] + 2 * roll_std
    df["vwap_lower2"] = df["vwap"] - 2 * roll_std

    df = _add_atr(df, period=14)
    df = _add_adx(df, period=14)
    df = _add_supertrend(df, period=10, multiplier=3)

    df.bfill(inplace=True)
    return df


def _add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / period, adjust=False).mean()
    return df


def _add_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    prev_high = df["high"].shift(1)
    prev_low = df["low"].shift(1)
    up_move = df["high"] - prev_high
    down_move = prev_low - df["low"]

    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    atr = df.get("atr")
    if atr is None:
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / period, adjust=False).mean()

    safe_atr = atr.replace(0, float("nan"))
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / safe_atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / safe_atr
    dx = (100 * (plus_di - minus_di).abs()
          / (plus_di + minus_di).replace(0, float("nan")))
    df["adx"] = dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0)
    df["plus_di"] = plus_di.fillna(0)
    df["minus_di"] = minus_di.fillna(0)
    return df


def _add_supertrend(df: pd.DataFrame, period: int = 10,
                    multiplier: int = 3) -> pd.DataFrame:
    if "atr" not in df.columns:
        df = _add_atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2
    upper = (hl2 + multiplier * df["atr"]).values
    lower = (hl2 - multiplier * df["atr"]).values
    close = df["close"].values
    n = len(close)

    direction = [1] * n
    final_band = lower.copy()

    for i in range(1, n):
        if direction[i - 1] == 1:
            cur = max(lower[i], final_band[i - 1])
            if close[i] < cur:
                direction[i] = -1
                final_band[i] = upper[i]
            else:
                direction[i] = 1
                final_band[i] = cur
        else:
            cur = min(upper[i], final_band[i - 1])
            if close[i] > cur:
                direction[i] = 1
                final_band[i] = lower[i]
            else:
                direction[i] = -1
                final_band[i] = cur

    df["supertrend_dir"] = direction
    df["supertrend_band"] = final_band
    return df


CandleKey = "InstrumentKey | str"
OnClose = Callable[[Any, pd.DataFrame], None]


@dataclass
class _Series:
    key: Any
    timeframe: str
    candles: pd.DataFrame = field(default_factory=pd.DataFrame)
    current: dict | None = None
    last_minute: dt.datetime | None = None
    last_cum_vol: int = 0
    lot_size: int = 1
    subscribers: set[str] = field(default_factory=set)
    listeners: dict[str, OnClose] = field(default_factory=dict)


class CandleStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._series: dict[tuple[Any, str], _Series] = {}
        self._hooked = False

    def _ensure_hooked(self) -> None:
        with self._lock:
            if self._hooked:
                return
            self._hooked = True
        manager.add_option_tick_listener(self._on_option_tick)
        manager.add_index_tick_listener(self._on_index_tick)

    # ── subscription (ref-counted) ─────────────────────────────────────
    def subscribe(self, subscriber_id: str, key: Any, timeframe: str,
                  on_close: OnClose | None = None) -> None:
        self._ensure_hooked()
        with self._lock:
            series = self._series.get((key, timeframe))
            if series is None:
                series = _Series(key=key, timeframe=timeframe)
                if isinstance(key, InstrumentKey):
                    meta = manager.option_meta(key.underlying, key.expiry,
                                               key.strike, key.opt_type) or {}
                    series.lot_size = max(1, int(meta.get("lotSize") or 1))
                self._series[(key, timeframe)] = series
            series.subscribers.add(subscriber_id)
            if on_close is not None:
                series.listeners[subscriber_id] = on_close
        if isinstance(key, InstrumentKey):
            self._resubscribe_options()

    def unsubscribe(self, subscriber_id: str, key: Any, timeframe: str) -> None:
        with self._lock:
            series = self._series.get((key, timeframe))
            if series is None:
                return
            series.subscribers.discard(subscriber_id)
            series.listeners.pop(subscriber_id, None)
            if not series.subscribers:
                del self._series[(key, timeframe)]
        if isinstance(key, InstrumentKey):
            self._resubscribe_options()

    def _resubscribe_options(self) -> None:
        """Hand the feed exactly the option contracts something still wants
        candles for — same union-of-sources contract every other subscriber
        of `option_subs` already honours (see subscriptions.py)."""
        with self._lock:
            keys = {k for (k, _tf) in self._series if isinstance(k, InstrumentKey)}
        option_subs.set(STRATEGY, keys)

    # ── read ──────────────────────────────────────────────────────────
    def get_candles(self, key: Any, timeframe: str) -> pd.DataFrame | None:
        with self._lock:
            series = self._series.get((key, timeframe))
            if series is None or series.candles.empty:
                return None
            return series.candles.copy()

    # ── tick ingestion — one global listener, dispatched only to what's
    #    actually tracked (mirrors CandleEngine._on_tick's "unknown token,
    #    return early" shape) ─────────────────────────────────────────
    def _on_option_tick(self, key: InstrumentKey, ltp: float, volume) -> None:
        self._feed_all(key, ltp, volume)

    def _on_index_tick(self, symbol: str, ltp: float) -> None:
        self._feed_all(symbol, ltp, None)

    def _feed_all(self, key: Any, ltp: float, cum_vol: int | None) -> None:
        now = dt.datetime.now()
        with self._lock:
            matching = [s for (k, _tf), s in self._series.items() if k == key]
        for series in matching:
            self._feed(series, ltp, cum_vol, now)

    def _feed(self, series: _Series, ltp: float, cum_vol: int | None,
             now: dt.datetime) -> None:
        closed = None
        with self._lock:
            minute = _candle_boundary(now, series.timeframe)
            if cum_vol is not None and cum_vol >= series.last_cum_vol:
                vol_delta = max(int(round(
                    (cum_vol - series.last_cum_vol) / series.lot_size)), 0)
                series.last_cum_vol = cum_vol
            else:
                vol_delta = 0

            candle = series.current
            if series.last_minute != minute:
                if candle:
                    closed = dict(candle)
                series.current = {"time": minute, "open": ltp, "high": ltp,
                                  "low": ltp, "close": ltp, "volume": vol_delta}
                series.last_minute = minute
            elif candle:
                candle["high"] = max(candle["high"], ltp)
                candle["low"] = min(candle["low"], ltp)
                candle["close"] = ltp
                candle["volume"] += vol_delta
            else:
                series.current = {"time": minute, "open": ltp, "high": ltp,
                                  "low": ltp, "close": ltp, "volume": vol_delta}
                series.last_minute = minute

        if closed is not None:
            self._close_candle(series, closed)

    def _close_candle(self, series: _Series, candle: dict) -> None:
        with self._lock:
            new_row = pd.DataFrame([candle])
            df = series.candles
            df = new_row.copy() if df.empty else pd.concat(
                [df, new_row], ignore_index=True)
            df = df.tail(MAX_CANDLES).reset_index(drop=True)
            df = _compute_indicators(df)
            series.candles = df
            key = series.key
            listeners = list(series.listeners.values())

        for fn in listeners:
            try:
                fn(key, df.copy())
            except Exception as exc:
                diagnostics.exception("strategy", "Candle-close listener failed",
                                      exc_info=exc, key=str(key))


candle_store = CandleStore()
