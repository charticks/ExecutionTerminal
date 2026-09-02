#!/usr/bin/env node
/**
 * Make sure exactly one sidecar owns the dev port — reclaiming it if the only
 * thing in the way is one of ours.
 *
 * `npm run dev` starts the sidecar on a hard-coded 8787, and the Electron main
 * process dials that same fixed port in dev (electron/main.ts). Windows lets a
 * second process bind an address another process is already listening on, so a
 * leftover session does NOT fail loudly — both sidecars sit on 8787 and the
 * renderer reaches whichever the OS feels like, which is usually the stale one.
 *
 * That failure is close to undiagnosable from the app: both processes write to
 * the same log files, so the logs look normal, while requests land on a sidecar
 * belonging to a session whose UI is long gone. It cost a full afternoon once.
 *
 * Leftovers are easy to create and easy to miss: if the VITE half of `npm run
 * dev` dies (a port clash, a crash, a Ctrl+C that only caught one child),
 * `concurrently` is left holding the sidecar half alone, with no window to make
 * it obvious. It happened twice in two days.
 *
 * WHY THIS RECLAIMS RATHER THAN REFUSING
 * An earlier version simply exited non-zero on a busy port. That was worse:
 * `concurrently -k` kills the other half when one exits, so a stale sidecar
 * stopped the whole dev session — the app did not launch at all, and the reason
 * scrolled past in interleaved output. Refusing to start is only the right
 * answer when we cannot tell whose process it is.
 *
 * So: a listener that identifies itself as a Charticks sidecar is ours, is a
 * disposable dev server, and in dev there is only ever meant to be one — it is
 * killed and the port reclaimed. Anything else is untouched and aborts the run,
 * because a dev script that kills unidentified processes is a worse problem
 * than the one it solves.
 *
 *     node scripts/check-sidecar-port.mjs [port] [--no-reclaim]
 *
 * Exits 0 when the port is free (or was freed), 1 when it could not be.
 */
import { execFileSync } from "node:child_process";
import { createConnection } from "node:net";

const args = process.argv.slice(2);
const RECLAIM = !args.includes("--no-reclaim");
const PORT = Number(args.find((a) => /^\d+$/.test(a)) || 8787);
const HOST = "127.0.0.1";
const PROBE_TIMEOUT_MS = 1500;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** True when something accepts a TCP connection on the port. */
function inUse() {
  return new Promise((resolve) => {
    const socket = createConnection({ host: HOST, port: PORT });
    const done = (result) => {
      socket.destroy();
      resolve(result);
    };
    socket.setTimeout(PROBE_TIMEOUT_MS);
    socket.once("connect", () => done(true));
    socket.once("timeout", () => done(false));
    // ECONNREFUSED — nothing listening, which is what we want.
    socket.once("error", () => done(false));
  });
}

/** Does the listener ANSWER as one of ours? Unauthenticated on purpose:
 *  /health is the one route with no bearer check, and this script has no way to
 *  know the token. */
