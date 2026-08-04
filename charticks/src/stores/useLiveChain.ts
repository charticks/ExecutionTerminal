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

export const useLiveChain = create<LiveChainState>((set) => ({
  snapshot: EMPTY,
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
      set({ snapshot: snap });
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
    set({
      snapshot: {
        symbol: e.symbol,
        expiry: e.expiry,
        expiries: e.expiries ?? [],
        atm: e.atm,
        rows: e.rows,
        watch: e.watch ?? [],
      },
    });
  },
}));

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
