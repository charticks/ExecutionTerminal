import { create } from "zustand";
import { lotSize } from "@/stores/usePositionsStore";
import { type RiskRule } from "@/lib/risk";
import { useTradingModeStore, resyncTradingMode } from "@/stores/useTradingModeStore";
import { bridge } from "@/bridge/client";

// Order/trade ledger. In PAPER mode the sidecar paper engine
// (services/paper_engine.py) is authoritative: placeOrder/modify/cancel POST to
// the sidecar and the resulting `paper_state` snapshot replaces orders + trades
// (see startPaperSync). Realistic fills — Market at bid/ask, Limit resting until
// the market reaches the price — happen in the engine, never on client timers.
// In LIVE mode orders route to the broker via the sidecar and reconcile on the
// response; live positions come from the broker book (useMarketStore).

export type OrderType = "MARKET" | "LIMIT";
export type OrderStatus =
  | "PENDING"
  | "OPEN"
  | "PARTIAL"
  | "EXECUTED"
  | "CANCELLED"
  | "REJECTED";

export interface Order {
  id: string;
  ts: number;
  underlying: string;
  /** Chain expiry — part of the instrument identity, so the duplicate-pending
   *  check never conflates the same strike on two different expiries. */
  expiry?: string;
  strike: number;
  optType: "CE" | "PE";
  side: "BUY" | "SELL";
  orderType: OrderType;
  lots: number; // original order quantity (lots)
  filledLots: number; // executed so far
  qty: number; // original quantity (lots × lot size)
  price: number; // limit price, or fill price for market
  status: OrderStatus;
  avgFill?: number | null;
  rule?: RiskRule; // risk defaults captured at submit, applied on execution
  /** The broker's own order id, once placement returns one. Live rows are
   *  matched on this by the Order Synchronization Engine's updates. */
  brokerOrderId?: string;
}

