"""Centralised exponential-backoff math — replaces the same
`min(base * 2**attempt, cap)` snippet duplicated across
engines/option_chain_engine.py and broker_manager.py's market feed reconnect.

Stateless / thread-safe by construction: it hands back numbers, callers still
own their own thread/timer/attempt counter. This only exists so "avoid
infinite retry loops" is enforced in exactly one place.
"""
from __future__ import annotations


class RetryManager:
    def __init__(self, max_attempts: int = 5, base_delay: float = 5.0, cap: float = 60.0) -> None:
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.cap = cap

    def next_delay(self, attempt: int) -> float:
        """Delay (seconds) to wait before retry number `attempt` (0-indexed)."""
        return min(self.base_delay * (2 ** attempt), self.cap)

    def exhausted(self, attempt: int) -> bool:
        """True once `attempt` (0-indexed attempts already made) has used up
        the configured budget — callers should stop retrying and surface a
        terminal failure instead."""
        return attempt >= self.max_attempts
