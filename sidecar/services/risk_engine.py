"""Server-side trading validation — the final say on whether an order is allowed.

Every check that can block an order lives here, runs in the sidecar, and runs
immediately before routing. The renderer keeps its own copies so it can grey out
a button or explain a limit without a round trip, but those are a convenience:
nothing the renderer does can let an order past this module.

Why this exists
---------------
Max Quantity, Max Price, Max Positions, Max Orders, Max Loss and Profit Target
were enforced only in the renderer, from localStorage. They were advisory: a
stale window, a renderer bug, or anything talking to the bridge directly went
straight through. Tick-size and away-from-LTP price checks existed only inside
the paper engine, so a fat-finger limit price was caught in paper and passed
untouched to a real broker in live.

Shape
-----
A rule is a function ``(ctx) -> Violation | None``. Rules are evaluated in
RULES order and the first violation wins, so cheap structural checks come before
ones that need quotes or the position book. Adding a control is a function plus
one list entry — no change to the order flow.

Config
------
Limits come from the active Trading Profile and the Home session bar, pushed by
the renderer (POST /risk-config) and re-pushed on every reconnect. Until that
push arrives ``configured`` is False and LIVE orders are refused outright: a
sidecar that does not yet know the user's loss limit must not be routing to a
broker. Paper is unaffected — there is nothing to protect.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import diagnostics

# Exchange price grid. Also enforced inside the paper engine; duplicated as a
# constant rather than imported to keep this module free of engine imports.
TICK_SIZE = 0.05
MIN_PRICE = 0.05
# A limit this far from the last traded price is a fat finger, not an intent.
MAX_AWAY_PCT = 50.0

PAPER = "paper"
LIVE = "live"


@dataclass(frozen=True)
class Violation:
    code: str
    error: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_response(self) -> dict:
        return {"ok": False, "code": self.code, "error": self.error, **self.details}


@dataclass
class RiskConfig:
    """Resolved limits. 0 always means "disabled" — matching the UI's own
    convention so a value shown as off is off on both sides."""
    configured: bool = False
    # Execution Defaults
    max_qty_per_order: int = 0
    max_price: float = 0.0
    # Risk / session limits
    max_positions: int = 0
    max_orders: int = 0
    max_loss: float = 0.0          # positive magnitude
    profit_target: float = 0.0
    # Session limits toggle: when the Home bar is off, only the profile's own
    # Execution Defaults apply.
    session_limits_enabled: bool = False
    # ── production guards, not exposed on the Settings page ───────────────
    # Rupee notional per order (qty x premium). A lot count says nothing about
    # money at risk: 30 lots of a ₹400 option and 30 of a ₹5 option are wildly
    # different trades, and only this catches the first.
    max_notional: float = 0.0
    # Runaway-loop protection. The auto-hedge retry path in particular has no
    # server-side bound of its own.
    max_orders_per_minute: int = 30
    # Reject an entry priced off a quote this old (seconds). 0 disables.
    max_quote_age_s: float = 0.0
    # Strike sanity: refuse an entry more than this % from spot. Catches a
    # mistyped strike (24000 -> 2400) that every other rule would wave through.
    max_strike_away_pct: float = 0.0

    @classmethod
    def from_payload(cls, body: dict) -> "RiskConfig":
        def num(key: str, default: float = 0.0) -> float:
            try:
                value = float(body.get(key, default) or 0)
            except (TypeError, ValueError):
                return 0.0
            return value if value > 0 else 0.0

        defaults = cls()
        return cls(
            configured=True,
            max_qty_per_order=int(num("maxQtyPerOrder")),
            max_price=num("maxPrice"),
            max_positions=int(num("maxPositions")),
            max_orders=int(num("maxOrders")),
            max_loss=num("maxLoss"),
            profit_target=num("profitTarget"),
            session_limits_enabled=bool(body.get("sessionLimitsEnabled")),
            max_notional=num("maxNotional"),
            # These carry a protective default rather than 0/disabled: they
            # guard against client bugs, so a client that omits them must not
            # be able to switch them off by omission.
            max_orders_per_minute=int(num("maxOrdersPerMinute",
                                          defaults.max_orders_per_minute)
                                      or defaults.max_orders_per_minute),
            max_quote_age_s=num("maxQuoteAgeSeconds"),
            max_strike_away_pct=num("maxStrikeAwayPct"),
        )


@dataclass
class OrderContext:
    """Everything a rule may consult. Built once per order so no rule reaches
    back into global state and every rule sees a consistent snapshot."""
    mode: str
    underlying: str
    expiry: str
    strike: float
    opt_type: str
    side: str
    qty: int
    lots: int
    order_type: str
    price: float
    product: str
    validity: str
    config: RiskConfig
    # Runtime state, resolved by the caller.
    ltp: float | None = None
    open_positions: int = 0
    held_lots: int = 0
    orders_today: int = 0
    session_pnl: float = 0.0
    # ── contract facts from the instrument master (None = master not loaded) ──
    lot_size: int | None = None
    tick_size: float | None = None
    # ── venue / feed state ───────────────────────────────────────────────────
    spot: float | None = None
    quote_age_s: float | None = None
    feed_stale: bool = False
    # Per-order quantity cap for the target broker(s), and whether the engine
    # may split to stay under it.
    broker_qty_cap: int | None = None
    supports_splitting: bool = True
    # Products the target broker(s) genuinely support for this instrument.
    unsupported_product: str | None = None
    # An identical order already working / just sent.
    duplicate_of: str | None = None
    # Explicit, audited escape hatches — mirrors `allow_duplicate`.
    override_max_pos: bool = False
    allow_duplicate: bool = False
    # True for an order that CLOSES existing exposure. Entry-only rules stand
    # down: a halt, a loss limit or a position cap must never be the reason a
    # user (or a stop loss) cannot get out of a trade. Structural and price
    # checks still apply — an exit with a broken quantity is still broken.
    is_exit: bool = False

    @property
    def symbol(self) -> str:
        return f"{self.underlying} {self.expiry} {int(self.strike)} {self.opt_type}"

    @property
    def effective_tick(self) -> float:
        return self.tick_size or TICK_SIZE

    @property
    def notional(self) -> float:
        """Rupee value of the order. MARKET orders have no price of their own,
        so they are valued at the last trade."""
        price = self.price if self.order_type == "LIMIT" and self.price > 0 else (self.ltp or 0.0)
        return price * self.qty


Rule = Callable[[OrderContext], "Violation | None"]

VALID_SIDES = ("BUY", "SELL")
VALID_OPT_TYPES = ("CE", "PE")
VALID_ORDER_TYPES = ("MARKET", "LIMIT")
VALID_PRODUCTS = ("NRML", "MIS")
VALID_VALIDITIES = ("DAY", "IOC")


# ── rules ──────────────────────────────────────────────────────────────────
# Ordered cheapest / most structural first.

def rule_enums(ctx: OrderContext) -> Violation | None:
    """Reject anything the broker SDKs would receive as a malformed field. These
    were previously passed straight through after an .upper()."""
    for value, allowed, label in (
        (ctx.side, VALID_SIDES, "side"),
        (ctx.opt_type, VALID_OPT_TYPES, "option type"),
        (ctx.order_type, VALID_ORDER_TYPES, "order type"),
        (ctx.product, VALID_PRODUCTS, "product"),
        (ctx.validity, VALID_VALIDITIES, "validity"),
    ):
        if value not in allowed:
            return Violation("INVALID_ORDER_FIELD",
                             f"Invalid {label} '{value}'. Expected one of "
                             f"{', '.join(allowed)}.")
    return None


def rule_quantity(ctx: OrderContext) -> Violation | None:
    if ctx.qty <= 0:
        return Violation("INVALID_QUANTITY", "Quantity must be greater than 0.")
    limit = ctx.config.max_qty_per_order
    if limit > 0 and ctx.qty > limit:
        return Violation(
            "MAX_QTY_PER_ORDER",
            f"Quantity {ctx.qty} exceeds your Max Quantity / Order limit ({limit}).",
            {"limit": limit, "requested": ctx.qty})
    return None


def rule_kill_switch(ctx: OrderContext) -> Violation | None:
    """Emergency halt blocks NEW ENTRIES only.

    Exits explicitly stand down: the usual reason for hitting the kill switch
    is to get flat, and a halt that blocked closing orders would trap the user
    in exactly the position they were trying to escape. It would also disable
    every stop loss the moment it was engaged."""
    from services.kill_switch import kill_switch

    if ctx.is_exit or not kill_switch.halted:
        return None
    state = kill_switch.state()
    return Violation(
        "TRADING_HALTED",
        f"Trading is halted — {state.get('reason') or 'kill-switch engaged'}. "
        f"Release the kill-switch to place new orders. Closing positions is "
        f"still allowed.",
        {"reason": state.get("reason")})


def rule_lot_size(ctx: OrderContext) -> Violation | None:
    """Quantity must be a whole multiple of the contract's real lot size, and
    must agree with the lot count the client sent.

    Nothing checked this before: the order splitter *inferred* lot size as
    qty/lots, so a client that miscounted defined its own truth and the wrong
    size went to the broker. The authority is the instrument master.
    """
    if not ctx.lot_size or ctx.lot_size <= 0:
        return None  # master not loaded — resolve_option will fail first anyway
    if ctx.qty % ctx.lot_size != 0:
        return Violation(
            "INVALID_LOT_SIZE",
            f"Quantity {ctx.qty} is not a multiple of the {ctx.underlying} lot "
            f"size ({ctx.lot_size}). Order sizes must be whole lots.",
            {"lotSize": ctx.lot_size, "qty": ctx.qty})
    if ctx.lots > 0 and ctx.lots * ctx.lot_size != ctx.qty:
        # Disagreement between the two numbers the client sent. Trusting either
        # would mean guessing which one the user actually meant.
        return Violation(
            "LOT_QTY_MISMATCH",
            f"Order is inconsistent: {ctx.lots} lot(s) of {ctx.underlying} is "
            f"{ctx.lots * ctx.lot_size}, but the quantity says {ctx.qty}.",
            {"lotSize": ctx.lot_size, "lots": ctx.lots, "qty": ctx.qty})
    return None


def rule_duplicate(ctx: OrderContext) -> Violation | None:
    """One working order per instrument + side. This existed only inside the
    paper engine, so LIVE — the mode where a double-fire costs money — had no
    duplicate protection at all."""
    if ctx.is_exit or ctx.allow_duplicate or not ctx.duplicate_of:
        return None
    return Violation(
        "DUPLICATE_PENDING",
        f"An order for {ctx.symbol} {ctx.side} is already working. Modify it "
        f"instead of placing another.",
        {"existingOrderId": ctx.duplicate_of})


def rule_limit_price(ctx: OrderContext) -> Violation | None:
    """Tick grid, minimum, and distance from the market. Previously paper-only —
    a live limit order was never price-checked at all."""
    if ctx.order_type != "LIMIT":
        return None
    price = ctx.price
    if price != price or price < MIN_PRICE:  # NaN or below the exchange floor
        return Violation("INVALID_PRICE",
                         f"Limit price must be at least ₹{MIN_PRICE:.2f}.")
    tick = ctx.effective_tick
    steps = round(price / tick)
    if abs(steps * tick - price) > 1e-6:
        return Violation("INVALID_PRICE",
                         f"Limit price must be a multiple of the ₹{tick:.2f} "
                         f"tick size.")
    if ctx.ltp and ctx.ltp > 0:
        away = abs(price - ctx.ltp) / ctx.ltp * 100
        if away > MAX_AWAY_PCT:
            return Violation(
                "PRICE_TOO_FAR",
                f"Limit price ₹{price:.2f} is {away:.0f}% away from the market "
                f"(LTP ₹{ctx.ltp:.2f}). Check the price before sending.",
                {"ltp": ctx.ltp, "awayPct": round(away, 1)})
    return None


def rule_max_price(ctx: OrderContext) -> Violation | None:
    limit = ctx.config.max_price
    if limit <= 0:
        return None
    # MARKET orders have no price to cap; the fill price is whatever the book
    # gives. Capping the (meaningless) 0 they carry would reject every one.
    if ctx.order_type != "LIMIT":
        return None
    if ctx.price > limit:
        return Violation(
            "MAX_PRICE",
            f"Price ₹{ctx.price:.2f} exceeds your Max Price limit (₹{limit:.2f}).",
            {"limit": limit, "requested": ctx.price})
    return None


def rule_session_locked(ctx: OrderContext) -> Violation | None:
    """Max Loss / Profit Target are hard locks: once breached, no NEW entry is
    allowed for the rest of the session. Closing orders are always allowed —
    hitting the loss limit must not prevent getting flat."""
    cfg = ctx.config
    if ctx.is_exit or not cfg.session_limits_enabled:
        return None
    if cfg.max_loss > 0 and ctx.session_pnl <= -abs(cfg.max_loss):
        return Violation(
            "MAX_LOSS_REACHED",
            f"Maximum Loss reached (₹{abs(ctx.session_pnl):,.0f} of "
            f"₹{cfg.max_loss:,.0f}). No new entries until you reset the session.",
            {"limit": cfg.max_loss, "sessionPnl": ctx.session_pnl})
    if cfg.profit_target > 0 and ctx.session_pnl >= cfg.profit_target:
        return Violation(
            "PROFIT_TARGET_REACHED",
            f"Profit Target achieved (₹{ctx.session_pnl:,.0f} of "
            f"₹{cfg.profit_target:,.0f}). No new entries until you reset the session.",
            {"limit": cfg.profit_target, "sessionPnl": ctx.session_pnl})
    return None


def rule_max_orders(ctx: OrderContext) -> Violation | None:
    cfg = ctx.config
    if ctx.is_exit or not cfg.session_limits_enabled or cfg.max_orders <= 0:
        return None
    if ctx.orders_today >= cfg.max_orders:
        return Violation(
            "MAX_TRADES_REACHED",
            f"Maximum Trades reached ({ctx.orders_today} of {cfg.max_orders}).",
            {"limit": cfg.max_orders, "count": ctx.orders_today})
    return None


def rule_max_positions(ctx: OrderContext) -> Violation | None:
    """Two distinct limits share this name in the UI, and both are enforced:
    the count of open positions, and the lots allowed on one instrument."""
    cfg = ctx.config
    limit = cfg.max_positions
    if ctx.is_exit or limit <= 0:
        return None
    # A genuinely new position — adding to a strike already held leaves the
    # open-position count unchanged, which is how the renderer treats it too.
    if cfg.session_limits_enabled and ctx.held_lots == 0 and ctx.open_positions >= limit:
        return Violation(
            "MAX_POSITIONS_REACHED",
            f"Maximum Positions reached ({ctx.open_positions} of {limit}).",
            {"limit": limit, "open": ctx.open_positions})
    if ctx.override_max_pos:
        # "Always Override" / an explicit user confirmation. Allowed, but it is
        # a deliberate breach of a configured limit, so it is never silent.
        if ctx.held_lots + ctx.lots > limit:
            diagnostics.event("risk", "Max Position override", "allowed",
                              level="warn", symbol=ctx.symbol, limit=limit,
                              held=ctx.held_lots, adding=ctx.lots)
        return None
    if ctx.held_lots + ctx.lots > limit:
        return Violation(
            "MAX_POSITION_LOTS",
            f"Adding {ctx.lots} lot(s) would take {ctx.symbol} to "
            f"{ctx.held_lots + ctx.lots} lots, over your Max Position limit ({limit}).",
            {"limit": limit, "held": ctx.held_lots, "requested": ctx.lots,
             "remaining": max(0, limit - ctx.held_lots)})
    return None


def rule_freeze_quantity(ctx: OrderContext) -> Violation | None:
    """Exchange freeze quantity. The splitter uses these caps to chop a large
    order into legal children — but when the broker does not support splitting
    the order was simply sent whole for the exchange to reject."""
    cap = ctx.broker_qty_cap
    if not cap or cap <= 0 or ctx.qty <= cap or ctx.supports_splitting:
        return None
    return Violation(
        "FREEZE_QTY_EXCEEDED",
        f"Quantity {ctx.qty} exceeds the exchange freeze limit for "
        f"{ctx.underlying} ({cap}), and this broker cannot split the order. "
        f"Send it as smaller orders.",
        {"cap": cap, "requested": ctx.qty})


def rule_broker_capability(ctx: OrderContext) -> Violation | None:
    """A broker that lacks the requested product must reject, not substitute.
    ICICI has no separate intraday product for options, and the router used to
    log a warning and place the order as carry-forward anyway — silently turning
    an intraday trade into a positional one."""
    if not ctx.unsupported_product:
        return None
    return Violation(
        "PRODUCT_NOT_SUPPORTED",
        f"{ctx.unsupported_product} does not support the {ctx.product} product "
        f"for options. Switch product, or disable Execute on that broker.",
        {"broker": ctx.unsupported_product, "product": ctx.product})


def rule_stale_quote(ctx: OrderContext) -> Violation | None:
    """Never price an entry off a frozen feed. A market order fills at whatever
    the book actually is, and the away-from-LTP check above is only as good as
    the quote behind it — both are dangerous against a dead feed."""
    # An exit is allowed on a stale feed: being unable to close because prices
    # stopped updating is worse than closing at an uncertain price.
    if ctx.is_exit:
        return None
    if ctx.feed_stale:
        return Violation(
            "FEED_STALE",
            "The market data feed is not currently updating, so this order "
            "would be priced off a stale quote. Wait for the feed to recover.")
    limit = ctx.config.max_quote_age_s
    if limit > 0 and ctx.quote_age_s is not None and ctx.quote_age_s > limit:
        return Violation(
            "QUOTE_TOO_OLD",
            f"The last quote for {ctx.symbol} is {ctx.quote_age_s:.0f}s old "
            f"(limit {limit:.0f}s). Wait for a fresh price.",
            {"quoteAgeSeconds": round(ctx.quote_age_s, 1)})
    return None


def rule_strike_sanity(ctx: OrderContext) -> Violation | None:
    """Catch a mistyped strike. 24000 typed as 2400 resolves to a real, tradeable
    contract and passes every other rule — this is the only thing that sees it."""
    # Entry-only: the strike of a position already held is not in question.
    limit = ctx.config.max_strike_away_pct
    if ctx.is_exit or limit <= 0 or not ctx.spot or ctx.spot <= 0:
        return None
    away = abs(ctx.strike - ctx.spot) / ctx.spot * 100
    if away > limit:
        return Violation(
            "STRIKE_TOO_FAR",
            f"Strike {int(ctx.strike)} is {away:.0f}% away from {ctx.underlying} "
            f"spot ({ctx.spot:,.0f}). Check the strike before sending.",
            {"spot": ctx.spot, "awayPct": round(away, 1)})
    return None


def rule_notional(ctx: OrderContext) -> Violation | None:
    """Rupee exposure per order. Lot counts hide the actual money at risk."""
    limit = ctx.config.max_notional
    if ctx.is_exit or limit <= 0:
        return None
    value = ctx.notional
    if value > limit:
        return Violation(
            "MAX_NOTIONAL",
            f"Order value ₹{value:,.0f} exceeds your per-order limit "
            f"(₹{limit:,.0f}).",
            {"limit": limit, "notional": round(value, 2)})
    return None


def rule_rate_limit(ctx: OrderContext) -> Violation | None:
    """Throttle. Protects against a client stuck in a loop — most plausibly the
    auto-hedge retry path, which has no server-side bound of its own."""
    limit = ctx.config.max_orders_per_minute
    if limit <= 0:
        return None
    recent = _rate_limiter.count_last_minute()
    if recent >= limit:
        return Violation(
            "RATE_LIMIT",
            f"Too many orders in the last minute ({recent}, limit {limit}). "
            f"This usually means something is retrying in a loop — Charticks "
            f"stopped it rather than keep sending.",
            {"limit": limit, "recent": recent})
    return None


class _RateLimiter:
    """Rolling one-minute count of accepted orders. Only orders that passed
    validation are recorded, so a burst of rejections cannot lock the user out."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stamps: list[float] = []

    def count_last_minute(self) -> int:
        cutoff = time.time() - 60.0
        with self._lock:
            self._stamps = [t for t in self._stamps if t >= cutoff]
            return len(self._stamps)

    def record(self) -> None:
        with self._lock:
            self._stamps.append(time.time())

    def reset(self) -> None:
        with self._lock:
            self._stamps.clear()


