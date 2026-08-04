import { create } from "zustand";
import type { RiskMode, RiskRule, TrailMode } from "@/lib/risk";

// ─────────────────────────────────────────────────────────────────────────────
// Single source of truth for all trading *configuration* (not runtime session
// controls, which live on Home). Config is organised into per-weekday Trading
// Profiles; the active profile determines every value the rest of the app reads
// through the accessors below. Trading Style controls only which cards are shown
// in the UI — it never discards hidden values.
//
// Persistence is a single `ck.settings` localStorage key (profiles + the
// auto-select preference + the last manual profile). Runtime state (which
// profile is active this session, and whether it's a session override) is not
// persisted — it is re-derived on every launch.
// ─────────────────────────────────────────────────────────────────────────────

export type Weekday = "MON" | "TUE" | "WED" | "THU" | "FRI" | "OTHERS";
export type TradingStyle = "SELLER" | "BUYER" | "HYBRID";
export type ProductType = "NRML" | "MIS";
export type Validity = "DAY" | "IOC";
export type SettingsOrderType = "MARKET" | "LIMIT";

export const WEEKDAYS: Weekday[] = ["MON", "TUE", "WED", "THU", "FRI", "OTHERS"];
export const WEEKDAY_LABEL: Record<Weekday, string> = {
  MON: "Mon",
  TUE: "Tue",
  WED: "Wed",
  THU: "Thu",
  FRI: "Fri",
  OTHERS: "Others",
};
export const WEEKDAY_SUBLABEL: Record<Weekday, string> = {
  MON: "Normal Day",
  TUE: "Expiry Day",
  WED: "Normal Day",
  THU: "Normal Day",
  FRI: "Weekly Expiry",
  OTHERS: "Default",
};

export const STYLE_LABEL: Record<TradingStyle, string> = {
  SELLER: "Seller",
  BUYER: "Buyer",
  HYBRID: "Hybrid",
};

/** Instruments configurable on the Settings page. FINNIFTY / MIDCPNIFTY are
 *  intentionally excluded here (they remain tradeable elsewhere). */
export const SETTINGS_INSTRUMENTS = ["NIFTY", "BANKNIFTY", "SENSEX"] as const;
export const PARTIAL_EXIT_CHOICES = [25, 50, 75, 100] as const;

/** Per-instrument default values. Both Trail SL modes keep their own pair of
 *  numbers so switching mode back and forth never destroys the other's values —
 *  only one pair is ever *shown* (see the Instrument Defaults table). */
export interface InstrumentDefault {
  sl: number;
  target: number;
  /** Point-based trailing: premium move (points) that earns one step. */
  trailAfter: number;
  /** Point-based trailing: points the SL moves per step. */
  trailStep: number;
  /** Profit-based trailing: rupee profit at which trailing arms. */
  startTrail: number;
  /** Profit-based trailing: rupee profit locked in per step. */
  profitStep: number;
}

/** Which risk features are active, and how their values are interpreted. Each
 *  feature is independently switchable; a disabled feature is ignored when a new
 *  trade is opened (existing positions keep whatever they captured at entry). */
export interface TradeSection {
  slEnabled: boolean;
  slMode: RiskMode;
  targetEnabled: boolean;
  targetMode: RiskMode;
  trailEnabled: boolean;
  trailMode: TrailMode;
}

/** Portfolio Trail Profit — a single GLOBAL feature (never per-instrument and
 *  never per-position). Once combined open P&L reaches `activateAfter`, a
 *  give-back of `trailDistance` from the running peak squares off everything. */
export interface PortfolioTrailSection {
  enabled: boolean;
  activateAfter: number;
  trailDistance: number;
}

export interface OrderSection {
  defaultQty: number;
  maxQtyPerOrder: number;
  maxPrice: number;
  execDelayMs: number;
  entryOffsetPct: number;
  orderType: SettingsOrderType;
  product: ProductType;
  validity: Validity;
  partialExits: number[]; // subset of PARTIAL_EXIT_CHOICES
}

/** What to do when an order would push a position past the Max Position limit.
 *  Only consulted when `customizeMaxPos` is on; otherwise "ask" applies. */
export type MaxPosBehavior = "ask" | "block" | "auto" | "override";

