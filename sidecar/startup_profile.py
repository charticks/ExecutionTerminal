"""Startup phase timing for the sidecar.

Imported FIRST, before anything expensive, so `T0` is as close to process start
as Python allows. Every phase after that measures from it, and the whole table
is served at ``GET /startup-profile`` where the Electron main process folds it
into the one cross-process timeline (see charticks/electron/startup.ts).

Deliberately dependency-free and side-effect-free: this module cannot be allowed
to add to the very thing it measures.
"""
from __future__ import annotations

import threading
import time

# As early as this module can be imported. `server.py` imports it on its first
# line so the interpreter's own start-up (site, encodings, the _pth walk) is the
# only thing that happens before it — and that part is measured from the
# Electron side, which knows when the process was spawned.
T0 = time.perf_counter()

_lock = threading.Lock()
_phases: list[tuple[str, float]] = []


def mark(phase: str) -> float:
    """Record a phase boundary. Returns ms since T0."""
    ms = (time.perf_counter() - T0) * 1000.0
    with _lock:
        _phases.append((phase, ms))
    return ms


def phases() -> list[dict]:
    with _lock:
        return [{"phase": p, "ms": round(ms, 1)} for p, ms in _phases]


def elapsed_ms() -> float:
    return (time.perf_counter() - T0) * 1000.0
