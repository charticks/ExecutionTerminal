import { app, BrowserWindow, dialog, ipcMain, safeStorage, session } from "electron";
import { spawn, ChildProcess } from "node:child_process";
import { join } from "node:path";
import { randomBytes, randomUUID } from "node:crypto";
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";

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
 * The Python that runs the trading engine.
 *
 * Packaged builds ship their own interpreter at `resources/python` (assembled
 * by scripts/build-runtime.mjs), so nothing needs installing on the user's
 * machine. Falling back to a PATH `python` keeps a runtime-less build working
 * for anyone who has Python already — and keeps the missing-Python dialog
 * meaningful rather than the app failing silently.
 */
function pythonExecutable(): string {
  if (!isDev) {
    const bundled = join(process.resourcesPath, "python",
                         process.platform === "win32" ? "python.exe" : "bin/python3");
    if (existsSync(bundled)) return bundled;
  }
  return process.platform === "win32" ? "python" : "python3";
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
  const python = pythonExecutable();
  const entry = join(cwd, "server.py");
  if (!existsSync(entry)) {
    console.error("[sidecar] server.py not found at", entry);
    return;
  }
  sidecar = spawn(
    python,
    // --app-dir, not cwd: the bundled embeddable Python runs in isolated mode
    // (its pythonNNN._pth fully determines sys.path), so the working directory
    // is NOT importable and PYTHONPATH is ignored. Without this, `server:app`
    // fails to resolve on a packaged build even though it works in dev.
    ["-m", "uvicorn", "server:app", "--app-dir", cwd,
     "--host", SIDECAR_HOST, "--port", String(SIDECAR_PORT)],
    {
      cwd,
      env: {
        ...process.env,
        CHARTICKS_BRIDGE_TOKEN: BRIDGE_TOKEN,
        // Instrument / scrip masters are ~100 MB a day and must not be written
        // into the install directory, which may be read-only and is wiped on
        // reinstall. See sidecar/services/paths.py.
        CHARTICKS_DATA_DIR: app.getPath("userData"),
        CHARTICKS_LOG_DIR: app.getPath("userData"),
        // Broker SDKs print ₹ and other non-ASCII; without this Python's
        // Windows console encoding raises UnicodeEncodeError mid-write and
        // takes the log line (or the handler) down with it.
        PYTHONIOENCODING: "utf-8",
      },
      stdio: ["ignore", "pipe", "pipe"],
    }
  );
  // Anything the sidecar prints before its own logging is up — an import error,
  // a missing dependency, a uvicorn bind failure — only exists here. In a
  // packaged build console.log goes nowhere, so mirror it to a file.
  sidecar.stdout?.on("data", (d) => writeSidecarOutput("out", d.toString()));
  sidecar.stderr?.on("data", (d) => writeSidecarOutput("err", d.toString()));
  // ENOENT (Python not installed / not on PATH) arrives here, NOT as an 'exit'.
  // Without this listener Node throws on the unhandled 'error' event, the app
  // sits on "Connecting…" forever, and nothing is written anywhere — which is
  // precisely the failure a machine without Python produces.
  sidecar.on("error", (err: NodeJS.ErrnoException) => {
    const missing = err.code === "ENOENT";
    writeSidecarOutput("err",
      missing
        ? `could not start the trading engine: '${python}' was not found on PATH. ` +
          `Python is required and is not bundled with this build.`
        : `could not start the trading engine: ${err.message}`);
    win?.webContents.send("bridge:sidecar-status", { alive: false });
    if (missing) reportMissingPython();
  });
  sidecar.on("exit", (code) => {
    writeSidecarOutput("err", `sidecar exited (code ${code})`);
    win?.webContents.send("bridge:sidecar-status", { alive: false });
    if (!quitting) scheduleSidecarRestart();
  });
}

/**
 * Tell the user, once, that the Python runtime is missing — the one failure
 * they can actually fix themselves. Retrying silently would leave the app
 * looking merely slow rather than misconfigured.
 */
let reportedMissingPython = false;
function reportMissingPython() {
  if (reportedMissingPython) return;
  reportedMissingPython = true;
  quitting = true; // stop the restart loop; retrying will not find Python either
  dialog.showErrorBox(
    "Charticks cannot start its trading engine",
    "Charticks needs Python to run its trading engine, and it was not found " +
      "on this PC.\n\n" +
      "1. Install Python 3.12 from python.org\n" +
      '2. On the first installer screen, tick "Add python.exe to PATH"\n' +
      "3. Run setup-tester.bat from the Charticks folder\n" +
      "4. Start Charticks again\n\n" +
      "Details were written to logs\\sidecar-process.log in:\n" +
      app.getPath("userData"),
  );
}

/**
 * Append sidecar stdout/stderr to logs/sidecar-process.log, next to the log
 * files the sidecar writes itself. This is the only record of a sidecar that
 * dies before it can start its own logging — previously that failure produced
 * nothing at all in a packaged build.
 */
