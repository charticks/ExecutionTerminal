// Renderer-side startup marks.
//
// Reported to the main process, which owns the single timeline across all three
// processes (see electron/startup.ts). Marking is fire-and-forget and is a no-op
// outside Electron, so it can be called from anywhere without a guard.

interface StartupApi {
  startupMark?(phase: string, detail?: string): void;
  startupTimeline?(): Promise<{ phase: string; at: number; detail?: string }[]>;
}

function api(): StartupApi | undefined {
  return (window as unknown as { charticks?: StartupApi }).charticks;
}

export function mark(phase: string, detail?: string): void {
  try {
    api()?.startupMark?.(phase, detail);
  } catch {
    /* profiling must never affect startup */
  }
}

/** The full cross-process timeline, for diagnostics. */
export async function timeline(): Promise<{ phase: string; at: number; detail?: string }[]> {
  try {
    return (await api()?.startupTimeline?.()) ?? [];
  } catch {
    return [];
  }
}
