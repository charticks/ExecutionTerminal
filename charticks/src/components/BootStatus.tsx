import { useEffect, useState } from "react";
import { useMarketStore } from "@/stores/useMarketStore";
import { useBrokerStore } from "@/stores/useBrokerStore";
import { useLiveChain } from "@/stores/useLiveChain";

/**
 * What is still coming up, while it is still coming up.
 *
 * The Home page renders immediately now — before the engine has started, before
 * a broker has connected, before the first tick. That is the right behaviour,
 * but an interface full of dashes and empty panels is its own kind of confusing:
 * the user cannot tell "still starting" from "broken", which is precisely the
 * complaint a black window produced, moved one step later.
 *
 * So this narrates the startup instead. It is driven entirely by REAL state —
 * the bridge socket, the broker health map, the market-feed status — never by a
 * timer, so it can never claim progress that has not happened, and it removes
 * itself the moment everything it was waiting for is live.
 *
 * Deliberately a thin strip rather than a splash screen or an overlay: the app
 * is usable while this is showing, and covering a usable interface to look busy
 * would be the exact thing we were asked not to do.
 */
interface Step {
  label: string;
  done: boolean;
  /** Shown instead of the spinner when this step cannot complete on its own. */
  blocked?: string;
}

export function BootStatus() {
  const bridgeUp = useMarketStore((s) => s.connected);
  const accounts = useBrokerStore((s) => s.accounts);
  const health = useBrokerStore((s) => s.health);
  const feed = useLiveChain((s) => s.feed);
  const chainSymbol = useLiveChain((s) => s.snapshot.symbol);
  const strikes = useLiveChain((s) => s.strikes);

  const anyConnected = accounts.some((a) => health[a.id]?.health === "connected");
  const connecting = accounts.some((a) => health[a.id]?.health === "connecting");
  const noAccounts = accounts.length === 0;

  const steps: Step[] = [
    {
      label: "Starting the trading engine",
      done: bridgeUp,
    },
    {
      label: noAccounts ? "No broker configured" : "Restoring broker sessions",
      done: anyConnected || noAccounts,
      blocked: noAccounts
        ? "Add one on the Brokers page"
        : !connecting && !anyConnected
          ? "Connect one on the Brokers page"
          : undefined,
    },
    {
      label: "Connecting to market data",
      done: !!feed?.connected,
      blocked: noAccounts || (!anyConnected && !connecting)
        ? "Waiting for a broker"
        : undefined,
    },
    {
      label: "Loading the option chain",
      done: !!chainSymbol && strikes.length > 0,
      blocked: !feed?.connected ? "Waiting for market data" : undefined,
    },
  ];

  // Once everything has been ready, stay gone — a momentary blip later in the
  // session belongs to the status bar and the monitoring alarm, not to a
  // startup narration that would reappear looking like a restart.
  //
  // Seeded from the state at MOUNT, so navigating between pages after startup
  // (this is global and remounts with the app, not the page) never flashes a
  // completed strip for a moment before hiding it.
  const [phase, setPhase] = useState<"live" | "collapsing" | "gone">(
    () => (steps.every((s) => s.done) || bridgeUp ? "gone" : "live"),
  );

  const allDone = steps.every((s) => s.done);
  // Only the FIRST outstanding step explains itself. Four simultaneous
  // explanations turned a status strip into a wall of text — and only one of
  // them is ever the thing actually holding startup up.
  const currentIndex = steps.findIndex((s) => !s.done);

  // Nothing is connecting and nothing is going to: without a broker, market
  // data and the option chain will never arrive on their own. Startup is over —
  // what is left is a decision for the user, not a task in progress — so the
  // strip retires rather than sitting there permanently. The header badge and
  // the option chain's own empty state carry that message from then on.
  const stalledOnUser = !connecting && (noAccounts || !anyConnected);
  const settled = allDone || stalledOnUser;

  useEffect(() => {
    if (phase !== "live" || !settled) return;
    // Linger on the final state so it is legible rather than vanishing the
    // instant it settles — longer when the user has something to act on.
    const t = setTimeout(() => setPhase("collapsing"), allDone ? 700 : 4000);
    return () => clearTimeout(t);
  }, [settled, allDone, phase]);

  useEffect(() => {
    if (phase !== "collapsing") return;
    // Matches the CSS height transition, then unmounts so the grid row is gone
    // rather than merely zero-height.
    const t = setTimeout(() => setPhase("gone"), 260);
    return () => clearTimeout(t);
  }, [phase]);

  if (phase === "gone") return null;

  return (
    <div
      className={`boot-status ${phase === "collapsing" ? "collapsing" : ""}`}
      role="status"
      aria-live="polite"
      aria-label="Application startup status"
    >
      {steps.map((s, i) => (
        <span
          key={s.label}
          className={`boot-step ${s.done ? "done" : s.blocked ? "blocked" : "busy"}`}
          title={s.blocked ?? s.label}
        >
          <span className="boot-dot" aria-hidden="true" />
          {s.label}
          {i === currentIndex && s.blocked && (
            <span className="boot-why">&nbsp;— {s.blocked}</span>
          )}
        </span>
      ))}
    </div>
  );
}
