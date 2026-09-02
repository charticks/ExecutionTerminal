import { create } from "zustand";
import { bridge } from "@/bridge/client";
import type { BridgeEvent, StrategyInstanceState } from "@/bridge/events";

/** An exception as a line worth putting in a tooltip — same helper
 *  useBrokerStore.ts uses, kept local since this store has no other
 *  dependency on that file. */
function errorText(e: unknown): string {
  if (e instanceof Error) return e.message;
  const text = String(e);
  return text === "[object Object]" ? "the request failed with no message" : text;
}

export interface StrategyParamField {
  key: string;
  label: string;
  kind: "string" | "number" | "bool" | "choice";
  default: unknown;
  choices: string[];
  required: boolean;
}

export interface StrategySpec {
  name: string;
  label: string;
  description: string;
  params: StrategyParamField[];
}

export interface StrategyInstance {
  id: string;
  strategy: string;
  params: Record<string, unknown>;
  autoStart: boolean;
  state: StrategyInstanceState;
  error: string | null;
  createdTs: number;
  startedTs: number | null;
  stoppedTs: number | null;
  /** "manual" (created via the create dialog) or "discovered" (created by
   *  scanning strategies/ — see sidecar/services/strategy_engine/discovery.py). */
  source: "manual" | "discovered";
  /** Finer-grained than `state` for a RUNNING instance — whether it's
   *  actively holding a trade, still watching for a signal, or done for the
   *  day. null when not running, or for a strategy type that doesn't
   *  implement Strategy.phase(). Only refreshed by GET /strategies (list())
   *  or GET /strategies/{id} (refreshOne) — NOT pushed by strategy_status,
   *  so a live status push preserves whatever value is already known. */
  phase: "waiting" | "in_position" | "completed" | null;
  /** How many open positions this instance currently owns. Same refresh
   *  characteristics as `phase` above. */
  positionCount: number;
  /** Filled in from strategy_pnl events; absent until the first one arrives
   *  after the instance starts running. */
  pnl?: number;
}

export interface StrategyLogRow {
  ts: number;
  level: "info" | "warn" | "error";
  message: string;
}

// Bounded the same way the sidecar's own per-instance ring buffer is
// (LOG_BUFFER_SIZE in manager.py) — a live tail, not a durable log viewer.
const MAX_LOG_ROWS = 200;

interface StrategyState {
  specs: StrategySpec[];
  instances: Record<string, StrategyInstance>;
  logs: Record<string, StrategyLogRow[]>;
  loaded: boolean;
  loadError: string | null;

  load: () => Promise<void>;
  /** Scans the strategies/ folder for new preset files and creates an
   *  instance for each — the manual counterpart to the automatic scan at
   *  sidecar startup. Returns the same counts the endpoint does so the
   *  caller can show "3 new strategies found" / "nothing new" feedback. */
  rescan: () => Promise<{ ok: boolean; scanned?: number; created?: number;
    skipped?: number; errors?: { file: string; reason: string }[]; error?: string }>;
  create: (specName: string, params: Record<string, unknown>, autoStart: boolean)
    => Promise<{ ok: boolean; id?: string; error?: string }>;
  /** Replace a STOPPED instance's params — the "Edit" action. Refused
   *  (STILL_RUNNING) while the instance is running. */
  update: (id: string, params: Record<string, unknown>)
    => Promise<{ ok: boolean; code?: string; error?: string }>;
  /** Create a fresh instance with another instance's current strategy +
   *  params — the "Duplicate" action. Always lands as source "manual", even
   *  when duplicating a discovered one (a duplicate isn't a JSON file). */
  duplicate: (id: string) => Promise<{ ok: boolean; id?: string; error?: string }>;
  start: (id: string) => Promise<{ ok: boolean; error?: string }>;
  stop: (id: string) => Promise<{ ok: boolean; error?: string }>;
  remove: (id: string) => Promise<{ ok: boolean; error?: string }>;

  ingest: (e: BridgeEvent) => void;
}

