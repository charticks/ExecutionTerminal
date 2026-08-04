// Client-side limit-price validation, mirroring sidecar/services/paper_engine.py
// (_validate). The sidecar re-validates as the authoritative check; this exists
// to reject obviously bad input before a round-trip and show a clear message.

export const TICK_SIZE = 0.05;
export const MIN_PRICE = 0.05;

/** True when `n` is a valid option limit price: a real number ≥ the exchange
 *  minimum that sits on the tick grid. Rejects 0, negatives, NaN, and sub-/off-
 *  tick values like 0.001 (which would otherwise round to 0.00). */
export function isValidLimitPrice(n: number): boolean {
  if (!Number.isFinite(n) || n < MIN_PRICE) return false;
  const steps = Math.round(n / TICK_SIZE);
  return Math.abs(steps * TICK_SIZE - n) < 1e-6;
}

/** Human-readable reason a price is invalid, or "" when it is valid. */
export function limitPriceError(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "Enter a price greater than 0.";
  if (n < MIN_PRICE) return `Minimum price is ₹${MIN_PRICE.toFixed(2)}.`;
  if (!isValidLimitPrice(n)) return `Price must be a multiple of ₹${TICK_SIZE.toFixed(2)}.`;
  return "";
}
