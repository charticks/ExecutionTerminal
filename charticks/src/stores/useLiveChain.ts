import { create } from "zustand";
import { bridge } from "@/bridge/client";
import type { BridgeEvent } from "@/bridge/events";

// Live option-chain snapshot from the sidecar. The panel selects the active
// index + strike count; the sidecar resolves tokens, subscribes on the
// shared market WebSocket, and pushes an option_chain_update event the
// instant a relevant tick lands (see broker_manager.add_option_tick_listener
// / OptionChainAdapter._on_option_tick) — no polling delay. A slow poll
// remains only as a reconciliation fallback in case a push is missed.

export interface ChainRow {
  strike: number;
  ce: number | null;
  pe: number | null;
  ceoi: number;
  peoi: number;
}

/** Live premium for one explicitly watched contract (see `watch`). */
export interface WatchQuote {
  strike: number;
  ltp: number | null;
}

export interface ChainSnapshot {
  symbol: string;
  expiry: string;
  expiries: string[];
  atm: number;
  rows: ChainRow[];
  /** Quotes for contracts registered via `watch` — the Roll picker's strikes,
   *  which usually sit outside the chain's ATM window. */
  watch: WatchQuote[];
}

/** Market-data plane health, independent of broker login state. */
export interface FeedStatus {
  shouldRun: boolean;
  connected: boolean;
  stale: boolean;
  consecutiveFailures: number;
  lastError: string | null;
}

interface LiveChainState {
  snapshot: ChainSnapshot;
  /** Per-strike rows, keyed by strike.
   *
   *  The identity of each entry is STABLE across updates whose numbers did not
   *  change. That is what makes the grid cheap: a row component subscribed to
   *  `byStrike[k]` re-renders only when its own strike actually moved, so a tick
   *  on three contracts repaints three rows instead of the whole chain. Before
   *  this, every push replaced the whole `rows` array and React re-rendered all
   *  ~100 rows and their ~400 buttons, ten times a second. */
  byStrike: Record<number, ChainRow>;
  /** The strikes currently in the window, ascending. Identity is stable while
   *  the window is unchanged, so the row LIST is not rebuilt on a price tick. */
  strikes: number[];
  /** null until first polled. */
  feed: FeedStatus | null;
  select: (symbol: string, count: number, expiry?: string) => void;
  /** Ask the sidecar to stream quotes for an explicit contract set (one index +
   *  expiry + option type, many strikes). Pass no strikes to stop watching. */
  watch: (symbol: string, expiry: string, optType: "CE" | "PE", strikes: number[]) => void;
  refresh: () => Promise<void>;
  ingest: (e: BridgeEvent) => void;
}

const EMPTY: ChainSnapshot = {
  symbol: "", expiry: "", expiries: [], atm: 0, rows: [], watch: [],
};

/** Fold a fresh snapshot's rows into the per-strike index, PRESERVING the object
 *  identity of every row whose numbers are unchanged.
 *
 *  This is the whole performance trick, and it belongs here rather than in the
 *  component: the store is the only place that can compare the incoming push
 *  against what is already on screen. Returns the previous objects untouched
 *  when nothing moved, so `React.memo` on a row is a real no-op rather than a
 *  shallow compare that always fails on a fresh object. */
function indexRows(
  prev: Record<number, ChainRow>,
  rows: ChainRow[],
): Record<number, ChainRow> {
  const byStrike: Record<number, ChainRow> = {};
  let changed = Object.keys(prev).length !== rows.length; // a strike left the window
  for (const row of rows) {
    const old = prev[row.strike];
    if (old && old.ce === row.ce && old.pe === row.pe
        && old.ceoi === row.ceoi && old.peoi === row.peoi) {
      byStrike[row.strike] = old;
    } else {
      byStrike[row.strike] = row;
      changed = true;
    }
  }
  // Return the SAME map when nothing moved, so a subscriber to the map (rather
  // than to one row) is not woken either.
  return changed ? byStrike : prev;
}

/** Keep the previous array when the contents are identical, so a subscriber is
 *  not woken by a price-only update that happened to rebuild the array. */
function stable<T>(prev: T[], next: T[]): T[] {
  if (prev.length === next.length && prev.every((v, i) => v === next[i])) return prev;
  return next;
}

export const useLiveChain = create<LiveChainState>((set) => ({
  snapshot: EMPTY,
  byStrike: {},
  strikes: [],
  feed: null,
  select: (symbol, count, expiry) => {
    bridge.post("/option-chain/select", { symbol, count, expiry }).catch(() => {});
  },
  watch: (symbol, expiry, optType, strikes) => {
    bridge.post("/option-chain/watch", { symbol, expiry, optType, strikes }).catch(() => {});
  },
  refresh: async () => {
    try {
      const snap = await bridge.get<ChainSnapshot>("/option-chain");
      set((s) => apply(s, snap));
    } catch {
      /* sidecar down — keep last snapshot */
    }
    try {
      set({ feed: await bridge.get<FeedStatus>("/market-feed") });
    } catch {
      /* keep last known feed status */
    }
  },
  ingest: (e) => {
    if (e.type !== "option_chain_update") return;
    set((s) => apply(s, {
      symbol: e.symbol,
      expiry: e.expiry,
      expiries: e.expiries ?? [],
      atm: e.atm,
      rows: e.rows,
      watch: e.watch ?? [],
    }));
  },
}));

/** One update, applied so that unchanged things keep their identity.
 *
 *  `snapshot` itself is rebuilt only when one of its own scalar fields moved.
 *  That matters as much as the per-row work: the panel subscribes to the header
 *  fields, and a fresh snapshot object on every tick re-rendered the whole
 *  panel — dropdowns, controls and all — ten times a second regardless of how
 *  cheap the rows had become.
 */
function apply(state: LiveChainState, snap: ChainSnapshot): Partial<LiveChainState> {
  const byStrike = indexRows(state.byStrike, snap.rows);
  const strikes = stable(state.strikes, snap.rows.map((r) => r.strike));
  const prev = state.snapshot;
  const expiries = stable(prev.expiries, snap.expiries);
  // WatchQuote objects are rebuilt on every push, so identity never matches;
  // compare by value, because the Roll dialog subscribes to this array.
  const watch = sameWatch(prev.watch, snap.watch) ? prev.watch : snap.watch;
  const unchanged = prev.symbol === snap.symbol && prev.expiry === snap.expiry
    && prev.atm === snap.atm && expiries === prev.expiries
    && watch === prev.watch && byStrike === state.byStrike;
  const snapshot = unchanged ? prev : { ...snap, expiries, watch };
  return { snapshot, byStrike, strikes };
}

function sameWatch(a: WatchQuote[], b: WatchQuote[]): boolean {
  return a.length === b.length
    && a.every((q, i) => q.strike === b[i].strike && q.ltp === b[i].ltp);
}

let started = false;
/** Live-pushed via option_chain_update; this is just the initial paint plus
 *  a slow reconciliation poll in case a push event is ever missed. */
export function startLiveChain() {
  if (started) return;
  started = true;
  bridge.on(useLiveChain.getState().ingest);
  const tick = () => useLiveChain.getState().refresh();
  tick();
  setInterval(tick, 5000);
}