export const useStrategyStore = create<StrategyState>((set, get) => ({
  specs: [],
  instances: {},
  logs: {},
  loaded: false,
  loadError: null,

  load: async () => {
    try {
      const res = await bridge.get<{ specs: StrategySpec[]; instances: StrategyInstance[] }>(
        "/strategies",
      );
      set((s) => {
        const instances: Record<string, StrategyInstance> = {};
        for (const inst of res.instances) {
          // Preserve a pnl already known from a live strategy_pnl event —
          // the list endpoint does not compute per-instance P&L (it would
          // mean summing every instance's positions on every list refresh),
          // so a bare reload must not blank out what streaming already gave us.
          instances[inst.id] = { ...inst, pnl: s.instances[inst.id]?.pnl };
        }
        return { specs: res.specs, instances, loaded: true, loadError: null };
      });
    } catch (e) {
      set({ loadError: errorText(e) });
    }
  },

  rescan: async () => {
    try {
      const res = await bridge.post<{ scanned: number; created: number; skipped: number;
        errors: { file: string; reason: string }[] }>("/strategies/rescan");
      await get().load();
      return { ok: true, ...res };
    } catch (e) {
      return { ok: false, error: errorText(e) };
    }
  },

  create: async (specName, params, autoStart) => {
    try {
      const res = await bridge.post<{ ok: boolean; id?: string; code?: string; error?: string }>(
        "/strategies",
        { strategy: specName, params, autoStart },
      );
      if (res.ok) await get().load();
      return res;
    } catch (e) {
      return { ok: false, error: errorText(e) };
    }
  },

  update: async (id, params) => {
    try {
      const res = await bridge.post<{ ok: boolean; code?: string; error?: string }>(
        `/strategies/${id}/update`,
        { params },
      );
      if (res.ok) await refreshOne(id, set);
      return res;
    } catch (e) {
      return { ok: false, error: errorText(e) };
    }
  },

  duplicate: async (id) => {
    const source = get().instances[id];
    if (!source) return { ok: false, error: "Strategy instance not found." };
    return get().create(source.strategy, source.params, false);
  },

  start: async (id) => {
    try {
      const res = await bridge.post<{ ok: boolean; code?: string; error?: string }>(
        `/strategies/${id}/start`,
      );
      await refreshOne(id, set);
      return res;
    } catch (e) {
      return { ok: false, error: errorText(e) };
    }
  },

  stop: async (id) => {
    try {
      const res = await bridge.post<{ ok: boolean; code?: string; error?: string }>(
        `/strategies/${id}/stop`,
      );
      await refreshOne(id, set);
      return res;
    } catch (e) {
      return { ok: false, error: errorText(e) };
    }
  },

  remove: async (id) => {
    try {
      const res = await bridge.post<{ ok: boolean; code?: string; error?: string }>(
        `/strategies/${id}/remove`,
      );
      if (res.ok) {
        set((s) => {
          const instances = { ...s.instances };
          const logs = { ...s.logs };
          delete instances[id];
          delete logs[id];
          return { instances, logs };
        });
      }
      return res;
    } catch (e) {
      return { ok: false, error: errorText(e) };
    }
  },

  ingest: (e) => {
    if (e.type === "strategy_status") {
      set((s) => {
        const instances = { ...s.instances };
        if (e.removed) {
          delete instances[e.id];
        } else {
          instances[e.id] = {
            id: e.id, strategy: e.strategy, params: e.params, autoStart: e.autoStart,
            state: e.state, error: e.error ?? null, createdTs: e.createdTs,
            startedTs: e.startedTs ?? null, stoppedTs: e.stoppedTs ?? null,
            source: e.source,
            // Not carried by strategy_status — only the periodic list/detail
            // refresh knows these, so a live push must not blank them out.
            phase: instances[e.id]?.phase ?? null,
            positionCount: instances[e.id]?.positionCount ?? 0,
            pnl: instances[e.id]?.pnl,
          };
        }
        return { instances };
      });
      return;
    }
    if (e.type === "strategy_pnl") {
      set((s) => {
        const instances = { ...s.instances };
        for (const [id, pnl] of Object.entries(e.pnls)) {
          if (instances[id]) instances[id] = { ...instances[id], pnl };
        }
        return { instances };
      });
      return;
    }
    if (e.type === "strategy_log") {
      set((s) => {
        const existing = s.logs[e.instanceId] ?? [];
        const next = [...existing, { ts: e.ts, level: e.level, message: e.message }];
        if (next.length > MAX_LOG_ROWS) next.splice(0, next.length - MAX_LOG_ROWS);
        return { logs: { ...s.logs, [e.instanceId]: next } };
      });
    }
  },
}));

/** GET /strategies/{id} and merge just that one instance — used after
 *  start/stop so the UI reflects the outcome immediately rather than
 *  waiting on the strategy_status event's round trip (which also arrives,
 *  and converges to the same state either way). */
async function refreshOne(
  id: string,
  set: (fn: (s: StrategyState) => Partial<StrategyState>) => void,
) {
  try {
    const detail = await bridge.get<StrategyInstance>(`/strategies/${id}`);
    set((s) => ({ instances: { ...s.instances, [id]: detail } }));
  } catch {
    /* the live strategy_status event, if it arrives, still reconciles this */
  }
}

// phase/positionCount aren't pushed live (see StrategyInstance's own
// comment) — a short poll keeps the library view's Running/Waiting/
// Completed split and per-row position counts from going stale, reusing the
// same GET /strategies the initial load already does rather than adding a
// new push event for two fields that change at most once every few seconds.
const PHASE_POLL_MS = 5000;

// Wire live strategy_* events + initial load once, at module load — same
// module-singleton "wired" guard useBrokerStore.ts uses.
let wired = false;
export function connectStrategyStore() {
  if (wired) return;
  wired = true;
  const store = useStrategyStore.getState();
  bridge.on(store.ingest);
  bridge.onStatus((connected) => {
    if (connected) store.load();
  });
  store.load();
  setInterval(() => useStrategyStore.getState().load(), PHASE_POLL_MS);
}
