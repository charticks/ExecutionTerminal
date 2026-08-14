import { app, BrowserWindow, dialog, ipcMain, safeStorage, session } from "electron";
import { spawn, ChildProcess } from "node:child_process";
import { createServer } from "node:net";
import { join } from "node:path";
import { randomBytes, randomUUID } from "node:crypto";
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { adopt, beginTrace, flush as flushStartup, mark, timeline } from "./startup";

// vite-plugin-electron injects this in dev; absent in packaged builds.
const DEV_SERVER_URL = process.env.VITE_DEV_SERVER_URL;
const isDev = !!DEV_SERVER_URL;

/**
 * Anything that kills the main process, written down.
 *
 * Electron on Windows is a GUI-subsystem binary: its stdout and stderr go
 * nowhere the user (or a support engineer) can see. An exception thrown out of
 * main.ts therefore produced a window that never appeared and not one byte of
 * explanation anywhere on disk — indistinguishable, from the outside, from the
 * app simply being slow to start. Installed before anything else can throw.
 */
function fatal(kind: string, err: unknown): void {
  const text = err instanceof Error ? (err.stack ?? err.message) : String(err);
  try {
    const dir = join(app.getPath("userData"), "logs");
    mkdirSync(dir, { recursive: true });
    appendFileSync(join(dir, "main-process.log"),
      `${new Date().toISOString()} | ${kind} | ${text}\n`, "utf-8");
  } catch {
    /* nothing left to try */
  }
  console.error(`[main] ${kind}:`, text);
}

process.on("uncaughtException", (err) => fatal("uncaughtException", err));
process.on("unhandledRejection", (err) => fatal("unhandledRejection", err));

// Localhost bridge config. The token guards the sidecar so only this app
// can connect to its REST + WebSocket endpoints.
const SIDECAR_HOST = "127.0.0.1";

/**
 * The port the sidecar listens on.
 *
 * This used to be hard-coded to 8787, and that single constant was capable of
 * making the application permanently unusable with no explanation whatsoever:
 *
 *   * If ANYTHING already held 8787 — a `npm run dev:sidecar` left running, an
 *     orphaned sidecar from a previous crash, a second copy of Charticks, an
 *     unrelated program — uvicorn failed to bind with `[Errno 10048]` and the
 *     sidecar process exited. `sidecar.on("exit")` then scheduled a restart
 *     1500 ms later, which failed the same way, forever. Each attempt paid the
 *     full Python start-up cost, so the machine also stayed busy.
 *   * Worse, the renderer went on talking to 127.0.0.1:8787 — i.e. to the
 *     FOREIGN process — whose bearer token is not ours. Every REST call came
 *     back 401 and the WebSocket was closed with 4401, so the reconnect backoff
 *     ran to its 10 s ceiling and never succeeded. The UI sat at "Connecting…"
 *     indefinitely while the real engine was never reachable.
 *
 * Measured on this machine: a stale `uvicorn --reload` from a development
 * session held 8787 for seven hours, and the packaged sidecar exited with
 * errno 10048 on every single attempt.
 *
 * A dynamically chosen free port removes the entire class of failure. 0 means
 * "not chosen yet"; `chooseSidecarPort()` fills it in before the sidecar spawns
 * and before the renderer can ask for the bridge config.
 */
let SIDECAR_PORT = 0;

/**
 * Ask the OS for a free port by binding to 0 and reading back what we got.
 *
 * Deliberately not "try 8787, then 8788, …": that races with anything else
 * doing the same, and the OS already solves this correctly. The listener is
 * closed before the port is handed to the sidecar, which leaves a small window
 * where something else could take it — hence `bindRetries` in the caller, which
 * simply asks for another one.
 */
function findFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.once("error", reject);
    srv.listen(0, SIDECAR_HOST, () => {
      const addr = srv.address();
      const port = typeof addr === "object" && addr ? addr.port : 0;
      srv.close(() => (port ? resolve(port) : reject(new Error("no port"))));
    });
  });
}
// In dev the sidecar is started by the npm script with the default "dev" token,
// so the renderer must use the same. In packaged builds Electron spawns the
// sidecar itself and injects a fresh random token (see startSidecar()).
const BRIDGE_TOKEN = isDev ? "dev" : randomBytes(24).toString("hex");

