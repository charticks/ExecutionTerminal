import { create } from "zustand";
import { INDEX_BY_ID } from "@/lib/indices";

// Shared Option Chain view state (selected instrument + strike range). Lifting
// these out of OptionChainPanel keeps the Option Chain and Positions tabs in
// sync and lets other screens read the "active instrument". Persisted.

export type StrikeRange = number | "all";

const KEY_INSTRUMENT = "ck.chain.instrument";
const KEY_RANGE = "ck.chain.range";

function loadInstrument(): string {
  const saved = localStorage.getItem(KEY_INSTRUMENT);
  return saved && saved in INDEX_BY_ID ? saved : "NIFTY";
}

function loadRange(): StrikeRange {
  const saved = localStorage.getItem(KEY_RANGE);
  if (saved === "all") return "all";
  const n = saved ? parseInt(saved, 10) : NaN;
  return Number.isNaN(n) ? 10 : n;
}

interface ChainState {
  instrument: string;
  range: StrikeRange;
  // "" → follow the nearest expiry; else a user-picked expiry. Cleared on
  // index change since each index has its own expiry calendar.
  expiry: string;
  setInstrument: (id: string) => void;
  setRange: (r: StrikeRange) => void;
  setExpiry: (e: string) => void;
}

export const useChainStore = create<ChainState>((set) => ({
  instrument: loadInstrument(),
  range: loadRange(),
  expiry: "",
  setInstrument: (instrument) => {
    localStorage.setItem(KEY_INSTRUMENT, instrument);
    set({ instrument, expiry: "" });
  },
  setRange: (range) => {
    localStorage.setItem(KEY_RANGE, String(range));
    set({ range });
  },
  setExpiry: (expiry) => set({ expiry }),
}));
