import { create } from "zustand";
import { lotSize } from "@/stores/usePositionsStore";
import { type RiskRule } from "@/lib/risk";
import { useTradingModeStore } from "@/stores/useTradingModeStore";
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
}

export interface PlaceResult {
  ok: boolean;
  error?: string;
  /** Machine-readable rejection reason from the sidecar — "MARKET_CLOSED",
   *  "DUPLICATE_PENDING" or "PARTIAL_FILL". Mirrors the sidecar contract. */
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
    const body = {
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
    };

    // LIVE: record a PENDING order for visibility, then reconcile on response.
    if (useTradingModeStore.getState().mode === "live") {
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
        get().updateStatus(oid, res.ok ? "EXECUTED" : "REJECTED");
        return res;
      } catch (e) {
        get().updateStatus(oid, "REJECTED");
        return { ok: false, error: String(e) };
      }
    }

    // PAPER: the engine validates, fills (market) or rests (limit), and pushes
    // the resulting book via `paper_state`. No client-side order/position here.
    try {
      return await bridge.post<PlaceResult>("/orders/place", body);
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  },

  modifyOrder: (id, patch) => {
    bridge.post("/orders/modify", { id, ...patch }).catch(() => {});
  },

  cancelOrder: (id) => {
    bridge.post("/orders/cancel", { id }).catch(() => {});
  },

  updateStatus: (id, status) =>
    set((s) => ({ orders: s.orders.map((o) => (o.id === id ? { ...o, status } : o)) })),
}));

// Re-export so callers that used lotSize via this module keep working.
export { lotSize };
