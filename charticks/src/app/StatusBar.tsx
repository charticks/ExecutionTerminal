import { useEffect, useState } from "react";
import { Icon } from "@/components/Icon";
import { Popover, usePopover } from "@/components/Popover";
import { useUiStore, type Density } from "@/stores/useUiStore";
import { useMarketStore } from "@/stores/useMarketStore";
import { useBrokerStore } from "@/stores/useBrokerStore";
import { useTradingModeStore } from "@/stores/useTradingModeStore";
import { SwitchToLiveDialog } from "@/components/TradingModeDialog";
import { displayName, healthClass } from "@/bridge/brokers";

const DENSITIES: { d: Density; icon: string; label: string }[] = [
  { d: "comfortable", icon: "d2", label: "Comfortable density" },
  { d: "compact", icon: "d3", label: "Compact density" },
  { d: "dense", icon: "d4", label: "Dense density" },
];

function useClock() {
  const [t, setT] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setT(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return t.toLocaleTimeString("en-GB");
}

/** Quick status menu (not a nav button). Shows connected brokers; the only
 *  navigation is an explicit "Manage Brokers →" / "Connect Broker →" action
 *  that opens the Brokers page. The user otherwise stays on the current screen. */
function BrokerIndicator() {
  const accounts = useBrokerStore((s) => s.accounts);
  const health = useBrokerStore((s) => s.health);
  const connectionHealth = useBrokerStore((s) => s.connectionHealth);
  const setScreen = useUiStore((s) => s.setScreen);
  const { open, toggle, setOpen, wrapRef } = usePopover();

  const states = accounts.map((a) => health[a.id]?.health);
  const connected = accounts.filter((a) => health[a.id]?.health === "connected");
  const any = connected.length > 0;
  // Worst actionable state drives the top-bar dot animation. Prefer the
  // sidecar's aggregate connection_health (reliability layer) once it has
  // reported in; fall back to the per-account heuristic until then.
  const dotState = connectionHealth
    ? connectionHealth.state === "connected"
      ? "ok"
      : connectionHealth.state === "auth_failed"
        ? "warn"
        : connectionHealth.state === "reconnecting"
          ? "pending"
          : "off"
    : any
      ? "ok"
      : states.some((h) => h === "session_expired")
        ? "warn"
        : states.some((h) => h === "connecting" || h === "reconnecting")
          ? "pending"
          : "off";

  // Accounts can be authenticated while the market-data feed is dead — the
  // badge used to show a plain green "Brokers (1)" for that, so an empty
  // option chain had no visible explanation. Call it out explicitly.
  const feedDown = connectionHealth?.detail?.startsWith("market data feed down") ?? false;

  // The colored `.d` dot (driven by dotState) is the sole status indicator —
  // keep the label text emoji-free to avoid a duplicate second dot.
  const badgeLabel = connectionHealth
    ? connectionHealth.state === "connected"
      ? `Brokers (${connectionHealth.accountsConnected})`
      : connectionHealth.state === "reconnecting"
        ? feedDown
          ? `Market Data Down${connectionHealth.accountsConnected ? ` (${connectionHealth.accountsConnected} logged in)` : ""}`
          : "Reconnecting..."
        : connectionHealth.state === "auth_failed"
          ? "Authentication Failed"
          : "Disconnected"
    : any
      ? `Brokers (${connected.length})`
      : "Disconnected";

  const goManage = () => {
    setOpen(false);
    setScreen("brokers");
  };

  return (
    <div className="pop-wrap" ref={wrapRef}>
      <button
        className={`conn ${dotState}`}
        onClick={toggle}
        aria-expanded={open}
        title={connectionHealth?.detail || "Broker connections"}
      >
        <span className="d" />
        {badgeLabel}
      </button>
      <Popover open={open} className="conn-pop">
        {feedDown && (
          <>
            <div className="pop-title warn">Market Data Feed Down</div>
            <div className="pop-note">
              Accounts are logged in, but no live prices are arriving — the
              option chain and index tiles stay empty until the feed recovers.
              Charticks retries automatically.
            </div>
            {connectionHealth?.detail && (
              <div className="pop-note dim">{connectionHealth.detail}</div>
            )}
            <div className="pop-divider" />
          </>
        )}
        {any ? (
          <>
            <div className="pop-title">Connected Brokers ({connected.length})</div>
            {connected.map((a) => (
              <div className="pop-row" key={a.id}>
                <span className={`d ${healthClass(health[a.id]?.health)}`} />
                {displayName(a)}
              </div>
            ))}
            <div className="pop-divider" />
            <button className="pop-action" onClick={goManage}>
              Manage Brokers →
            </button>
          </>
        ) : (
          <>
            <div className="pop-title">No Broker Connected</div>
            <div className="pop-divider" />
            <button className="pop-action" onClick={goManage}>
              Connect Broker →
            </button>
          </>
        )}
      </Popover>
    </div>
  );
}

/** Compact Paper/Live badge beside the broker status. Clicking opens a simple
 *  dropdown; Paper→Live asks for confirmation (with optional session save),
 *  Live→Paper switches immediately into a fresh paper session. */
function TradingModeIndicator() {
  const mode = useTradingModeStore((s) => s.mode);
  const setMode = useTradingModeStore((s) => s.setMode);
  const clearPaperSession = useTradingModeStore((s) => s.clearPaperSession);
  const { open, toggle, setOpen, wrapRef } = usePopover();
  const [confirmLive, setConfirmLive] = useState(false);

  const choose = (next: "paper" | "live") => {
    setOpen(false);
    if (next === mode) return;
    if (next === "live") {
      setConfirmLive(true); // Paper → Live needs confirmation
    } else {
      // Live → Paper: immediate, fresh paper session.
      clearPaperSession();
      setMode("paper");
    }
  };

  return (
    <div className="pop-wrap" ref={wrapRef}>
      <button
        className={`mode-badge ${mode}`}
        onClick={toggle}
        aria-expanded={open}
        title="Trading mode"
      >
        {mode === "paper" ? "🟡 Paper" : "🔴 Live"} ▾
      </button>
      <Popover open={open} className="mode-pop">
        <div className="pop-title">Trading Mode</div>
        <button className="pop-action" onClick={() => choose("paper")}>
          {mode === "paper" ? "✓ " : "  "}Paper
        </button>
        <button className="pop-action" onClick={() => choose("live")}>
          {mode === "live" ? "✓ " : "  "}Live
        </button>
      </Popover>
      {confirmLive && (
        <SwitchToLiveDialog
          onConfirm={(savePaper) => {
            if (!savePaper) clearPaperSession();
            setMode("live");
            setConfirmLive(false);
          }}
          onCancel={() => setConfirmLive(false)}
        />
      )}
    </div>
  );
}

export function StatusBar() {
  const clock = useClock();
  const { theme, toggleTheme, density, setDensity } = useUiStore();
  const { connected, riskHalted } = useMarketStore();

  return (
    <header className="status">
      <div className="bridge">
        <span className={`mkt-dot ${connected ? "" : "off"}`} />
        <small>{connected ? "Bridge live" : "Connecting…"}</small>
      </div>

      <BrokerIndicator />
      <TradingModeIndicator />

      <span className="grow" />

      <div className="seg" role="group" aria-label="Density">
        {DENSITIES.map((x) => (
          <button
            key={x.d}
            className={density === x.d ? "on" : ""}
            title={x.label}
            aria-label={x.label}
            aria-pressed={density === x.d}
            onClick={() => setDensity(x.d)}
          >
            <Icon name={x.icon} size={15} />
          </button>
        ))}
      </div>

      <button className={`kill ${riskHalted ? "armed" : ""}`} title="Kill-switch">
        <span className="k" />
        {riskHalted ? "Halted" : "Kill-switch"}
      </button>

      <div className="right-stack">
        <b className="num clock-txt">{clock}</b>
        <button className="theme-btn" onClick={toggleTheme} title="Toggle theme" aria-label="Toggle theme">
          <Icon name={theme === "dark" ? "sun" : "moon"} size={14} />
        </button>
      </div>
    </header>
  );
}
