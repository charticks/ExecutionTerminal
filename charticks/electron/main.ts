import { app, BrowserWindow, ipcMain, safeStorage } from "electron";
import { spawn, ChildProcess } from "node:child_process";
import { join } from "node:path";
import { randomBytes, randomUUID } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";

// vite-plugin-electron injects this in dev; absent in packaged builds.
const DEV_SERVER_URL = process.env.VITE_DEV_SERVER_URL;
const isDev = !!DEV_SERVER_URL;

// Localhost bridge config. The token guards the sidecar so only this app
// can connect to its REST + WebSocket endpoints.
const SIDECAR_HOST = "127.0.0.1";
const SIDECAR_PORT = 8787;
// In dev the sidecar is started by the npm script with the default "dev" token,
// so the renderer must use the same. In packaged builds Electron spawns the
// sidecar itself and injects a fresh random token (see startSidecar()).
const BRIDGE_TOKEN = isDev ? "dev" : randomBytes(24).toString("hex");

let win: BrowserWindow | null = null;
let sidecar: ChildProcess | null = null;
let sidecarRestartTimer: NodeJS.Timeout | null = null;
let quitting = false;

/**
 * Resolve the Python sidecar's server directory in dev vs packaged builds.
 * Dev: ../sidecar relative to project root. Packaged: resources/sidecar.
 */
function sidecarDir(): string {
  if (isDev) return join(__dirname, "..", "..", "sidecar");
  return join(process.resourcesPath, "sidecar");
}

/**
 * In dev we let `npm run dev:sidecar` (uvicorn --reload) own the process, so
 * main.ts does not spawn a second one. In packaged builds we spawn it here.
 */
function startSidecar() {
  if (isDev) {
    console.log("[sidecar] dev mode — managed by npm script, not spawning");
    return;
  }
  const cwd = sidecarDir();
  const python = process.platform === "win32" ? "python" : "python3";
  const entry = join(cwd, "server.py");
  if (!existsSync(entry)) {
    console.error("[sidecar] server.py not found at", entry);
    return;
  }
  sidecar = spawn(
    python,
    ["-m", "uvicorn", "server:app", "--host", SIDECAR_HOST, "--port", String(SIDECAR_PORT)],
    {
      cwd,
      env: { ...process.env, CHARTICKS_BRIDGE_TOKEN: BRIDGE_TOKEN },
      stdio: ["ignore", "pipe", "pipe"],
    }
  );
  sidecar.stdout?.on("data", (d) => console.log("[sidecar]", d.toString().trim()));
  sidecar.stderr?.on("data", (d) => console.error("[sidecar]", d.toString().trim()));
  sidecar.on("exit", (code) => {
    console.error(`[sidecar] exited (code ${code})`);
    win?.webContents.send("bridge:sidecar-status", { alive: false });
    if (!quitting) scheduleSidecarRestart();
  });
}

function scheduleSidecarRestart() {
  if (sidecarRestartTimer) return;
  sidecarRestartTimer = setTimeout(() => {
    sidecarRestartTimer = null;
    console.log("[sidecar] restarting…");
    startSidecar();
  }, 1500);
}

function stopSidecar() {
  if (sidecar && !sidecar.killed) {
    sidecar.kill();
    sidecar = null;
  }
}

