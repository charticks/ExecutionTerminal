import { app } from "electron";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

/**
 * Where Charticks writes its logs.
 *
 * They used to live in `%APPDATA%\Charticks\logs`. That is the correct place by
 * Windows convention and the wrong place in practice: AppData is hidden, the
 * path only exists in a troubleshooting document, and a tester who is asked for
 * logs during a failed live session does not go hunting through a hidden
 * folder — so a real Kotak Neo trading failure was reported with no log at all.
 *
 * `Documents\Charticks\logs` is somewhere a person can actually find, survives
 * uninstalling and reinstalling the app, and can be zipped and sent without
 * anyone being told a path. It is NOT the install directory: that is wiped on
 * reinstall and may be read-only, which is the failure that put the SDK's
 * logzero output somewhere nobody could reach in the first place.
 *
 * AppData remains the fallback for the case Documents cannot be written —
 * a redirected/offline OneDrive profile, a locked-down corporate machine. The
 * app must never fail to start because it could not open a log file.
 */

/** Resolved once per process: the probe below touches the disk, and every log
 *  line would otherwise repeat it. */
let cachedRoot = "";

/**
 * The directory passed to the sidecar as CHARTICKS_LOG_DIR.
 *
 * The sidecar appends `logs/` itself (see sidecar/services/paths.py), so this
 * is the PARENT of the log folder — `…\Documents\Charticks`, not `…\logs`.
 */
export function logRoot(): string {
  if (cachedRoot) return cachedRoot;
  const candidates = [
    () => join(app.getPath("documents"), "Charticks"),
    () => app.getPath("userData"),
  ];
  for (const candidate of candidates) {
    try {
      const root = candidate();
      // mkdir alone is not proof: a redirected Documents folder can exist and
      // still refuse writes. Write something and delete it.
      const dir = join(root, "logs");
      mkdirSync(dir, { recursive: true });
      const probe = join(dir, ".write-probe");
      writeFileSync(probe, "", "utf-8");
      rmSync(probe, { force: true });
      cachedRoot = root;
      return cachedRoot;
    } catch {
      /* try the next candidate */
    }
  }
  // Nothing is writable. Return the AppData path anyway: every caller already
  // guards its own writes, and an empty string would produce nonsense paths in
  // the error dialogs that tell the user where to look.
  cachedRoot = app.getPath("userData");
  return cachedRoot;
}

/** The folder the log files themselves go in — what the user is shown, opens,
 *  and sends in. */
export function logsDir(): string {
  return join(logRoot(), "logs");
}
