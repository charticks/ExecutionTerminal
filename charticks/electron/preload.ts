import { contextBridge, ipcRenderer } from "electron";

// Minimal, explicit surface exposed to the renderer. No Node APIs leak through.
export interface BridgeConfig {
  host: string;
  port: number;
  token: string;
  restUrl: string;
  wsUrl: string;
}

type BrokerId = "angel" | "kotak" | "dhan";
interface AccountMeta {
  id: string;
  broker: BrokerId;
  nickname: string;
  autoConnect: boolean;
}
type Credentials = Record<string, string>;

contextBridge.exposeInMainWorld("charticks", {
  getBridgeConfig: (): Promise<BridgeConfig> => ipcRenderer.invoke("bridge:config"),
  onSidecarStatus: (cb: (s: { alive: boolean }) => void) => {
    const handler = (_e: unknown, s: { alive: boolean }) => cb(s);
    ipcRenderer.on("bridge:sidecar-status", handler);
    return () => ipcRenderer.removeListener("bridge:sidecar-status", handler);
  },
  // Encrypted broker credential store (main process is the only place
  // safeStorage works). Secrets never persist in the renderer.
  brokers: {
    list: (): Promise<AccountMeta[]> => ipcRenderer.invoke("brokers:list"),
    add: (payload: { broker: BrokerId; nickname: string; autoConnect: boolean; credentials: Credentials }): Promise<AccountMeta> =>
      ipcRenderer.invoke("brokers:add", payload),
    update: (id: string, patch: { nickname?: string; autoConnect?: boolean; credentials?: Credentials }): Promise<{ ok: boolean; error?: string; account?: AccountMeta }> =>
      ipcRenderer.invoke("brokers:update", id, patch),
    rename: (id: string, nickname: string): Promise<{ ok: boolean; error?: string }> =>
      ipcRenderer.invoke("brokers:rename", id, nickname),
    remove: (id: string): Promise<{ ok: boolean }> => ipcRenderer.invoke("brokers:delete", id),
    getSecrets: (id: string): Promise<Credentials | null> => ipcRenderer.invoke("brokers:getSecrets", id),
  },
});
