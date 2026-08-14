import { create } from "zustand";
import { type RiskRule, type TrailRule } from "@/lib/risk";
import { bridge } from "@/bridge/client";
import { lotSizeOf } from "@/stores/useContractSpecs";

// Paper positions are owned by the sidecar paper engine (services/paper_engine.py)
// and streamed here via the `paper_state` event (see startPaperSync). They mark to
// market off the SAME live feed as Live trading, so paper P&L tracks the market in
// real time — the only difference between Paper and Live is the execution engine,
// not the pricing. Structural actions (adjust/close/roll/risk/square-off) POST to
// the sidecar; the resulting snapshot replaces state, so the engine stays the
// single source of truth (no optimistic local edits that a snapshot would undo).

export type OptSide = "BUY" | "SELL";
export type OptType = "CE" | "PE";

export interface OptionPosition {
  id: string;
  underlying: string; // index id, e.g. "NIFTY"
  /** Contract expiry as the broker states it, e.g. "31JUL2026". Surfaced in the
   *  Positions grid and used to keep a roll on the same expiry. */
  expiry: string;
  strike: number;
  optType: OptType;
  side: OptSide;
  lots: number;
  entry: number; // first fill price
  avgEntry: number; // weighted average across all fills (== entry until averaged)
  exit?: number; // set when closed
  /** Set when the ENGINE closed the position automatically rather than the
   *  user — the paper book enforces SL / Target the way a live broker does. */
  exitReason?: "SL" | "TARGET";
  ltp: number;
  sl?: number;
  target?: number;
  /** Risk rule captured at entry (kept for parity with the engine payload). */
  rule?: RiskRule;
  /** Trail SL snapshot taken at entry. Absent when Trail SL was off for the
   *  profile that opened the trade. Editing it affects only this position. */
  trail?: TrailRule;
  status: "OPEN" | "CLOSED";
}

/** Lot size for an underlying.
 *
 *  Delegates to the engine's instrument master (useContractSpecs), falling back
 *  to the shipped table only while no master is loaded. This used to read the
 *  table directly, which meant an exchange lot-size revision silently made every
 *  order for that index fail the sidecar's lot-size check until a new build
 *  shipped. Kept as this name because every call site already uses it. */
export function lotSize(underlying: string): number {
  return lotSizeOf(underlying);
}

export function qtyOf(p: OptionPosition): number {
  return p.lots * lotSize(p.underlying);
}

export function pnlOf(p: OptionPosition): number {
  const last = p.status === "CLOSED" && p.exit != null ? p.exit : p.ltp;
  const dir = p.side === "BUY" ? 1 : -1;
  return (last - p.avgEntry) * qtyOf(p) * dir;
}

interface PositionsState {
  positions: OptionPosition[];
  /** Replace the book from an engine snapshot (called by startPaperSync). */
  setFromSnapshot: (positions: OptionPosition[]) => void;
  /** Add (delta>0, averages at LTP) or reduce lots — routed to the engine. */
  adjustLots: (id: string, delta: number) => void;
  /** Edit SL, Target and/or the trail parameters for a single position. Never
   *  touches any other position or the profile defaults. */
  setRisk: (
    id: string,
    patch: { sl?: number; target?: number; trailAfter?: number; trailStep?: number },
  ) => void;
  /** Roll a position to a new strike (close old, open new at newEntry). */
  rollPosition: (id: string, newStrike: number, newEntry: number) => void;
  /** Exit a fraction (0..1) of an open position (1 closes it fully). */
  closePosition: (id: string, fraction: number) => void;
  /** Square off every open position at market. */
  squareOffAll: () => void;
}

const post = (path: string, body?: unknown) => bridge.post(path, body).catch(() => {});

export const usePositionsStore = create<PositionsState>((set) => ({
  positions: [],
  setFromSnapshot: (positions) => set({ positions }),
  adjustLots: (id, delta) => post("/positions/adjust", { id, delta }),
  setRisk: (id, patch) => post("/positions/risk", { id, ...patch }),
  rollPosition: (id, newStrike, newEntry) =>
    post("/positions/roll", { id, newStrike, newEntry }),
  closePosition: (id, fraction) => post("/positions/close", { id, fraction }),
  squareOffAll: () => post("/positions/square-off"),
}));
