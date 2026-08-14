import { create } from "zustand";

export type Theme = "dark" | "light";
export type Density = "comfortable" | "compact" | "dense";
export type Screen = "home" | "orders" | "strategies" | "brokers" | "settings";

interface UiState {
  theme: Theme;
  density: Density;
  screen: Screen;
  /** Raised by marketGate() when a trading action is attempted outside market
   *  hours; the single "Market Closed" dialog lives in App and reads this.
   *  A string carries a MORE SPECIFIC reason than the generic message — a
   *  holiday name, or whatever the engine sent back — because "the market is
   *  closed" on a Tuesday morning reads as a bug rather than as a fact. */
  marketClosedNotice: boolean | string;
  /** Working order the user asked to modify (from the duplicate-order dialog).
   *  The matching row in the Home position grid enters edit mode and clears this. */
  editOrderId: string | null;
  setTheme: (t: Theme) => void;
  toggleTheme: () => void;
  setDensity: (d: Density) => void;
  setScreen: (s: Screen) => void;
  setMarketClosedNotice: (v: boolean | string) => void;
  setEditOrderId: (id: string | null) => void;
}

// Defaults chosen with the user: cyan accent (in CSS), dark theme, compact density.
const saved = {
  theme: (localStorage.getItem("ck.theme") as Theme) || "dark",
  density: (localStorage.getItem("ck.density") as Density) || "compact",
};

function apply(theme: Theme, density: Density) {
  const root = document.documentElement;
  root.setAttribute("data-theme", theme);
  root.setAttribute("data-density", density);
  localStorage.setItem("ck.theme", theme);
  localStorage.setItem("ck.density", density);
}
apply(saved.theme, saved.density);

export const useUiStore = create<UiState>((set, get) => ({
  theme: saved.theme,
  density: saved.density,
  screen: "home",
  marketClosedNotice: false,
  editOrderId: null,
  setTheme: (theme) => {
    apply(theme, get().density);
    set({ theme });
  },
  toggleTheme: () => {
    const theme = get().theme === "dark" ? "light" : "dark";
    apply(theme, get().density);
    set({ theme });
  },
  setDensity: (density) => {
    apply(get().theme, density);
    set({ density });
  },
  setScreen: (screen) => set({ screen }),
  setMarketClosedNotice: (marketClosedNotice) => set({ marketClosedNotice }),
  setEditOrderId: (editOrderId) => set({ editOrderId }),
}));
