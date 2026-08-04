import { create } from "zustand";

// Positions-grid column visibility + roll preferences. Persisted so a trader's
// choices (hiding the option-seller "Roll" column, the active Strike Band)
// survive across sessions.

export type ColKey = "roll";

export interface ColPrefs {
  roll: boolean;
}

/** Roll Strike Band — how many strikes away Roll Up/Down offers. */
export type StrikeBand = "1-5" | "5-10" | "10-15";

/** [lo, hi] offset range (inclusive) for a band. */
export const BAND_RANGES: Record<StrikeBand, [number, number]> = {
  "1-5": [1, 5],
  "5-10": [5, 10],
  "10-15": [10, 15],
};

const KEY = "ck.positions.cols";
const BAND_KEY = "ck.roll.band";
const DEFAULTS: ColPrefs = { roll: true };
const DEFAULT_BAND: StrikeBand = "1-5";

function load(): ColPrefs {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return { ...DEFAULTS, ...(JSON.parse(raw) as Partial<ColPrefs>) };
  } catch {
    /* fall through to defaults */
  }
  return { ...DEFAULTS };
}

function loadBand(): StrikeBand {
  const raw = localStorage.getItem(BAND_KEY);
  return raw === "5-10" || raw === "10-15" || raw === "1-5" ? raw : DEFAULT_BAND;
}

interface GridPrefsState {
  cols: ColPrefs;
  strikeBand: StrikeBand;
  setCol: (key: ColKey, on: boolean) => void;
  setStrikeBand: (band: StrikeBand) => void;
}

export const useGridPrefsStore = create<GridPrefsState>((set, get) => ({
  cols: load(),
  strikeBand: loadBand(),
  setCol: (key, on) => {
    const cols = { ...get().cols, [key]: on };
    localStorage.setItem(KEY, JSON.stringify(cols));
    set({ cols });
  },
  setStrikeBand: (strikeBand) => {
    localStorage.setItem(BAND_KEY, strikeBand);
    set({ strikeBand });
  },
}));