let win: BrowserWindow | null = null;
let sidecar: ChildProcess | null = null;
let sidecarRestartTimer: NodeJS.Timeout | null = null;
let quitting = false;
// Set once the user has answered the "positions are being managed" prompt, so
// the re-issued window close is not intercepted a second time.
let closeConfirmed = false;
// When the sidecar process was spawned, on this process's clock. The sidecar
// times its own phases from its own start; this is the offset that puts them
// on one timeline with everything else.
let sidecarSpawnedAt = 0;

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
/**
 * Choose the port and bring the sidecar up. Must complete before the renderer
 * asks for the bridge config, which is why `bridge:config` awaits it.
 */
async function startSidecarWithPort(): Promise<void> {
  if (isDev) {
    // In dev the sidecar is started by `npm run dev:sidecar` on the fixed port
    // with the fixed "dev" token, so the renderer has to use the same.
    SIDECAR_PORT = 8787;
    mark("sidecar:dev-mode", "managed by npm script on 8787");
    console.log("[sidecar] dev mode — managed by npm script, not spawning");
    return;
  }
  try {
    SIDECAR_PORT = await findFreePort();
    mark("sidecar:port-chosen", String(SIDECAR_PORT));
  } catch (err) {
    // Falling back to the historic fixed port is better than not starting at
    // all, and the collision handling below now reports it properly.
    SIDECAR_PORT = 8787;
    fatal("port-selection", err);
  }
  markPortReady();
  startSidecar();
}

function startSidecar() {
  if (isDev) {
    console.log("[sidecar] dev mode — managed by npm script, not spawning");
    return;
  }
  const cwd = sidecarDir();
  const python = pythonExecutable();
  const entry = join(cwd, "server.py");
  const bundled = python !== "python" && python !== "python3";
  mark("sidecar:resolve", `python=${bundled ? "bundled" : "PATH"} dir=${cwd}`);
  if (!existsSync(entry)) {
    // A build whose sidecar payload does not match what this main process
    // expects. It happened for real: an installer predating the Python runtime
    // shipped a frozen `charticks-sidecar.exe` instead of `server.py`, so this
    // returned silently and the engine simply never started — with the UI
    // showing nothing but "Connecting…".
    mark("sidecar:missing", entry);
    fatal("sidecar-missing",
      new Error(`server.py not found at ${entry}. This installation's engine `
        + `payload does not match the application — reinstall Charticks.`));
    dialog.showErrorBox(
      "Charticks is not installed correctly",
      `The trading engine could not be found at:\n${entry}\n\n`
      + `This usually means the installation is from an older version whose `
      + `engine was packaged differently. Please reinstall Charticks using the `
      + `latest installer.`);
    return;
  }
  sidecarSpawnedAt = mark("sidecar:spawn");
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
        // Deliberately NOT setting PYTHONPYCACHEPREFIX. It looked like the right
        // answer for a read-only install directory, but setting it makes Python
        // ignore in-tree `__pycache__` entirely — and the runtime now SHIPS
        // pre-compiled (scripts/build-runtime.mjs), so redirecting the cache
        // throws away the very thing that makes startup fast. Measured on the
        // shipped runtime: 1.94s with in-tree bytecode, 2.87s with the prefix.
      },
      stdio: ["ignore", "pipe", "pipe"],
    }
  );
  // Anything the sidecar prints before its own logging is up — an import error,
  // a missing dependency, a uvicorn bind failure — only exists here. In a
  // packaged build console.log goes nowhere, so mirror it to a file.
  sidecar.stdout?.on("data", (d) => writeSidecarOutput("out", d.toString()));
  sidecar.stderr?.on("data", (d) => writeSidecarOutput("err", d.toString()));
  // Poll until the engine actually answers. This is the number that was missing
  // from every previous investigation: how long the app spends waiting for an
  // engine that may be starting slowly, failing to bind, or not there at all.
  void waitForSidecar();
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
    if (quitting) return;
    // A sidecar that exits without ever having served a request has failed to
    // START, which is a different thing from one that died mid-session — and
    // retrying it forever, every 1500 ms, is exactly what turned a port
    // collision into a permanently unusable application that never said why.
    sidecarFailures += 1;
    if (sidecarFailures >= SIDECAR_MAX_FAILURES) {
      reportSidecarUnstartable();
      return;
    }
    scheduleSidecarRestart();
  });
}

