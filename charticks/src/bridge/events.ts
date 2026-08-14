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

/** Whether a position is actually being protected right now. Mirror of the
 *  vocabulary in sidecar/services/live_book.py — keep the two in step.
 *
 *  Only "protected" and "no_rule" mean nothing is wrong: in "no_rule" the user
 *  chose to trade without a stop. Every other value means automation is NOT
 *  acting on this position, and the UI must say so rather than drawing a normal
 *  row. */
export type MonitorState =
  | "protected"
  | "no_rule"
  | "exiting"
  | "feed_lost"
  | "paused"
  | "restoring"
  | "unmanaged";

/** Where a position came from: opened by Charticks, opened elsewhere and
 *  adopted by the user, or opened elsewhere and not managed. */
export type PositionSource = "charticks" | "adopted" | "external";

export interface PositionUpdate {
  type: "position_update";
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
  /** Structured contract, so the renderer never has to parse `symbol` back
   *  apart. Absent on foreign legs (equity/futures rows) which have no
   *  canonical option identity. */
  underlying?: string;
  expiry?: string;
  strike?: number;
  optType?: "CE" | "PE";
  lots?: number;
  /** True when Charticks is enforcing this position's SL / Target / Trail. */
  managed?: boolean;
  monitorState?: MonitorState;
  /** Plain-language reason, shown in the row's tooltip and the alarm banner. */
  monitorDetail?: string;
  source?: PositionSource;
  account?: string | null;
  broker?: string | null;
  /** Quantity of this position currently being exited at the broker, and why.
   *
   *  A square-off is an ACTION ON THE POSITION, not a second position: the grid
   *  keeps one row from entry to close and moves it through its states. These
   *  are what drive the "Exit Pending" badge and the locking of the row's
   *  controls, instead of a separate pending-order row appearing above it. */
  exitPendingQty?: number;
  exitReason?: string | null;
  /** The protective hedge covering this (short) position, if Charticks opened
   *  one — the id of the hedge's own row. */
  hedgedBy?: string | null;
  /** The short position ids this leg is a HEDGE for. Present only on a hedge,
   *  and more than one when the same hedge covers several shorts. A hedge is
   *  never drawn as an independent trade. */
  hedgeFor?: string[] | null;
  /** What the renderer should do with this row. "no longer open" covers two
   *  opposite outcomes and they must not be conflated:
   *
   *    open     a live position — show it
   *    closed   a trade of ours completed — ARCHIVE it into the session's
   *             history with its exit price, times and realised P&L
   *    removed  the row is simply gone (a foreign leg that vanished, a position
   *             reconciliation dropped) — delete it
   *
   *  Both of the last two used to arrive as a bare `closed: true`, so the grid
   *  could only delete — which is why a completed live trade left no record. */
  disposition?: "open" | "closed" | "removed";
  /** Closed-trade fields, present on a "closed" row. */
  exit?: number | null;
  openedTs?: number | null;
  closedTs?: number | null;
  realised?: number | null;
  /** Set when the position is gone (qty 0) — the row should disappear. */
  closed?: boolean;
}

/** The last short a protective hedge was covering has closed, leaving the hedge
 *  on its own. Charticks neither closes nor keeps it by itself — both are
 *  decisions the user has to make — so it asks. */
export interface HedgeOrphanedEvent {
  type: "hedge_orphaned";
  hedgeId: string;
  symbol: string;
  qty: number;
  lots: number;
  parent: string;
  pnl: number;
  ts: number;
}

/** Raised while Charticks cannot actively protect one or more positions it is
 *  displaying. Stays active until monitoring resumes, so it is rendered as a
 *  persistent banner rather than a toast. */
export interface MonitorAlarmEvent {
  type: "monitor_alarm";
  active: boolean;
  positions: { id: string; state: MonitorState; detail: string }[];
  ts: number;
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
  /** The structured contract. Present so the renderer can CREATE an order row
   *  from this event and not only update one it already has — every order the
   *  sidecar originates (stop-loss and target exits, square-offs, roll legs,
   *  hedges) exists nowhere else, and was previously dropped for lack of a row
   *  to match. Optional so an older sidecar still updates known rows. */
  underlying?: string;
  expiry?: string;
  strike?: number;
  optType?: "CE" | "PE";
  lotSize?: number;
  /** Ordered quantity, as distinct from `qty` which is filled-or-ordered. */
  requestedQty?: number;
  filledQty?: number;
  /** The limit price the order was placed at (0 for MARKET), as distinct from
   *  `price`, which is the average fill once there is one. */
  limitPrice?: number;
  avgPrice?: number;
  orderType?: "MARKET" | "LIMIT";
  product?: "NRML" | "MIS";
  validity?: "DAY" | "IOC";
  parentId?: string;
  account?: string;
  broker?: string;
  /** The broker's rejection text, when there is one. */
  reason?: string | null;
  /** Set when this order CLOSES a position — the position's id. */
  exitFor?: string | null;
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
  | MonitorAlarmEvent
  | HedgeOrphanedEvent
  | OrderUpdate
  | PnlUpdate
  | RiskEvent
  | BrokerStatusEvent
  | LogLine
  | ConnectionHealthEvent
  | OptionChainUpdateEvent
  | PaperStateEvent;
