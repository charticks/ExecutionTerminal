// Typed event contract between the Python sidecar (data plane) and the renderer.
// Keep in sync with sidecar/bridge/events.py.

export type BrokerId = "angel" | "kotak" | "dhan" | "icici";
// Kept in sync with sidecar/services/broker_manager.py health values.
// "reconnecting" is retained as an alias of the amber/transitional state.
export type BrokerHealth =
  | "connecting"
  | "connected"
  | "reconnecting"
  | "session_expired"
  | "down";

export interface TickEvent {
  type: "tick";
  token: string;
  ltp: number;
  volume: number;
  ts: number; // epoch ms
}

export interface CandleCloseEvent {
  type: "candle_close";
  token: string;
  interval: string;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
  ts: number;
}

export interface IndexQuote {
  type: "index_quote";
  symbol: string; // NIFTY, BANKNIFTY, ...
  ltp: number;
  changePct: number;
  ts: number;
}

export interface PositionUpdate {
  type: "position_update";
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

/** Canonical live-order lifecycle, mirrored from
 *  sidecar/services/order_sync/base.py. These are the broker's real states as
 *  reported by the Order Synchronization Engine — not a guess made at
 *  submission time. "COMPLETE" is retained only because the paper engine still
 *  emits it. */
export type OrderUpdateStatus =
  | "NEW"
  | "SUBMITTED"
  | "ACCEPTED"
  | "PENDING"
  | "PARTIAL"
  | "FILLED"
  | "REJECTED"
  | "CANCELLED"
  | "EXPIRED"
  | "COMPLETE";

export interface OrderUpdate {
  type: "order_update";
  id: string;
  symbol: string;
  side: "BUY" | "SELL";
  qty: number;
  price: number;
  status: OrderUpdateStatus;
  ts: number;
}

export interface PnlUpdate {
  type: "pnl_update";
  netPnl: number;
  ts: number;
}

export interface RiskEvent {
  type: "risk_event";
  halted: boolean;
  reason?: string;
  ts: number;
}

export interface BrokerStatusEvent {
  type: "broker_status";
  broker: BrokerId;
  health: BrokerHealth;
  detail?: string | null;
  /** Account id this status belongs to (multi-account model). May be absent
   *  for legacy/broker-level events. */
  account?: string | null;
}

export interface LogLine {
  type: "log_line";
  level: "info" | "warn" | "error";
  message: string;
  ts: number;
}

export interface OptionChainRowEvent {
  strike: number;
  ce: number | null;
  pe: number | null;
  ceoi: number;
  peoi: number;
}

export interface OptionChainUpdateEvent {
  type: "option_chain_update";
  symbol: string;
  expiry: string;
  expiries: string[];
  atm: number;
  rows: OptionChainRowEvent[];
  /** Live LTPs for contracts registered via POST /option-chain/watch — the
   *  Roll picker's strikes, which sit outside the chain window. */
  watch?: { strike: number; ltp: number | null }[];
}

// Full paper-book snapshot pushed by sidecar/services/paper_engine.py on every
// change or relevant tick. The renderer's paper stores replace their state with
// this so Paper is driven by the engine exactly as Live is by the broker book.
// Shapes mirror useOrdersStore.Order/Trade and usePositionsStore.OptionPosition.
export interface PaperOrderSnap {
  id: string;
  ts: number;
  underlying: string;
  /** Contract expiry — carried so the renderer's duplicate-pending-order check
   *  can tell the same strike on two expiries apart. */
  expiry?: string;
  strike: number;
  optType: "CE" | "PE";
  side: "BUY" | "SELL";
  orderType: "MARKET" | "LIMIT";
  lots: number;
  filledLots: number;
  qty: number;
  price: number;
  status: "OPEN" | "PENDING" | "PARTIAL" | "EXECUTED" | "CANCELLED" | "REJECTED";
  avgFill?: number | null;
}

export interface PaperTradeSnap {
  id: string;
  orderId: string;
  ts: number;
  underlying: string;
  strike: number;
  optType: "CE" | "PE";
  side: "BUY" | "SELL";
  qty: number;
  price: number;
}

export interface PaperPositionSnap {
  id: string;
  underlying: string;
  strike: number;
  optType: "CE" | "PE";
  side: "BUY" | "SELL";
  lots: number;
  qty: number;
  entry: number;
  avgEntry: number;
  ltp: number;
  exit?: number;
  sl?: number;
  target?: number;
  status: "OPEN" | "CLOSED";
}

export interface PaperStateEvent {
  type: "paper_state";
  orders: PaperOrderSnap[];
  trades: PaperTradeSnap[];
  positions: PaperPositionSnap[];
  netPnl: number;
  ts: number;
}

// Aggregate connectivity state for the header badge. Published by
// sidecar/services/reliability/health_monitor.py on state transitions only —
// distinct from per-account BrokerStatusEvent.
export type ConnectionHealthState =
  | "connected"
  | "reconnecting"
  | "auth_failed"
  | "down";

export interface ConnectionHealthEvent {
  type: "connection_health";
  state: ConnectionHealthState;
  accountsConnected: number;
  detail?: string | null;
  ts: number;
}

export type BridgeEvent =
  | TickEvent
  | CandleCloseEvent
  | IndexQuote
  | PositionUpdate
  | OrderUpdate
  | PnlUpdate
  | RiskEvent
  | BrokerStatusEvent
  | LogLine
  | ConnectionHealthEvent
  | OptionChainUpdateEvent
  | PaperStateEvent;