function writeSidecarOutput(stream: "out" | "err", chunk: string) {
  const text = chunk.trimEnd();
  if (!text) return;
  if (stream === "err") console.error("[sidecar]", text);
  else console.log("[sidecar]", text);
  try {
    const dir = join(app.getPath("userData"), "logs");
    mkdirSync(dir, { recursive: true });
    const stamp = new Date().toISOString();
    const line = text.split("\n").map((l) => `${stamp} | ${stream} | ${l}`).join("\n");
    appendFileSync(join(dir, "sidecar-process.log"), line + "\n", "utf-8");
  } catch {
    // Logging must never take the app down; the console copy above survives.
  }
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
type BrokerId = "angel" | "kotak" | "dhan" | "icici";
interface StoredAccount {
  id: string;
  broker: BrokerId | string;
  nickname: string;
  autoConnect: boolean;
  secretsEnc: string; // base64 — safeStorage ciphertext, or plaintext JSON if enc unavailable
  enc: boolean; // whether secretsEnc is actually encrypted
  // Whether LIVE orders may be routed to this account. Deliberately separate
  // from autoConnect: connecting a broker gets you market data and account
  // services, executing on it is an explicit second opt-in. Optional so a store
  // written before this field reads back as "no execution" rather than
  // inheriting the old route-to-every-connected-broker behaviour.
  execute?: boolean;
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
  execute: !!a.execute,
});

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

ipcMain.handle("brokers:update", (_e, id: string, patch: { nickname?: string; autoConnect?: boolean; execute?: boolean; credentials?: Record<string, string> }) => {
  const store = readStore();
  const acct = store.accounts.find((a) => a.id === id);
  if (!acct) return { ok: false, error: "account not found" };
  if (patch.nickname !== undefined) acct.nickname = patch.nickname;
  if (patch.autoConnect !== undefined) acct.autoConnect = patch.autoConnect;
  if (patch.execute !== undefined) acct.execute = patch.execute;
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

// ── ICICI Direct daily login popup ──────────────────────────────────────
// ICICI will not let an app authenticate on its own: the user logs in through
// ICICI's own page and ICICI hands back a session key that dies overnight.
// Rather than making the user copy/paste it every morning, the real login page
// is opened in a modal child window and the key is captured as it comes back.
//
// It comes back in one of two shapes depending on the app's registered
// redirect URL, so BOTH are watched: as a query parameter on a navigation, and
// as a form POST body. The redirect target itself is never loaded (nothing
// listens on it) — the request is cancelled the moment the key is read.
const ICICI_LOGIN_PARTITION = "persist:icici-login";
const ICICI_LOGIN_TIMEOUT_MS = 10 * 60 * 1000; // OTP entry can be slow
let iciciLoginWindow: BrowserWindow | null = null;

function findSessionParam(raw: string): string | null {
  // Accept apisession / API_Session / api_session in any casing.
  try {
    const url = new URL(raw);
    for (const [k, v] of url.searchParams.entries()) {
      if (k.toLowerCase().replace(/_/g, "") === "apisession" && v) return v;
    }
  } catch {
    /* not a parseable URL — ignore */
  }
  return null;
}

function findSessionInBody(body: string): string | null {
  try {
    const params = new URLSearchParams(body);
    for (const [k, v] of params.entries()) {
      if (k.toLowerCase().replace(/_/g, "") === "apisession" && v) return v;
    }
  } catch {
    /* not form-encoded — ignore */
  }
  return null;
}

ipcMain.handle("icici:login", async (_e, apiKey: string): Promise<{ ok: boolean; token?: string; error?: string }> => {
  if (!apiKey || !apiKey.trim()) {
    return { ok: false, error: "Enter your ICICI API Key first." };
  }
  if (iciciLoginWindow && !iciciLoginWindow.isDestroyed()) {
    iciciLoginWindow.focus();
    return { ok: false, error: "An ICICI login window is already open." };
  }

  const popup = new BrowserWindow({
    parent: win ?? undefined,
    modal: true,
    width: 520,
    height: 760,
    autoHideMenuBar: true,
    title: "Log in to ICICI Direct",
    webPreferences: {
      partition: ICICI_LOGIN_PARTITION,
      nodeIntegration: false,
      contextIsolation: true,
      // No preload: this window renders a third-party page and must never
      // reach any Charticks API.
    },
  });
  iciciLoginWindow = popup;

  const ses = session.fromPartition(ICICI_LOGIN_PARTITION);

  return await new Promise((resolve) => {
    let settled = false;
    const finish = (result: { ok: boolean; token?: string; error?: string }) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      ses.webRequest.onBeforeRequest(null);
      if (!popup.isDestroyed()) popup.close();
      resolve(result);
    };

    const timer = setTimeout(
      () => finish({ ok: false, error: "Login timed out. Please try again." }),
      ICICI_LOGIN_TIMEOUT_MS
    );

    // (a) token as a query parameter on any navigation/redirect
    const onNavigate = (_ev: unknown, url: string) => {
      const token = findSessionParam(url);
      if (token) finish({ ok: true, token });
    };
    popup.webContents.on("will-redirect", onNavigate);
    popup.webContents.on("will-navigate", onNavigate);
    popup.webContents.on("did-navigate", onNavigate);

    // (b) token in a form POST body — cancel the request so the popup never
    // tries to load the (dead) redirect target and flash a connection error.
    ses.webRequest.onBeforeRequest({ urls: ["*://*/*"] }, (details, callback) => {
      if (details.method === "POST" && details.uploadData?.length) {
        const body = details.uploadData
          .map((part) => (part.bytes ? Buffer.from(part.bytes).toString("utf-8") : ""))
          .join("");
        const token = findSessionInBody(body);
        if (token) {
          callback({ cancel: true });
          finish({ ok: true, token });
          return;
        }
      }
      callback({});
    });

    popup.on("closed", () => {
      iciciLoginWindow = null;
      finish({ ok: false, error: "Login window closed before a session key was captured." });
    });

    popup.loadURL(
      `https://api.icicidirect.com/apiuser/login?api_key=${encodeURIComponent(apiKey.trim())}`
    ).catch((err: Error) => finish({ ok: false, error: `Could not open ICICI login: ${err.message}` }));
  });
});

app.whenReady().then(() => {
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
