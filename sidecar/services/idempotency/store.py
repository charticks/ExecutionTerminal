"""Durable record of every order Charticks has claimed the right to send.

Why this is on disk
-------------------
"A restart must not duplicate an order" cannot be satisfied by memory. The
dangerous sequence is: send an order, lose the answer (timeout, crash, power
cut), restart, and have the user press Buy again. Only a record that outlived
the process can tell the second attempt that the first one may already be live.

Format is one JSON object per line, appended and fsync'd before the SDK call
returns. Append-only because a partially written line at the tail of a file is
recoverable (skip it) whereas a partially rewritten record is not, and fsync'd
because the failure this exists for is precisely the one that loses buffered
writes. A day's trading is a few hundred lines; files are per-day and pruned.

State machine
-------------
    CLAIMED ──► PLACED     broker returned an id
        │  └──► FAILED     broker explicitly rejected it
        └─────► CLAIMED    still, if we never learned the outcome

A claim that is still CLAIMED after the call is the *unresolved* case: the order
may or may not exist at the broker. It is never garbage-collected by age, only
by an explicit resolution, because an unresolved claim is the one piece of state
that must not be forgotten.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import diagnostics
from services.paths import data_dir

CLAIMED = "CLAIMED"
PLACED = "PLACED"
FAILED = "FAILED"

_FILE_PREFIX = "idempotency_"
_KEEP_DAYS = 7


@dataclass
class Claim:
    coid: str
    fingerprint: str
    attempt: int
    account_id: str
    broker: str
    symbol: str
    side: str
    qty: int
    price: float
    state: str = CLAIMED
    order_id: str = ""
    reason: str = ""
    created_ts: float = field(default_factory=time.time)
    updated_ts: float = field(default_factory=time.time)
    # Set once the claim's outcome has been established against the broker
    # (either by the SDK's own answer or by later reconciliation).
    resolved: bool = False

    @property
    def unresolved(self) -> bool:
        return self.state == CLAIMED and not self.resolved


class ClaimStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._claims: dict[str, Claim] = {}          # coid -> claim
        self._by_fingerprint: dict[str, list[str]] = {}
        self._path: str | None = None
        self._loaded = False

    # ── file plumbing ─────────────────────────────────────────────────────
    def _file(self) -> str | None:
        """Today's journal, or None if no writable location exists.

        A missing journal degrades to in-memory-only — which still stops
        double-clicks and in-session retries. Trading must not stop because a
        disk is full; the loss of restart protection is logged loudly instead.
        """
        if self._path is not None:
            return self._path or None
        try:
            self._path = os.path.join(
                data_dir(), f"{_FILE_PREFIX}{date.today():%Y%m%d}.jsonl")
        except OSError as exc:
            self._path = ""
            diagnostics.emit("orders", "error",
                             "idempotency journal unavailable — restart protection "
                             "is OFF for this session", error=str(exc))
            return None
        return self._path

    def load(self) -> int:
        """Read today's journal. Idempotent; safe to call from anywhere."""
        with self._lock:
            if self._loaded:
                return len(self._claims)
            self._loaded = True
            path = self._file()
            if not path or not os.path.exists(path):
                return 0
            recovered = 0
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except ValueError:
                            # A torn tail line from a crash mid-append. Skipping
                            # it is correct: the record it would have completed
                            # was never acknowledged to anyone.
                            continue
                        claim = self._from_record(record)
                        if claim is not None:
                            self._remember(claim)
                            recovered += 1
            except OSError as exc:
                diagnostics.exception("orders", "idempotency journal unreadable",
                                      exc_info=exc)
                return 0
            pending = [c.coid for c in self._claims.values() if c.unresolved]
            diagnostics.emit("orders", "info", "Idempotency journal loaded",
                             records=recovered, claims=len(self._claims),
                             unresolved=len(pending))
            self._prune()
            return len(self._claims)

    @staticmethod
    def _from_record(record: dict) -> Claim | None:
        try:
            fields = {k: record[k] for k in ("coid", "fingerprint", "attempt",
                                             "account_id", "broker", "symbol",
                                             "side", "qty", "price")}
        except KeyError:
            return None
        claim = Claim(**fields)
        for key in ("state", "order_id", "reason", "created_ts", "updated_ts",
                    "resolved"):
            if key in record:
                setattr(claim, key, record[key])
        return claim

    def _remember(self, claim: Claim) -> None:
        existing = self._claims.get(claim.coid)
        if existing is not None and claim.updated_ts < existing.updated_ts:
            return  # a later line already superseded this one
        self._claims[claim.coid] = claim
        coids = self._by_fingerprint.setdefault(claim.fingerprint, [])
        if claim.coid not in coids:
            coids.append(claim.coid)

    def _append(self, claim: Claim) -> None:
        path = self._file()
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(claim), separators=(",", ":")) + "\n")
                fh.flush()
                # The whole point of the journal is surviving a hard stop, and a
                # buffered write does not. Cheap next to the network round trip
                # it precedes.
                os.fsync(fh.fileno())
        except OSError as exc:
            diagnostics.emit("orders", "warn", "idempotency journal write failed",
                             coid=claim.coid, error=str(exc))

    def _prune(self) -> None:
        path = self._file()
        if not path:
            return
        directory = os.path.dirname(path)
        try:
            names = sorted(n for n in os.listdir(directory)
                           if n.startswith(_FILE_PREFIX) and n.endswith(".jsonl"))
        except OSError:
            return
        for name in names[:-_KEEP_DAYS]:
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass

    # ── read / write ──────────────────────────────────────────────────────
    def get(self, coid: str) -> Claim | None:
        self.load()
        with self._lock:
            return self._claims.get(coid)

    def for_fingerprint(self, fingerprint: str) -> list[Claim]:
        """Every claim for this intent, oldest first."""
        self.load()
        with self._lock:
            return [self._claims[c] for c in self._by_fingerprint.get(fingerprint, [])
                    if c in self._claims]

    def add(self, claim: Claim) -> Claim:
        self.load()
        with self._lock:
            self._remember(claim)
            self._append(claim)
            return claim

    def update(self, coid: str, **changes: Any) -> Claim | None:
        self.load()
        with self._lock:
            claim = self._claims.get(coid)
            if claim is None:
                return None
            for key, value in changes.items():
                setattr(claim, key, value)
            claim.updated_ts = time.time()
            self._append(claim)
            return claim

    def unresolved(self) -> list[Claim]:
        self.load()
        with self._lock:
            return [c for c in self._claims.values() if c.unresolved]

    def snapshot(self) -> dict:
        """For /health and diagnostics — an unresolved claim is operationally
        important, so it must be visible without reading a log file."""
        self.load()
        with self._lock:
            claims = list(self._claims.values())
        return {
            "journal": self._path or None,
            "claims": len(claims),
            "placed": sum(1 for c in claims if c.state == PLACED),
            "failed": sum(1 for c in claims if c.state == FAILED),
            "unresolved": [
                {"coid": c.coid, "broker": c.broker, "symbol": c.symbol,
                 "side": c.side, "qty": c.qty, "age": round(time.time() - c.created_ts)}
                for c in claims if c.unresolved
            ],
        }

    def reset(self) -> None:
        """Tests only — forgets everything, including the journal path."""
        with self._lock:
            self._claims.clear()
            self._by_fingerprint.clear()
            self._path = None
            self._loaded = False


store = ClaimStore()