async function answersAsSidecar() {
  try {
    const res = await fetch(`http://${HOST}:${PORT}/health`, {
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    const body = await res.json();
    return body?.service === "charticks-sidecar";
  } catch {
    // Listening but not answering HTTP, or answering something else entirely.
    return false;
  }
}

/** The command line of a running PID, or "". */
function commandLine(pid) {
  try {
    if (process.platform === "win32") {
      return execFileSync(
        "powershell",
        ["-NoProfile", "-Command",
         `(Get-CimInstance Win32_Process -Filter "ProcessId=${Number(pid)}").CommandLine`],
        { encoding: "utf-8", stdio: ["ignore", "pipe", "ignore"] },
      ).trim();
    }
    return execFileSync("ps", ["-o", "args=", "-p", String(Number(pid))], {
      encoding: "utf-8", stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    return "";
  }
}

/** Does the PROCESS look like our sidecar, whether or not it still answers?
 *
 *  This is the identification that matters. A sidecar can hold the socket while
 *  being completely wedged — accepting connections and answering nothing — and
 *  that is the state most in need of reclaiming, yet it is exactly the state in
 *  which /health cannot reply. Identifying by what the process IS rather than
 *  by what it says keeps that case reclaimable.
 *
 *  Matched narrowly: the literal uvicorn invocation from package.json, not a
 *  loose "python" test that would match any script the user happens to run. */
function looksLikeOurSidecar(pid) {
  const cmd = commandLine(pid).toLowerCase();
  return cmd.includes("uvicorn") && cmd.includes("server:app");
}

/** PIDs listening on the port. Best-effort: a failure here costs a nicer
 *  message, not the check itself. */
function listeningPids() {
  try {
    const out =
      process.platform === "win32"
        ? execFileSync("netstat", ["-ano", "-p", "TCP"], { encoding: "utf-8" })
        : execFileSync("lsof", [`-ti`, `tcp:${PORT}`, "-sTCP:LISTEN"], {
            encoding: "utf-8",
          });
    if (process.platform !== "win32") {
      return [...new Set(out.split(/\s+/).filter(Boolean))];
    }
    const pids = out
      .split(/\r?\n/)
      .filter((l) => /LISTENING/i.test(l) && new RegExp(`:${PORT}\\b`).test(l))
      .map((l) => l.trim().split(/\s+/).pop())
      .filter(Boolean);
    return [...new Set(pids)];
  } catch {
    return [];
  }
}

/** Kill one PID and its children. The reload supervisor spawns a worker that
 *  holds the socket too, so killing only the parent leaves the port bound. */
function killTree(pid) {
  try {
    if (process.platform === "win32") {
      execFileSync("taskkill", ["/F", "/T", "/PID", String(pid)], {
        stdio: "ignore",
      });
    } else {
      process.kill(Number(pid), "SIGKILL");
    }
    return true;
  } catch {
    return false;
  }
}

const killCmdFor = (pids) =>
  process.platform === "win32"
    ? `taskkill /F /T ${pids.map((p) => `/PID ${p}`).join(" ") || "/PID <pid>"}`
    : `kill -9 ${pids.join(" ") || "<pid>"}`;

if (!(await inUse())) {
  process.exit(0);
}

const pids = listeningPids();
// Either signal is enough, and they cover different failures: a healthy
// sidecar answers /health, a hung one only looks right from the outside.
const answers = await answersAsSidecar();
const kind =
  answers || (pids.length > 0 && pids.every(looksLikeOurSidecar))
    ? "sidecar"
    : "other";
const hung = !answers && kind === "sidecar";

// ── ours, and reclaimable ────────────────────────────────────────────────
if (kind === "sidecar" && RECLAIM && pids.length) {
  console.log(
    `[dev] Port ${PORT} was held by a leftover Charticks sidecar ` +
    `(PID ${pids.join(", ")}${hung ? ", hung — bound but not answering" : ""}) ` +
    `— reclaiming it.`,
  );
  const failed = pids.filter((pid) => !killTree(pid));

  // Confirm rather than assume: a kill that reports success can still leave
  // the socket in TIME_WAIT for a moment, and starting uvicorn into that races
  // straight back into the double-bind this exists to prevent.
  for (let i = 0; i < 20 && (await inUse()); i++) await sleep(250);

  if (!(await inUse())) {
    console.log(`[dev] Port ${PORT} is free. Starting the sidecar.`);
    process.exit(0);
  }

  console.error(
    `\n  Could not free port ${PORT}${failed.length ? ` (kill failed for PID ${failed.join(", ")})` : ""}.\n` +
    `  It may belong to another user or need elevation. Clear it with:\n\n` +
    `      ${killCmdFor(pids)}\n`,
  );
  process.exit(1);
}

// ── not ours, or reclaim disabled — never kill something unidentified ────
console.error(`
────────────────────────────────────────────────────────────────────────
  Port ${PORT} is already in use — not starting a second sidecar.
────────────────────────────────────────────────────────────────────────
`);

if (kind === "sidecar") {
  console.error(
    `  A Charticks sidecar is already listening there.\n` +
    (RECLAIM
      ? `  Its PID could not be determined, so it was left alone.\n`
      : `  --no-reclaim was passed, so it was left alone.\n`),
  );
} else {
  console.error(
    `  Something that is not a Charticks sidecar is listening there, so it\n` +
    `  was left alone. Stop it, or change the port in package.json\n` +
    `  (dev:sidecar) and in electron/main.ts, which dials the same fixed\n` +
    `  port in dev.\n`,
  );
}

console.error(
  `  Listening PID(s): ${pids.length ? pids.join(", ") : "could not determine"}\n\n` +
  `  To clear it:\n` +
  `      ${killCmdFor(pids)}\n\n` +
  `  Then run "npm run dev" again.\n`,
);

process.exit(1);