/**
 * Poll /health until the engine answers, marking the moment it does.
 *
 * Purely observational — nothing waits on this. It exists so the startup trace
 * distinguishes "the engine took 8 seconds" from "the engine never came up",
 * which from the outside look identical: an interface that is drawn but empty.
 */
async function waitForSidecar(): Promise<void> {
  const startedAt = Date.now();
  const deadline = startedAt + 60_000;
  const port = SIDECAR_PORT;
  while (Date.now() < deadline) {
    if (quitting || port !== SIDECAR_PORT) return; // restarted on a new port
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 900);
      const res = await fetch(`http://${SIDECAR_HOST}:${port}/health`,
                              { signal: controller.signal });
      clearTimeout(timer);
      if (res.ok) {
        mark("sidecar:health-ok", `${Date.now() - startedAt}ms after spawn, port ${port}`);
        sidecarFailures = 0;   // it started; a later exit is a crash, not a failure to start
        win?.webContents.send("bridge:sidecar-status", { alive: true });
        return;
      }
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 150));
  }
  mark("sidecar:health-timeout", `no response within 60s on port ${port}`);
}

// How many consecutive failed starts before we stop and tell the user. Low,
// because the failure modes that get here (port taken, missing interpreter,
// broken install) do not clear on their own, and each attempt costs seconds.
const SIDECAR_MAX_FAILURES = 4;
let sidecarFailures = 0;
let reportedUnstartable = false;

/** The last thing the sidecar printed, so the reason reaches the user. */
let lastSidecarError = "";

function reportSidecarUnstartable() {
  if (reportedUnstartable) return;
  reportedUnstartable = true;
  const portClash = /10048|address already in use|only one usage/i.test(lastSidecarError);
  mark("sidecar:unstartable", portClash ? "port collision" : "repeated failure");
  win?.webContents.send("bridge:sidecar-status", {
    alive: false,
    fatal: true,
    reason: portClash
      ? "Another program is already using the trading engine's port."
      : "The trading engine stopped immediately after starting.",
    detail: lastSidecarError.slice(0, 400),
  });
  dialog.showErrorBox(
    "Charticks cannot start its trading engine",
    (portClash
      ? `The trading engine could not claim a network port on this PC.\n\n`
        + `This usually means another copy of Charticks is already running, or a `
        + `development server was left running from a previous session.\n\n`
        + `Close any other Charticks window, then start Charticks again.\n\n`
      : `The trading engine started and then stopped, ${SIDECAR_MAX_FAILURES} times in a row.\n\n`)
    + `Last message from the engine:\n${lastSidecarError.slice(0, 300) || "(none)"}\n\n`
    + `Full details are in logs\\sidecar-process.log in:\n${app.getPath("userData")}`,
  );
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
  // Keep the last error so a failure to start can be EXPLAINED rather than
  // merely reported. "[Errno 10048] address already in use" is the difference
  // between a user closing a stray window and a user reinstalling for an hour.
  if (stream === "err") lastSidecarError = text;
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
    // A FRESH port each attempt. If the last one failed because something else
    // claimed the port, retrying on the same one is guaranteed to fail again —
    // which is precisely how the old fixed-port restart loop could never
    // recover from a collision.
    void (async () => {
      if (!isDev) {
        try {
          SIDECAR_PORT = await findFreePort();
          mark("sidecar:port-rechosen", String(SIDECAR_PORT));
          // The renderer is holding the previous port; tell it to re-read the
          // config and reconnect, or it would keep dialling a dead address.
          win?.webContents.send("bridge:config-changed");
        } catch (err) {
          fatal("port-selection-retry", err);
        }
      }
      startSidecar();
    })();
  }, 1500);
}

