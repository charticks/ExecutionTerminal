import { useEffect } from "react";
import { Rail } from "./Rail";
import { StatusBar } from "./StatusBar";
import { Home } from "@/screens/Home";
import { Orders } from "@/screens/Orders";
import { Positions } from "@/screens/Positions";
import { Brokers } from "@/screens/Brokers";
import { Settings } from "@/screens/Settings";
import { Placeholder } from "@/screens/Placeholder";
import { useUiStore } from "@/stores/useUiStore";
import { connectMarketStore } from "@/stores/useMarketStore";
import { connectBrokerStore } from "@/stores/useBrokerStore";
import { startLiveChain } from "@/stores/useLiveChain";
import { syncTradingMode } from "@/stores/useTradingModeStore";
import { startPaperSync } from "@/stores/paperSync";
import { bridge } from "@/bridge/client";
import { InfoDialog } from "@/components/InfoDialog";
import { MARKET_CLOSED_MESSAGE, MARKET_CLOSED_TITLE } from "@/lib/marketSession";

export function App() {
  const screen = useUiStore((s) => s.screen);
  // One "Market Closed" dialog for the whole app — every trading action raises
  // this flag via marketGate() instead of owning its own modal.
  const marketClosedNotice = useUiStore((s) => s.marketClosedNotice);
  const setMarketClosedNotice = useUiStore((s) => s.setMarketClosedNotice);

  useEffect(() => {
    connectMarketStore();
    connectBrokerStore();
    startLiveChain();
    syncTradingMode();
    startPaperSync();

    // Network-up detection (absent before): the moment the OS regains
    // connectivity, ask the sidecar to force-reconnect the broker feed instead
    // of waiting out the reconnect backoff timer.
    const onOnline = () => {
      bridge.post("/brokers/reconnect").catch(() => {});
    };
    window.addEventListener("online", onOnline);
    return () => window.removeEventListener("online", onOnline);
  }, []);

  return (
    <div className="app">
      <Rail />
      <StatusBar />
      <main className="work">
        {screen === "home" && <Home />}
        {screen === "orders" && <Orders />}
        {screen === "positions" && <Positions />}
        {screen === "strategies" && <Placeholder title="Strategies" />}
        {screen === "brokers" && <Brokers />}
        {screen === "settings" && <Settings />}
      </main>
      <InfoDialog
        open={marketClosedNotice}
        title={MARKET_CLOSED_TITLE}
        message={MARKET_CLOSED_MESSAGE}
        onClose={() => setMarketClosedNotice(false)}
      />
    </div>
  );
}
