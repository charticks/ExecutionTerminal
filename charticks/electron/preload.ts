import { contextBridge, ipcRenderer } from "electron";

// Minimal, explicit surface exposed to the renderer. No Node APIs leak through.
export interface BridgeConfig {
  host: string;
  port: number;
  token: string;
  restUrl: string;
  wsUrl: string;
}

type BrokerId = "angel" | "kotak" | "dhan" | "icici";
interface AccountMeta {
  id: string;
  broker: BrokerId;
  nickname: string;
  autoConnect: boolean;
  /** Live orders may be routed to this account (see StoredAccount.execute). */
  execute: boolean;
}
type Credentials = Record<string, string>;

contextBridge.exposeInMainWorld("charticks", {
  getBridgeConfig: (): Promise<BridgeConfig> => ipcRenderer.invoke("bridge:config"),
  // Startup profiling. The renderer times its own phases and reports them to the
  // main process, which owns the one timeline covering all three processes —
  // Electron, this window, and the Python sidecar. See electron/startup.ts.
  startupMark: (phase: string, detail?: string): void =>
    ipcRenderer.send("startup:mark", phase, detail),
  startupTimeline: (): Promise<{ phase: string; at: number; detail?: string }[]> =>
    ipcRenderer.invoke("startup:timeline"),
  onSidecarStatus: (
    cb: (s: { alive: boolean; fatal?: boolean; reason?: string; detail?: string }) => void,
  ) => {
    const handler = (_e: unknown, s: { alive: boolean; fatal?: boolean; reason?: string; detail?: string }) => cb(s);
    ipcRenderer.on("bridge:sidecar-status", handler);
    return () => ipcRenderer.removeListener("bridge:sidecar-status", handler);
  },
  // The sidecar binds a port chosen at launch, and a restart picks a new one.
  // Without this the renderer would go on dialling the old port forever.
  onBridgeConfigChanged: (cb: () => void) => {
    const handler = () => cb();
    ipcRenderer.on("bridge:config-changed", handler);
    return () => ipcRenderer.removeListener("bridge:config-changed", handler);
  },
  // Reaching the log files from inside the app, so nobody has to be told a
  // path when something fails mid-session. See electron/logs.ts.
  diagnostics: {
    info: (): Promise<{ dir: string }> => ipcRenderer.invoke("diagnostics:info"),
    open: (): Promise<{ ok: boolean; dir: string; error?: string }> =>
      ipcRenderer.invoke("diagnostics:open"),
    saveBundle: (): Promise<{ ok: boolean; path?: string; error?: string }> =>
      ipcRenderer.invoke("diagnostics:bundle"),
  },
  // Encrypted broker credential store (main process is the only place
  // safeStorage works). Secrets never persist in the renderer.
  brokers: {
    list: (): Promise<AccountMeta[]> => ipcRenderer.invoke("brokers:list"),
    add: (payload: { broker: BrokerId; nickname: string; autoConnect: boolean; credentials: Credentials }): Promise<AccountMeta> =>
      ipcRenderer.invoke("brokers:add", payload),
    update: (id: string, patch: { nickname?: string; autoConnect?: boolean; execute?: boolean; credentials?: Credentials }): Promise<{ ok: boolean; error?: string; account?: AccountMeta }> =>
      ipcRenderer.invoke("brokers:update", id, patch),
    rename: (id: string, nickname: string): Promise<{ ok: boolean; error?: string }> =>
      ipcRenderer.invoke("brokers:rename", id, nickname),
    remove: (id: string): Promise<{ ok: boolean }> => ipcRenderer.invoke("brokers:delete", id),
    getSecrets: (id: string): Promise<Credentials | null> => ipcRenderer.invoke("brokers:getSecrets", id),
    // Opens ICICI's own login page and returns the daily session key it hands
    // back. ICICI has no TOTP, so this is the only way to obtain one.
    iciciLogin: (apiKey: string): Promise<{ ok: boolean; token?: string; error?: string }> =>
      ipcRenderer.invoke("icici:login", apiKey),
  },
});
