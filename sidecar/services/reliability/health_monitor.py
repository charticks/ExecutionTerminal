"""Single background loop that aggregates account health + WebSocket
liveness + in-flight recoveries into one connectivity state for the header
badge (🟢 connected / 🟡 reconnecting / 🔴 auth_failed), publishing
events.connection_health only on transitions so the /stream WS isn't spammed
every tick.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

import diagnostics
from bridge import events
from bridge.hub import hub

from .session_manager import SessionManager
from .ws_manager import WebSocketManager

CONNECTED = "connected"
RECONNECTING = "reconnecting"
AUTH_FAILED = "auth_failed"
DOWN = "down"

# A silent/zombie socket (stale but still "connected") that stays stale this long
# is force-reconnected instead of waiting for the OS TCP timeout — which on a hard
# internet drop can take minutes (the reported "stuck Reconnecting…" symptom).
STALE_RECONNECT_AFTER = 8.0
# Don't hammer: wait this long between forced reconnects on the same socket.
FORCE_COOLDOWN = 20.0


class ConnectionHealthMonitor:
    def __init__(
        self,
        status_map: Callable[[], dict],
        session_manager: SessionManager,
        ws_managers: "list[WebSocketManager] | Callable[[], list[WebSocketManager]] | None" = None,
        interval: float = 2.0,
    ) -> None:
        self._status_map = status_map
        self._session_manager = session_manager
        # Either a fixed list or a callable returning the current one. Feeds are
        # attached and detached as brokers connect, so the set of sockets to
        # watch is no longer known at construction time; a snapshot taken here
        # would monitor a feed that no longer exists and miss every new one.
        self._ws_source = ws_managers or []
        self.interval = interval
        self._last_state: str | None = None
        self._thread: threading.Thread | None = None
        # Stale-socket watchdog bookkeeping, keyed by ws manager name.
        self._stale_since: dict[str, float] = {}
        self._last_forced: dict[str, float] = {}

    @property
    def _ws_managers(self) -> list:
        src = self._ws_source
        return list(src() if callable(src) else src)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="connection-health-monitor")
        self._thread.start()

    def _compute(self) -> tuple[str, int, str | None]:
        accounts = self._status_map()
        connected = [a for a, v in accounts.items() if v.get("health") == "connected"]
        auth_failed = [a for a, v in accounts.items() if v.get("health") == "down"
                        and "re-authentication" in (v.get("detail") or "").lower()]
        recovering = any(self._session_manager.is_recovering(a) for a in accounts)
        ws_stale_or_down = any(
            (wsm.should_run and (not wsm.connected or wsm.stale)) for wsm in self._ws_managers
        )

        if auth_failed and not connected:
            return AUTH_FAILED, len(connected), f"{len(auth_failed)} account(s) failed to re-authenticate"
        if recovering:
            return RECONNECTING, len(connected), None
        if ws_stale_or_down:
            # Accounts can be happily authenticated while the market-data plane
            # is dead — that combination silently emptied the option chain and
            # showed a green badge. Report it, and say which feed and why.
            dead = [w for w in self._ws_managers
                    if w.should_run and (not w.connected or w.stale)]
            names = ", ".join(w.name for w in dead)
            why = next((w.last_error for w in dead if w.last_error), None)
            detail = f"market data feed down ({names})" + (f": {why}" if why else "")
            return RECONNECTING, len(connected), detail
        if connected:
            return CONNECTED, len(connected), None
        return DOWN, 0, None

    def _watchdog(self) -> None:
        """Force-reconnect any socket that has been stale (connected but no ticks)
        for too long, rather than waiting on the OS TCP timeout. Also fires for a
        socket that reports not-connected but whose reconnect timer is stuck."""
        now = time.time()
        for wsm in self._ws_managers:
            if not wsm.should_run:
                self._stale_since.pop(wsm.name, None)
                continue
            zombie = wsm.stale or (not wsm.connected)
            if not zombie:
                self._stale_since.pop(wsm.name, None)
                continue
            since = self._stale_since.setdefault(wsm.name, now)
            if (now - since) < STALE_RECONNECT_AFTER:
                continue
            if (now - self._last_forced.get(wsm.name, 0.0)) < FORCE_COOLDOWN:
                continue
            self._last_forced[wsm.name] = now
            # diagnostics, not hub alone: this watchdog is the last line of
            # defence for a dead feed, and its actions were previously visible
            # only in the live UI panel. When a feed stayed down, websocket.log
            # showed the failure and nothing about the recovery attempts — the
            # one question the log was there to answer.
            diagnostics.emit("websocket", "warn",
                             f"[health] {wsm.name} stale/zombie — forcing reconnect",
                             publish=True, feed=wsm.name,
                             connected=wsm.connected, stale=wsm.stale,
                             downFor=round(now - since, 1),
                             lastError=wsm.last_error)
            try:
                wsm.reconnect()
            except Exception as exc:
                diagnostics.exception("websocket", "[health] forced reconnect failed",
                                      exc_info=exc, feed=wsm.name)
                hub.publish(events.log_line("warn", f"[health] forced reconnect failed: {exc}"))

    def force_reconnect_all(self) -> None:
        """Immediately force a fresh connection on every running feed — called on
        network-up so recovery doesn't wait out the backoff timer."""
        for wsm in self._ws_managers:
            if wsm.should_run:
                self._last_forced[wsm.name] = time.time()
                try:
                    wsm.reconnect()
                except Exception as e:
                    hub.publish(events.log_line("warn", f"[health] reconnect failed: {e}"))

    def _run(self) -> None:
        while True:
            try:
                self._watchdog()
                state, n_connected, detail = self._compute()
                if state != self._last_state:
                    self._last_state = state
                    hub.publish(events.connection_health(state, n_connected, detail))
                    hub.publish(events.log_line("info", f"[health] connection state -> {state} ({n_connected} connected)"))
            except Exception as e:
                hub.publish(events.log_line("warn", f"[health] monitor error: {e}"))
            time.sleep(self.interval)
