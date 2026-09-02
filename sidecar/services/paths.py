"""Filesystem locations owned by the sidecar.

One place decides where downloaded instrument / scrip masters land, because a
packaged build must not write into its own install directory (``resources/``
may be read-only, and reinstalling would wipe the cache). Electron passes
CHARTICKS_DATA_DIR — the per-user appData path — when it spawns the sidecar;
`npm run dev:sidecar` sets nothing and falls back to the sidecar tree.

Previously each feed rebuilt this path with its own four-deep ``dirname`` walk
pointing at the (now retired) Tkinter app's ``app/data_cache``.
"""
from __future__ import annotations

import os

SIDECAR_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir() -> str:
    """Writable cache directory for instrument / scrip masters. Created on first
    use so callers never have to makedirs it themselves."""
    base = os.environ.get("CHARTICKS_DATA_DIR") or SIDECAR_DIR
    path = os.path.join(base, "data_cache")
    os.makedirs(path, exist_ok=True)
    return path


def strategies_dir() -> str:
    """The project's `strategies/` folder — legacy-format preset JSON files,
    one file per strategy, read-only from the sidecar's point of view.
    Sits next to `sidecar/` in both layouts this project ships: the repo
    root in dev, `resources/` in a packaged build (see charticks/
    package.json's `extraResources`, which copies `../strategies` there
    alongside `sidecar`) — so the same one-level-up-from-SIDECAR_DIR walk
    resolves correctly in both."""
    return os.path.join(os.path.dirname(SIDECAR_DIR), "strategies")


def log_dir() -> str:
    """Writable directory for Charticks' own log files.

    Separate env var from CHARTICKS_DATA_DIR because logs are what a user is
    asked to send when reporting a problem, so they need a stable, findable
    location even if the cache is redirected to a scratch disk. The packaged app
    points this at ``Documents/Charticks`` (see charticks/electron/logs.ts) —
    somewhere a tester can reach without being told a path — while the ~100 MB/day
    instrument cache stays in appData.
    """
    base = os.environ.get("CHARTICKS_LOG_DIR") or os.environ.get("CHARTICKS_DATA_DIR") or SIDECAR_DIR
    path = os.path.join(base, "logs")
    os.makedirs(path, exist_ok=True)
    return path