export interface Trade {
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

export interface OrderInput {
  underlying: string;
  strike: number;
  optType: "CE" | "PE";
  side: "BUY" | "SELL";
  orderType: OrderType;
  lots: number;
  qty: number;
  price: number;
  rule?: RiskRule;
  /** Chain expiry — required so the sidecar can resolve the broker
   *  trading symbol/token for both paper (pricing) and live routing. */
  expiry?: string;
  /** Product + validity from the active profile's Order Defaults. */
  product?: "NRML" | "MIS";
  validity?: "DAY" | "IOC";
  /** Bypass the one-working-order-per-instrument-and-side rule. Set only by the
   *  "retry remaining quantity" flow after a partially executed split order. */
  allowDuplicate?: boolean;
  /** The user knowingly exceeded Max Position. The sidecar enforces that limit
   *  independently, so the choice must be declared rather than assumed. */
  overrideMaxPos?: boolean;
}

export interface PlaceResult {
  ok: boolean;
  error?: string;
  /** Machine-readable rejection reason from the sidecar — "MARKET_CLOSED",
   *  "DUPLICATE_PENDING", "PARTIAL_FILL", "NO_EXECUTION_BROKER",
   *  "EXECUTION_BROKER_DISCONNECTED", "MODE_REQUIRED" or "MODE_MISMATCH".
   *  Mirrors the sidecar contract. */
  code?: string;
  /** Present when code === "PARTIAL_FILL": a split order stopped part-way. */
  executedQty?: number;
  remainingQty?: number;
}

/** Instrument + side identity used by the duplicate-pending-order rule. */
export interface OrderKey {
  underlying: string;
  expiry?: string;
  strike: number;
  optType: "CE" | "PE";
  side: "BUY" | "SELL";
}

/** An order is a "working order" (shown in Positions) while it is submitted but
 *  not yet fully executed or cancelled. */
export function isWorking(o: Order): boolean {
  return o.status === "PENDING" || o.status === "OPEN" || o.status === "PARTIAL";
}

interface OrdersState {
  orders: Order[];
  trades: Trade[];
  nextId: number;
  /** Replace the ledger from an engine snapshot (called by startPaperSync). */
  setFromSnapshot: (orders: Order[], trades: Trade[]) => void;
  /** The existing working order on this exact instrument + side, if any — the
   *  duplicate-pending-order check. Completed/cancelled/rejected never match. */
  findWorkingOrder: (key: OrderKey) => Order | undefined;
  placeOrder: (input: OrderInput) => Promise<PlaceResult>;
  modifyOrder: (id: string, patch: { price?: number; qty?: number; lots?: number }) => void;
  cancelOrder: (id: string) => void;
  updateStatus: (id: string, status: OrderStatus) => void;
  /** Apply broker-confirmed state to a live order (see liveOrderSync). This is
   *  the only path that may mark a live order EXECUTED. */
  applyBrokerUpdate: (
    id: string,
    patch: { brokerOrderId?: string; status: OrderStatus; filledQty?: number; avgFill?: number },
  ) => void;
}

export const useOrdersStore = create<OrdersState>((set, get) => ({
  orders: [],
  trades: [],
  nextId: 1,

  setFromSnapshot: (orders, trades) => set({ orders, trades }),

  findWorkingOrder: (key) =>
    get().orders.find(
      (o) =>
        isWorking(o) &&
        o.underlying === key.underlying &&
        o.strike === key.strike &&
        o.optType === key.optType &&
        o.side === key.side &&
        // Treat a missing expiry on either side as "same contract" — older
        // snapshots predate the field and must not silently stop matching.
        (!o.expiry || !key.expiry || o.expiry === key.expiry),
    ),

  placeOrder: async (input) => {
    // Read the mode ONCE and send it with the order. The sidecar routes on this
    // value (after checking it against its own), so the engine that fills the
    // order is the one the badge showed when the user clicked — not whatever
    // the sidecar happens to think later.
    const mode = useTradingModeStore.getState().mode;
    const body = {
      mode,
      underlying: input.underlying,
      allowDuplicate: input.allowDuplicate ?? false,
      expiry: input.expiry ?? "",
      strike: input.strike,
      optType: input.optType,
      side: input.side,
      qty: input.qty,
      lots: input.lots,
      orderType: input.orderType,
      price: input.price,
      rule: input.rule,
      product: input.product ?? "NRML",
      validity: input.validity ?? "DAY",
      overrideMaxPos: input.overrideMaxPos ?? false,
    };

    // LIVE: record a PENDING order for visibility, then reconcile on response.
    if (mode === "live") {
      const oid = `O${get().nextId}`;
      const order: Order = {
        id: oid, ts: Date.now(), underlying: input.underlying, expiry: input.expiry,
        strike: input.strike,
        optType: input.optType, side: input.side, orderType: input.orderType,
        lots: input.lots, filledLots: 0, qty: input.qty, price: input.price,
        status: "PENDING", rule: input.rule,
      };
      set((s) => ({ nextId: s.nextId + 1, orders: [order, ...s.orders] }));
      try {
        const res = await bridge.post<PlaceResult>("/orders/place", body);
        if (res.code === "MODE_MISMATCH") resyncTradingMode();
        // A successful response means SUBMITTED, not filled — the broker
        // returned an order id and nothing more. The row stays PENDING until
        // the Order Synchronization Engine reports the broker's real state
        // (see liveOrderSync). Only an outright rejection is final here.
        if (!res.ok) get().updateStatus(oid, "REJECTED");
        return res;
      } catch (e) {
        // The request itself failed, so we do not know whether the broker got
        // it. Left PENDING deliberately: marking it REJECTED would claim the
        // order does not exist, and sync will resolve it if it does.
        return { ok: false, error: String(e) };
      }
    }

    // PAPER: the engine validates, fills (market) or rests (limit), and pushes
    // the resulting book via `paper_state`. No client-side order/position here.
    try {
      const res = await bridge.post<PlaceResult>("/orders/place", body);
      if (res.code === "MODE_MISMATCH") resyncTradingMode();
      return res;
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  },

  modifyOrder: (id, patch) => {
    const mode = useTradingModeStore.getState().mode;
    bridge.post("/orders/modify", { mode, id, ...patch }).catch(() => {});
  },

  cancelOrder: (id) => {
    const mode = useTradingModeStore.getState().mode;
    bridge.post("/orders/cancel", { mode, id }).catch(() => {});
  },

  updateStatus: (id, status) =>
    set((s) => ({ orders: s.orders.map((o) => (o.id === id ? { ...o, status } : o)) })),

  applyBrokerUpdate: (id, patch) =>
    set((s) => ({
      orders: s.orders.map((o) => {
        if (o.id !== id) return o;
        const lotSz = Math.max(1, Math.round(o.qty / Math.max(1, o.lots)));
        return {
          ...o,
          brokerOrderId: patch.brokerOrderId ?? o.brokerOrderId,
          status: patch.status,
          // filledQty arrives in units; the ledger tracks lots.
          filledLots: patch.filledQty != null
            ? Math.min(o.lots, Math.round(patch.filledQty / lotSz))
            : o.filledLots,
          avgFill: patch.avgFill ?? o.avgFill,
        };
      }),
    })),
}));

// Re-export so callers that used lotSize via this module keep working.
export { lotSize };
