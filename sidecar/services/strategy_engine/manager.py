"""StrategyManager — owns every configured strategy instance.

Shape mirrors ``services/live_manager.py`` deliberately: one singleton, one
``RLock``, one background thread running a periodic ``cycle()``, started and
stopped from the same two hooks LiveManager is (see ``server.py``'s
startup/shutdown events, wired in a later phase). Nothing here talks to a
broker or a market-data feed yet — Phase 2 gives instances candle/tick data,
Phase 3 gives them a way to trade. This phase is the container: create a
configured instance, start it, stop it, list them, and make sure one
instance's mistake (a bug in `on_start`, an unhandled exception later) never
takes another instance — or the shared loop — down with it.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import diagnostics
from bridge import events
from bridge.hub import hub
from services.live_store import LiveStore
from services.paths import data_dir

from .base import Strategy, StrategyContext
from .registry import spec_for

# How many recent log lines per instance are kept in memory for the Terminal
# UI's log tail and for a late-joining WebSocket client's replay (see
# snapshot_events). File logging (diagnostics) is unbounded and durable; this
# is deliberately a small, bounded, in-memory convenience on top of it.
LOG_BUFFER_SIZE = 50

# The roster (which instances are configured) and the position-ownership map
# change rarely — created once, mutated once in a while — so both persist
# the same way hedge.py's parent-child links do: a small flat JSON file,
# written synchronously right after the mutation, loaded lazily on first
# use. Per-instance RUNTIME state (candle-adjacent flags, not candle buffers
# themselves) can change every cycle, so it reuses LiveStore's debounced,
# schema-versioned writer instead — the same class live_book.py's own
# persistence is built on, just a second instance of it with its own file
# and its own snapshot callback.
_ROSTER_FILE = "strategy_roster.json"
_OWNERSHIP_FILE = "strategy_ownership.json"

NEW = "new"                # created, never started
RUNNING = "running"
STOPPED = "stopped"        # started at least once, currently not running
ERROR = "error"            # on_start (or a later cycle) raised; not running

# The evaluation cycle's cadence. Phase 1 has nothing for it to do yet beyond
# bookkeeping; Phase 2 hangs candle-close fan-out off the same loop, the same
# way live_manager.py's cycle() grew from a bookkeeping pass into the real
# evaluation loop without changing its threading shape.
EVAL_INTERVAL_S = 1.0


@dataclass
class StrategyInstance:
    """One configured strategy — a spec name plus the params it was given.
    Exists in the roster whether or not it is currently running."""
    id: str
    spec_name: str
    params: dict
    auto_start: bool = False
    state: str = NEW
    error: str = ""
    created_ts: float = field(default_factory=time.time)
    started_ts: float = 0.0
    stopped_ts: float = 0.0
    # "manual" (created via the UI's create dialog / POST /strategies) or
    # "discovered" (created by scanning the project's strategies/ folder —
    # see discovery.py). Purely descriptive: it changes nothing about how
    # the instance runs, only how the UI presents it (discovered instances
    # show their params read-only, sourced from the JSON file).
    source: str = "manual"
    strategy: Strategy | None = field(default=None, repr=False)
    ctx: StrategyContext | None = field(default=None, repr=False)

    def snapshot(self) -> dict:
        return {
            "id": self.id, "strategy": self.spec_name, "params": self.params,
            "autoStart": self.auto_start, "state": self.state,
            "error": self.error or None, "createdTs": self.created_ts,
            "startedTs": self.started_ts or None,
            "stoppedTs": self.stopped_ts or None,
            "source": self.source,
        }


class StrategyManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._instances: dict[str, StrategyInstance] = {}
        self._started = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cycles = 0
        self._last_cycle_ts = 0.0
        # position_id -> instance_id, for the instances that opened them.
        # LiveBook itself never learns a strategy exists (Design Decision B)
        # — this map is the only place that link is recorded, and it is
        # durable for the same reason hedge.py's own link map is: an
        # ownership tag lost on restart would leave a strategy unable to
        # manage a position it is still legitimately responsible for.
        self._ownership: dict[str, str] = {}
        self._ownership_loaded = False
        self._roster_loaded = False
        self._state_store = LiveStore(filename="strategy_runtime_state.json")
        self._state_store.bind(self._snapshot_runtime_state)
        # instance_id -> recent log lines, newest last, capped at
        # LOG_BUFFER_SIZE. In-memory only — a restart's clean slate here is
        # fine, unlike the roster/ownership/runtime-state maps above.
        self._logs: dict[str, list[dict]] = {}
        # key (InstrumentKey | index symbol) -> subscriber instance ids, for
        # Strategy.on_tick — independent of CandleStore's own subscriptions:
        # a strategy that wants tick-level reaction (e.g. watching a position
        # it already holds) should not have to pay for candle bucketing on a
        # timeframe it will never read.
        self._tick_subs: dict[Any, set[str]] = {}
        self._tick_hooked = False

    # ── ticks (Strategy.on_tick — Phase 6: this was declared since Phase 1
    #    and never actually wired to anything until now) ────────────────
    def subscribe_ticks(self, instance_id: str, key: Any) -> None:
        self._ensure_tick_hook()
        with self._lock:
            self._tick_subs.setdefault(key, set()).add(instance_id)

    def unsubscribe_ticks(self, instance_id: str, key: Any) -> None:
        with self._lock:
            subs = self._tick_subs.get(key)
            if subs is None:
                return
            subs.discard(instance_id)
            if not subs:
                del self._tick_subs[key]

    def _ensure_tick_hook(self) -> None:
        with self._lock:
            if self._tick_hooked:
                return
            self._tick_hooked = True
        from services.broker_manager import manager as broker_manager

        broker_manager.add_option_tick_listener(
            lambda key, ltp, _volume: self._dispatch_tick(key, ltp))
        broker_manager.add_index_tick_listener(
            lambda symbol, ltp: self._dispatch_tick(symbol, ltp))

    def _dispatch_tick(self, key: Any, ltp: float) -> None:
        """One global listener (registered once, like CandleStore's own),
        dispatched only to instances actually subscribed to `key` — and,
        same as candle-close delivery, crash-isolated per instance so one
        strategy's bug in on_tick can never block delivery to another, or to
        the next tick."""
        with self._lock:
            subs = self._tick_subs.get(key)
            if not subs:
                return
            targets = [(iid, self._instances[iid].strategy) for iid in subs
                      if iid in self._instances and self._instances[iid].state == RUNNING
                      and self._instances[iid].strategy is not None]
        for instance_id, strategy in targets:
            try:
                strategy.on_tick(key, ltp)
            except Exception as exc:
                diagnostics.exception("strategy", "on_tick failed", exc_info=exc,
                                      instanceId=instance_id, key=str(key))

    # ── position ownership ────────────────────────────────────────────────
    def claim_position(self, position_id: str, instance_id: str) -> None:
        with self._lock:
            self._ownership[position_id] = instance_id
        self._persist_ownership()
        self._publish_status(instance_id)

    def owner_of(self, position_id: str) -> str | None:
        self._load_ownership()
        with self._lock:
            return self._ownership.get(position_id)

    def owns(self, position_id: str, instance_id: str) -> bool:
        return self.owner_of(position_id) == instance_id

    def positions_of(self, instance_id: str) -> list[str]:
        """Every position this instance currently owns — the reverse of the
        ownership map. Computed on demand rather than maintained as a second
        index: this is called at most once per instance per UI refresh, not
        per tick."""
        self._load_ownership()
        with self._lock:
            return [pid for pid, iid in self._ownership.items() if iid == instance_id]

    def pnl_of(self, instance_id: str) -> float:
        """Combined open P&L across every position this instance owns — the
        same `LivePosition.pnl()` the Positions screen itself reads, summed.
        A position that has since closed (no longer in live_book) simply
        contributes nothing; realised P&L from a closed trade is not counted
        here, matching how the Positions screen's own "running P&L" reads
        only what is currently open."""
        from services.live_book import live_book

        total = 0.0
        for pid in self.positions_of(instance_id):
            pos = live_book.get(pid)
            if pos is not None:
                total += pos.pnl()
        return round(total, 2)

    # ── logs (in-memory ring buffer + live push) ────────────────────────
    def record_log(self, instance_id: str, spec_name: str, level: str,
                   message: str) -> None:
        entry = {"ts": time.time(), "level": level, "message": message}
        with self._lock:
            buf = self._logs.setdefault(instance_id, [])
            buf.append(entry)
            del buf[:-LOG_BUFFER_SIZE]
        hub.publish(events.strategy_log(instance_id, spec_name, level, message))

    def logs_of(self, instance_id: str) -> list[dict]:
        with self._lock:
            return list(self._logs.get(instance_id, []))

    # ── push to late-joining WebSocket clients ──────────────────────────
    def snapshot_events(self) -> list[dict]:
        """Replayed by the /stream handler on connect, the same way
        live_book.republish() is — so a just-opened Strategies page shows
        every configured instance and its recent logs immediately, not only
        deltas from this point forward."""
        out = []
        for row in self.list_instances():
            out.append(events.strategy_status(row))
            for entry in self.logs_of(row["id"]):
                out.append(events.strategy_log(
                    row["id"], row["strategy"], entry["level"], entry["message"]))
        return out

    def _publish_status(self, instance_id: str) -> None:
        inst = self.get(instance_id)
        if inst is not None:
            hub.publish(events.strategy_status(inst.snapshot()))

    # ── ownership persistence — hedge.py's own pattern, unchanged ─────────
    @property
    def _ownership_path(self) -> str:
        return os.path.join(data_dir(), _OWNERSHIP_FILE)

    def _load_ownership(self) -> None:
        with self._lock:
            if self._ownership_loaded:
                return
            self._ownership_loaded = True
            try:
                if not os.path.exists(self._ownership_path):
                    return
                with open(self._ownership_path, encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict):
                    self._ownership = {str(k): str(v) for k, v in raw.items() if v}
            except Exception as exc:
                diagnostics.event("strategy", "Ownership map restore", "failed",
                                  level="warn", reason=str(exc))

    def _persist_ownership(self) -> None:
        try:
            with self._lock:
                links = dict(self._ownership)
            tmp = self._ownership_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(links, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._ownership_path)
        except Exception as exc:
            # Broad on purpose, matching LiveStore.flush()'s own reasoning: a
            # write failure (disk) and a serialisation failure (a bad value
            # somewhere) are both "this persist did not happen," and neither
            # may be allowed to crash out of claim_position — the caller is
            # mid-order-placement, not expecting a persistence detail to fail
            # loudly.
            diagnostics.event("strategy", "Ownership map persist", "failed",
                              level="warn", reason=str(exc))

    # ── manager lifecycle (the background loop) ─────────────────────────────
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="strategy-manager")
        self._thread.start()
        diagnostics.event("strategy", "Strategy manager", "started",
                          evaluationIntervalMs=int(EVAL_INTERVAL_S * 1000))

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._started = False

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self._started and self._thread and self._thread.is_alive())

    def _run(self) -> None:
        """Same safety-net shape as LiveManager._run: never exits on error,
        because an exception escaping this loop would silently end
        evaluation for every running instance at once."""
        while not self._stop.wait(EVAL_INTERVAL_S):
            try:
                self.cycle()
            except Exception as exc:
                diagnostics.exception("strategy", "Strategy manager cycle failed",
                                      exc_info=exc)

    def cycle(self) -> None:
        """Bookkeeping, marking runtime state dirty (cheap — mark() just
        flags a background thread), and pushing each running instance's
        current P&L so the Terminal UI's "running P&L" moves with the
        market rather than only on the next manual refresh."""
        with self._lock:
            self._cycles += 1
            self._last_cycle_ts = time.time()
            running_ids = [i.id for i in self._instances.values() if i.state == RUNNING]
        if running_ids:
            self._state_store.mark()
            # Per-instance, not a dict comprehension: one instance's pnl_of
            # raising (a corrupt position, an unexpected live_book shape)
            # must not blank out every OTHER running instance's P&L for this
            # cycle — the same isolation get_state() already has below.
            pnls: dict[str, float] = {}
            for iid in running_ids:
                try:
                    pnls[iid] = self.pnl_of(iid)
                except Exception as exc:
                    diagnostics.exception("strategy", "pnl_of failed", exc_info=exc,
                                          instanceId=iid)
            if pnls:
                hub.publish(events.strategy_pnl(pnls))

    def _snapshot_runtime_state(self) -> dict:
        """What LiveStore persists for us — one entry per RUNNING instance
        whose strategy actually has something to save. A STOPPED instance's
        last-known state stays in the file (still useful if it is started
        again) until the instance is removed from the roster entirely."""
        with self._lock:
            instances = list(self._instances.values())
        out: dict[str, dict] = {}
        for inst in instances:
            if inst.state != RUNNING or inst.strategy is None:
                continue
            try:
                state = inst.strategy.get_state()
            except Exception as exc:
                diagnostics.exception("strategy", "get_state failed", exc_info=exc,
                                      instanceId=inst.id, strategy=inst.spec_name)
                continue
            if state:
                out[inst.id] = state
        return {"instances": out}

    def flush_state(self) -> None:
        """Write runtime state synchronously — called at shutdown, where
        "within half a second" is not good enough (mirrors live_store's own
        flush-at-shutdown reasoning)."""
        self._state_store.flush()

    # ── instance roster ──────────────────────────────────────────────────
    def create_instance(self, spec_name: str, params: dict | None = None,
                        auto_start: bool = False, instance_id: str | None = None,
                        source: str = "manual") -> dict:
        """`instance_id` lets a caller (discovery.py) pick a stable,
        human-readable id instead of a random one — and, given one, this is
        idempotent: an id already in the roster is left untouched rather
        than overwritten, which is what makes re-running discovery against
        an unchanged file a no-op instead of a duplicate."""
        spec = spec_for(spec_name)
        if spec is None:
            return {"ok": False, "code": "UNKNOWN_STRATEGY",
                    "error": f"No strategy is registered as '{spec_name}'."}
        if instance_id is not None:
            with self._lock:
                if instance_id in self._instances:
                    return {"ok": False, "code": "ALREADY_EXISTS",
                            "error": f"Strategy instance '{instance_id}' already exists.",
                            "id": instance_id}
        else:
            instance_id = uuid.uuid4().hex[:12]
        self._register(instance_id, spec.name, dict(params or {}), auto_start, source)
        diagnostics.event("strategy", "Strategy instance", "created",
                          instanceId=instance_id, strategy=spec.name, source=source)
        self._persist_roster()
        self._publish_status(instance_id)
        return {"ok": True, "id": instance_id}

    def _register(self, instance_id: str, spec_name: str, params: dict,
                 auto_start: bool, source: str = "manual") -> StrategyInstance:
        """The part create_instance and restore() share: put a NEW
        (not-yet-started) instance into the in-memory roster. restore() calls
        this directly so a recreated instance keeps its ORIGINAL id — the
        ownership map is keyed by that id, and generating a fresh one on
        every restart would silently orphan every position a strategy had
        already opened."""
        inst = StrategyInstance(id=instance_id, spec_name=spec_name,
                                params=params, auto_start=auto_start, source=source)
        with self._lock:
            self._instances[instance_id] = inst
        return inst

    def remove_instance(self, instance_id: str) -> dict:
        with self._lock:
            inst = self._instances.get(instance_id)
            if inst is None:
                return {"ok": False, "code": "NOT_FOUND",
                        "error": "No such strategy instance."}
            if inst.state == RUNNING:
                return {"ok": False, "code": "STILL_RUNNING",
                        "error": "Stop the instance before removing it."}
            del self._instances[instance_id]
        diagnostics.event("strategy", "Strategy instance", "removed",
                          instanceId=instance_id, strategy=inst.spec_name)
        self._persist_roster()
        with self._lock:
            self._logs.pop(instance_id, None)
        hub.publish(events.strategy_status({**inst.snapshot(), "removed": True}))
        return {"ok": True}

    # ── roster persistence — same lazy-JSON pattern as ownership above ────
    @property
    def _roster_path(self) -> str:
        return os.path.join(data_dir(), _ROSTER_FILE)

    def _load_roster_file(self) -> dict:
        """The persisted roster as {id: {specName, params, autoStart,
        createdTs}}, or {} if there is none / it is unusable. Separate from
        `restore()` so a test (or a future admin endpoint) can inspect what
        is on disk without recreating instances from it."""
        try:
            if not os.path.exists(self._roster_path):
                return {}
            with open(self._roster_path, encoding="utf-8") as fh:
                raw = json.load(fh)
            return raw if isinstance(raw, dict) else {}
        except Exception as exc:
            diagnostics.event("strategy", "Roster restore", "failed",
                              level="warn", reason=str(exc))
            return {}

    def _persist_roster(self) -> None:
        try:
            with self._lock:
                roster = {
                    inst.id: {"specName": inst.spec_name, "params": inst.params,
                             "autoStart": inst.auto_start, "createdTs": inst.created_ts,
                             "source": inst.source}
                    for inst in self._instances.values()}
            tmp = self._roster_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(roster, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._roster_path)
        except Exception as exc:
            # Broad on purpose (see _persist_ownership's comment) — in
            # particular this also catches a params value that turns out not
            # to be JSON-serialisable, which must degrade to "not persisted"
            # rather than crash create_instance() out from under its caller.
            diagnostics.event("strategy", "Roster persist", "failed",
                              level="warn", reason=str(exc))

    def restore(self) -> dict:
        """Called once at process startup, before start() (see server.py).
        Recreates every configured instance from disk, restores the position-
        ownership map, and starts every instance flagged auto_start — each
        start failure isolated exactly like a live start_instance call, so
        one broken strategy never blocks the others from coming back up.

        A strategy's OPEN POSITIONS need no special recovery here at all:
        they were opened through the ordinary order_manager/order_sync path,
        so they are already `source=SRC_CHARTICKS` positions the existing
        reconciliation flow (services.position_reconciler, unchanged) picks
        back up on its own. Only the ownership TAG — which instance manages
        which position — is this method's job to restore.
        """
        if self._roster_loaded:
            return {"restored": 0, "started": 0, "failed": 0}
        self._roster_loaded = True
        self._load_ownership()
        saved_state = self._state_store.load().get("instances", {})

        roster = self._load_roster_file()
        # Validate BEFORE sorting: a malformed entry (not a dict — a hand-
        # edited or half-written roster file) must not raise inside the sort
        # key itself, which would abort every OTHER, perfectly good entry
        # along with it. Skipped here, loudly, rather than guessed at.
        entries: list[tuple[str, dict]] = []
        for instance_id, entry in roster.items():
            if isinstance(entry, dict):
                entries.append((instance_id, entry))
            else:
                diagnostics.event("strategy", "Roster restore", "skipped",
                                  level="warn", instanceId=instance_id,
                                  reason="entry is not an object")
        def _sort_key(kv: tuple[str, dict]) -> float:
            # A non-numeric createdTs (a hand-edited file) must not crash the
            # sort itself — Python raises comparing str against float, which
            # would take every entry down over one bad timestamp.
            try:
                return float(kv[1].get("createdTs", 0) or 0)
            except (TypeError, ValueError):
                return 0.0

        entries.sort(key=_sort_key)

        restored, started, failed = 0, 0, 0
        for instance_id, entry in entries:
            # One malformed/unexpected entry must not abort the rest of the
            # restore — everything below start_instance already isolates its
            # OWN failures; this is the belt for the entry-parsing braces.
            try:
                spec_name = entry.get("specName", "")
                params = entry.get("params") or {}
                auto_start = bool(entry.get("autoStart"))
                source = entry.get("source") or "manual"
                # A corrupted timestamp is cosmetic (it only affects restore
                # ORDER) — it must not sacrifice an otherwise-good entry the
                # way a corrupted specName/params legitimately should.
                try:
                    created_ts = float(entry.get("createdTs") or time.time())
                except (TypeError, ValueError):
                    created_ts = time.time()
                inst = self._register(instance_id, spec_name, params, auto_start, source)
                inst.created_ts = created_ts
            except Exception as exc:
                diagnostics.exception("strategy", "Roster entry restore failed",
                                      exc_info=exc, instanceId=instance_id)
                continue
            restored += 1
            if not inst.auto_start:
                continue
            try:
                res = self.start_instance(instance_id)
            except Exception as exc:
                # start_instance already catches on_start's own failures;
                # this is the (should-be-impossible) case of the CALL itself
                # raising, and it must not stop the remaining instances from
                # getting their turn.
                diagnostics.exception("strategy", "start_instance raised during restore",
                                      exc_info=exc, instanceId=instance_id)
                failed += 1
                continue
            if not res.get("ok"):
                failed += 1
                continue
            started += 1
            state = saved_state.get(instance_id)
            if state:
                strategy = self.get(instance_id).strategy if self.get(instance_id) else None
                if strategy is not None:
                    try:
                        strategy.restore_state(state)
                    except Exception as exc:
                        diagnostics.exception(
                            "strategy", "restore_state failed", exc_info=exc,
                            instanceId=instance_id, strategy=spec_name)

        diagnostics.event("strategy", "Strategy roster restore", "success",
                          restored=restored, started=started, failed=failed)
        return {"restored": restored, "started": started, "failed": failed}

    def get(self, instance_id: str) -> StrategyInstance | None:
        with self._lock:
            return self._instances.get(instance_id)

    def _phase_of(self, inst: StrategyInstance) -> str | None:
        """A finer-grained status than `inst.state` for the UI's Running/
        Waiting/Completed split — see Strategy.phase()'s own docstring.
        Isolated the same way pnl_of/get_state already are: one instance's
        strategy raising here must not break the whole list/detail response."""
        if inst.state != RUNNING or inst.strategy is None:
            return None
        try:
            return inst.strategy.phase()
        except Exception as exc:
            diagnostics.exception("strategy", "phase() failed", exc_info=exc,
                                  instanceId=inst.id, strategy=inst.spec_name)
            return None

    def list_instances(self) -> list[dict]:
        with self._lock:
            instances = sorted(self._instances.values(), key=lambda i: i.created_ts)
        out = []
        for inst in instances:
            try:
                position_count = len(self.positions_of(inst.id))
            except Exception as exc:
                diagnostics.exception("strategy", "positions_of failed", exc_info=exc,
                                      instanceId=inst.id)
                position_count = 0
            out.append({**inst.snapshot(), "phase": self._phase_of(inst),
                       "positionCount": position_count})
        return out

    def instance_detail(self, instance_id: str) -> dict | None:
        """Everything the Terminal UI's detail view needs for one instance —
        the roster snapshot plus what only makes sense to compute on demand
        (P&L, owned positions, recent logs)."""
        inst = self.get(instance_id)
        if inst is None:
            return None
        position_ids = self.positions_of(instance_id)
        return {**inst.snapshot(),
               "pnl": self.pnl_of(instance_id),
               "positionIds": position_ids,
               "positionCount": len(position_ids),
               "phase": self._phase_of(inst),
               "logs": self.logs_of(instance_id)}

    def update_params(self, instance_id: str, params: dict) -> dict:
        """Replace a NOT-RUNNING instance's configured params — the "Edit"
        action. Same STILL_RUNNING guard shape as remove_instance: editing
        params out from under a strategy currently mid-cycle could hand it a
        config it never saw on_start, which is worse than just asking for a
        stop first."""
        with self._lock:
            inst = self._instances.get(instance_id)
            if inst is None:
                return {"ok": False, "code": "NOT_FOUND",
                        "error": "No such strategy instance."}
            if inst.state == RUNNING:
                return {"ok": False, "code": "STILL_RUNNING",
                        "error": "Stop the instance before editing it."}
            inst.params = dict(params or {})
        diagnostics.event("strategy", "Strategy instance", "updated",
                          instanceId=instance_id, strategy=inst.spec_name)
        self._persist_roster()
        self._publish_status(instance_id)
        return {"ok": True}

    # ── per-instance lifecycle ───────────────────────────────────────────
    def start_instance(self, instance_id: str) -> dict:
        with self._lock:
            inst = self._instances.get(instance_id)
            if inst is None:
                return {"ok": False, "code": "NOT_FOUND",
                        "error": "No such strategy instance."}
            if inst.state == RUNNING:
                return {"ok": True, "detail": "Already running."}
            spec = spec_for(inst.spec_name)
            if spec is None:
                # The instance was configured against a strategy that has
                # since been removed from the registry (a deploy dropped a
                # plugin). Surface that rather than crashing on a None factory.
                inst.state, inst.error = ERROR, (
                    f"strategy '{inst.spec_name}' is no longer registered")
                return {"ok": False, "code": "UNKNOWN_STRATEGY", "error": inst.error}
            strategy = spec.factory()
            ctx = StrategyContext(instance_id, spec.name, inst.params, strategy, self)

        # on_start runs OUTSIDE the lock: a plugin's start-up work (subscribing
        # to candles, etc.) must never be able to deadlock the manager, and a
        # slow start must not block other instances' start/stop calls.
        try:
            strategy.ctx = ctx
            strategy.on_start(ctx, inst.params)
        except Exception as exc:
            diagnostics.exception("strategy", "Strategy on_start failed",
                                  exc_info=exc, instanceId=instance_id,
                                  strategy=spec.name)
            with self._lock:
                inst.state, inst.error = ERROR, str(exc)
            self._publish_status(instance_id)
            return {"ok": False, "code": "START_FAILED", "error": str(exc)}

        with self._lock:
            inst.strategy, inst.ctx = strategy, ctx
            inst.state, inst.error = RUNNING, ""
            inst.started_ts = time.time()
        diagnostics.event("strategy", "Strategy instance", "started",
                          instanceId=instance_id, strategy=spec.name)
        self._publish_status(instance_id)
        return {"ok": True}

    def stop_instance(self, instance_id: str) -> dict:
        with self._lock:
            inst = self._instances.get(instance_id)
            if inst is None:
                return {"ok": False, "code": "NOT_FOUND",
                        "error": "No such strategy instance."}
            if inst.state != RUNNING:
                return {"ok": True, "detail": "Already stopped."}
            strategy, ctx = inst.strategy, inst.ctx

        try:
            if strategy is not None:
                strategy.on_stop()
        except Exception as exc:
            # A failing on_stop must still leave the instance stopped — the
            # alternative (leaving it "running" because shutdown broke) is
            # exactly the silent-failure shape this whole engine exists to
            # avoid elsewhere (see live_manager.py's own module docstring).
            diagnostics.exception("strategy", "Strategy on_stop failed",
                                  exc_info=exc, instanceId=instance_id,
                                  strategy=inst.spec_name)

        # Release candle subscriptions regardless of whether on_stop raised —
        # a stopped instance must never keep a market-data subscription (and
        # the tick/CPU cost of computing its candles) alive indefinitely.
        if ctx is not None:
            try:
                ctx._release()
            except Exception as exc:
                diagnostics.exception("strategy", "Candle release failed",
                                      exc_info=exc, instanceId=instance_id)

        with self._lock:
            inst.state = STOPPED
            inst.stopped_ts = time.time()
            inst.strategy, inst.ctx = None, None
        diagnostics.event("strategy", "Strategy instance", "stopped",
                          instanceId=instance_id, strategy=inst.spec_name)
        self._publish_status(instance_id)
        return {"ok": True}

    def stop_all(self) -> None:
        """Called from shutdown — best-effort, one instance's failure must
        not stop the rest from being asked to stop too."""
        for instance_id in list(self._instances):
            try:
                self.stop_instance(instance_id)
            except Exception as exc:
                diagnostics.exception("strategy", "Strategy shutdown stop failed",
                                      exc_info=exc, instanceId=instance_id)


strategy_manager = StrategyManager()
