import { useEffect } from "react";
import { Rail } from "./Rail";
import { StatusBar } from "./StatusBar";
import { Home } from "@/screens/Home";
import { Orders } from "@/screens/Orders";
import { Brokers } from "@/screens/Brokers";
import { Settings } from "@/screens/Settings";
import { Placeholder } from "@/screens/Placeholder";
import { useUiStore } from "@/stores/useUiStore";
import { connectMarketStore } from "@/stores/useMarketStore";
import { connectBrokerStore } from "@/stores/useBrokerStore";
import { startLiveChain } from "@/stores/useLiveChain";
import { syncTradingMode } from "@/stores/useTradingModeStore";
import { syncRiskConfig } from "@/stores/useRiskSync";
import { startPaperSync } from "@/stores/paperSync";
import { startLiveOrderSync } from "@/stores/liveOrderSync";
import { startContractSpecs } from "@/stores/useContractSpecs";
import { bridge } from "@/bridge/client";
import { mark } from "@/lib/startup";
import { InfoDialog } from "@/components/InfoDialog";
import { BootStatus } from "@/components/BootStatus";
import {
  MARKET_CLOSED_MESSAGE,
  MARKET_CLOSED_TITLE,
  startMarketSession,
} from "@/lib/marketSession";

export function App() {
  const screen = useUiStore((s) => s.screen);
  // One "Market Closed" dialog for the whole app — every trading action raises
  // this flag via marketGate() instead of owning its own modal.
  const marketClosedNotice = useUiStore((s) => s.marketClosedNotice);
  const setMarketClosedNotice = useUiStore((s) => s.setMarketClosedNotice);

  useEffect(() => {
    // Nothing here is needed to DRAW the Home page — every one of these
    // bootstraps talks to the sidecar, which is still starting. Running them in
    // this effect meant React's first commit was followed immediately by a
    // burst of IPC and fetches on the same thread, before the browser had a
    // chance to paint.
    //
    // They are therefore scheduled after the first frame. The user sees the
    // interface, and the wiring happens underneath it. The order below is
    // deliberate: the connection and the things the UI reads first, then the
    // config pushes, then the book mirrors.
    const boot = () => {
      mark("renderer:bootstrap-begin");
      // Each step timed individually. A single "bootstrap" mark tells you the
      // total and nothing about which of nine calls owned it; these are what
      // make a slow startup diagnosable from a log file alone.
      const step = (name: string, fn: () => void) => {
        const t0 = performance.now();
        try {
          fn();
        } catch (err) {
          mark(`renderer:boot-failed:${name}`, String(err));
          return;
        }
        mark(`renderer:boot:${name}`, `${Math.round(performance.now() - t0)}ms`);
      };
      step("market-store", connectMarketStore);     // opens the WebSocket
      step("broker-store", connectBrokerStore);     // stored accounts, then auto-connect
      // Before anything can size an order: lot sizes come from the engine's
      // instrument master, not the shipped table (see useContractSpecs).
      step("contract-specs", startContractSpecs);
      // Exchange holidays: nothing to compute, so the calendar is fetched.
      step("market-session", startMarketSession);
      step("live-chain", startLiveChain);
      step("trading-mode", syncTradingMode);
      step("risk-config", syncRiskConfig);
      step("paper-sync", startPaperSync);
      step("live-order-sync", startLiveOrderSync);
      mark("renderer:interactive");
    };
    // Two frames: one to commit, one to paint. `requestIdleCallback` would be
    // better still but is not guaranteed to run promptly on a busy startup, and
    // the engine connection should not wait on an idle window that may not come.
    const id = requestAnimationFrame(() => requestAnimationFrame(boot));

    // Network-up detection (absent before): the moment the OS regains
    // connectivity, ask the sidecar to force-reconnect the broker feed instead
    // of waiting out the reconnect backoff timer.
    const onOnline = () => {
      bridge.post("/brokers/reconnect").catch(() => {});
    };
    window.addEventListener("online", onOnline);
    return () => {
      cancelAnimationFrame(id);
      window.removeEventListener("online", onOnline);
    };
  }, []);

  return (
    <div className="app">
      <Rail />
      <StatusBar />
      {/* Narrates the background startup over an already-usable Home page,
          rather than hiding it behind a splash screen. Removes itself. */}
      <BootStatus />
      <main className="work">
        {screen === "home" && <Home />}
        {screen === "orders" && <Orders />}
        {screen === "strategies" && <Placeholder title="Strategies" />}
        {screen === "brokers" && <Brokers />}
        {screen === "settings" && <Settings />}
      </main>
      <InfoDialog
        open={marketClosedNotice !== false}
        title={MARKET_CLOSED_TITLE}
        message={
          typeof marketClosedNotice === "string"
            ? marketClosedNotice
            : MARKET_CLOSED_MESSAGE
        }
        onClose={() => setMarketClosedNotice(false)}
      />
    </div>
  );
}
