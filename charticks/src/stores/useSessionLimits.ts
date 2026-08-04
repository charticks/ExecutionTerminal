import { create } from "zustand";
import { useSettingsStore } from "@/stores/useSettingsStore";

// Session-level risk controls (NOT per-position SL/target). Values persist;
// counters + lock status are per-session (reset on demand). A limit of 0 (or
// empty) means "disabled" — no validation is applied for that parameter.

export type LimitKey = "maxPos" | "maxTrades" | "maxLoss" | "profitTarget";

export type SessionStatus =
  | "active"
  | "locked-maxloss"
  | "locked-profit"
  | "locked-maxtrades"
  | "locked-maxpos";

export interface SessionLimits {
  enabled: boolean;
  maxPos: number;
  maxTrades: number;
  maxLoss: number; // rupees, positive magnitude; 0 = disabled
  profitTarget: number; // rupees; 0 = disabled
}

const KEY = "ck.sessionLimits";
const DEFAULTS: SessionLimits = {
  enabled: false,
  maxPos: 5,
  maxTrades: 5,
  maxLoss: 5000,
  profitTarget: 10000,
};

function load(): SessionLimits {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return { ...DEFAULTS, ...(JSON.parse(raw) as Partial<SessionLimits>) };
  } catch {
    /* fall through to defaults */
  }
  return DEFAULTS;
}

/** Human-readable reason for a locked / blocked state. */
export function statusReason(status: SessionStatus): string {
  switch (status) {
    case "locked-maxloss": return "Maximum Loss Reached";
    case "locked-profit": return "Profit Target Achieved";
    case "locked-maxtrades": return "Maximum Trades Reached";
    case "locked-maxpos": return "Maximum Positions Reached";
    default: return "Active";
  }
}

/** A hard lock (Max Loss / Profit Target) blocks ALL new entries until reset. */
export function isHardLocked(status: SessionStatus): boolean {
  return status === "locked-maxloss" || status === "locked-profit";
}

interface SessionLimitsState extends SessionLimits {
  tradesCount: number;
  status: SessionStatus;
  setEnabled: (enabled: boolean) => void;
  setLimit: (key: LimitKey, value: number) => void;
  /** Count a genuine new trade entry (NOT rolls or lot adjustments). */
  registerTrade: () => void;
  /** Clear counters + unlock for a fresh trading session. */
  reset: () => void;
  /** May a NEW position be opened right now? Exits/edits/rolls are unaffected. */
  canOpenPosition: (openCount: number) => { ok: boolean; reason: string };
  /** The Max Position limit in force: the live session value while session
   *  limits are on, otherwise the active profile's Risk Default. 0 = disabled. */
  effectiveMaxPos: () => number;
  /** Monitor Net P&L; hard-lock + square off when Max Loss / Profit Target hit.
   *  Returns true if it just triggered a lock (caller squares off). */
  evaluatePnl: (netPnl: number) => boolean;
}

export const useSessionLimits = create<SessionLimitsState>((set, get) => {
  const persist = () => {
    const { enabled, maxPos, maxTrades, maxLoss, profitTarget } = get();
    localStorage.setItem(KEY, JSON.stringify({ enabled, maxPos, maxTrades, maxLoss, profitTarget }));
  };
  return {
    ...load(),
    tradesCount: 0,
    status: "active",
    setEnabled: (enabled) => {
      set({ enabled });
      persist();
    },
    setLimit: (key, value) => {
      set({ [key]: Math.max(0, Math.round(value)) } as Pick<SessionLimits, LimitKey>);
      persist();
    },
    registerTrade: () => set((s) => ({ tradesCount: s.tradesCount + 1 })),
    reset: () => {
      // Seed a fresh session's limits from the active profile's Risk Defaults
      // (0 = disabled). These are defaults only; the Home bar can still override.
      const r = useSettingsStore.getState().riskDefaults();
      set({
        tradesCount: 0,
        status: "active",
        maxLoss: r.maxLoss,
        maxTrades: r.maxOrders,
        maxPos: r.maxPositions,
      });
      persist();
    },
    canOpenPosition: (openCount) => {
      const s = get();
      if (!s.enabled) return { ok: true, reason: "" };
      if (isHardLocked(s.status)) return { ok: false, reason: statusReason(s.status) };
      if (s.maxPos > 0 && openCount >= s.maxPos) {
        return { ok: false, reason: statusReason("locked-maxpos") };
      }
      if (s.maxTrades > 0 && s.tradesCount >= s.maxTrades) {
        return { ok: false, reason: statusReason("locked-maxtrades") };
      }
      return { ok: true, reason: "" };
    },
    effectiveMaxPos: () => {
      const s = get();
      return s.enabled ? s.maxPos : useSettingsStore.getState().riskDefaults().maxPositions;
    },
    evaluatePnl: (netPnl) => {
      const s = get();
      if (!s.enabled || isHardLocked(s.status)) return false;
      if (s.maxLoss > 0 && netPnl <= -s.maxLoss) {
        set({ status: "locked-maxloss" });
        return true;
      }
      if (s.profitTarget > 0 && netPnl >= s.profitTarget) {
        set({ status: "locked-profit" });
        return true;
      }
      return false;
    },
  };
});