_rate_limiter = _RateLimiter()


# Order matters: structural → contract facts → venue state → sizing → price →
# session state. Cheap local checks run before anything needing a quote, and the
# halt is first so nothing else can matter while trading is stopped.
RULES: list[Rule] = [
    rule_kill_switch,
    rule_enums,
    rule_quantity,
    rule_lot_size,
    rule_broker_capability,
    rule_duplicate,
    rule_rate_limit,
    rule_stale_quote,
    rule_limit_price,
    rule_max_price,
    rule_freeze_quantity,
    rule_notional,
    rule_strike_sanity,
    rule_session_locked,
    rule_max_orders,
    rule_max_positions,
]


class RiskEngine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._config = RiskConfig()

    # ── config, pushed by the renderer ─────────────────────────────────────
    def set_config(self, body: dict) -> dict:
        config = RiskConfig.from_payload(body or {})
        with self._lock:
            self._config = config
        diagnostics.event(
            "risk", "Risk config synced", "success",
            maxQtyPerOrder=config.max_qty_per_order, maxPrice=config.max_price,
            maxPositions=config.max_positions, maxOrders=config.max_orders,
            maxLoss=config.max_loss, profitTarget=config.profit_target,
            sessionLimits="on" if config.session_limits_enabled else "off")
        return {"ok": True, "configured": True}

    @property
    def config(self) -> RiskConfig:
        with self._lock:
            return self._config

    # ── evaluation ─────────────────────────────────────────────────────────
    def validate(self, ctx: OrderContext) -> Violation | None:
        """Run every rule. Returns the first violation, or None to allow."""
        diagnostics.event("risk", "Risk validation", "started", level="debug",
                          mode=ctx.mode, symbol=ctx.symbol, side=ctx.side,
                          qty=ctx.qty, orderType=ctx.order_type, price=ctx.price)

        # A live order may never be validated against limits we have not been
        # told. Paper has nothing at stake, so it proceeds on defaults.
        if ctx.mode == LIVE and not ctx.config.configured:
            violation = Violation(
                "RISK_CONFIG_NOT_SYNCED",
                "Charticks has not finished syncing your risk settings, so live "
                "orders are blocked. This clears on its own in a moment — if it "
                "persists, reopen Settings to re-apply your profile.")
            self._log_violation(ctx, violation)
            return violation

        for rule in RULES:
            try:
                violation = rule(ctx)
            except Exception as exc:
                # A crashing rule must not become an open door. Fail closed and
                # log the trace — an unvalidatable order is not a safe order.
                diagnostics.exception("risk", "Risk rule crashed", exc_info=exc,
                                      rule=rule.__name__, symbol=ctx.symbol)
                violation = Violation(
                    "RISK_CHECK_FAILED",
                    "A risk check could not be completed, so the order was not "
                    "sent. See logs/exceptions.log.")
            if violation is not None:
                self._log_violation(ctx, violation, rule=rule.__name__)
                return violation

        # Only accepted orders count toward the throttle, so a burst of
        # rejections can never lock the user out of trading.
        _rate_limiter.record()
        diagnostics.event("risk", "Risk validation", "success", level="debug",
                          mode=ctx.mode, symbol=ctx.symbol)
        return None

    def _log_violation(self, ctx: OrderContext, violation: Violation,
                       rule: str = "") -> None:
        fields = {
            "rule": rule, "mode": ctx.mode, "symbol": ctx.symbol,
            "side": ctx.side, "qty": ctx.qty, "lots": ctx.lots,
            "orderType": ctx.order_type, "price": ctx.price,
            "code": violation.code, "reason": violation.error,
        }
        # A violation's details legitimately repeat context keys (rule_lot_size
        # reports the offending `qty`, for instance). Prefix rather than pass
        # them through: duplicate keywords used to raise TypeError here, and
        # since this runs OUTSIDE the per-rule try/except, that turned a clean
        # rejection into a 500.
        for key, value in violation.details.items():
            fields[key if key not in fields else f"detail_{key}"] = value
        diagnostics.event("risk", "Risk validation", "rejected", **fields)


risk_engine = RiskEngine()
