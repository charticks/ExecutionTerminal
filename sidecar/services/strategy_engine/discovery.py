"""Turns every `*.json` file in the project's `strategies/` folder into a
`quant_preset` instance — the "add a strategy by dropping in a JSON file,
no code" half of the hybrid architecture (see the plan doc's Phase 4).

Load-at-startup + explicit rescan, not a live file-watcher (decided when
this was planned): `discover()` is called once at process startup
alongside `strategy_manager.restore()`, and again on demand from the
`POST /strategies/rescan` endpoint / the UI's Rescan button.

A file's own name is the instance id (via `StrategyManager.create_instance`'s
`instance_id`/`source` params) — stable and human-readable, and it is what
makes a rescan idempotent: `create_instance` refuses an id that already
exists, so re-running this against an unchanged file resolves to the same
instance rather than creating a duplicate. It does NOT update an existing
instance's params if the file's contents changed since — editing a preset
and re-scanning has no effect on an already-discovered instance, only on
new files; this mirrors the plan's literal wording ("for each, create an
instance if not already configured") rather than adding update semantics
it didn't ask for.
"""
from __future__ import annotations

import json
import os

import diagnostics

from .manager import StrategyManager

SPEC_NAME = "quant_preset"


def discover(manager: StrategyManager, directory: str) -> dict:
    """Scan `directory` for `*.json` preset files and create a manager
    instance for each one not already in the roster. Returns counts plus
    any per-file errors — used both by the HTTP endpoint's response body and
    by the startup log line."""
    result: dict = {"scanned": 0, "created": 0, "skipped": 0, "errors": []}
    if not os.path.isdir(directory):
        return result

    try:
        names = sorted(n for n in os.listdir(directory) if n.lower().endswith(".json"))
    except OSError as exc:
        diagnostics.event("strategy", "Preset discovery", "failed",
                          level="warn", reason=str(exc))
        result["errors"].append({"file": "", "reason": str(exc)})
        return result

    for name in names:
        result["scanned"] += 1
        instance_id = os.path.splitext(name)[0]
        if manager.get(instance_id) is not None:
            result["skipped"] += 1
            continue

        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as fh:
                params = json.load(fh)
            if not isinstance(params, dict):
                raise ValueError("preset file does not contain a JSON object")
        except Exception as exc:
            diagnostics.event("strategy", "Preset discovery", "skipped",
                              level="warn", file=name, reason=str(exc))
            result["errors"].append({"file": name, "reason": str(exc)})
            continue

        res = manager.create_instance(SPEC_NAME, params, auto_start=False,
                                      instance_id=instance_id, source="discovered")
        if res.get("ok"):
            result["created"] += 1
        else:
            diagnostics.event("strategy", "Preset discovery", "skipped",
                              level="warn", file=name, reason=res.get("error"))
            result["errors"].append({"file": name, "reason": res.get("error")})

    diagnostics.event("strategy", "Preset discovery", "completed",
                      scanned=result["scanned"], created=result["created"],
                      skipped=result["skipped"], errors=len(result["errors"]))
    return result
