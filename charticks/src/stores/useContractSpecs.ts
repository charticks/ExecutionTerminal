import { create } from "zustand";
import { bridge } from "@/bridge/client";
import { INDEX_BY_ID } from "@/lib/indices";

// Contract facts the ENGINE is authoritative for: lot size and strike step, read
// from the instrument master.
//
// Why this exists
// ---------------
// `lib/indices.ts` ships a hard-coded lot size per index, and every order's
// quantity is `lots x that number`. Exchanges revise lot sizes. When one drifted,
// the renderer sent a quantity that no longer matched the contract, the sidecar's
// lot-size rule rejected it (correctly), and every order for that index failed
// with LOT_QTY_MISMATCH until someone shipped a new build.
//
// The instrument master is the authority, so the sidecar serves it and this store
// caches it. The shipped table remains the fallback for an index the master has
// not loaded — and in exactly that case the sidecar's own rule stands down too,
// so the two sides can never disagree about a size.

export interface ContractSpec {
  lotSize: number;
  tickSize: number | null;
}

interface ContractSpecsState {
  specs: Record<string, ContractSpec>;
  steps: Record<string, number>;
  loaded: boolean;
  refresh: () => Promise<void>;
}

export const useContractSpecs = create<ContractSpecsState>((set) => ({
  specs: {},
  steps: {},
  loaded: false,
  refresh: async () => {
    try {
      const res = await bridge.get<{
        specs?: Record<string, ContractSpec>;
        steps?: Record<string, number>;
      }>("/contract-specs");
      const specs = res.specs ?? {};
      // An empty answer means no instrument master is loaded yet — keep whatever
      // we already had rather than blanking a good table with an empty one.
      if (Object.keys(specs).length === 0) return;
      set({ specs, steps: res.steps ?? {}, loaded: true });
    } catch {
      // Sidecar not up. `startContractSpecs` refreshes on every reconnect.
    }
  },
}));

/** Lot size for an underlying: the instrument master's figure when the engine
 *  has one, else the shipped fallback. Never 0 — a 0 would silently produce a
 *  zero-quantity order. */
export function lotSizeOf(underlying: string): number {
  const live = useContractSpecs.getState().specs[underlying]?.lotSize;
  if (live && live > 0) return live;
  return INDEX_BY_ID[underlying]?.lot || 1;
}

/** Strike ladder for an underlying, from the engine when known. */
export function strikeStepOf(underlying: string): number {
  const live = useContractSpecs.getState().steps[underlying];
  if (live && live > 0) return live;
  return INDEX_BY_ID[underlying]?.step || 50;
}

let started = false;

/** Fetch once at startup and again on every sidecar reconnect — a restarted
 *  sidecar may have loaded a different (newer) master. Also re-read periodically
 *  because the master arrives asynchronously after the first broker connects,
 *  which can be minutes after the app opens. */
export function startContractSpecs() {
  if (started) return;
  started = true;
  const { refresh } = useContractSpecs.getState();
  void refresh();
  bridge.onStatus((connected) => {
    if (connected) void refresh();
  });
  setInterval(() => {
    if (!useContractSpecs.getState().loaded) void refresh();
  }, 15_000);
}
