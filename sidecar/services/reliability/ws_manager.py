"""Generic reconnecting-feed wrapper: retry policy, liveness, and error routing.

Broker-agnostic by construction. It drives a `Transport` (see
reliability.transport) rather than a socket, so it holds the *policy* — the
generation-counter guard against stale callbacks, RetryManager backoff, and
error classification via reliability.errors so an AG8001 surfaced through the
feed triggers the same session-recovery path as one surfaced through REST —
while the transport holds one SDK's mechanics. Previously the two were fused:
this class assigned SmartWebSocketV2's four callback attributes directly, which
any non-Angel feed would have had to imitate.

Also tracks heartbeat/tick staleness (`stale` property) the way the retired
Tkinter bot's OptionChainEngine.canary_alive did (legacy/engines/), so
ConnectionHealthMonitor has one shape to poll for both feeds.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

import diagnostics
from bridge import events
from bridge.hub import hub

from .retry_manager import RetryManager
from .transport import Transport, TransportCallbacks

# How long a connection that reported an error is given to either recover on its
# own or close cleanly, before it is treated as ended and retried.
#
# This exists because on_close is not a guarantee. An SDK can report a terminal
# failure through on_error and never close: SmartWebSocketV2 hands its give-up to
# on_error("Max retry attempt reached", "Connection closed") and, on
# websocket-client >= 1.0, its own on_close never survives the call (see
# AngelTransport). Reconnect used to be driven by on_close alone, so that
# combination left the feed down with no timer scheduled — permanently, until the
# app was restarted.
ERROR_GRACE_S = 10.0


class WebSocketManager:
    def __init__(
        self,
        name: str,
        build_transport: Callable[[], Transport],
        subscribe: Callable[[Any], None],
        report_error: Callable[[Any], str] | None = None,
        on_tick: Callable[[Any, Any], None] | None = None,
        retry: RetryManager | None = None,
        stale_after: float = 15.0,
    ) -> None:
        """
        name              — label used in logs (e.g. "market-feed").
        build_transport() — returns a freshly-constructed Transport carrying
                            current credentials (call sites rebuild this each
                            attempt so a post-recovery reconnect picks up the
                            new session tokens).
        subscribe(handle) — called on_open to (re)subscribe every active token,
                            with the transport's handle (for a WebSocket SDK,
                            the socket).
        report_error(err) -> classification — reliability.errors.classify_error
                         or a SessionManager.report_error partial; None skips
                         classification (plain retry).
        """
        self.name = name
        self._build_transport = build_transport
        self._subscribe = subscribe
        self._report_error = report_error
        self._on_tick = on_tick
        self._retry = retry or RetryManager(max_attempts=0, base_delay=5, cap=60)  # 0 = unbounded feed retries
        self.stale_after = stale_after

        self.should_run = False
        self.connected = False
        self.last_tick_ts: float = 0.0
        # Diagnostics for a feed that never comes up: why it failed, when, and
        # how many attempts in a row have failed. Surfaced to the UI so a dead
        # data plane can't hide behind a green "broker connected" badge.
        self.last_error: str | None = None
        self.last_error_ts: float = 0.0
        self.consecutive_failures = 0
        self.last_connected_ts: float = 0.0
        self._transport: Transport | None = None
        self._generation = 0
        self._attempt = 0
        self._lock = threading.Lock()

    def _log(self, level: str, msg: str) -> None:
        diagnostics.emit("websocket", level, f"[ws:{self.name}] {msg}",
                         publish=True, feed=self.name)

    @property
    def stale(self) -> bool:
        """True if we're supposedly connected but no tick has arrived within
        stale_after seconds — a silent/zombie connection (heartbeat lost)."""
        if not self.connected:
            return False
        if self.last_tick_ts == 0.0:
            return False  # just connected, hasn't had a chance to tick yet
        return (time.time() - self.last_tick_ts) > self.stale_after

    def start(self) -> None:
        self.should_run = True
        self._attempt = 0
        self._connect()

    def stop(self) -> None:
        self.should_run = False
        if self._transport is not None:
            self._safe_close(self._transport)

    def reconnect(self) -> None:
        """Force a fresh connection now (e.g. after session re-auth changed
        the tokens) instead of waiting for the current backoff timer."""
        self._attempt = 0
        self._connect()

    def _connect(self) -> None:
        # A dropped attempt used to be permanent: two reconnect paths (the
        # backoff timer and the health watchdog) race, the loser returned
        # immediately, and if the winner's socket then failed there was no
        # timer left alive — should_run stayed True, connected stayed False,
        # and NOTHING ever retried again. Wait briefly for the lock, and if we
        # still can't get it, hand the attempt to a retry rather than dropping.
        if not self._lock.acquire(timeout=2.0):
            self._schedule_retry()
            return
        try:
            gen = self._generation + 1
            self._generation = gen
            # The new transport is not open yet, so we are NOT connected.
            # Without this, `connected` kept the previous generation's True
            # value (the old transport's on_close is generation-guarded and
            # returns early), so callers saw "connected" while `_transport`
            # pointed at one that had never opened — and anything they sent on
            # it was lost.
            self.connected = False

            old = self._transport
            if old is not None:
                threading.Thread(target=self._safe_close, args=(old,), daemon=True,
                                 name=f"{self.name}-close").start()

            transport = self._build_transport()
            self._transport = transport

            # Every way a connection can end funnels through end_session, and it
            # runs at most once per generation. Both properties matter: on_close
            # is not guaranteed to fire (see ERROR_GRACE_S), so on_error must
            # also be able to end the session — and when both do fire, only one
            # retry timer may be started.
            ended = threading.Event()

            def end_session(why: str) -> None:
                if gen != self._generation or ended.is_set():
                    return
                ended.set()
                was_connected = self.connected
                self.connected = False
                if not self.should_run:
                    return
                if not was_connected:
                    # Never opened — count it so a feed that can't establish at
                    # all is distinguishable from one that drops mid-session.
                    self.consecutive_failures += 1
                self._log("warn", f"{why} — reconnecting"
                          + (f" (attempt {self.consecutive_failures}, last error: {self.last_error})"
                             if self.last_error and not was_connected else ""))
                self._schedule_retry()

            def watch_after_error(detail: str) -> None:
                """A connected socket that errored is given ERROR_GRACE_S to
                prove it is still alive. A tick arriving in that window means the
                error was incidental; silence means the socket is a zombie and
                nothing else is going to tell us."""
                mark = self.last_tick_ts

                def watch() -> None:
                    time.sleep(ERROR_GRACE_S)
                    if gen != self._generation or ended.is_set():
                        return
                    if self.last_tick_ts != mark:
                        return  # data still flowing; the error was not terminal
                    end_session(f"no data {ERROR_GRACE_S:.0f}s after error ({detail})")

                threading.Thread(target=watch, daemon=True,
                                 name=f"{self.name}-error-grace").start()

            def on_open() -> None:
                if gen != self._generation:
                    return
                self._attempt = 0
                self.connected = True
                self.consecutive_failures = 0
                self.last_connected_ts = time.time()
                # Never let a subscribe failure escape. This runs on whatever
                # thread the SDK opened the connection from; an exception
                # propagating out of here kills that thread before on_close can
                # fire, so nothing schedules a retry and the feed is stranded
                # with should_run=True and connected=False. Feeds guard their
                # own subscribes, but this is the backstop that makes the
                # failure mode structurally impossible rather than a rule every
                # future feed has to remember.
                try:
                    self._subscribe(transport.handle())
                except Exception as e:
                    self.last_error = f"subscribe failed: {e}"
                    self.last_error_ts = time.time()
                    self._log("warn", f"subscribe failed on open: {e}")
                self._log("info", "connected")

            def on_data(msg: Any) -> None:
                if gen != self._generation:
                    return
                self.last_tick_ts = time.time()
                if self._on_tick:
                    self._on_tick(transport.handle(), msg)

            def on_error(err: Any, detail: str) -> None:
                if gen != self._generation:
                    return
                # `detail` may carry more than `err` alone: SmartWebSocketV2
                # signals a give-up as two arguments, and that pair is the ONE
                # message explaining why a feed never comes up. The transport
                # composes it; classification still keys off the raw error.
                self.last_error = detail
                self.last_error_ts = time.time()
                classification = self._report_error(err) if self._report_error else "unknown"
                # Routine post-close noise stays at debug volume; everything
                # else is surfaced so a dead feed is never silent.
                routine = "already closed" in detail.lower()
                self._log("info" if routine else "warn",
                          f"error ({classification}): {detail}")
                # An error must be able to end the session, because on_close may
                # never come. If we were never open there is nothing to wait for;
                # if we were, give the socket a moment to prove itself first.
                if not self.connected:
                    end_session(f"error before open ({classification})")
                else:
                    watch_after_error(detail)

            def on_close() -> None:
                end_session("disconnected")

            transport.open(TransportCallbacks(on_open=on_open, on_data=on_data,
                                              on_error=on_error, on_close=on_close))
        finally:
            self._lock.release()

    def live_socket(self) -> Any:
        """The transport handle that is actually open right now, or None.
        Callers that push a subscription outside the on_open path MUST go
        through this — a feed's own `last_transport` is simply the most
        recently *built* one, which during a reconnect is not carrying data."""
        return self._transport.handle() if (self.connected and self._transport) else None

    def _schedule_retry(self) -> None:
        """Queue exactly one delayed reconnect. Every failure path funnels
        through here so there is always a live timer while should_run is set —
        the feed can never end up permanently unattended."""
        attempt = self._attempt
        delay = self._retry.next_delay(attempt)
        self._attempt = attempt + 1

        def _retry() -> None:
            time.sleep(delay)
            if self.should_run and not self.connected:
                self._connect()
        threading.Thread(target=_retry, daemon=True, name=f"{self.name}-reconnect").start()

    def status(self) -> dict:
        """Feed diagnostics for /health and the UI — so 'broker connected' can
        never imply 'market data flowing' without evidence."""
        return {
            "name": self.name,
            "shouldRun": self.should_run,
            "connected": self.connected,
            "stale": self.stale,
            "consecutiveFailures": self.consecutive_failures,
            "lastError": self.last_error,
            "lastTickTs": self.last_tick_ts,
            "lastConnectedTs": self.last_connected_ts,
        }

    @staticmethod
    def _safe_close(transport: Transport) -> None:
        try:
            transport.close()
        except Exception as exc:
            # Deliberately non-fatal: we are replacing this transport anyway.
            # Recorded because a close that always fails means the old socket
            # is still up, and duplicate feeds are very hard to spot later.
            diagnostics.emit("websocket", "warn", "transport close failed",
                             error=str(exc))