function stopSidecar() {
  if (sidecar && !sidecar.killed) {
    sidecar.kill();
    sidecar = null;
  }
}

/**
 * What the user is about to walk away from.
 *
 * Every live stop loss, target and trailing stop is enforced by the sidecar,
 * which this process kills on quit — so closing the window while a managed
 * position is open silently removes the only thing watching it, and the broker
 * goes on holding the position. That has to be a decision, not a side effect.
 *
 * Fails OPEN (returns null) on any error: if we cannot ask, we must not invent
 * a scary dialog, and a sidecar that is already down is protecting nothing
 * anyway.
 */
interface ShutdownCheck {
  live: boolean;
  openPositions: number;
  managedPositions: number;
  workingOrders: number;
  symbols: string[];
}

async function shutdownCheck(): Promise<ShutdownCheck | null> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 2000);
    const res = await fetch(`http://${SIDECAR_HOST}:${SIDECAR_PORT}/shutdown-check`, {
      headers: { Authorization: `Bearer ${BRIDGE_TOKEN}` },
      signal: controller.signal,
    });
    clearTimeout(timer);
    if (!res.ok) return null;
    return (await res.json()) as ShutdownCheck;
  } catch {
    return null;
  }
}

/** True when the user confirmed they want to quit anyway. */
async function confirmQuitWithLiveRisk(state: ShutdownCheck): Promise<boolean> {
  const lines = [
    `${state.managedPositions} live position${state.managedPositions === 1 ? "" : "s"} ` +
      `${state.managedPositions === 1 ? "is" : "are"} being managed by Charticks.`,
    "",
    "Stop Loss, Target and Trailing Stop Loss are enforced by this application —",
    "no stop order is resting at the exchange. If you quit now, nothing will",
    "close these positions automatically. Your broker still holds them.",
  ];
  if (state.symbols.length) lines.push("", state.symbols.join("\n"));
  if (state.workingOrders > 0) {
    lines.push(
      "",
      `${state.workingOrders} working order${state.workingOrders === 1 ? "" : "s"} ` +
        `will also stop being tracked (they stay live at your broker).`,
    );
  }
  const { response } = await dialog.showMessageBox(win ?? undefined!, {
    type: "warning",
    buttons: ["Keep Charticks running", "Quit anyway"],
    defaultId: 0,
    cancelId: 0,
    noLink: true,
    title: "Positions are being managed",
    message: "Quit Charticks and stop managing your live positions?",
    detail: lines.join("\n"),
  });
  return response === 1;
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

  mark("window:created");
  // Show as soon as there is anything to show. `ready-to-show` fires on the
  // renderer's FIRST PAINT, so the window can never flash white — but if the
  // renderer is somehow slow to get there, an invisible window is
  // indistinguishable from a hung app. The fallback shows the window anyway
  // after a short grace period; `backgroundColor` means it appears as the app's
  // own dark shell rather than as a black rectangle.
  let shown = false;
  const show = (why: string) => {
    if (shown || !win) return;
    shown = true;
    mark("window:shown", why);
    win.show();
  };
  win.once("ready-to-show", () => show("first paint"));
  setTimeout(() => show("grace period — renderer had not painted yet"), 1200);

  win.webContents.once("did-finish-load", () => mark("renderer:loaded"));

  // A failed load must RETRY. Without this the window opened on a blank page and
  // stayed there until the user pressed Ctrl+R: `loadURL` / `loadFile` reject
  // asynchronously, nothing was listening, and 'ready-to-show' fires for the
  // error page too — so the app showed a black window with only the menu bar.
  //
  // In dev the cause is a race: Electron is spawned as soon as the main bundle
  // is written, which can be before Vite's dev server is accepting connections,
  // and the load fails with ERR_CONNECTION_REFUSED. Packaged builds load from
  // disk and rarely fail, but a retry costs nothing and the symptom was
  // indistinguishable to the user.
  win.webContents.on("did-fail-load", (_e, code, description, url, isMainFrame) => {
    // Sub-frames and navigation the user cancelled are not our problem.
    if (!isMainFrame || code === -3) return;
    console.error("[window] load failed", code, description, url);
    scheduleRendererLoad(`${description} (${code})`);
  });

  loadRenderer();
  if (DEV_SERVER_URL) win.webContents.openDevTools({ mode: "detach" });

  // The usual way people quit is the window's X, so the warning belongs here —
  // while the window is still on screen to own the dialog. `before-quit` covers
  // the other routes.
  win.on("close", (event) => {
    if (quitting || closeConfirmed) return;
    event.preventDefault();
    void (async () => {
      if (!(await allowedToQuit())) return;
      closeConfirmed = true;
      win?.close();
    })();
  });

  win.on("closed", () => {
    win = null;
    closeConfirmed = false;
    rendererAttempt = 0;
    if (rendererRetry) {
      clearTimeout(rendererRetry);
      rendererRetry = null;
    }
  });
}

