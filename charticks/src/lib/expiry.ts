// Renderer mirror of sidecar/services/expiry.py — Charticks' own expiry
// validation layer.
//
// Brokers keep returning yesterday's contracts for a while after the open, so
// the app never trusts a broker expiry list directly. Every surface that shows
// or selects a contract (Option Chain, strike selection, trade setup, rolls,
// contract search) filters through here, so the whole UI works off one
// validated set of active expiries. The sidecar re-checks authoritatively —
// this exists so an expired contract is never even offered.
//
// A contract stays tradable all day on its own expiry date and becomes expired
// once that session's close (15:30 IST) passes.

const IST_OFFSET_MIN = 5 * 60 + 30;
const CLOSE_MINUTES = 15 * 60 + 30; // 15:30 IST

const MONTHS: Record<string, number> = {
  JAN: 0, FEB: 1, MAR: 2, APR: 3, MAY: 4, JUN: 5,
  JUL: 6, AUG: 7, SEP: 8, OCT: 9, NOV: 10, DEC: 11,
};

/** Current wall-clock time in IST, independent of the host timezone. */
function nowIst(now = new Date()): { days: number; minutes: number } {
  const ist = now.getTime() + (IST_OFFSET_MIN + now.getTimezoneOffset()) * 60_000;
  const d = new Date(ist);
  return {
    days: Math.floor(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) / 86_400_000),
    minutes: d.getHours() * 60 + d.getMinutes(),
  };
}

/** Parse a broker expiry string ("23JUL2026", "2026-07-23") to a day number,
 *  or null when the format isn't recognised. */
export function parseExpiry(expiry: string): number | null {
  const raw = (expiry ?? "").trim().toUpperCase();
  if (!raw) return null;

  const ddMmmYyyy = /^(\d{1,2})[- ]?([A-Z]{3})[- ]?(\d{4})$/.exec(raw);
  if (ddMmmYyyy) {
    const month = MONTHS[ddMmmYyyy[2]];
    if (month === undefined) return null;
    return Math.floor(Date.UTC(+ddMmmYyyy[3], month, +ddMmmYyyy[1]) / 86_400_000);
  }

  const iso = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw);
  if (iso) return Math.floor(Date.UTC(+iso[1], +iso[2] - 1, +iso[3]) / 86_400_000);

  return null;
}

/** True when `expiry` has already passed in IST. An unparseable expiry is
 *  treated as NOT expired — hiding a contract we merely failed to parse would
 *  be worse than showing one stale row. */
export function isExpired(expiry: string, now = new Date()): boolean {
  const day = parseExpiry(expiry);
  if (day === null) return false;
  const ist = nowIst(now);
  if (day < ist.days) return true;
  return day === ist.days && ist.minutes > CLOSE_MINUTES;
}

/** Filter a broker expiry list to the still-tradable ones, chronologically
 *  ascending. Unparseable entries are kept and sort last. */
export function activeExpiries(expiries: string[], now = new Date()): string[] {
  return expiries
    .filter((e) => e && !isExpired(e, now))
    .sort((a, b) => (parseExpiry(a) ?? Infinity) - (parseExpiry(b) ?? Infinity));
}

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Compact, scannable expiry label for dense rows, e.g. "31 Jul". Falls back to
 *  the raw string when the format isn't recognised, and "" for no expiry. */
export function formatExpiry(expiry: string): string {
  const day = parseExpiry(expiry);
  if (day === null) return (expiry ?? "").trim();
  const d = new Date(day * 86_400_000);
  return `${d.getUTCDate()} ${MONTH_NAMES[d.getUTCMonth()]}`;
}

/** The expiry the app should default to: the caller's pick when it is still
 *  active, else the nearest active one ("" when none are). */
export function resolveActiveExpiry(preferred: string, expiries: string[], now = new Date()): string {
  const active = activeExpiries(expiries, now);
  if (preferred && active.includes(preferred)) return preferred;
  return active[0] ?? "";
}
