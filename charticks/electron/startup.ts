import { appendFileSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { logsDir } from "./logs";

/**
 * Startup phase timing.
 *
 * Charticks is three processes that come up at once — Electron, the renderer,
 * and a Python sidecar — and "it takes ages to start" is unanswerable without
 * knowing which of them the time went to. Every phase marks itself here; the
 * table is written to `logs/startup.log` once the app is interactive, and is
 * readable at any time from the renderer.
 *
 * Kept in the product rather than used once and deleted: startup cost is the
 * kind of thing that creeps back, and a number nobody can see is a number
 * nobody defends.
 *
 * All times are milliseconds since the Electron process began, so marks from
 * the three processes sit on one timeline. The renderer's own marks arrive over
 * IPC and are offset by the main process's clock, not its own.
 */
export interface Mark {
  phase: string;
  at: number;      // ms since process start
  detail?: string;
}

const marks: Mark[] = [];
let written = false;

/**
 * Append every mark to disk AS IT HAPPENS, not only in the summary at the end.
 *
 * The summary is written when the app reaches "interactive" — which is no use
 * at all for diagnosing a startup that never gets there, and that is exactly
 * the case that matters. A live trace means the last line in the file names the
 * step it died on.
 */
function trace(m: Mark): void {
  try {
    const dir = logsDir();
    mkdirSync(dir, { recursive: true });
    appendFileSync(
      join(dir, "startup.log"),
      `${String(m.at).padStart(6)}ms  ${m.phase}${m.detail ? ` — ${m.detail}` : ""}\n`,
      "utf-8",
    );
  } catch {
    /* never break startup to log it */
  }
}

/** ms since this process started. `process.uptime()` counts from exec, which is
 *  earlier than any code of ours could run — exactly the origin we want. */
function now(): number {
  return Math.round(process.uptime() * 1000);
}

export function mark(phase: string, detail?: string): number {
  const at = now();
  const m: Mark = { phase, at, detail };
  marks.push(m);
  trace(m);
  return at;
}

/** Open a fresh trace for this launch, so one file is one startup. */
export function beginTrace(): void {
  try {
    const dir = logsDir();
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "startup.log"),
      `=== Charticks startup trace — ${new Date().toISOString()} ===\n`, "utf-8");
  } catch {
    /* never break startup to log it */
  }
}

/** Fold in marks measured inside another process (renderer, sidecar). `at` is
 *  already on this process's timeline. */
export function adopt(external: Mark[]): void {
  for (const m of external) marks.push(m);
}

export function timeline(): Mark[] {
  return [...marks].sort((a, b) => a.at - b.at);
}

/**
 * Write the timeline once, when the app is interactive.
 *
 * Deliberately once: the interesting number is cold start, and a file that
 * accumulates every re-render loses it in noise. Never throws — a startup log
 * that can take the app down would be worse than no startup log.
 */
export function flush(reason: string): void {
  if (written) return;
  written = true;
  const rows = timeline();
  const total = rows.length ? rows[rows.length - 1].at : 0;
  const lines = [
    "",
    `=== Charticks startup — ${new Date().toISOString()} (${reason}) ===`,
    ...rows.map((m, i) => {
      const delta = i === 0 ? m.at : m.at - rows[i - 1].at;
      return `  ${String(m.at).padStart(6)}ms  (+${String(delta).padStart(5)}ms)  `
        + `${m.phase}${m.detail ? ` — ${m.detail}` : ""}`;
    }),
    `  ${String(total).padStart(6)}ms  TOTAL to interactive`,
  ];
  try {
    const dir = logsDir();
    mkdirSync(dir, { recursive: true });
    appendFileSync(join(dir, "startup.log"), lines.join("\n") + "\n", "utf-8");
    // A copy that is never overwritten, so the last complete startup survives
    // the next launch truncating the live trace.
    appendFileSync(join(dir, "startup-history.log"), lines.join("\n") + "\n", "utf-8");
  } catch {
    /* logging must never break startup */
  }
  console.log(lines.join("\n"));
}
