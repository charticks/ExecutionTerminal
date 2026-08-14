// Market-session validation for the renderer.
//
// Mirrors sidecar/services/market_session.py. The sidecar is authoritative — it
// re-checks and rejects with code "MARKET_CLOSED" — but every trading action asks
// this first so the user gets the dialog immediately instead of a round trip.
//
// Session: 09:15–15:30 IST, Monday–Friday, minus the exchange holidays the
// sidecar serves from its calendar (GET /market-session). Holidays cannot be
// mirrored the way the clock can — there is nothing to compute — so they are
// fetched and cached here. Until they arrive a holiday behaves as it always did:
// the UI thinks the market is open and the sidecar returns the real reason.

import { useUiStore } from "@/stores/useUiStore";
import { bridge } from "@/bridge/client";

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

// Today's holiday per segment, as the sidecar reported it. null = trading day,
// undefined = not asked yet.
let holidayEquity: string | null | undefined;
let holidayCommodity: string | null | undefined;
let holidayDate = "";

/** Today's holiday for `symbol`, or null on a trading day. */
export function holidayFor(symbol?: string): string | null {
  const commodity = (symbol ?? "").toUpperCase() === "CRUDEOIL";
  return (commodity ? holidayCommodity : holidayEquity) ?? null;
}

/** Fetch the calendar. Called at startup, on every sidecar reconnect, and once
 *  the IST date rolls over — an app left open overnight must not carry
 *  yesterday's answer into a holiday morning. */
export async function refreshMarketSession(): Promise<void> {
  try {
    const res = await bridge.get<{
      holidayEquity?: string | null;
      holidayCommodity?: string | null;
      date?: string;
    }>("/market-session");
    holidayEquity = res.holidayEquity ?? null;
    holidayCommodity = res.holidayCommodity ?? null;
    holidayDate = res.date ?? "";
  } catch {
    // Sidecar not up. Leaves the cache as it was; the engine still rejects with
    // the real reason, so nothing can be traded on a holiday either way.
  }
}

let sessionWired = false;

export function startMarketSession() {
  if (sessionWired) return;
  sessionWired = true;
  void refreshMarketSession();
  bridge.onStatus((connected) => {
    if (connected) void refreshMarketSession();
  });
  // Cheap date-rollover check: re-ask whenever the IST date no longer matches
  // the one the cached answer was for.
  setInterval(() => {
    const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" })
      .format(new Date());
    if (holidayDate && today !== holidayDate) void refreshMarketSession();
  }, 60_000);
}

/** True while the session for `symbol` is live (equity-derivatives by default).
 *  Evaluated in IST, so a machine running in any timezone gets the same answer. */
export function isMarketOpen(now: Date = new Date(), symbol?: string): boolean {
  const parts = IST_FMT.formatToParts(now);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  const weekday = get("weekday");
  if (weekday === "Sat" || weekday === "Sun") return false;
  if (holidayFor(symbol)) return false;
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
  const holiday = holidayFor(symbol);
  useUiStore.getState().setMarketClosedNotice(
    holiday
      ? `The market is closed today — ${holiday}. No orders can be placed or modified.`
      : true,
  );
  return false;
}

/** Same check without the dialog — for automated actions (auto square-off on a
 *  session-limit breach, auto-hedge) that must fail quietly. */
export function marketGateSilent(symbol?: string): boolean {
  return isMarketOpen(new Date(), symbol);
}
