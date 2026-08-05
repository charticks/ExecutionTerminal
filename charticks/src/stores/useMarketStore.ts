import { create } from "zustand";
import { bridge } from "@/bridge/client";
import type { BridgeEvent, BrokerHealth, BrokerId } from "@/bridge/events";

export interface IndexTile {
  symbol: string;
  ltp: number;
  changePct: number;
  history: number[];
}

export interface Position {
  id: string;
  symbol: string;
  side: "BUY" | "SELL";
  qty: number;
  entry: number;
  ltp: number;
  pnl: number;
  sl?: number;
  target?: number;
  tsl?: number;
}

export interface ParsedOption {
  underlying: string;
  /** Broker-style expiry ("31JUL2026"); "" when the symbol doesn't carry one. */
  expiry: string;
  strike: number;
  optType: "CE" | "PE";
}

/** Parsed option leg from a live position symbol. Handles both the broker
 *  tradingsymbol ("NIFTY31JUL2625000CE") and the spaced display form
 *  ("NIFTY 24900 CE"). Returns null for non-option symbols (nothing to roll). */
export function parseOptionSymbol(symbol: string): ParsedOption | null {
  const raw = symbol.trim().toUpperCase();

  // Broker tradingsymbol: NAME + DDMMMYY + strike + CE/PE, no separators.
  const compact = /^([A-Z]+)(\d{2})([A-Z]{3})(\d{2})(\d+)(CE|PE)$/.exec(raw);
  if (compact) {
    return {
      underlying: compact[1],
      // Two-digit years are this century — brokers list nothing older.
      expiry: `${compact[2]}${compact[3]}20${compact[4]}`,
      strike: Number(compact[5]),
      optType: compact[6] as "CE" | "PE",
    };
  }

  const spaced = /^(.+?)\s+(\d+(?:\.\d+)?)\s+(CE|PE)$/.exec(raw);
  if (spaced) {
    return {
      underlying: spaced[1],
      expiry: "",
      strike: Number(spaced[2]),
      optType: spaced[3] as "CE" | "PE",
    };
  }
  return null;
}

interface MarketState {
  connected: boolean;
  indices: Record<string, IndexTile>;
  positions: Record<string, Position>;
  brokers: Record<BrokerId, BrokerHealth>;
  netPnl: number;
  riskHalted: boolean;
  ingest: (e: BridgeEvent) => void;
  setConnected: (c: boolean) => void;
  /** Roll a live position to a new strike: close the current leg and open the
   *  new-strike leg with the same side/qty. Mirrors the mock Roll Decider so the
   *  feature works identically on the live book. */
  rollPosition: (id: string, newStrike: number, premium: number) => void;
}

const HISTORY = 40;

export const useMarketStore = create<MarketState>((set) => ({
  connected: false,
  indices: {},
  positions: {},
  brokers: { angel: "down", kotak: "down", dhan: "down", icici: "down" },
  netPnl: 0,
  riskHalted: false,
  setConnected: (connected) => set({ connected }),
  rollPosition: (id, newStrike, premium) =>
    set((s) => {
      const src = s.positions[id];
      if (!src) return {};
      const parsed = parseOptionSymbol(src.symbol);
      if (!parsed) return {};
      const entry = +premium.toFixed(2);
      const nid = `roll-${id}-${Date.now()}`;
      const rolled: Position = {
        id: nid,
        symbol: `${parsed.underlying} ${newStrike} ${parsed.optType}`,
        side: src.side,
        qty: src.qty,
        entry,
        ltp: entry,
        pnl: 0,
      };
      const positions = { ...s.positions, [nid]: rolled };
      delete positions[id]; // close the rolled-from leg
      return { positions };
    }),
  ingest: (e) =>
    set((s) => {
      switch (e.type) {
        case "index_quote": {
          const prev = s.indices[e.symbol];
          const history = [...(prev?.history ?? []), e.ltp].slice(-HISTORY);
          return {
            indices: {
              ...s.indices,
              [e.symbol]: { symbol: e.symbol, ltp: e.ltp, changePct: e.changePct, history },
            },
          };
        }
        case "position_update":
          return { positions: { ...s.positions, [e.id]: { ...e } } };
        case "pnl_update":
          return { netPnl: e.netPnl };
        case "broker_status":
          return { brokers: { ...s.brokers, [e.broker]: e.health } };
        case "risk_event":
          return { riskHalted: e.halted };
        default:
          return {};
      }
    }),
}));

// Wire the store to the live bridge once, at module load.
let wired = false;
export function connectMarketStore() {
  if (wired) return;
  wired = true;
  const { ingest, setConnected } = useMarketStore.getState();
  bridge.on(ingest);
  bridge.onStatus(setConnected);
  bridge.connect();
}
