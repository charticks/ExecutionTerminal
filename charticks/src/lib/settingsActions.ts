import { useSettingsStore } from "@/stores/useSettingsStore";
import { TICK_SIZE, MIN_PRICE } from "@/lib/orderValidation";

// Helpers that apply the active profile's Order Defaults at execution time.
// These read the store imperatively (getState) so they can be used inside async
// action handlers without re-rendering.

/** Delay applied before any trading action (Buy / Sell / Square Off / Exit /
 *  Roll), per the active profile's Execution Delay. Resolves immediately when 0. */
export function execDelay(): Promise<void> {
  const ms = useSettingsStore.getState().orderConfig().execDelayMs;
  if (!ms || ms <= 0) return Promise.resolve();
  return new Promise((r) => setTimeout(r, ms));
}

/** Round a price to the exchange tick grid, floored at the minimum price. */
export function roundToTick(price: number): number {
  const steps = Math.max(1, Math.round(price / TICK_SIZE));
  return +Math.max(MIN_PRICE, steps * TICK_SIZE).toFixed(2);
}

/** Apply the profile's Entry Price Offset (%) to a limit price. BUY shifts the
 *  acceptable price up, SELL shifts it down; both improve fill probability. */
export function applyEntryOffset(price: number, side: "BUY" | "SELL"): number {
  const pct = useSettingsStore.getState().orderConfig().entryOffsetPct;
  if (!pct) return price;
  const adj = side === "BUY" ? price * (1 + pct / 100) : price * (1 - pct / 100);
  return roundToTick(adj);
}

/** Pre-submit guard against the profile's Max Quantity / Max Price limits.
 *  Returns an error message, or "" when the order is within limits. */
export function orderLimitError(qty: number, price: number): string {
  const oc = useSettingsStore.getState().orderConfig();
  if (oc.maxQtyPerOrder > 0 && qty > oc.maxQtyPerOrder) {
    return `Quantity ${qty} exceeds the max per order (${oc.maxQtyPerOrder}).`;
  }
  if (oc.maxPrice > 0 && price > oc.maxPrice) {
    return `Price ₹${price.toFixed(2)} exceeds the max price (₹${oc.maxPrice.toFixed(2)}).`;
  }
  return "";
}
