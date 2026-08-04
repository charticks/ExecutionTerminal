"""Fan-out hub: engines publish events here; connected WebSocket clients receive them.

This is the single seam between the (synchronous, thread-based) trading engines
and the async web layer. Engines call `publish()` from any thread; the hub
forwards onto the event loop and broadcasts to all clients.
"""
from __future__ import annotations

import asyncio
from typing import Any


class EventHub:
    def __init__(self) -> None:
        self._clients: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def register(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._clients.add(q)
        return q

    def unregister(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)

    async def broadcast(self, event: dict[str, Any]) -> None:
        for q in list(self._clients):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow client — drop the frame rather than block the whole stream.
                pass

    def publish(self, event: dict[str, Any]) -> None:
        """Thread-safe entry point for engine callbacks."""
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self.broadcast(event), self._loop)


hub = EventHub()
