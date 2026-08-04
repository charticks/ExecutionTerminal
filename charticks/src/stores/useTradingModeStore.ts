import { create } from "zustand";
import { bridge } from "@/bridge/client";
import { useOrdersStore } from "@/stores/useOrdersStore";
import { usePositionsStore } from "@/stores/usePositionsStore";

// Paper vs Live execution mode. Paper simulates fills client-side (the existing
// mock order/position flow); Live routes real orders through the sidecar to the
// connected broker(s). The sidecar holds its own authoritative copy of the mode
// (set via POST /trading-mode) as a backstop so a stray order can never reach a
// broker while in Paper. Persisted so the choice survives restarts.

export type TradingMode = "paper" | "live";

const KEY = "ck.tradingMode";

function load(): TradingMode {
  return localStorage.getItem(KEY) === "live" ? "live" : "paper";
}

interface TradingModeState {
  mode: TradingMode;
  /** Set the mode, persist it, and sync the sidecar's authoritative copy. */
  setMode: (mode: TradingMode) => void;
  /** Wipe the client-side paper session (orders + positions) so Paper and Live
   *  never intermingle. Called when starting a fresh paper session. */
  clearPaperSession: () => void;
}

export const useTradingModeStore = create<TradingModeState>((set) => ({
  mode: load(),
  setMode: (mode) => {
    localStorage.setItem(KEY, mode);
    set({ mode });
    // Defense-in-depth: tell the sidecar so it refuses live placement unless it
    // too is in live mode. Best-effort — the client also branches on `mode`.
    bridge.post("/trading-mode", { mode }).catch(() => {});
  },
  clearPaperSession: () => {
    // The sidecar paper engine is authoritative — reset it, then clear the
    // local mirror so the UI blanks immediately (the reset also pushes an empty
    // paper_state snapshot).
    bridge.post("/paper/reset").catch(() => {});
    useOrdersStore.setState({ orders: [], trades: [], nextId: 1 });
    usePositionsStore.setState({ positions: [] });
  },
}));

// Push the persisted mode to the sidecar once at startup so server + client
// agree before any order is placed.
export function syncTradingMode() {
  bridge.post("/trading-mode", { mode: useTradingModeStore.getState().mode }).catch(() => {});
}