function createWindow() {
  win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 640,
    backgroundColor: "#0b0e14",
    show: false,
    webPreferences: {
      preload: join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  win.once("ready-to-show", () => win?.show());

  if (DEV_SERVER_URL) {
    win.loadURL(DEV_SERVER_URL);
    win.webContents.openDevTools({ mode: "detach" });
  } else {
    win.loadFile(join(__dirname, "..", "dist", "index.html"));
  }

  win.on("closed", () => (win = null));
}

// Expose bridge connection details to the renderer via IPC.
ipcMain.handle("bridge:config", () => ({
  host: SIDECAR_HOST,
  port: SIDECAR_PORT,
  token: BRIDGE_TOKEN,
  restUrl: `http://${SIDECAR_HOST}:${SIDECAR_PORT}`,
  wsUrl: `ws://${SIDECAR_HOST}:${SIDECAR_PORT}/stream`,
}));

// ---------------------------------------------------------------------------
// Broker credential store (main process is the only place safeStorage works).
// Secrets are encrypted at rest via the OS keystore (DPAPI on Windows). The
// renderer never receives secrets except transiently, just before a connect.
// ---------------------------------------------------------------------------
// Predefined SDK brokers; custom brokers store their own key string here.
type BrokerId = "angel" | "kotak" | "dhan";
interface StoredAccount {
  id: string;
  broker: BrokerId | string;
  nickname: string;
  autoConnect: boolean;
  secretsEnc: string; // base64 — safeStorage ciphertext, or plaintext JSON if enc unavailable
  enc: boolean; // whether secretsEnc is actually encrypted
}
interface StoreFile {
  accounts: StoredAccount[];
}

function credStorePath(): string {
  return join(app.getPath("userData"), "brokers.json");
}

function readStore(): StoreFile {
  const p = credStorePath();
  if (!existsSync(p)) return { accounts: [] };
  try {
    return JSON.parse(readFileSync(p, "utf-8")) as StoreFile;
  } catch {
    return { accounts: [] };
  }
}

function writeStore(store: StoreFile): void {
  writeFileSync(credStorePath(), JSON.stringify(store, null, 2), "utf-8");
}

function encryptSecrets(creds: Record<string, string>): { secretsEnc: string; enc: boolean } {
  const json = JSON.stringify(creds);
  if (safeStorage.isEncryptionAvailable()) {
    return { secretsEnc: safeStorage.encryptString(json).toString("base64"), enc: true };
  }
  // Fallback (e.g. Linux without a keyring): store base64 plaintext so the app
  // still works; flagged enc:false so we decode it correctly on read.
  return { secretsEnc: Buffer.from(json, "utf-8").toString("base64"), enc: false };
}

function decryptSecrets(a: StoredAccount): Record<string, string> {
  const buf = Buffer.from(a.secretsEnc, "base64");
  const json = a.enc ? safeStorage.decryptString(buf) : buf.toString("utf-8");
  try {
    return JSON.parse(json) as Record<string, string>;
  } catch {
    return {};
  }
}

const sanitize = (a: StoredAccount) => ({
  id: a.id,
  broker: a.broker,
  nickname: a.nickname,
  autoConnect: a.autoConnect,
});

/** One-time seed from the repo-root config.py so existing users keep their
 *  primary accounts without re-entering credentials. Best-effort regex parse. */
function migrateFromConfigPy(): void {
  const store = readStore();
  if (store.accounts.length > 0) return;
  const configPath = isDev
    ? join(__dirname, "..", "..", "config.py")
    : join(process.resourcesPath, "config.py");
  if (!existsSync(configPath)) return;
  let text = "";
  try {
    text = readFileSync(configPath, "utf-8");
  } catch {
    return;
  }
  const val = (key: string): string => {
    const m = text.match(new RegExp(`^\\s*${key}\\s*=\\s*["']([^"']*)["']`, "m"));
    return m ? m[1] : "";
  };
  const seeded: StoredAccount[] = [];
  const add = (broker: BrokerId, creds: Record<string, string>, required: string[]) => {
    if (!required.every((k) => creds[k])) return;
    const { secretsEnc, enc } = encryptSecrets(creds);
    seeded.push({ id: randomUUID(), broker, nickname: "Primary", autoConnect: false, secretsEnc, enc });
  };
  add("angel", { apiKey: val("API_KEY"), clientId: val("CLIENT_ID"), pin: val("PIN"), totpSecret: val("TOTP_SECRET") }, ["apiKey", "clientId"]);
  add("kotak", { consumerKey: val("KOTAK_CONSUMER_KEY"), mobile: val("KOTAK_MOBILE_NO"), ucc: val("KOTAK_UCC"), mpin: val("KOTAK_MPIN"), totpSecret: val("KOTAK_TOTP_SECRET") }, ["consumerKey", "ucc"]);
  add("dhan", { clientId: val("DHAN_CLIENT_ID"), accessToken: val("DHAN_ACCESS_TOKEN") }, ["clientId"]);
  if (seeded.length) writeStore({ accounts: seeded });
}

ipcMain.handle("brokers:list", () => readStore().accounts.map(sanitize));

ipcMain.handle("brokers:add", (_e, payload: { broker: BrokerId; nickname: string; autoConnect: boolean; credentials: Record<string, string> }) => {
  const store = readStore();
  const { secretsEnc, enc } = encryptSecrets(payload.credentials || {});
  const acct: StoredAccount = {
    id: randomUUID(),
    broker: payload.broker,
    nickname: payload.nickname || "",
    autoConnect: !!payload.autoConnect,
    secretsEnc,
    enc,
  };
  store.accounts.push(acct);
  writeStore(store);
  return sanitize(acct);
});

ipcMain.handle("brokers:update", (_e, id: string, patch: { nickname?: string; autoConnect?: boolean; credentials?: Record<string, string> }) => {
  const store = readStore();
  const acct = store.accounts.find((a) => a.id === id);
  if (!acct) return { ok: false, error: "account not found" };
  if (patch.nickname !== undefined) acct.nickname = patch.nickname;
  if (patch.autoConnect !== undefined) acct.autoConnect = patch.autoConnect;
  if (patch.credentials) {
    const { secretsEnc, enc } = encryptSecrets(patch.credentials);
    acct.secretsEnc = secretsEnc;
    acct.enc = enc;
  }
  writeStore(store);
  return { ok: true, account: sanitize(acct) };
});

ipcMain.handle("brokers:rename", (_e, id: string, nickname: string) => {
  const store = readStore();
  const acct = store.accounts.find((a) => a.id === id);
  if (!acct) return { ok: false, error: "account not found" };
  acct.nickname = nickname;
  writeStore(store);
  return { ok: true };
});

ipcMain.handle("brokers:delete", (_e, id: string) => {
  const store = readStore();
  store.accounts = store.accounts.filter((a) => a.id !== id);
  writeStore(store);
  return { ok: true };
});

// Returns decrypted credentials for a connect. Renderer forwards these to the
// localhost sidecar (bearer-guarded) and does not persist them.
ipcMain.handle("brokers:getSecrets", (_e, id: string) => {
  const acct = readStore().accounts.find((a) => a.id === id);
  if (!acct) return null;
  return decryptSecrets(acct);
});

app.whenReady().then(() => {
  migrateFromConfigPy();
  startSidecar();
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("before-quit", () => {
  quitting = true;
  stopSidecar();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
