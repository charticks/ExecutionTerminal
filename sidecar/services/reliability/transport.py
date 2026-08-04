"""How a feed's connection is opened, closed, and delivers events.

`WebSocketManager` owns the *policy* — generation guards, backoff, staleness,
error classification — and that policy is broker-agnostic. What is not
broker-agnostic is the mechanics: Angel's SmartWebSocketV2 wants four callback
attributes assigned and a blocking `connect()`, while Dhan's SDK is asyncio and
wants a coroutine driven on an event loop. Wiring the Angel shape directly into
the manager, as it was, meant any second broker either had to fake that shape or
duplicate the whole reconnect policy.

A `Transport` is the seam. It knows one SDK; it knows nothing about retries.

Nothing here imports a broker SDK — concrete transports live next to the feed
that uses them.
"""
from __future__ import annotations

import asyncio
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class TransportCallbacks:
    """What a transport reports upward. Every one is invoked from whatever
    thread the SDK uses, so the manager guards them with a generation counter.
    """
    on_open: Callable[[], None]
    on_data: Callable[[Any], None]
    # (err, detail): `err` is the raw object handed to error classification;
    # `detail` is the human-readable string for logs. They differ because some
    # SDKs split one failure across two arguments — SmartWebSocketV2 reports
    # giving up as on_error("Max retry attempt reached", "Connection closed"),
    # and both halves are needed to explain why a feed never came up.
    on_error: Callable[[Any, str], None]
    on_close: Callable[[], None]


class Transport(ABC):
    """One SDK's connection mechanics."""

    @abstractmethod
    def open(self, cb: TransportCallbacks) -> None:
        """Begin connecting. Must return promptly — if the SDK's connect call
        blocks, run it on a thread. Connection success is reported through
        `cb.on_open`, not by returning."""

    @abstractmethod
    def close(self) -> None:
        """Tear down. Must be safe to call on an already-closed transport, and
        must never raise — callers close old connections on a best-effort path
        while a replacement is already being built."""

    @abstractmethod
    def handle(self) -> Any:
        """The object subscription calls are issued against (for a WebSocket
        SDK, the socket itself). The manager hands this to its `subscribe`
        callback and exposes it via `live_socket()`."""


class AsyncioTransport(Transport):
    """Base for SDKs whose client is a coroutine (Dhan's feed, among others).

    Runs a private event loop on its own daemon thread, so an async SDK can sit
    behind the same synchronous `Transport` contract as a callback-style one
    without the rest of the sidecar — which is thread-based throughout — having
    to grow an event loop of its own.

    Subclasses implement `run()`; it should await until the connection ends.
    """

    def __init__(self, name: str = "async-transport") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closing = False
        self._cb: TransportCallbacks | None = None

    @abstractmethod
    async def run(self, cb: TransportCallbacks) -> None:
        """Connect and pump events until the connection closes. Call
        `cb.on_open()` once established; raise or return to end the session."""

    async def shutdown(self) -> None:
        """Optional hook: close the SDK client. Runs on the transport's loop."""

    def open(self, cb: TransportCallbacks) -> None:
        self._cb = cb
        self._closing = False
        self._thread = threading.Thread(target=self._run_loop, args=(cb,),
                                        daemon=True, name=self._name)
        self._thread.start()

    def _run_loop(self, cb: TransportCallbacks) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.run(cb))
        except Exception as e:
            # A cancellation we caused via close() is an expected shutdown, not
            # a failure — reporting it would trip the reconnect/backoff policy
            # on every deliberate teardown.
            if not self._closing:
                cb.on_error(e, f"{type(e).__name__}: {e}")
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self._loop = None
            # Always report closure: the manager's reconnect policy is driven
            # entirely by on_close, so swallowing it strands the feed down.
            try:
                cb.on_close()
            except Exception:
                pass

    def close(self) -> None:
        self._closing = True
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self.shutdown(), loop)
            fut.result(timeout=5)
        except Exception:
            pass
        finally:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
