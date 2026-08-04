// Risk-rule maths shared by instrument defaults, new-trade creation, and rolls.
// A rule is an *offset* (points or percent of entry premium), not an absolute
// price — so a rolled position re-derives its SL/Target from the new premium
// while keeping the same rule.
//
// A rule is captured on the position at entry and never re-read from Settings
// afterwards, so changing defaults only affects future trades.

export type RiskMode = "points" | "percent";

/** How Trail SL follows a position.
 *  "point"  — trails off the option premium (Trail After / Trail Step points).
 *  "profit" — trails off the position's rupee profit (Start Trail / Trail Step ₹). */
export type TrailMode = "point" | "profit";

export interface TrailRule {
  mode: TrailMode;
  /** point: premium move (points) that earns one step.
   *  profit: rupee profit at which trailing arms. */
  after: number;
  /** point: points the SL moves per step.
   *  profit: rupee profit locked in per step. */
  step: number;
}

export interface RiskRule {
  /** Stop Loss / Target may each be switched off in Trade Defaults; when off the
   *  position simply carries no SL / Target. Undefined means on (legacy rules
   *  saved before the checkboxes existed). */
  slEnabled?: boolean;
  slMode: RiskMode;
  slVal: number;
  targetEnabled?: boolean;
  targetMode: RiskMode;
  targetVal: number;
  /** Absent when Trail SL is disabled for the profile that opened the trade. */
  trail?: TrailRule;
}

export type RiskSide = "BUY" | "SELL";

function offset(entry: number, mode: RiskMode, val: number): number {
  return mode === "percent" ? (entry * val) / 100 : val;
}

/** Absolute SL / Target prices for an option position from its entry premium.
 *  BUY: SL below entry, Target above. SELL inverts (SL above, Target below).
 *  A disabled leg comes back undefined rather than as a price. */
export function computeRiskPrices(
  entry: number,
  side: RiskSide,
  rule: RiskRule,
): { sl?: number; target?: number } {
  const out: { sl?: number; target?: number } = {};
  const clamp = (n: number) => +Math.max(0.05, n).toFixed(2);
  if (rule.slEnabled !== false) {
    const slOff = offset(entry, rule.slMode, rule.slVal);
    out.sl = clamp(side === "BUY" ? entry - slOff : entry + slOff);
  }
  if (rule.targetEnabled !== false) {
    const tgtOff = offset(entry, rule.targetMode, rule.targetVal);
    out.target = clamp(side === "BUY" ? entry + tgtOff : entry - tgtOff);
  }
  return out;
}
