// Renderer-side access to the log files.
//
// The main process owns the location (electron/logs.ts); this is only the
// window onto it. Every call is a no-op outside Electron — the Vite dev server
// in a browser tab has no main process to ask — so callers can render the
// controls unconditionally and simply get `available: false`.

interface DiagnosticsApi {
  diagnostics?: {
    info(): Promise<{ dir: string }>;
    open(): Promise<{ ok: boolean; dir: string; error?: string }>;
    saveBundle(): Promise<{ ok: boolean; path?: string; error?: string }>;
  };
}

function api() {
  return (window as unknown as { charticks?: DiagnosticsApi }).charticks?.diagnostics;
}

/** True when running inside Electron, where the log folder can be reached. */
export function available(): boolean {
  return !!api();
}

/** Absolute path of the log folder, or "" if it cannot be asked for. */
export async function logFolder(): Promise<string> {
  try {
    return (await api()?.info())?.dir ?? "";
  } catch {
    return "";
  }
}

/** Open the log folder in Explorer. */
export async function openLogFolder(): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await api()?.open();
    return res ?? { ok: false, error: "Not available outside the desktop app." };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}

/** Zip the current logs onto the Desktop and reveal the file. */
export async function saveDiagnosticsBundle(): Promise<{ ok: boolean; path?: string; error?: string }> {
  try {
    const res = await api()?.saveBundle();
    return res ?? { ok: false, error: "Not available outside the desktop app." };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}