// Renderer load retry state. Bounded, so a genuinely broken build reports itself
// instead of retrying behind a black window forever.
//
// The delay is now RAMPED rather than flat. A flat 750ms x 40 meant the common
// case — the dev server being a beat behind Electron — cost most of a second
// every time, and a genuinely broken build sat behind an unexplained window for
// the full 30 seconds. Early retries are near-immediate (the usual cause clears
// in tens of milliseconds) and back off only if something is really wrong.
const RENDERER_MAX_ATTEMPTS = 40;
const RENDERER_RETRY_MS = 750;         // ceiling, not the constant delay
function rendererRetryDelay(attempt: number): number {
  return Math.min(RENDERER_RETRY_MS, 40 * Math.pow(1.7, attempt));
}
let rendererAttempt = 0;
let rendererRetry: NodeJS.Timeout | null = null;

function loadRenderer() {
  if (!win) return;
  rendererAttempt += 1;
  const target = DEV_SERVER_URL
    ? win.loadURL(DEV_SERVER_URL)
    : win.loadFile(join(__dirname, "..", "dist", "index.html"));
  // loadURL/loadFile reject on failure. Unhandled, that rejection was the whole
  // bug — the failure was invisible and nothing retried.
  target
    .then(() => {
      rendererAttempt = 0;
    })
    .catch((err: Error) => scheduleRendererLoad(err.message));
}

function scheduleRendererLoad(reason: string) {
  if (!win || rendererRetry) return;
  if (rendererAttempt >= RENDERER_MAX_ATTEMPTS) {
    reportRendererFailure(reason);
    return;
  }
  const delay = rendererRetryDelay(rendererAttempt);
  mark("renderer:load-retry", `attempt ${rendererAttempt} in ${Math.round(delay)}ms — ${reason}`);
  rendererRetry = setTimeout(() => {
    rendererRetry = null;
    loadRenderer();
  }, delay);
}

/** Tell the user, once, that the interface could not be loaded. A black window
 *  with no explanation is the worst possible outcome — they cannot tell it from
 *  a hung app, and the previous behaviour gave them exactly that. */
let reportedRendererFailure = false;
function reportRendererFailure(reason: string) {
  if (reportedRendererFailure) return;
  reportedRendererFailure = true;
  win?.show();
  dialog.showErrorBox(
    "Charticks could not load its interface",
    `The application window failed to load after ${RENDERER_MAX_ATTEMPTS} attempts.\n\n` +
      `Last error: ${reason}\n\n` +
      (DEV_SERVER_URL
        ? `Expected the Vite dev server at ${DEV_SERVER_URL}. Make sure it is running.`
        : "The installation may be incomplete — try reinstalling Charticks.")
  );
}

// Expose bridge connection details to the renderer via IPC.
//
// Awaits the port being chosen: the renderer asks for this as soon as it
// mounts, which can be before the sidecar has been given a port, and answering
// with 0 would send it to a dead address it would then retry with backoff.
ipcMain.handle("bridge:config", async () => {
  await portReady;
  return {
    host: SIDECAR_HOST,
    port: SIDECAR_PORT,
    token: BRIDGE_TOKEN,
    restUrl: `http://${SIDECAR_HOST}:${SIDECAR_PORT}`,
    wsUrl: `ws://${SIDECAR_HOST}:${SIDECAR_PORT}/stream`,
  };
});

