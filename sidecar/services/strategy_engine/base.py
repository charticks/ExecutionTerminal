"""The strategy plugin contract.

A strategy is a signal generator, nothing else. It never touches a broker SDK
or a market-data socket directly — it is handed a narrow ``StrategyContext``
facade (Phase 3 grows this to place orders; Phase 2 grows it to read candles)
and reacts to the callbacks the platform calls on it. This is deliberately
NOT the shape the legacy Tkinter bot used: there, a strategy's code read and
wrote a shared app object (Tkinter widgets, ~15 config variables, a single
position dict everyone raced on). Here, everything a strategy can see or do
is enumerated on ``StrategyContext``, and every running instance gets its own
context and its own state — two instances of the same strategy watching two
different contracts cannot see or corrupt each other's data, which the
legacy multi-strategy runner could (see the state-file collision noted
during the port audit).

See ``sidecar/services/strategy_engine/registry.py`` for how a module
registers itself, and ``manager.py`` for what calls these methods and when.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable


class StrategyContext:
    """Everything one running strategy instance is allowed to do.

    Deliberately narrow. A strategy holds this and nothing else — no broker
    session, no SDK object, no reference to the platform's own singletons.
    Market-data access (Phase 2) and order placement (Phase 3) are added here
    as they land, each as a thin pass-through to the platform's existing,
    already-hardened services (the same ones the Positions screen's own
    buttons call) — never a second implementation of what they do.
    """

    def __init__(self, instance_id: str, spec_name: str, params: dict,
                strategy: "Strategy", manager: Any) -> None:
        self.instance_id = instance_id
        self.spec_name = spec_name
        self.params = params
        self._strategy = strategy
        # The StrategyManager that OWNS this instance — never the module-
        # level `strategy_manager` singleton imported directly, so a test (or
        # a future second manager) gets correct ownership bookkeeping rather
        # than silently writing to/reading from a different manager's map.
        self._manager = manager
        self._candle_keys: set[tuple[Any, str]] = set()
        self._tick_keys: set[Any] = set()

    # ── market data (Phase 2) ────────────────────────────────────────────
    def subscribe_candles(self, key: Any, timeframe: str) -> None:
        """Ask the shared CandleStore for this instrument/index's candles at
        `timeframe`, and start receiving this instance's own
        `on_candle_close(key, df)` whenever one closes. Ref-counted — many
        instances can subscribe to the same series without duplicating the
        underlying tick handling or market-data subscription."""
        from .candles import candle_store

        candle_store.subscribe(self.instance_id, key, timeframe,
                               on_close=self._on_candle_close)
        self._candle_keys.add((key, timeframe))

    def unsubscribe_candles(self, key: Any, timeframe: str) -> None:
        from .candles import candle_store

        candle_store.unsubscribe(self.instance_id, key, timeframe)
        self._candle_keys.discard((key, timeframe))

    def get_candles(self, key: Any, timeframe: str):
        """The current candle DataFrame for a subscribed series, or None if
        not enough candles have closed yet — the "cold start" a strategy
        begun mid-session must handle (see Phase 2's warm-up decision)."""
        from .candles import candle_store

        return candle_store.get_candles(key, timeframe)

    def get_tick(self, key: Any) -> dict:
        """The current tick snapshot for a subscribed OPTION contract —
        {"ltp", "bid", "ask", "oi", "volume", "ts"} (see feed_router.py's
        option_ticks cache), or {} if nothing has ticked yet. Reuses the
        exact same read model LiveManager itself uses (`manager.
        get_option_tick`); index symbols have no such snapshot (their only
        signal is the ltp already delivered to on_tick)."""
        from services.broker_manager import manager as broker_manager

        return broker_manager.get_option_tick(key)

    def subscribe_ticks(self, key: Any) -> None:
        """Receive this instance's own on_tick(key, ltp) for every tick on
        `key`. Independent of subscribe_candles — a strategy watching a
        position it already holds for tick-level reaction does not need to
        pay for candle bucketing on a timeframe it will never read."""
        self._manager.subscribe_ticks(self.instance_id, key)
        self._tick_keys.add(key)

    def unsubscribe_ticks(self, key: Any) -> None:
        self._manager.unsubscribe_ticks(self.instance_id, key)
        self._tick_keys.discard(key)

    def _on_candle_close(self, key: Any, candles: Any) -> None:
        """Routed here (not straight into the strategy) so a bug in one
        instance's on_candle_close is caught at the same boundary on_start/
        on_stop already are — one instance misbehaving must never take
        another instance's candle delivery down with it."""
        try:
            self._strategy.on_candle_close(key, candles)
        except Exception as exc:
            import diagnostics

            diagnostics.exception("strategy", "on_candle_close failed",
                                  exc_info=exc, instanceId=self.instance_id,
                                  strategy=self.spec_name, key=str(key))

    def _release(self) -> None:
        """Called by the manager when this instance stops — releases every
        candle AND tick subscription (and, transitively, the market-data
        subscription candles implied) rather than leaking a stopped
        instance's watch forever."""
        from .candles import candle_store

        for key, timeframe in list(self._candle_keys):
            candle_store.unsubscribe(self.instance_id, key, timeframe)
        self._candle_keys.clear()
        for key in list(self._tick_keys):
            self._manager.unsubscribe_ticks(self.instance_id, key)
        self._tick_keys.clear()

    # ── trading (Phase 3) ─────────────────────────────────────────────────
    # Every method here is a thin pass-through to the platform's existing,
    # already-hardened order pipeline — the exact same calls the Positions
    # screen's own buttons make. A strategy never touches a broker session,
    # never sees `order_manager`'s LIVE/PAPER mode gate, and never gets to
    # bypass a risk/margin/idempotency check: nothing here sets
    # `allow_duplicate`/`override_max_pos`, which is precisely what makes a
    # fresh strategy entry indistinguishable — to every protection in the
    # pipeline — from a human placing the same order by hand.
    def place_order(self, underlying: str, expiry: str, strike: float,
                    opt_type: str, side: str, qty: int, lots: int,
                    order_type: str = "MARKET", price: float = 0.0,
                    rule: dict | None = None, product: str = "NRML",
                    validity: str = "DAY", tag: str = "") -> dict:
        """Place a real live entry order. `rule` is a LiveManager-shaped SL/
        Target/Trail dict — build one with `strategy_engine.risk_rules.
        build_rule` rather than assembling it by hand, so every strategy
        derives risk the same, already-validated way.

        On success, this instance is recorded as the position's owner (see
        `manager.StrategyManager.claim_position`) — position identity is
        derived from the CONTRACT, not the order, so the claim is made
        immediately rather than waiting for the fill to confirm; nothing
        else in the book exists to accidentally act on until it does.
        """
        from services.instruments import InstrumentKey
        from services.order_manager import LIVE, order_manager

        request_id = (f"strategy:{self.instance_id}:"
                      f"{tag or f'{underlying}{int(strike)}{opt_type}{side}'}")
        result = order_manager.place_order(
            LIVE, underlying, expiry, strike, opt_type, side, qty, order_type,
            price, lots=lots, rule=rule, product=product, validity=validity,
            request_id=request_id)
        if result.get("ok"):
            position_id = InstrumentKey.option(
                underlying, expiry, strike, opt_type).position_id
            self._manager.claim_position(position_id, self.instance_id)
        return result

    def close_position(self, position_id: str, fraction: float = 1.0) -> dict:
        """Close all or part of a position THIS instance owns."""
        refusal = self._require_ownership(position_id)
        if refusal is not None:
            return refusal
        from services.live_manager import live_manager

        return live_manager.close_position(position_id, fraction)

    def adjust_lots(self, position_id: str, delta: int) -> dict:
        """Grow or shrink a position THIS instance owns by `delta` lots."""
        refusal = self._require_ownership(position_id)
        if refusal is not None:
            return refusal
        from services.live_manager import live_manager

        return live_manager.adjust_lots(position_id, delta)

    def set_risk(self, position_id: str, sl: float | None = None,
                target: float | None = None) -> bool:
        """Edit the SL/Target of a position THIS instance owns — the
        primitive a strategy that wants CONTINUOUS repegging (VWAP-adaptive,
        gamma-expansion trailing — deferred features, see risk_rules.py's
        docstring) will call from its own `on_candle_close`. Silently
        refuses (returns False, same as live_book.set_risk's own "no such
        position" contract) rather than raising, for a position this
        instance does not own."""
        if self._require_ownership(position_id) is not None:
            return False
        from services.live_book import live_book

        return live_book.set_risk(position_id, sl=sl, target=target)

    def get_position(self, position_id: str) -> Any:
        """Read-only — any instance may look at any position (needed for a
        portfolio-aware strategy), only mutation is ownership-gated."""
        from services.live_book import live_book

        return live_book.get(position_id)

    def _require_ownership(self, position_id: str) -> dict | None:
        if self._manager.owns(position_id, self.instance_id):
            return None
        return {"ok": False, "code": "NOT_OWNER",
                "error": "This position was not opened by this strategy instance."}

    def log(self, level: str, message: str, **fields: Any) -> None:
        """A log line attributable to this one instance.

        Goes through the shared ``diagnostics`` sink (so it lands in the same
        durable log files as everything else — unbounded, filterable by
        instance id) AND the manager's small in-memory ring buffer, which is
        what the Terminal UI's log tail actually reads (see
        StrategyManager.record_log/logs_of) and pushes live over the
        WebSocket as a ``strategy_log`` event.
        """
        import diagnostics

        diagnostics.emit("strategy", level, message,
                         instanceId=self.instance_id, strategy=self.spec_name,
                         **fields)
        self._manager.record_log(self.instance_id, self.spec_name, level, message)


class Strategy(ABC):
    """Base class every strategy plugin implements.

    Lifecycle only, in this file — a strategy that only implements
    ``on_start``/``on_stop`` is already valid (it just never reacts to
    anything, which is a legitimate no-op strategy for testing the
    framework). ``on_candle_close``/``on_tick`` are optional hooks a real
    strategy overrides once Phase 2 gives them market data to react to; the
    default implementations here are deliberately inert, not
    ``NotImplementedError`` — a strategy that only trades off ticks should
    not have to stub out a candle handler it will never use, and vice versa.
    """

    #: Set by StrategyManager before on_start is called. Present here (not
    #: only on the context) so a strategy that logs from a helper method
    #: doesn't need to thread ctx through every call.
    ctx: StrategyContext

    @abstractmethod
    def on_start(self, ctx: StrategyContext, params: dict) -> None:
        """Called once when this instance transitions to RUNNING.

        Raising here is caught by StrategyManager and lands the instance in
        ERROR rather than crashing anything else — see manager.py."""

    @abstractmethod
    def on_stop(self) -> None:
        """Called once when this instance transitions to STOPPED (including
        an operator-initiated stop and a graceful shutdown). Must be safe to
        call even if on_start partially failed."""

    def on_candle_close(self, key: str, candles: Any) -> None:
        """A subscribed instrument's candle just closed. `key` is the
        canonical instrument/index identity the strategy subscribed to (see
        Phase 2's CandleStore); `candles` is that store's OHLCV+indicator
        frame. No-op by default."""

    def on_tick(self, key: str, ltp: float) -> None:
        """A subscribed instrument ticked. No-op by default — most strategies
        only need candle-close cadence; a strategy that wants tick-level
        reaction (e.g. its own exit logic before Phase 3's LiveManager
        handoff takes over) overrides this."""

    # ── restart survival (Phase 4) ──────────────────────────────────────
    def get_state(self) -> dict:
        """Arbitrary JSON-serializable state to persist across a restart —
        an entry-taken-today flag, a target-hit index, anything beyond
        candle buffers (which rebuild live off ticks, see Phase 2) and
        beyond the position book itself (already restored by the existing
        reconciliation flow, unchanged — a strategy's position looks like
        any other Charticks position). Default: nothing to persist, which is
        the right answer for a stateless strategy and does not need
        overriding just to say so."""
        return {}

    def restore_state(self, state: dict) -> None:
        """Called once, immediately after a successful on_start, ONLY when
        this instance has non-empty state saved from a previous run — a
        strategy starting fresh never receives this call, so it need not
        handle "no state" itself. Default: ignore it."""

    # ── UI status (Strategy & Positions UI redesign) ────────────────────
    def phase(self) -> str | None:
        """A finer-grained status than `state` (which is a process lifecycle
        — NEW/RUNNING/STOPPED/ERROR — and stays exactly as-is) for a RUNNING
        instance: whether it is actively holding a position, still watching
        for a signal, or done for the day. One of:
          "waiting"      — running, not yet armed, or armed and watching for
                            an entry signal with nothing open.
          "in_position"  — currently holding at least one open trade.
          "completed"    — every entry opportunity for today has already
                            been taken (or exhausted); nothing left to watch.
        Default None — "unknown/not applicable," the right answer for a
        strategy type that doesn't have this notion of a trading day (the
        manager falls back to the plain `state` label for display)."""
        return None


@dataclass(frozen=True)
class ParamField:
    """One configurable parameter a strategy instance takes, for the UI to
    render a form from (Phase 5) and for basic validation (Phase 1)."""
    key: str
    label: str
    kind: str = "string"          # string | number | bool | choice
    default: Any = None
    choices: tuple[str, ...] = ()
    required: bool = False

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "kind": self.kind,
                "default": self.default, "choices": list(self.choices),
                "required": self.required}


@dataclass(frozen=True)
class StrategySpec:
    """What a strategy plugin registers itself as.

    ``factory`` builds a fresh ``Strategy`` instance per running instance —
    a callable rather than a bare class reference so a plugin can do
    constructor work (or simply be ``MyStrategy`` itself, which is callable).
    """
    name: str
    label: str
    description: str
    factory: Callable[[], Strategy]
    params: tuple[ParamField, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        """JSON-safe — `factory` is deliberately excluded, it is not
        meaningful outside the process. This is what GET /strategies serves
        for the Terminal UI to render a "new instance" form from."""
        return {"name": self.name, "label": self.label,
               "description": self.description,
               "params": [p.to_dict() for p in self.params]}
