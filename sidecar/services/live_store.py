"""Durable storage for the live position book.

Why
---
Stop Loss, Target, Trail SL and Portfolio Trail Profit lived in RAM only. A PC
crash, a sidecar restart or a plain application restart destroyed them while the
broker still held the position — and because the Positions tab is driven by the
broker's book, the row came back looking exactly as it had, so the trade
*appeared* protected when nothing was watching it any more. That is the most
dangerous failure this system can have, because it is invisible.

The book is therefore written to disk on every change and read back at startup,
where it is reconciled against the broker's own positions (see
services.position_reconciler) before any automation acts on it.

Durability model
----------------
* Write to ``live_book.json.tmp`` then ``os.replace`` onto the real file —
  atomic on Windows and POSIX alike, so a crash mid-write can never leave a
  half-written book to be parsed on the next launch.
* Keep the previous good file as ``live_book.bak``. If the current file is
  unreadable (disk corruption, a kill during the rename window), the backup is
  tried before giving up.
* Writes are debounced onto a background thread: a tick storm marks the book
  dirty hundreds of times a second and each write must not sit on the tick path.
  ``flush()`` forces one synchronously — used after a fill and at shutdown,
  where "written within a second" is not good enough.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable

import diagnostics
from services.paths import data_dir

# Debounce window for background writes. Small enough that a crash loses at most
# this much bookkeeping (a trail step, never a position), large enough that a
# busy tick second writes once rather than hundreds of times.
FLUSH_INTERVAL_S = 0.5

SCHEMA_VERSION = 1


class LiveStore:
    def __init__(self, filename: str = "live_book.json") -> None:
        self._filename = filename
        self._lock = threading.RLock()
        self._snapshot: Callable[[], dict] | None = None
        self._dirty = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_write_ts = 0.0
        self._last_error: str | None = None

    # ── paths ─────────────────────────────────────────────────────────────
    @property
    def path(self) -> str:
        return os.path.join(data_dir(), self._filename)

    @property
    def _backup(self) -> str:
        return self.path + ".bak"

    # ── wiring ────────────────────────────────────────────────────────────
    def bind(self, snapshot: Callable[[], dict]) -> None:
        """Register the callable that produces the state to persist. Called
        once by the live book; the store never reaches into it."""
        with self._lock:
            self._snapshot = snapshot

    def mark(self) -> None:
        """The book changed — persist it soon."""
        self._dirty.set()
        self._ensure_running()

    def _ensure_running(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="live-book-store")
            self._thread.start()

    def _run(self) -> None:
        while True:
            self._dirty.wait()
            time.sleep(FLUSH_INTERVAL_S)  # coalesce a burst into one write
            self._dirty.clear()
            self.flush()

    # ── read / write ──────────────────────────────────────────────────────
    def flush(self) -> bool:
        """Write the current state now. Never raises: a failed persist must not
        take down the trading engine that called it — it is logged, and the
        in-memory book carries on."""
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None:
            return False
        try:
            state = snapshot()
            state["version"] = SCHEMA_VERSION
            state["savedTs"] = time.time()
            payload = json.dumps(state)
        except Exception as exc:
            diagnostics.exception("orders", "Live book serialise failed", exc_info=exc)
            return False

        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
                # The point of the whole exercise is surviving a crash, and a
                # rename of a file still sitting in the OS cache survives a
                # process kill but not a power loss.
                f.flush()
                os.fsync(f.fileno())
            if os.path.exists(self.path):
                try:
                    os.replace(self.path, self._backup)
                except OSError:
                    pass  # no backup this round; the atomic write below still holds
            os.replace(tmp, self.path)
            with self._lock:
                self._last_write_ts = time.time()
                self._last_error = None
            return True
        except OSError as exc:
            with self._lock:
                self._last_error = str(exc)
            diagnostics.event("orders", "Live book persist", "failed", level="error",
                              path=self.path, reason=str(exc))
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False

    def load(self) -> dict:
        """Read the persisted book, or {} when there is none / it is unusable."""
        for path in (self.path, self._backup):
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    state = json.load(f)
                if not isinstance(state, dict):
                    raise ValueError("live book is not an object")
                if int(state.get("version", 0)) != SCHEMA_VERSION:
                    # A future version written by a newer build. Refuse to
                    # interpret it rather than restore half of it — the broker
                    # reconciliation below will report every position as
                    # unmanaged, which is visible and safe.
                    diagnostics.event(
                        "orders", "Live book restore", "skipped", level="warn",
                        path=path, reason=f"schema version {state.get('version')} "
                                          f"!= {SCHEMA_VERSION}")
                    return {}
                return state
            except Exception as exc:
                diagnostics.event("orders", "Live book restore", "failed",
                                  level="warn", path=path, reason=str(exc))
        return {}

    def clear(self) -> None:
        for path in (self.path, self._backup):
            try:
                os.remove(path)
            except OSError:
                pass

    def status(self) -> dict:
        with self._lock:
            return {"path": self.path, "lastWriteTs": self._last_write_ts,
                    "lastError": self._last_error,
                    "exists": os.path.exists(self.path)}


live_store = LiveStore()
