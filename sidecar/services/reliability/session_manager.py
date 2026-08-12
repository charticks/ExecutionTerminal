"""Per-account session-expiry detection + automatic re-authentication.

Wired into BrokerManager (see broker_manager.py __init__): whenever a REST
call or WS callback fails, the caller reports the error here via
`report_error(account_id, broker, exc)`. If it classifies as a dead session
(AG8001 etc.), this runs the "Detect Failure -> Re-authenticate -> Reconnect
WebSocket -> Restore Subscriptions -> Resume Streaming" workflow on a
background thread, de-duped per account so a flood of failing requests
doesn't spawn concurrent re-logins.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

import diagnostics
from bridge import events
from bridge.hub import hub

from .errors import classify_error
from .retry_manager import RetryManager

# broker -> health string constants mirrored from broker_manager to avoid a
# circular import (broker_manager constructs SessionManager, not vice versa).
SESSION_EXPIRED = "session_expired"
CONNECTED = "connected"
DOWN = "down"


class SessionManager:
    def __init__(
        self,
        set_health: Callable[[str, str, str, str | None], None],
        reauthenticate: Callable[[str], dict],
        on_recovered: Callable[[str], None] | None = None,
        retry: RetryManager | None = None,
    ) -> None:
        """
        set_health(account_id, broker, health, detail) — BrokerManager._set_health.
        reauthenticate(account_id) -> {"ok": bool, "error": str|None} — re-runs
            the login sequence for this account using cached credentials.
        on_recovered(account_id) — called after a successful re-auth so the
            caller can reconnect the WS feed + replay subscriptions.
        """
        self._set_health = set_health
        self._reauthenticate = reauthenticate
        self._on_recovered = on_recovered
        self._retry = retry or RetryManager(max_attempts=5, base_delay=5, cap=60)
        self._recovering: set[str] = set()
        self._lock = threading.Lock()

    def _log(self, level: str, msg: str) -> None:
        diagnostics.emit("broker", level, msg, publish=True)

    def report_error(self, account_id: str, broker: str, exc: Any) -> str:
        """Classify `exc` and, if it's a dead session, kick off recovery.
        Returns the classification so callers can decide how to log/handle
        the non-session-expired cases themselves."""
        classification = classify_error(exc)
        if classification != "session_expired":
            return classification

        with self._lock:
            if account_id in self._recovering:
                return classification
            self._recovering.add(account_id)

        self._log("warn", f"[session] {broker}:{account_id} session expired ({exc}) — recovering")
        hub.publish(events.log_line("warn", f"[session] AG8001/session-expired detected for {account_id}"))
        self._set_health(account_id, broker, SESSION_EXPIRED, str(exc))
        threading.Thread(
            target=self._recover, args=(account_id, broker), daemon=True,
            name=f"session-recover-{account_id}",
        ).start()
        return classification

    def _recover(self, account_id: str, broker: str) -> None:
        try:
            self._log("info", f"[recovery] started for {account_id}")
            attempt = 0
            while not self._retry.exhausted(attempt):
                self._log("info", f"[recovery] retry {attempt + 1}/{self._retry.max_attempts} for {account_id}")
                result = self._reauthenticate(account_id)
                if result.get("ok"):
                    self._log("info", f"[session] refreshed for {account_id}")
                    self._log("info", f"[recovery] succeeded for {account_id}")
                    if self._on_recovered:
                        self._on_recovered(account_id)
                    return
                if result.get("permanent"):
                    # This broker's session CANNOT be renewed programmatically
                    # (e.g. ICICI's daily browser login). Retrying would burn
                    # the attempts and end at DOWN with a generic message —
                    # stay at session_expired with the actionable one instead.
                    msg = result.get("error") or "Re-authentication requires a manual login"
                    self._log("warn", f"[recovery] {account_id}: {msg}")
                    self._set_health(account_id, broker, SESSION_EXPIRED, msg)
                    return
                import time as _time
                _time.sleep(self._retry.next_delay(attempt))
                attempt += 1
            self._log("error", f"[recovery] failed for {account_id} after {self._retry.max_attempts} attempts")
            self._set_health(account_id, broker, DOWN, "Automatic re-authentication failed")
        finally:
            with self._lock:
                self._recovering.discard(account_id)

    def is_recovering(self, account_id: str) -> bool:
        with self._lock:
            return account_id in self._recovering