export const MAX_POS_BEHAVIORS: { value: MaxPosBehavior; label: string }[] = [
  { value: "ask", label: "Ask Me (Recommended)" },
  { value: "block", label: "Block Order" },
  { value: "auto", label: "Auto Add Remaining Quantity" },
  { value: "override", label: "Always Override" },
];

export const MAX_POS_BEHAVIOR_LABEL: Record<MaxPosBehavior, string> = Object.fromEntries(
  MAX_POS_BEHAVIORS.map((b) => [b.value, b.label]),
) as Record<MaxPosBehavior, string>;

export interface RiskSection {
  maxLoss: number;
  maxOrders: number;
  maxPositions: number;
  /** Advanced: opt in to choosing the overflow behaviour explicitly. */
  customizeMaxPos: boolean;
  maxPosBehavior: MaxPosBehavior;
}

export interface HedgeSection {
  enabled: boolean;
  distancePts: number;
  retryFailed: boolean;
  maxRetries: number;
}

export interface NotifySection {
  executed: boolean;
  modified: boolean;
  tradeAlert: boolean;
  system: boolean;
}

export interface ProfileConfig {
  style: TradingStyle;
  trade: TradeSection;
  instruments: Record<string, InstrumentDefault>;
  order: OrderSection;
  portfolioTrail: PortfolioTrailSection;
  risk: RiskSection;
  notify: NotifySection;
  hedge: HedgeSection;
}

interface SettingsPersisted {
  profiles: Record<Weekday, ProfileConfig>;
  autoSelect: boolean;
  lastManualProfile: Weekday;
}

const KEY = "ck.settings";

// Seed SL/Target/trailing values, matching the legacy per-instrument defaults.
const SEED_INSTRUMENTS: Record<string, InstrumentDefault> = {
  NIFTY: { sl: 20, target: 40, trailAfter: 10, trailStep: 10, startTrail: 5000, profitStep: 1000 },
  BANKNIFTY: { sl: 40, target: 80, trailAfter: 20, trailStep: 10, startTrail: 5000, profitStep: 1000 },
  SENSEX: { sl: 30, target: 60, trailAfter: 40, trailStep: 20, startTrail: 5000, profitStep: 1000 },
};

/** Fallback used for an instrument with no configured row (e.g. FINNIFTY). */
export const FALLBACK_INSTRUMENT_DEFAULT: InstrumentDefault = {
  sl: 20, target: 40, trailAfter: 10, trailStep: 10, startTrail: 5000, profitStep: 1000,
};

function seedInstruments(): Record<string, InstrumentDefault> {
  return Object.fromEntries(
    SETTINGS_INSTRUMENTS.map((id) => [id, { ...SEED_INSTRUMENTS[id] }]),
  );
}

function defaultProfile(style: TradingStyle = "SELLER"): ProfileConfig {
  return {
    style,
    trade: {
      slEnabled: true,
      slMode: "points",
      targetEnabled: true,
      targetMode: "points",
      trailEnabled: false,
      trailMode: "point",
    },
    instruments: seedInstruments(),
    order: {
      defaultQty: 1,
      maxQtyPerOrder: 0,
      maxPrice: 0,
      execDelayMs: 0,
      entryOffsetPct: 0,
      orderType: "LIMIT",
      product: "NRML",
      validity: "DAY",
      partialExits: [25, 50, 100],
    },
    portfolioTrail: {
      enabled: false,
      activateAfter: 10000,
      trailDistance: 2000,
    },
    risk: {
      maxLoss: 0,
      maxOrders: 0,
      maxPositions: 0,
      customizeMaxPos: false,
      maxPosBehavior: "ask",
    },
    notify: {
      executed: true,
      modified: true,
      tradeAlert: true,
      system: true,
    },
    hedge: {
      enabled: false,
      distancePts: 100,
      retryFailed: true,
      maxRetries: 3,
    },
  };
}

function seedProfiles(): Record<Weekday, ProfileConfig> {
  return Object.fromEntries(
    WEEKDAYS.map((d) => [d, defaultProfile()]),
  ) as Record<Weekday, ProfileConfig>;
}

/** Map a JS Date weekday (0=Sun … 6=Sat) to a profile; weekend → Others. */
export function weekdayForToday(date = new Date()): Weekday {
  switch (date.getDay()) {
    case 1: return "MON";
    case 2: return "TUE";
    case 3: return "WED";
    case 4: return "THU";
    case 5: return "FRI";
    default: return "OTHERS";
  }
}

