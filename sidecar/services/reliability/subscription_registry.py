"""Live registry of active market-data subscriptions so a reconnect can
restore *everything* the user had running — indices, option chain strikes,
watchlists, etc. — instead of only the one feed that happened to own the
socket at connect time.

kind/key namespace subscriptions (e.g. kind="index", key="NIFTY";
kind="option_chain", key="NIFTY:24000:CE"); `spec` is opaque payload the
owning adapter knows how to resubscribe from.
"""
from __future__ import annotations

import threading
from typing import Any, Callable


class SubscriptionRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str], Any] = {}
        # Per-kind callback invoked with the list of specs for that kind when
        # a reconnect needs everything replayed (e.g. a resubscribe() on the
        # relevant adapter/engine).
        self._replayers: dict[str, Callable[[list[Any]], None]] = {}

    def register(self, kind: str, key: str, spec: Any) -> None:
        with self._lock:
            self._entries[(kind, key)] = spec

    def unregister(self, kind: str, key: str) -> None:
        with self._lock:
            self._entries.pop((kind, key), None)

    def clear_kind(self, kind: str) -> None:
        with self._lock:
            for k in [k for k in self._entries if k[0] == kind]:
                del self._entries[k]

    def all(self, kind: str | None = None) -> list[Any]:
        with self._lock:
            if kind is None:
                return list(self._entries.values())
            return [v for (k, _key), v in self._entries.items() if k == kind]

    def set_replayer(self, kind: str, fn: Callable[[list[Any]], None]) -> None:
        """Register the function that knows how to resubscribe every spec of
        `kind` (e.g. OptionChainAdapter.resubscribe)."""
        with self._lock:
            self._replayers[kind] = fn

    def replay_all(self) -> None:
        """Called after a successful reconnect — resubscribes every kind that
        has a registered replayer, using whatever specs are currently held."""
        with self._lock:
            replayers = dict(self._replayers)
        for kind, fn in replayers.items():
            specs = self.all(kind)
            if specs:
                fn(specs)
