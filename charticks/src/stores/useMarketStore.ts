import { create } from "zustand";
import { bridge } from "@/bridge/client";
import type {
  BridgeEvent,
  BrokerHealth,
  BrokerId,
  MonitorState,
  PositionSource,
} from "@/bridge/events";

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
  sl?: number | null;
  target?: number | null;
  tsl?: number;
  underlying?: string;
  expiry?: string;
  strike?: number;
  optType?: "CE" | "PE";
  lots?: number;
  /** Whether Charticks is enforcing this position's SL / Target / Trail right
   *  now. A row with `managed === false` must never look like a managed one. */
  managed?: boolean;
  monitorState?: MonitorState;
  monitorDetail?: string;
  source?: PositionSource;
  account?: string | null;
  broker?: string | null;
  /** An exit for this position is at the broker. The row shows "Exit Pending"
   *  and locks its controls rather than a separate order row appearing. */
  exitPendingQty?: number;
  exitReason?: string | null;
  /** The hedge covering this short, and — on a hedge — the shorts it covers. */
  hedgedBy?: string | null;
  hedgeFor?: string[] | null;
  /** Epoch ms this position was opened — already sent for OPEN rows too
   *  (LivePosition.to_dict() always carries it), just not declared here
   *  until now. */
  openedTs?: number | null;
  /** The strategy instance that placed this position, if any — null/absent
   *  for a manually-opened trade. */
  ownerInstanceId?: string | null;
}

/** A completed live trade, kept as this session's history.
 *
 *  The Positions tab used to lose these entirely: the row was deleted the moment
 *  the position went flat, so a trade that had just been opened, managed and
 *  closed left no record in the application at all. */
export interface ClosedPosition extends Position {
  /** Fill price of the closing order. */
  exit?: number | null;
  /** Epoch ms. `openedTs` is inherited from Position now. */
  closedTs?: number | null;
  /** Booked P&L for this trade. */
  realised?: number | null;
}

/** How many completed trades the grid keeps. The complete, durable record of
 *  everything sent to a broker is orders.log; this is a session view. */
const CLOSED_LIMIT = 500;

/** A hedge whose last protected short has closed, awaiting the user's decision. */
export interface OrphanedHedge {
  hedgeId: string;
  symbol: string;
  qty: number;
  lots: number;
  parent: string;
  pnl: number;
}

/** One position Charticks currently cannot protect, as reported by the
 *  sidecar's monitoring alarm. */
export interface MonitorAlarmEntry {
  id: string;
  state: MonitorState;
  detail: string;
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

  // Canonical display form the sidecar's live book emits: "NIFTY 28AUG2026
  // 25000 CE". Matched FIRST, because the looser two-part rule below would
  // swallow the expiry into the underlying ("NIFTY 28AUG2026") and every
  // lookup keyed on it — lot size, roll target, market session — would miss.
  const full = /^([A-Z]+)\s+(\d{1,2}[A-Z]{3}\d{4})\s+(\d+(?:\.\d+)?)\s+(CE|PE)$/.exec(raw);
  if (full) {
    return {
      underlying: full[1],
      expiry: full[2],
      strike: Number(full[3]),
      optType: full[4] as "CE" | "PE",
    };
  }

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
  /** Positions the sidecar cannot currently protect. Empty when everything
   *  open is being monitored. */
  monitorAlarm: MonitorAlarmEntry[];
  /** Completed live trades, oldest first. */
  closedPositions: ClosedPosition[];
  clearClosedPositions: () => void;
  /** Hedges awaiting a Close / Keep decision. Queued rather than held one at a
   *  time: squaring off several shorts at once can orphan several hedges, and
   *  each is a separate decision that must not be lost. */
  orphanedHedges: OrphanedHedge[];
  dismissOrphanedHedge: (hedgeId: string) => void;
  ingest: (e: BridgeEvent) => void;
  setConnected: (c: boolean) => void;
}

const HISTORY = 40;

export const useMarketStore = create<MarketState>((set) => ({
  connected: false,
  indices: {},
  positions: {},
  brokers: { angel: "down", kotak: "down", dhan: "down", icici: "down",
             firstock: "down" },
  netPnl: 0,
  riskHalted: false,
  monitorAlarm: [],
  closedPositions: [],
  clearClosedPositions: () => {
    // The engine owns the history, so it is cleared there and the resulting
    // events empty the grid — never cleared locally, which would leave the two
    // disagreeing until the next reload.
    void bridge.post("/positions/clear-history").catch(() => {});
    set({ closedPositions: [] });
  },
  orphanedHedges: [],
  dismissOrphanedHedge: (hedgeId) =>
    set((s) => ({ orphanedHedges: s.orphanedHedges.filter((h) => h.hedgeId !== hedgeId) })),
  setConnected: (connected) => set({ connected }),
  // There is deliberately no local `rollPosition` here any more. Rolling a live
  // position is two real broker orders, sequenced by the sidecar (close, then
  // open on confirmation) — see POST /positions/roll. This store used to fake
  // it: it deleted the row and inserted a synthetic "rolled" position, so the
  // user saw a roll that had never been sent while the original position sat
  // untouched at the broker. A position grid must only ever show what the
  // broker actually holds.
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
        case "position_update": {
          // `disposition` says what to DO with this row, because "no longer
          // open" covers two opposite outcomes:
          //
          //   closed   a trade of ours completed — ARCHIVE it. It moves out of
          //            the live book and into the session's history, keeping
          //            entry, exit, quantity, both timestamps and realised P&L.
          //            These used to be deleted along with everything else, so a
          //            completed live trade left no record in the application.
          //   removed  the row is simply gone (a foreign leg that vanished, a
          //            position reconciliation dropped) — DELETE it.
          //
          // Older sidecars send neither; `closed`/`qty<=0` then means delete, as
          // it always did.
          if (e.disposition === "closed") {
            const positions = { ...s.positions };
            // The open row is keyed by position id; the history row gets its own
            // id, so remove the open one explicitly.
            for (const key of Object.keys(positions)) {
              if (e.underlying && positions[key].symbol === e.symbol
                  && positions[key].qty > 0 && key !== e.id) {
                delete positions[key];
              }
            }
            const closed = s.closedPositions.some((c) => c.id === e.id)
              ? s.closedPositions.map((c) => (c.id === e.id ? { ...c, ...e } : c))
              : [...s.closedPositions, e as unknown as ClosedPosition];
            return { positions, closedPositions: closed.slice(-CLOSED_LIMIT) };
          }
          const positions = { ...s.positions };
          if (e.disposition === "removed" || e.closed || e.qty <= 0) {
            delete positions[e.id];
            if (e.disposition === "removed") {
              return {
                positions,
                closedPositions: s.closedPositions.filter((c) => c.id !== e.id),
              };
            }
          } else {
            positions[e.id] = { ...positions[e.id], ...e };
          }
          return { positions };
        }
        case "monitor_alarm":
          return { monitorAlarm: e.active ? e.positions : [] };
        case "hedge_orphaned": {
          // De-duplicated by hedge id: a reconnect can replay the question, and
          // asking twice about one hedge would be noise.
          if (s.orphanedHedges.some((h) => h.hedgeId === e.hedgeId)) return {};
          return {
            orphanedHedges: [...s.orphanedHedges, {
              hedgeId: e.hedgeId, symbol: e.symbol, qty: e.qty,
              lots: e.lots, parent: e.parent, pnl: e.pnl,
            }],
          };
        }
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