/** Deep-merge a persisted profile onto a fresh default so new fields added in
 *  later versions are always present (and hidden/unknown values are preserved). */
function mergeProfile(base: ProfileConfig, saved: Partial<ProfileConfig> | undefined): ProfileConfig {
  if (!saved) return base;
  // Per-row merge (not a plain spread) so instrument rows persisted before the
  // trailing fields existed come back with the seeded trail values filled in.
  const instruments = { ...base.instruments };
  for (const [id, row] of Object.entries(saved.instruments ?? {})) {
    instruments[id] = { ...(base.instruments[id] ?? FALLBACK_INSTRUMENT_DEFAULT), ...row };
  }
  return {
    style: saved.style ?? base.style,
    trade: { ...base.trade, ...saved.trade },
    instruments,
    order: { ...base.order, ...saved.order },
    portfolioTrail: { ...base.portfolioTrail, ...saved.portfolioTrail },
    risk: { ...base.risk, ...saved.risk },
    notify: { ...base.notify, ...saved.notify },
    hedge: { ...base.hedge, ...saved.hedge },
  };
}

/** One-time import of the legacy fragmented localStorage keys into every seeded
 *  profile, so an upgrading user keeps their SL/Target modes, per-instrument
 *  defaults, order type and session-limit values. */
function importLegacy(profiles: Record<Weekday, ProfileConfig>): void {
  try {
    const td = JSON.parse(localStorage.getItem("ck.tradeDefaults") ?? "null");
    const sl = JSON.parse(localStorage.getItem("ck.sessionLimits") ?? "null");
    const orderType = localStorage.getItem("ck.orderEntry.type");
    for (const d of WEEKDAYS) {
      const p = profiles[d];
      if (td && typeof td === "object") {
        if (td.slMode) p.trade.slMode = td.slMode;
        if (td.targetMode) p.trade.targetMode = td.targetMode;
        if (td.perInstrument) {
          for (const id of SETTINGS_INSTRUMENTS) {
            // Merge onto the seed so the trailing fields survive the import.
            if (td.perInstrument[id]) {
              p.instruments[id] = { ...p.instruments[id], ...td.perInstrument[id] };
            }
          }
        }
      }
      if (sl && typeof sl === "object") {
        if (typeof sl.maxLoss === "number") p.risk.maxLoss = sl.maxLoss;
        if (typeof sl.maxTrades === "number") p.risk.maxOrders = sl.maxTrades;
        if (typeof sl.maxPos === "number") p.risk.maxPositions = sl.maxPos;
      }
      if (orderType === "LIMIT" || orderType === "MARKET") p.order.orderType = orderType;
    }
  } catch {
    /* best-effort import; ignore malformed legacy data */
  }
}

function load(): SettingsPersisted {
  const fresh: SettingsPersisted = {
    profiles: seedProfiles(),
    autoSelect: true,
    lastManualProfile: weekdayForToday(),
  };
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) {
      importLegacy(fresh.profiles);
      return fresh;
    }
    const p = JSON.parse(raw) as Partial<SettingsPersisted>;
    const profiles = seedProfiles();
    for (const d of WEEKDAYS) profiles[d] = mergeProfile(profiles[d], p.profiles?.[d]);
    return {
      profiles,
      autoSelect: p.autoSelect ?? true,
      lastManualProfile: (p.lastManualProfile && WEEKDAYS.includes(p.lastManualProfile))
        ? p.lastManualProfile
        : weekdayForToday(),
    };
  } catch {
    return fresh;
  }
}

interface SettingsState extends SettingsPersisted {
  /** Profile active for this session (never persisted directly). */
  activeProfile: Weekday;
  /** True when the user manually overrode auto-selection this session. */
  sessionOverride: boolean;

  setActiveProfile: (w: Weekday) => void;
  setAutoSelect: (b: boolean) => void;
  setStyle: (style: TradingStyle) => void;
  /** Patch one section of the ACTIVE profile. */
  patch: <K extends keyof Omit<ProfileConfig, "style">>(
    section: K,
    value: Partial<ProfileConfig[K]>,
  ) => void;
  setInstrumentDefault: (id: string, d: InstrumentDefault) => void;
  /** Commit a whole edited profile at once — used by the Settings page, which
   *  edits a local draft and saves it on "Save Changes". */
  replaceActive: (p: ProfileConfig) => void;