// Resolves once SIDECAR_PORT holds a real port.
let markPortReady: () => void = () => {};
const portReady = new Promise<void>((resolve) => { markPortReady = resolve; });

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

// One Charticks at a time.
//
// Two copies used to fight over the fixed sidecar port: the second one's engine
// could never bind, so it retried forever while its renderer silently talked to
// the FIRST copy's engine — two windows driving one trading engine, each
// believing it owned it. Dynamic ports make that merely wasteful rather than
// dangerous, but a second window is still never what the user meant.
if (!isDev && !app.requestSingleInstanceLock()) {
  console.log("[main] another Charticks is already running — focusing it");
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.focus();
  });

  beginTrace();
  mark("main:script-evaluated", `isDev=${isDev} pid=${process.pid}`);
  app.whenReady().then(() => {
    mark("electron:ready");
    // Window FIRST, sidecar second.
    //
    // Nothing the window needs comes from the sidecar: the shell, the theme and
    // the Home layout are all local. Spawning the engine first put a process
    // creation (and, on a cold disk, its DLL loading) in front of the one thing
    // the user is actually waiting to see. The order is now: get pixels on the
    // screen, then start everything that fills them in.
    createWindow();
    void startSidecarWithPort();
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });
}

// ── startup profiling ──────────────────────────────────────────────────────
// The renderer measures its own phases and reports them here, so one timeline
// covers all three processes.
ipcMain.on("startup:mark", (_e, phase: string, detail?: string) => {
  mark(phase, detail);
  if (phase === "renderer:interactive") {
    // Give the sidecar a moment to report its own phases, then write the table.
    void collectSidecarProfile().finally(() => flushStartup("interactive"));
  }
});

ipcMain.handle("startup:timeline", () => timeline());

/** Ask the sidecar for its internal phase timings and put them on our clock. */
async function collectSidecarProfile(): Promise<void> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 1500);
    const res = await fetch(`http://${SIDECAR_HOST}:${SIDECAR_PORT}/startup-profile`, {
      headers: { Authorization: `Bearer ${BRIDGE_TOKEN}` },
      signal: controller.signal,
    });
    clearTimeout(timer);
    if (!res.ok) return;
    const body = (await res.json()) as { phases?: { phase: string; ms: number }[] };
    // The sidecar times from its OWN start, which is `sidecarSpawnedAt` on this
    // process's clock — that offset is what makes the two comparable.
    adopt((body.phases ?? []).map((p) => ({
      phase: `sidecar:${p.phase}`,
      at: sidecarSpawnedAt + Math.round(p.ms),
    })));
  } catch {
    /* the sidecar may not be up yet; the rest of the timeline still stands */
  }
}

app.on("before-quit", (event) => {
  if (quitting) return;
  // Reached by Cmd/Alt+F4 on the app, a menu Quit, or the second pass after the
  // window close guard below. The check is async and `before-quit` is not, so
  // the first pass always defers and re-issues the quit once the answer is in.
  event.preventDefault();
  void (async () => {
    if (!(await allowedToQuit())) return; // user chose to keep managing
    quitting = true;
    await requestSidecarShutdown();
    stopSidecar();
    app.quit();
  })();
});

/** Whether quitting right now is safe, asking the user when it is not. */
async function allowedToQuit(): Promise<boolean> {
  const state = await shutdownCheck();
  if (!state || !state.live || state.managedPositions === 0) return true;
  return confirmQuitWithLiveRisk(state);
}

/** Ask the sidecar to shut down cleanly, and wait briefly for it to exit. */
async function requestSidecarShutdown(timeoutMs = 3000): Promise<void> {
  if (!sidecar || sidecar.killed) return;
  const exited = new Promise<void>((resolve) => {
    sidecar?.once("exit", () => resolve());
    setTimeout(resolve, timeoutMs);
  });
  // SIGTERM lets uvicorn run FastAPI's shutdown event, which flushes the book.
  // On Windows Node has no real SIGTERM, so this is best-effort and the timeout
  // plus stopSidecar() below is what guarantees the process actually goes.
  try {
    sidecar.kill("SIGTERM");
  } catch {
    /* already gone */
  }
  await exited;
}

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
