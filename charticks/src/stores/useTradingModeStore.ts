import { create } from "zustand";
import { bridge } from "@/bridge/client";
import { useOrdersStore } from "@/stores/useOrdersStore";
import { usePositionsStore } from "@/stores/usePositionsStore";

// Paper vs Live execution mode. Paper fills against the sidecar's tick-driven
// paper engine; Live routes real orders to the brokers with Execute enabled.
//
// This store is the user's choice, and every order request carries it (see
// useOrdersStore.placeOrder). The sidecar keeps its own confirmed copy purely to
// cross-check that value — it routes on the mode in the request, and rejects
// outright when the two disagree rather than picking one. So this store must
// keep the sidecar's copy current: on startup, on every change, and on every
// reconnect, since a sidecar restart resets its copy to paper.
//
// Persisted so the choice survives restarts.

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
  /** Blank the client-side order/trade/position mirror WITHOUT resetting either
   *  engine. The mirror only ever shows the active mode's book. */
  clearLedger: () => void;
}

function clearLedger() {
  useOrdersStore.setState({ orders: [], trades: [], nextId: 1 });
  usePositionsStore.setState({ positions: [] });
}

export const useTradingModeStore = create<TradingModeState>((set) => ({
  mode: load(),
  setMode: (mode) => {
    // ALWAYS blank the mirror on a mode change. The order book, trade book and
    // position grid are a mirror of ONE engine's state, and the two engines'
    // rows are indistinguishable once rendered — a paper fill left on screen
    // after switching to Live reads as a real trade, which is exactly what the
    // "save paper session" option used to cause.
    //
    // Nothing is lost: each engine keeps its own book (the sidecar's paper
    // engine, and the Order Synchronization Engine for live), and the mirror is
    // repainted from whichever is now active — see paperSync.paint() and
    // liveOrderSync.paintLiveBook().
    clearLedger();
    localStorage.setItem(KEY, mode);
    set({ mode });
    pushMode(mode);
  },
  clearPaperSession: () => {
    // The sidecar paper engine is authoritative — reset it, then clear the
    // local mirror so the UI blanks immediately (the reset also pushes an empty
    // paper_state snapshot).
    bridge.post("/paper/reset").catch(() => {});
    clearLedger();
  },
  clearLedger,
}));

function pushMode(mode: TradingMode) {
  bridge.post("/trading-mode", { mode }).catch(() => {});
}

/** Re-confirm the current mode with the sidecar. Safe to call at any time: it
 *  only ever tells the sidecar what the user already selected and the badge
 *  already shows. Called after a MODE_MISMATCH rejection so the user's next
 *  attempt goes through — deliberately WITHOUT retrying the order itself, which
 *  must stay a human decision. */
export function resyncTradingMode() {
  pushMode(useTradingModeStore.getState().mode);
}

/** Keep the sidecar's cross-check copy current: once at startup, and again on
 *  every reconnect — a restarted sidecar comes back believing it is in paper,
 *  which would otherwise reject every subsequent live order as a mismatch. */
export function syncTradingMode() {
  resyncTradingMode();
  bridge.onStatus((connected) => {
    if (connected) resyncTradingMode();
  });
}