  // ── accessors (read the active profile) ──────────────────────────────────
  active: () => ProfileConfig;
  ruleFor: (instrument: string) => RiskRule;
  orderConfig: () => OrderSection;
  portfolioTrailConfig: () => PortfolioTrailSection;
  hedgeConfig: () => HedgeSection;
  notifyConfig: () => NotifySection;
  riskDefaults: () => RiskSection;
  /** Overflow behaviour actually in force: "ask" unless the user opted into
   *  customising it, and "override" (i.e. never blocked) when Max Positions is
   *  disabled — the setting doesn't apply then. */
  maxPosBehavior: () => MaxPosBehavior;
}

/** Which profile should be active at startup given the persisted prefs. */
function initialActive(p: SettingsPersisted): Weekday {
  return p.autoSelect ? weekdayForToday() : p.lastManualProfile;
}

export const useSettingsStore = create<SettingsState>((set, get) => {
  const persisted = load();

  const persist = () => {
    const { profiles, autoSelect, lastManualProfile } = get();
    localStorage.setItem(KEY, JSON.stringify({ profiles, autoSelect, lastManualProfile }));
  };

  const updateActive = (fn: (p: ProfileConfig) => ProfileConfig) => {
    const { activeProfile, profiles } = get();
    const next = { ...profiles, [activeProfile]: fn(profiles[activeProfile]) };
    set({ profiles: next });
    persist();
  };

  return {
    ...persisted,
    activeProfile: initialActive(persisted),
    sessionOverride: false,

    setActiveProfile: (w) => {
      const { autoSelect } = get();
      if (autoSelect) {
        // Session-only override — do NOT persist; next launch re-derives by weekday.
        set({ activeProfile: w, sessionOverride: w !== weekdayForToday() });
      } else {
        set({ activeProfile: w, lastManualProfile: w, sessionOverride: false });
        persist();
      }
    },

    setAutoSelect: (b) => {
      set({ autoSelect: b });
      // Recompute the active profile from the new preference.
      if (b) {
        set({ activeProfile: weekdayForToday(), sessionOverride: false });
      } else {
        set({ lastManualProfile: get().activeProfile, sessionOverride: false });
      }
      persist();
    },

    setStyle: (style) => updateActive((p) => ({ ...p, style })),

    patch: (section, value) =>
      updateActive((p) => ({ ...p, [section]: { ...p[section], ...value } })),

    setInstrumentDefault: (id, d) =>
      updateActive((p) => ({ ...p, instruments: { ...p.instruments, [id]: d } })),

    replaceActive: (next) => updateActive(() => next),

    active: () => get().profiles[get().activeProfile],
    // The rule a NEW trade captures. Disabled features are encoded here rather
    // than consulted later, which is what makes a settings change apply to
    // future trades only — a live position keeps the rule it was opened with.
    ruleFor: (instrument) => {
      const p = get().profiles[get().activeProfile];
      const d = p.instruments[instrument] ?? FALLBACK_INSTRUMENT_DEFAULT;
      const t = p.trade;
      return {
        slEnabled: t.slEnabled,
        slMode: t.slMode,
        slVal: d.sl,
        targetEnabled: t.targetEnabled,
        targetMode: t.targetMode,
        targetVal: d.target,
        // Trail SL moves an existing stop, so it is meaningless — and would be
        // a nasty surprise — without one. A profile with Stop Loss off never
        // emits a trail, whatever the Trail SL checkbox says.
        ...(t.trailEnabled && t.slEnabled
          ? {
              trail: t.trailMode === "point"
                ? { mode: "point" as const, after: d.trailAfter, step: d.trailStep }
                : { mode: "profit" as const, after: d.startTrail, step: d.profitStep },
            }
          : {}),
      };
    },
    orderConfig: () => get().profiles[get().activeProfile].order,
    portfolioTrailConfig: () => get().profiles[get().activeProfile].portfolioTrail,
    hedgeConfig: () => get().profiles[get().activeProfile].hedge,
    notifyConfig: () => get().profiles[get().activeProfile].notify,
    riskDefaults: () => get().profiles[get().activeProfile].risk,
    maxPosBehavior: () => {
      const r = get().profiles[get().activeProfile].risk;
      if (r.maxPositions <= 0) return "override";
      return r.customizeMaxPos ? r.maxPosBehavior : "ask";
    },
  };
});
