import { create } from "zustand";
import { bridge } from "@/bridge/client";
import { credentials } from "@/bridge/credentials";
import type { BridgeEvent } from "@/bridge/events";
import type { AccountHealth, BrokerAccount, Credentials } from "@/bridge/brokers";
import { isPredefinedBroker } from "@/bridge/brokers";
import type { ConnectionHealthState } from "@/bridge/events";

interface BrokerStatusResponse {
  status: Record<string, { health: AccountHealth["health"]; detail?: string | null; broker?: string }>;
}

// Aggregate connectivity state driving the header badge — published by the
// sidecar's reliability layer (ConnectionHealthMonitor), distinct from the
// per-account `health` map above.
export interface ConnectionHealth {
  state: ConnectionHealthState;
  accountsConnected: number;
  detail?: string | null;
}

interface BrokerState {
  accounts: BrokerAccount[];
  health: Record<string, AccountHealth>;
  connectionHealth: ConnectionHealth | null;
  loaded: boolean;

  loadAccounts: () => Promise<void>;
  addAccount: (p: { broker: string; nickname: string; autoConnect: boolean; credentials: Credentials }) => Promise<void>;
  updateAccount: (id: string, patch: { nickname?: string; autoConnect?: boolean; execute?: boolean; credentials?: Credentials }) => Promise<void>;
  renameAccount: (id: string, nickname: string) => Promise<void>;
  deleteAccount: (id: string) => Promise<void>;

  /** Opt an account in/out of live order execution. Persisted with the broker
   *  config and pushed to the sidecar, which is authoritative at order time. */
  setExecute: (id: string, execute: boolean) => Promise<void>;

  connect: (ids: string[]) => Promise<void>;
  disconnect: (ids: string[]) => Promise<void>;

  refreshHealth: () => Promise<void>;
  ingest: (e: BridgeEvent) => void;
}

export const useBrokerStore = create<BrokerState>((set, get) => ({
  accounts: [],
  health: {},
  connectionHealth: null,
  loaded: false,

  loadAccounts: async () => {
    const accounts = await credentials.list();
    set({ accounts, loaded: true });
    // The sidecar starts with an empty execution set and refuses live orders
    // until told; push the persisted selection as soon as we know it.
    pushExecutionSet(accounts);
    // Auto-connect flagged accounts that aren't already up.
    const health = get().health;
    const toConnect = accounts
      .filter((a) => a.autoConnect && health[a.id]?.health !== "connected")
      .map((a) => a.id);
    if (toConnect.length) get().connect(toConnect);
  },

  addAccount: async (p) => {
    await credentials.add(p);
    await get().loadAccounts();
  },

  updateAccount: async (id, patch) => {
    await credentials.update(id, patch);
    await get().loadAccounts();
  },

  renameAccount: async (id, nickname) => {
    await credentials.rename(id, nickname);
    await get().loadAccounts();
  },

  setExecute: async (id, execute) => {
    await credentials.update(id, { execute });
    // loadAccounts re-reads the persisted config and re-pushes the set, so the
    // sidecar can never diverge from what the Brokers page shows.
    await get().loadAccounts();
  },

  deleteAccount: async (id) => {
    // Disconnect first so we don't orphan a live session.
    if (get().health[id]?.health === "connected") {
      await get().disconnect([id]);
    }
    await credentials.remove(id);
    set((s) => {
      const health = { ...s.health };
      delete health[id];
      return { health };
    });
    await get().loadAccounts();
  },

  connect: async (ids) => {
    const accounts = get().accounts;
    await Promise.all(
      ids.map(async (id) => {
        const acct = accounts.find((a) => a.id === id);
        if (!acct) return;
        if (get().health[id]?.health === "connected") return; // ignore already-connected
        // Custom brokers have no backend SDK integration yet — surface that
        // cleanly instead of POSTing an unknown broker the sidecar will reject.
        if (!isPredefinedBroker(acct.broker)) {
          set((s) => ({
            health: {
              ...s.health,
              [id]: { health: "down", detail: "Live integration not available for custom brokers yet." },
            },
          }));
          return;
        }
        set((s) => ({ health: { ...s.health, [id]: { health: "connecting" } } }));
        try {
          const creds = await credentials.getSecrets(id);
          const res = await bridge.post<{ ok: boolean; error?: string }>("/brokers/connect", {
            accountId: id,
            broker: acct.broker,
            credentials: creds ?? {},
          });
          if (!res.ok) {
            set((s) => ({ health: { ...s.health, [id]: { health: "session_expired", detail: res.error } } }));
          }
        } catch (e) {
          set((s) => ({ health: { ...s.health, [id]: { health: "down", detail: String(e) } } }));
        }
      }),
    );
    get().refreshHealth();
  },

  disconnect: async (ids) => {
    await Promise.all(
      ids.map(async (id) => {
        if (get().health[id]?.health === "down" || !get().health[id]) return; // ignore already-off
        try {
          await bridge.post("/brokers/disconnect", { accountId: id });
        } catch {
          /* status reconciles via refresh/events */
        }
      }),
    );
    get().refreshHealth();
  },

  refreshHealth: async () => {
    try {
      const res = await bridge.get<BrokerStatusResponse>("/brokers");
      set((s) => {
        const health = { ...s.health };
        for (const [id, v] of Object.entries(res.status)) {
          health[id] = { health: v.health, detail: v.detail };
        }
        return { health };
      });
    } catch {
      /* sidecar not up yet — live events will fill in */
    }
  },

  ingest: (e) => {
    if (e.type === "connection_health") {
      set({ connectionHealth: { state: e.state, accountsConnected: e.accountsConnected, detail: e.detail } });
      return;
    }
    if (e.type !== "broker_status" || !e.account) return;
    set((s) => ({
      health: { ...s.health, [e.account as string]: { health: e.health, detail: e.detail } },
    }));
  },
}));

/** Tell the sidecar which accounts may receive live orders. Best-effort by
 *  necessity (the sidecar may be down), but never silently wrong: the sidecar
 *  defaults to an EMPTY set, so a dropped push means live orders are refused
 *  with a clear message rather than routed somewhere unintended. Every
 *  reconnect re-pushes, so the window is a moment, not a session. */
function pushExecutionSet(accounts: BrokerAccount[]) {
  const accountIds = accounts.filter((a) => a.execute).map((a) => a.id);
  bridge.post("/brokers/execute", { accountIds }).catch(() => {});
}

// Wire live broker_status events + initial load once, at module load.
let wired = false;
export function connectBrokerStore() {
  if (wired) return;
  wired = true;
  const store = useBrokerStore.getState();
  bridge.on(store.ingest);
  bridge.onStatus((connected) => {
    if (!connected) return;
    store.refreshHealth();
    // A sidecar restart wipes its in-memory execution set. Re-push from the
    // persisted config so live routing is restored without user action.
    pushExecutionSet(useBrokerStore.getState().accounts);
  });
  store.loadAccounts();
  store.refreshHealth();
}
