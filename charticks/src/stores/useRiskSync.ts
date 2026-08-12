import { bridge } from "@/bridge/client";
import { useSettingsStore } from "@/stores/useSettingsStore";
import { useSessionLimits } from "@/stores/useSessionLimits";

// Ships the limits the sidecar enforces (services/risk_engine.py). The renderer
// still evaluates its own copies so it can grey out a control or explain a limit
// without a round trip — but the sidecar makes the final call, and it can only
// enforce what it has been told.
//
// The sidecar refuses LIVE orders until this has arrived, so this must be pushed
// on startup, on every settings/session-limit change, and on every reconnect.

export interface RiskConfigPayload {
  maxQtyPerOrder: number;
  maxPrice: number;
  maxPositions: number;
  maxOrders: number;
  maxLoss: number;
  profitTarget: number;
  sessionLimitsEnabled: boolean;
}

/** Resolve the limits actually in force, exactly as the UI resolves them: the
 *  live session bar wins while session limits are on, otherwise the active
 *  profile's Risk Defaults apply. 0 means disabled on both sides. */
export function resolveRiskConfig(): RiskConfigPayload {
  const order = useSettingsStore.getState().orderConfig();
  const defaults = useSettingsStore.getState().riskDefaults();
  const session = useSessionLimits.getState();
  const on = session.enabled;
  return {
    maxQtyPerOrder: order.maxQtyPerOrder,
    maxPrice: order.maxPrice,
    maxPositions: on ? session.maxPos : defaults.maxPositions,
    maxOrders: on ? session.maxTrades : defaults.maxOrders,
    maxLoss: on ? session.maxLoss : defaults.maxLoss,
    // Profit Target exists only as a live session control — there is no
    // profile-level default for it, so it is simply off when the bar is off.
    profitTarget: on ? session.profitTarget : 0,
    sessionLimitsEnabled: on,
  };
}

let lastSent = "";

/** Push the current limits. Skips a redundant push when nothing changed, since
 *  both source stores fire on unrelated edits too. */
export function pushRiskConfig(force = false) {
  const payload = resolveRiskConfig();
  const encoded = JSON.stringify(payload);
  if (!force && encoded === lastSent) return;
  lastSent = encoded;
  bridge.post("/risk-config", payload).catch(() => {
    // Failed to land — clear the cache so the next attempt re-sends rather than
    // being suppressed as a duplicate. The sidecar meanwhile refuses live
    // orders, so a dropped push is safe but must not become permanent.
    lastSent = "";
  });
}

let wired = false;

/** Wire the pushes. Called once at startup alongside the other store bootstraps. */
export function syncRiskConfig() {
  if (wired) return;
  wired = true;
  pushRiskConfig(true);
  useSettingsStore.subscribe(() => pushRiskConfig());
  useSessionLimits.subscribe(() => pushRiskConfig());
  bridge.onStatus((connected) => {
    // A restarted sidecar has no config and is refusing live orders — re-send
    // unconditionally rather than relying on the dedupe cache.
    if (connected) pushRiskConfig(true);
  });
}
