"""Emergency halt on new entries.

The UI has carried a "Kill-switch" button since the first build. It had no
click handler, nothing in the sidecar ever emitted the ``risk_event`` that
drives its state, and no code path consulted it — a control that looked like an
emergency stop and could not stop anything.

This is the backing implementation. It is deliberately server-side and
deliberately narrow:

  * It blocks NEW ENTRIES only. Exits, squaring off and cancelling stay
    available, because the whole point of hitting it is usually to get flat, and
    a halt that traps you in a position is not a safety feature.
  * It is sticky. Nothing clears it automatically — not a reconnect, not a
    settings change, not a new session. Only an explicit unlock, so an emergency
    stop cannot be undone by a background event.
  * It survives a renderer reload, because it lives here rather than in the UI.

It does NOT square off on trigger. That is a much larger action (a mis-click
closes the entire book at market) and belongs behind its own confirmation.

It is also on DISK. "Sticky" and "in RAM" are contradictory in a process the
main process restarts automatically after a crash: a halt engaged because
something was going wrong was released by exactly the event most likely to
follow it, and the badge went green on its own. Only an explicit release clears
the file.
"""
from __future__ import annotations

import json
import os
import threading
import time

import diagnostics
from bridge import events
from bridge.hub import hub
from services.paths import data_dir

_FILE = "kill_switch.json"


class KillSwitch:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._halted = False
        self._reason: str | None = None
        self._since: float = 0.0
        self._restore()

    # ── durability ────────────────────────────────────────────────────────
    @property
    def _path(self) -> str:
        return os.path.join(data_dir(), _FILE)

    def _restore(self) -> None:
        """Read a halt left in place by a previous run. A file we cannot parse
        is treated as NOT halted rather than as halted: refusing every order on
        the strength of a corrupt file would be its own outage, and the state is
        one click away from being re-engaged."""
        try:
            if not os.path.exists(self._path):
                return
            with open(self._path, encoding="utf-8") as fh:
                state = json.load(fh)
            if not isinstance(state, dict) or not state.get("halted"):
                return
            self._halted = True
            self._reason = state.get("reason") or "Kill-switch engaged"
            self._since = float(state.get("since") or time.time())
        except Exception as exc:
            diagnostics.event("risk", "Kill switch restore", "failed",
                              level="warn", reason=str(exc))
            return
        diagnostics.event("risk", "Kill switch", "restored", level="critical",
                          reason=self._reason,
                          engagedAt=time.strftime("%H:%M:%S",
                                                  time.localtime(self._since)))

    def _persist(self) -> None:
        """Never raises: a halt that could not be written is still in force for
        this process, and taking the engine down over it would be worse."""
        try:
            with self._lock:
                state = {"halted": self._halted, "reason": self._reason,
                         "since": self._since}
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except OSError as exc:
            diagnostics.event("risk", "Kill switch persist", "failed",
                              level="error", reason=str(exc))

    @property
    def halted(self) -> bool:
        with self._lock:
            return self._halted

    def state(self) -> dict:
        with self._lock:
            return {"halted": self._halted, "reason": self._reason,
                    "since": self._since}

    def engage(self, reason: str | None = None, source: str = "user") -> dict:
        reason = reason or "Kill-switch engaged"
        with self._lock:
            already = self._halted
            self._halted = True
            self._reason = reason
            if not already:
                self._since = time.time()
        self._persist()
        if not already:
            diagnostics.event("risk", "Kill switch", "engaged", level="critical",
                              reason=reason, source=source)
            hub.publish(events.risk_event(True, reason))
        return self.state()

    def release(self, source: str = "user") -> dict:
        with self._lock:
            was = self._halted
            self._halted = False
            self._reason = None
            self._since = 0.0
        self._persist()
        if was:
            diagnostics.event("risk", "Kill switch", "released", level="warn",
                              source=source)
            hub.publish(events.risk_event(False, None))
        return self.state()

    def snapshot_events(self) -> list[dict]:
        """Replayed to a newly-connected client so a reloaded window shows the
        halt immediately rather than defaulting to 'not halted'."""
        with self._lock:
            if not self._halted:
                return []
            return [events.risk_event(True, self._reason)]


kill_switch = KillSwitch()
