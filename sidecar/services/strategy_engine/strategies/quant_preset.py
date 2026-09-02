"""The ported legacy quant-filter engine, as one configuration-driven
strategy plugin — not one Python class per strategy. Every JSON file under
the project's `strategies/` folder becomes one instance of THIS class, with
that file's contents as `params`, unchanged (see `filters.py`/
`entry_modes.py`/`strike_selection.py`/`risk_rules.py` for the ported math
this class assembles, and the plan doc for the audit that grounds every
decision below in the real legacy source and the real 24 preset files).

Per-contract state (`_TokenState`) is instance-local — a plain dict on this
object, never shared across instances — which is the structural fix over
legacy's own cross-preset state-file collision (multiple presets' bound
methods writing the same `open_positions_state.json`, found during the
original port audit). `get_state`/`restore_state` (built in Phase 4) persist
and restore it across a restart.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from .. import entry_modes, filters, risk_rules, strike_selection
from ..base import ParamField, Strategy, StrategyContext, StrategySpec
from ..registry import register

# Preset field default when a preset omits it — matches legacy's own Tk
# variable defaults for the two fields that are genuinely optional.
_DEFAULT_SPOT_TIME = "09:15"
_DEFAULT_TIMEFRAME = "3min"


@dataclass
class _TokenState:
    """Mirrors legacy's per-token `strike_state` — same fields, same
    meaning, scoped to one instance instead of a shared dict every preset
    wrote into."""
    key: Any                      # InstrumentKey
    opt_type: str
    trade_open: bool = False
    entry_taken_today: bool = False
    order_in_progress: bool = False
    entry_band_triggered: bool = False
    entry_price: float = 0.0
    highest_price: float = 0.0
    sl: float | None = None
    targets: list[float] = field(default_factory=list)
    targets_hit: list[float] = field(default_factory=list)
    gexp_override: bool = False
    gexp_method: str = "none"
    gexp_tsl_step: float = 0.0

    def to_dict(self) -> dict:
        return {
            "underlying": self.key.underlying, "expiry": self.key.expiry,
            "strike": self.key.strike, "optType": self.opt_type,
            "tradeOpen": self.trade_open, "entryTakenToday": self.entry_taken_today,
            "entryBandTriggered": self.entry_band_triggered,
            "entryPrice": self.entry_price, "highestPrice": self.highest_price,
            "sl": self.sl, "targets": self.targets, "targetsHit": self.targets_hit,
            "gexpOverride": self.gexp_override, "gexpMethod": self.gexp_method,
            "gexpTslStep": self.gexp_tsl_step,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "_TokenState":
        from services.instruments import InstrumentKey

        key = InstrumentKey.option(raw["underlying"], raw["expiry"],
                                   raw["strike"], raw["optType"])
        inst = cls(key=key, opt_type=raw["optType"])
        inst.trade_open = bool(raw.get("tradeOpen"))
        inst.entry_taken_today = bool(raw.get("entryTakenToday"))
        inst.entry_band_triggered = bool(raw.get("entryBandTriggered"))
        inst.entry_price = float(raw.get("entryPrice") or 0)
        inst.highest_price = float(raw.get("highestPrice") or 0)
        inst.sl = raw.get("sl")
        inst.targets = list(raw.get("targets") or [])
        inst.targets_hit = list(raw.get("targetsHit") or [])
        inst.gexp_override = bool(raw.get("gexpOverride"))
        inst.gexp_method = raw.get("gexpMethod", "none")
        inst.gexp_tsl_step = float(raw.get("gexpTslStep") or 0)
        return inst


def _resolve_rule_price(rule: dict, entry: float, kind: str) -> float | None:
    """The absolute price a `risk_rules.build_rule` SL/Target resolves to for
    a BUY, given the rule already carries only a points-or-percent OFFSET.
    Local-state bookkeeping only (see the call site) — never re-derives what
    LiveManager itself enforces."""
    enabled = rule.get("slEnabled" if kind == "sl" else "targetEnabled")
    if not enabled:
        return None
    mode = rule.get(f"{kind}Mode")
    val = rule.get(f"{kind}Val", 0)
    if mode == "percent":
        off = entry * val / 100.0
    else:
        off = val
    return round(entry - off, 2) if kind == "sl" else round(entry + off, 2)


class QuantPresetStrategy(Strategy):
    def __init__(self) -> None:
        self.params: dict = {}
        self.underlying: str = ""
        self.timeframe: str = _DEFAULT_TIMEFRAME
        self._tokens: dict[Any, _TokenState] = {}   # InstrumentKey -> state
        self._armed = False

    # ── lifecycle ─────────────────────────────────────────────────────────
    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        self.params = params
        self.underlying = str(params.get("index", "")).upper()
        self.timeframe = params.get("live_interval", _DEFAULT_TIMEFRAME)
        if not self.underlying:
            raise ValueError("quant_preset requires an 'index' param")
        # Spot detection: only TIME mode is implemented — every one of the
        # 24 current presets uses it. EMA_CROSS/PDH_PDL need historical spot
        # candles Charticks does not fetch (same "no historical warm-up for
        # v1" decision Phase 2 made); a preset requesting them is refused
        # loudly rather than silently treated as TIME.
        mode = params.get("spot_detect_mode", "TIME")
        if mode != "TIME":
            raise ValueError(
                f"spot_detect_mode '{mode}' is not supported yet (only TIME "
                f"is) — see risk_rules.py's module docstring for what else "
                f"is deferred and why")
        ctx.subscribe_ticks(self.underlying)
        ctx.log("info", "quant_preset armed — waiting for spot",
               index=self.underlying, spotTime=params.get("spot_time", _DEFAULT_SPOT_TIME))

    def on_stop(self) -> None:
        pass

    # ── restart survival (Phase 4 hooks) ────────────────────────────────
    def get_state(self) -> dict:
        # Persisted state is JSON, so the map key must be a string —
        # position_id, not the InstrumentKey object the in-memory dict below
        # uses directly (they are NOT the same string: InstrumentKey's own
        # __str__ is a human-readable "NIFTY 29SEP2026 24000 CE", not the
        # pipe-delimited position_id — mixing the two was a real bug caught
        # in review, not a hypothetical one).
        return {"armed": self._armed,
               "tokens": {st.key.position_id: st.to_dict()
                         for st in self._tokens.values()}}

    def restore_state(self, state: dict) -> None:
        self._armed = bool(state.get("armed"))
        tokens = state.get("tokens") or {}
        for raw in tokens.values():
            try:
                st = _TokenState.from_dict(raw)
            except Exception:
                continue
            self._tokens[st.key] = st
            if self._armed:
                self.ctx.subscribe_candles(st.key, self.timeframe)
        if self._armed:
            self.ctx.log("info", f"restored {len(self._tokens)} tracked contract(s)",
                         index=self.underlying)

    # ── UI status ─────────────────────────────────────────────────────────
    def phase(self) -> str | None:
        if not self._armed or not self._tokens:
            return "waiting"
        if any(st.trade_open for st in self._tokens.values()):
            return "in_position"
        if all(st.entry_taken_today for st in self._tokens.values()):
            return "completed"
        return "waiting"

    # ── spot arming ──────────────────────────────────────────────────────
    def on_tick(self, key: Any, ltp: float) -> None:
        # Index ticks arrive with `key` already a plain string (the index
        # symbol itself, e.g. "NIFTY") — see feed_router.py's
        # add_index_tick_listener; option ticks arrive with the real
        # InstrumentKey object, which is what `self._tokens` is keyed by.
        if not self._armed and key == self.underlying:
            self._maybe_arm(ltp)
            return
        state = self._tokens.get(key)
        if state is not None and state.trade_open:
            self._trail_gexp2(state, ltp)

    def _maybe_arm(self, spot_ltp: float) -> None:
        try:
            spot_time = dt.datetime.strptime(
                self.params.get("spot_time", _DEFAULT_SPOT_TIME), "%H:%M").time()
        except (TypeError, ValueError):
            spot_time = dt.datetime.strptime(_DEFAULT_SPOT_TIME, "%H:%M").time()
        if dt.datetime.now().time() < spot_time:
            return
        self._armed = True

        from services.instruments import InstrumentKey, instruments

        atm, step = strike_selection.calculate_atm(spot_ltp, self.underlying)
        strikes = strike_selection.generate_strikes(atm, step, self.underlying, self.params)
        expiry = self._resolve_expiry()
        subscribed = 0
        for strike, opt_type in strikes:
            key = InstrumentKey.option(self.underlying, expiry, strike, opt_type)
            if not instruments.has(key):
                self.ctx.log("warn", "strike not listed by any connected broker — skipped",
                             strike=strike, optType=opt_type, expiry=expiry)
                continue
            self._tokens[key] = _TokenState(key=key, opt_type=opt_type)
            self.ctx.subscribe_candles(key, self.timeframe)
            subscribed += 1
        self.ctx.log("info", f"armed — {subscribed} contract(s) selected",
                     atm=atm, expiry=expiry, spot=spot_ltp)

    def _resolve_expiry(self) -> str:
        """The preset schema has no expiry field — legacy read it from a
        separate Option Chain panel dropdown, external to any preset.
        Defaults to the nearest active expiry; a preset MAY set "expiry"
        explicitly to pin one, which is additive and does not change
        behaviour for any of the 24 files that omit it."""
        explicit = self.params.get("expiry")
        if explicit:
            return explicit
        from services import expiry as expiry_filter
        from services.instruments import instruments

        candidates = expiry_filter.active_expiries(instruments.expiries(self.underlying))
        if not candidates:
            raise RuntimeError(f"no active expiry known for {self.underlying} — "
                               f"is a broker with this instrument connected?")
        return sorted(candidates, key=expiry_filter.sort_key)[0]

    # ── entry evaluation (candle close) ─────────────────────────────────
    def on_candle_close(self, key: Any, candles) -> None:
        state = self._tokens.get(key)
        if state is None:
            return
        if state.trade_open or state.entry_taken_today or state.order_in_progress:
            return
        if candles is None or len(candles) < 5:
            return
        if not all(c in candles.columns for c in ("ema", "rsi", "vwap")):
            return

        tick = self.ctx.get_tick(key)
        ltp = tick.get("ltp")
        if not ltp:
            return

        ok, reason = filters.pre_filters(dt.datetime.now(), tick, self.params)
        if not ok:
            return
        if not filters.run_filters(candles, self.params, state.opt_type):
            return

        entry_state = {"entry_band_triggered": state.entry_band_triggered}
        signal = entry_modes.evaluate_entry(
            self.params.get("entry_mode", entry_modes.MARKET), ltp, candles,
            self.params, entry_state)
        state.entry_band_triggered = entry_state["entry_band_triggered"]
        if not signal:
            return

        self._enter(state, ltp, candles)

    # ── entry ─────────────────────────────────────────────────────────────
    def _enter(self, state: _TokenState, ltp: float, candles) -> None:
        state.order_in_progress = True
        rule = self._build_entry_rule(state, ltp, candles)
        if rule is None or not rule.get("slEnabled"):
            # "validate-then-buy" — legacy refuses an entry with no valid
            # stop rather than open one unprotected (order_manager.py:1044-1053).
            state.order_in_progress = False
            state.entry_taken_today = False
            state.entry_band_triggered = False
            self.ctx.log("warn", "entry skipped — no valid stop", symbol=str(state.key))
            return

        from services.broker_manager import manager as broker_manager

        meta = broker_manager.option_meta(state.key.underlying, state.key.expiry,
                                          state.key.strike, state.opt_type) or {}
        lot_size = max(1, int(meta.get("lotSize") or 1))
        lots = max(1, int(self.params.get("lots", 1)))
        qty = lots * lot_size

        res = self.ctx.place_order(
            state.key.underlying, state.key.expiry, state.key.strike, state.opt_type,
            "BUY", qty, lots, rule=rule, product="NRML", tag=str(state.key))
        if not res.get("ok"):
            state.order_in_progress = False
            state.entry_taken_today = False
            state.entry_band_triggered = False
            self.ctx.log("warn", "entry order rejected", symbol=str(state.key),
                         error=res.get("error"))
            return

        state.trade_open = True
        state.entry_taken_today = True
        state.order_in_progress = False
        # The INTENDED price at signal time, not a confirmed fill price — the
        # fill itself arrives asynchronously (order_sync) and LiveManager's
        # own LivePosition is the authoritative record of what actually
        # happened; this local copy exists only for logging and to seed GExp
        # approach-2's continuous trail baseline, matching legacy's own
        # "set state right after the order call returns, don't wait for a
        # separate fill event" timing (order_manager.py:1067-1076).
        state.entry_price = ltp
        state.highest_price = ltp
        state.sl = _resolve_rule_price(rule, ltp, "sl")
        target_price = _resolve_rule_price(rule, ltp, "target")
        state.targets = [target_price] if target_price is not None else []
        self.ctx.log("info", "entry placed", symbol=str(state.key), price=ltp,
                     sl=state.sl, targets=state.targets)

    def _build_entry_rule(self, state: _TokenState, price: float, candles) -> dict | None:
        """Precedence exactly as `open_trade` had it: GExp override (if on)
        takes priority over the preset's own candle_sl_mode; vanilla
        trailing rides on top of whichever base SL was chosen."""
        trail = None
        if self.params.get("enable_tsl") and not self.params.get("gexp_override"):
            step = float(self.params.get("tsl_step", 0) or 0)
            if step > 0:
                # Point mode, after=0: trails on every favourable point of
                # move — the closest existing LiveManager trail shape to
                # legacy's "trail on every new high" continuous behaviour.
                trail = {"mode": "point", "after": 0.01, "step": step}

        if self.params.get("gexp_override"):
            method = self.params.get("gexp_method", "approach1")
            state.gexp_override = True
            state.gexp_method = method
            if method == "approach1":
                sl_pct = float(self.params.get("gexp_sl_pct", 0) or 0)
                rr = float(self.params.get("gexp_rr", 0) or 0)
                return risk_rules.gexp_approach1_rule(price, sl_pct, rr)
            # approach2 — a plain points SL at entry; the trail from here is
            # continuous (see _trail_gexp2), no target.
            state.gexp_tsl_step = float(self.params.get("gexp_tsl_step", 0) or 0)
            return risk_rules.build_rule(
                side="BUY", entry=price,
                sl_mode="points", sl_val=state.gexp_tsl_step)

        candle_sl_mode = self.params.get("candle_sl_mode", "points")
        if candle_sl_mode == "pct_entry":
            return risk_rules.build_rule(
                side="BUY", entry=price,
                sl_mode="percent", sl_val=float(self.params.get("sl_pct_entry", 0) or 0),
                target_mode="percent", target_val=float(self.params.get("target_pct_entry", 0) or 0),
                trail=trail)
        if candle_sl_mode in ("prev_ohlc", "swing_low"):
            return risk_rules.build_rule(
                side="BUY", entry=price,
                sl_mode=candle_sl_mode, target_mode="prev_ohlc",
                candles=candles, sl_field=self.params.get("sl_ohlc_field", "low"),
                target_field=self.params.get("target_ohlc_field", "high"),
                swing_lookback=int(self.params.get("swing_lookback", 5) or 5),
                trail=trail)
        # "points" (or anything unrecognised) — no per-preset Day Profile
        # equivalent exists in Charticks, so a preset that wants a plain
        # points SL/Target sets sl_pct_entry-style values via candle_sl_mode
        # "pct_entry"; bare "points" mode has nothing to size itself from and
        # is refused the same way an unconfigured stop always is.
        return risk_rules.build_rule(side="BUY", entry=price, trail=trail)

    # ── continuous trailing (GExp approach-2) ───────────────────────────
    def _trail_gexp2(self, state: _TokenState, ltp: float) -> None:
        if not (state.gexp_override and state.gexp_method == "approach2"):
            return
        if ltp <= state.highest_price:
            return
        state.highest_price = ltp
        new_sl = risk_rules.gexp_approach2_trail_sl(state.highest_price, state.gexp_tsl_step)
        if state.sl is not None and new_sl <= state.sl:
            return
        if self.ctx.set_risk(state.key.position_id, sl=new_sl):
            state.sl = new_sl


register(StrategySpec(
    name="quant_preset",
    label="Quant Preset",
    description=("The ported legacy multi-filter engine (EMA/RSI/VWAP/ADX/"
                 "Supertrend + candle-based entry), configured by a JSON "
                 "preset — the same shape as the 24 existing presets in the "
                 "project's strategies/ folder."),
    factory=QuantPresetStrategy,
    params=(
        ParamField("index", "Index", kind="choice",
                  choices=("NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL"),
                  required=True),
        ParamField("lots", "Lots", kind="number", default=1, required=True),
        ParamField("live_interval", "Candle timeframe", kind="choice",
                  choices=("1min", "3min", "5min"), default="3min"),
    ),
))
