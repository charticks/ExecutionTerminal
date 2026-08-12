// Renderer-side facade over the broker credential store. In Electron the store
// lives in the main process (encrypted via safeStorage); in a plain browser
// (vite dev without Electron) it falls back to localStorage so the UI still
// works — cleartext, dev-only.
import type { BrokerAccount, Credentials } from "./brokers";

interface BrokersApi {
  list(): Promise<BrokerAccount[]>;
  add(p: { broker: string; nickname: string; autoConnect: boolean; credentials: Credentials }): Promise<BrokerAccount>;
  update(id: string, patch: { nickname?: string; autoConnect?: boolean; execute?: boolean; credentials?: Credentials }): Promise<{ ok: boolean; error?: string }>;
  rename(id: string, nickname: string): Promise<{ ok: boolean; error?: string }>;
  remove(id: string): Promise<{ ok: boolean }>;
  getSecrets(id: string): Promise<Credentials | null>;
  iciciLogin?(apiKey: string): Promise<{ ok: boolean; token?: string; error?: string }>;
}

/** Opens ICICI's login page (Electron only) and returns the daily session key.
 *  Unavailable in the dev-browser fallback — there is no window to open. */
export async function iciciLogin(apiKey: string): Promise<{ ok: boolean; token?: string; error?: string }> {
  const api = electronApi();
  if (!api?.iciciLogin) {
    return { ok: false, error: "ICICI login is only available in the desktop app." };
  }
  return api.iciciLogin(apiKey);
}

function electronApi(): BrokersApi | null {
  const api = (window as unknown as { charticks?: { brokers?: BrokersApi } }).charticks;
  return api?.brokers ?? null;
}

// ── localStorage fallback (dev browser only) ──────────────────────────────
const LS_KEY = "ck.brokers";
interface LsRecord extends BrokerAccount {
  credentials: Credentials;
}
function lsRead(): LsRecord[] {
  try {
    return JSON.parse(localStorage.getItem(LS_KEY) || "[]") as LsRecord[];
  } catch {
    return [];
  }
}
function lsWrite(rows: LsRecord[]) {
  localStorage.setItem(LS_KEY, JSON.stringify(rows));
}
const meta = ({ id, broker, nickname, autoConnect, execute }: LsRecord): BrokerAccount =>
  ({ id, broker, nickname, autoConnect, execute: !!execute });

const lsApi: BrokersApi = {
  async list() {
    return lsRead().map(meta);
  },
  async add(p) {
    const rows = lsRead();
    // New accounts never execute until explicitly opted in on the Brokers page.
    const rec: LsRecord = { id: crypto.randomUUID(), broker: p.broker, nickname: p.nickname, autoConnect: p.autoConnect, execute: false, credentials: p.credentials };
    rows.push(rec);
    lsWrite(rows);
    return meta(rec);
  },
  async update(id, patch) {
    const rows = lsRead();
    const r = rows.find((x) => x.id === id);
    if (!r) return { ok: false, error: "not found" };
    if (patch.nickname !== undefined) r.nickname = patch.nickname;
    if (patch.autoConnect !== undefined) r.autoConnect = patch.autoConnect;
    if (patch.execute !== undefined) r.execute = patch.execute;
    if (patch.credentials) r.credentials = patch.credentials;
    lsWrite(rows);
    return { ok: true };
  },
  async rename(id, nickname) {
    return this.update(id, { nickname });
  },
  async remove(id) {
    lsWrite(lsRead().filter((x) => x.id !== id));
    return { ok: true };
  },
  async getSecrets(id) {
    return lsRead().find((x) => x.id === id)?.credentials ?? null;
  },
};

export const credentials: BrokersApi = {
  list: () => (electronApi() ?? lsApi).list(),
  add: (p) => (electronApi() ?? lsApi).add(p),
  update: (id, patch) => (electronApi() ?? lsApi).update(id, patch),
  rename: (id, nickname) => (electronApi() ?? lsApi).rename(id, nickname),
  remove: (id) => (electronApi() ?? lsApi).remove(id),
  getSecrets: (id) => (electronApi() ?? lsApi).getSecrets(id),
};
