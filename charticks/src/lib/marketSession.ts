// Market-session validation for the renderer.
//
// Mirrors sidecar/services/market_session.py. The sidecar is authoritative — it
// re-checks and rejects with code "MARKET_CLOSED" — but every trading action asks
// this first so the user gets the dialog immediately instead of a round trip.
//
// Session: 09:15–15:30 IST, Monday–Friday. No exchange holiday calendar yet.

import { useUiStore } from "@/stores/useUiStore";

export const MARKET_OPEN_MINUTES = 9 * 60 + 15; // 09:15
export const MARKET_CLOSE_MINUTES = 15 * 60 + 30; // 15:30

export const MARKET_CLOSED_TITLE = "Market Closed";
export const MARKET_CLOSED_MESSAGE =
  "Trading is currently unavailable because the market is closed. " +
  "Please place orders during market hours.";

/** Sidecar error code for a market-hours rejection (bridge contract). */
export const MARKET_CLOSED_CODE = "MARKET_CLOSED";

const IST_FMT = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kolkata",
  weekday: "short",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

// Instruments with their own session. MCX commodities run 09:00–23:30 IST, well
// past the equity close. Mirrors _SESSIONS in sidecar/services/market_session.py.
// Anything absent here uses the equity window, so every index is unchanged.
const SESSIONS: Record<string, [number, number]> = {
  CRUDEOIL: [9 * 60, 23 * 60 + 30],
};

function sessionWindow(symbol?: string): [number, number] {
  return SESSIONS[(symbol ?? "").toUpperCase()] ?? [MARKET_OPEN_MINUTES, MARKET_CLOSE_MINUTES];
}

/** True while the session for `symbol` is live (equity-derivatives by default).
 *  Evaluated in IST, so a machine running in any timezone gets the same answer. */
export function isMarketOpen(now: Date = new Date(), symbol?: string): boolean {
  const parts = IST_FMT.formatToParts(now);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  const weekday = get("weekday");
  if (weekday === "Sat" || weekday === "Sun") return false;
  const minutes = parseInt(get("hour"), 10) * 60 + parseInt(get("minute"), 10);
  const [open, close] = sessionWindow(symbol);
  return minutes >= open && minutes <= close;
}

/** Guard used by every trading action:
 *
 *    if (!marketGate()) return;   // dialog shown, action aborted
 *
 *  Returns true when trading is allowed; otherwise raises the shared
 *  "Market Closed" dialog (rendered once in App) and returns false. */
export function marketGate(symbol?: string): boolean {
  if (isMarketOpen(new Date(), symbol)) return true;
  useUiStore.getState().setMarketClosedNotice(true);
  return false;
}

/** Same check without the dialog — for automated actions (auto square-off on a
 *  session-limit breach, auto-hedge) that must fail quietly. */
export function marketGateSilent(symbol?: string): boolean {
  return isMarketOpen(new Date(), symbol);
}
