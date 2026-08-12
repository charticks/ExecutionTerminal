"""Multi-broker order-limit resolution.

The execution engine must never know that "Angel caps NIFTY at 27 lots". It asks
this service for the limits that apply to a given (account, broker, underlying)
and splits accordingly. Resolution order:

  1. API     — the broker's adapter exposes live limits (register_adapter below).
  2. Config  — config/broker_limits.json, keyed broker -> UNDERLYING -> limits,
               with a per-broker "*" fallback and a top-level "default" block.
  3. Default — no cap, splitting off (today's single-order behaviour).

Resolved limits are cached per (account_id, underlying) for the life of the
session; `invalidate(account_id)` is called by the broker manager on connect,
reconnect and disconnect so a re-established session re-queries the broker.

Adding a broker = a JSON entry (+ optionally an adapter). No engine changes.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import diagnostics
from bridge import events
from bridge.hub import hub

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "broker_limits.json"


@dataclass(frozen=True)
class OrderLimits:
    """Maximum size a single order may carry at this broker."""
    max_lots_per_order: int | None = None
    max_qty_per_order: int | None = None
    supports_splitting: bool = False
    source: str = "default"  # "api" | "config" | "default"

    def cap_qty(self, lot_size: int) -> int | None:
        """The effective per-order quantity cap for the SPLITTER, folding both
        dimensions together. None means "send the order whole"."""
        if not self.supports_splitting:
            return None
        return self.hard_cap_qty(lot_size)

    def hard_cap_qty(self, lot_size: int) -> int | None:
        """The exchange freeze cap itself, regardless of whether this broker can
        split to stay under it.

        cap_qty() returns None when splitting is unsupported, which is right for
        the splitter but hid the limit from validation: an oversized order at a
        non-splitting broker was sent whole for the exchange to reject.
        """
        caps = []
        if self.max_lots_per_order and lot_size > 0:
            caps.append(int(self.max_lots_per_order) * int(lot_size))
        if self.max_qty_per_order:
            caps.append(int(self.max_qty_per_order))
        return min(caps) if caps else None


NO_LIMIT = OrderLimits()

# ── adapter registry ──────────────────────────────────────────────────────
# A broker adapter is `fn(session, underlying) -> OrderLimits | None`. Returning
# None means "this broker can't tell us" and the resolver falls through to config.
LimitFetcher = Callable[[Any, str], "OrderLimits | None"]
_ADAPTERS: dict[str, LimitFetcher] = {}


def register_adapter(broker: str, fetcher: LimitFetcher) -> None:
    _ADAPTERS[(broker or "").lower()] = fetcher


def _angel_fetch(_session: Any, _underlying: str) -> OrderLimits | None:
    """Angel SmartAPI exposes no freeze-quantity / max-order-size endpoint and the
    instrument master carries no freeze column, so there is nothing to query —
    the resolver falls back to config. Kept as the reference adapter shape: when
    Angel (or any broker) adds the capability, only this function changes."""
    return None


register_adapter("angel", _angel_fetch)


class BrokerLimitResolver:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], OrderLimits] = {}
        self._config: dict | None = None

    # ── config ────────────────────────────────────────────────────────────
    def _load_config(self) -> dict:
        if self._config is None:
            try:
                self._config = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception as exc:  # missing/corrupt config must never block trading
                self._log("warn", f"[limits] could not read broker_limits.json ({exc}) "
                                  f"— orders will not be split")
                self._config = {}
        return self._config

    def _from_config(self, broker: str, underlying: str) -> OrderLimits | None:
        cfg = self._load_config()
        for scope in (cfg.get(broker) or {}, cfg.get("default") or {}):
            raw = scope.get(underlying) or scope.get("*")
            if raw:
                return OrderLimits(
                    max_lots_per_order=raw.get("maxLotsPerOrder"),
                    max_qty_per_order=raw.get("maxQtyPerOrder"),
                    supports_splitting=bool(raw.get("supportsSplitting")),
                    source="config",
                )
        return None

    # ── resolution ────────────────────────────────────────────────────────
    def resolve(self, account_id: str, broker: str, session: Any,
                underlying: str) -> OrderLimits:
        broker = (broker or "").lower()
        underlying = (underlying or "").upper()
        key = (account_id, underlying)
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit

        limits: OrderLimits | None = None
        fetcher = _ADAPTERS.get(broker)
        if fetcher is not None and session is not None:
            try:
                fetched = fetcher(session, underlying)
                if fetched is not None:
                    limits = OrderLimits(fetched.max_lots_per_order,
                                         fetched.max_qty_per_order,
                                         fetched.supports_splitting, "api")
            except Exception as exc:  # a flaky broker API must not block the order
                self._log("warn", f"[limits] {broker} limit lookup failed ({exc}) "
                                  f"— using configured limits")

        if limits is None:
            limits = self._from_config(broker, underlying) or NO_LIMIT

        with self._lock:
            self._cache[key] = limits
        self._log("info", f"[limits] {broker}/{underlying}: "
                          f"maxLots={limits.max_lots_per_order} "
                          f"maxQty={limits.max_qty_per_order} "
                          f"split={limits.supports_splitting} (source={limits.source})")
        return limits

    def invalidate(self, account_id: str | None = None) -> None:
        """Drop cached limits so the next order re-resolves. Called on broker
        connect / reconnect / disconnect."""
        with self._lock:
            if account_id is None:
                self._cache.clear()
            else:
                for key in [k for k in self._cache if k[0] == account_id]:
                    del self._cache[key]

    def _log(self, level: str, msg: str) -> None:
        try:
            diagnostics.emit("broker", level, msg, publish=True)
        except Exception:  # logging must never break limit resolution
            pass


limit_resolver = BrokerLimitResolver()
