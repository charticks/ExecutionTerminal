"""Strategy plugin registry.

Same idiom as every broker registry in this codebase (``services/margin/base.py``,
``services/order_sync/base.py``, ``services/idempotency/base.py``,
``services/feeds/registry.py``): a flat ``dict[str, T]``, ``register()``,
a by-name lookup, and a discovery list. Registration happens at import time,
as a side effect of importing each plugin module from ``strategies/__init__.py``
— explicitly, not by scanning the directory. A strategy that fails to import
is a loud error at process start, not a silently-missing entry in the list;
the same reasoning ``services/margin/__init__.py`` gives for brokers applies
here unchanged.
"""
from __future__ import annotations

from .base import StrategySpec

_SPECS: dict[str, StrategySpec] = {}


def register(spec: StrategySpec) -> None:
    key = spec.name.lower()
    if key in _SPECS:
        raise ValueError(f"strategy '{spec.name}' is already registered")
    _SPECS[key] = spec


def spec_for(name: str) -> StrategySpec | None:
    return _SPECS.get((name or "").lower())


def registered_strategies() -> list[str]:
    return sorted(_SPECS)


def all_specs() -> list[StrategySpec]:
    return [_SPECS[k] for k in sorted(_SPECS)]
