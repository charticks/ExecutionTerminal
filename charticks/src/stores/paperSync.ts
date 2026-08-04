import { bridge } from "@/bridge/client";
import type { BridgeEvent } from "@/bridge/events";
import { useOrdersStore, type Order, type Trade } from "@/stores/useOrdersStore";
import { usePositionsStore, type OptionPosition } from "@/stores/usePositionsStore";
import { useTradingModeStore } from "@/stores/useTradingModeStore";
import { useSettingsStore } from "@/stores/useSettingsStore";

// Bridges the sidecar paper engine's `paper_state` snapshots into the paper
// order + position stores. Applied only in Paper mode so it never clobbers the
// live order book. The engine publishes on every change and on every relevant
// market tick, so the paper book (orders, trades, positions, P&L) stays live.

let started = false;

export function startPaperSync() {
  if (started) return;
  started = true;

  bridge.on((e: BridgeEvent) => {
    if (e.type !== "paper_state") return;
    if (useTradingModeStore.getState().mode !== "paper") return;
    useOrdersStore.getState().setFromSnapshot(
      e.orders as unknown as Order[],
      e.trades as unknown as Trade[],
    );
    usePositionsStore.getState().setFromSnapshot(e.positions as unknown as OptionPosition[]);
  });

  // Repaint from the engine's current book on (re)connect — a reloaded renderer
  // otherwise stays empty until the next tick/action.
  const paint = () => {
    if (useTradingModeStore.getState().mode === "paper") {
      bridge.get("/paper/state").catch(() => {});
    }
  };
  // Portfolio Trail Profit is enforced by the engine (it has to act on ticks
  // between renders), so the active profile's config is pushed down whenever it
  // changes — on save, on profile switch, and on every (re)connect.
  let lastSent = "";
  const pushPortfolioTrail = (force = false) => {
    const cfg = useSettingsStore.getState().portfolioTrailConfig();
    const json = JSON.stringify(cfg);
    if (!force && json === lastSent) return;
    lastSent = json;
    bridge.post("/portfolio-trail", cfg).catch(() => {});
  };
  useSettingsStore.subscribe(() => pushPortfolioTrail());

  bridge.onStatus((connected) => {
    if (connected) {
      paint();
      pushPortfolioTrail(true);
    }
  });
  paint();
  pushPortfolioTrail(true);
}
