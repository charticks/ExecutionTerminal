import { Icon } from "@/components/Icon";
import { useUiStore, type Screen } from "@/stores/useUiStore";

const NAV: { screen: Screen; icon: string; label: string }[] = [
  { screen: "home", icon: "grid", label: "Home" },
  { screen: "orders", icon: "list", label: "Orders" },
  { screen: "strategies", icon: "cpu", label: "Strategies" },
  { screen: "brokers", icon: "link", label: "Brokers" },
];

export function Rail() {
  const { screen, setScreen } = useUiStore();
  return (
    <nav className="rail">
      <div className="logo">C</div>
      {NAV.map((n) => (
        <button
          key={n.screen}
          className={screen === n.screen ? "on" : ""}
          title={n.label}
          aria-label={n.label}
          onClick={() => setScreen(n.screen)}
        >
          <Icon name={n.icon} />
        </button>
      ))}
      <span className="sp" />
      <button
        className={screen === "settings" ? "on" : ""}
        title="Settings"
        aria-label="Settings"
        onClick={() => setScreen("settings")}
      >
        <Icon name="gear" />
      </button>
    </nav>
  );
}
