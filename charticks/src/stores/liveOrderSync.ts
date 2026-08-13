import { bridge } from "@/bridge/client";
import type { BridgeEvent, OrderUpdateStatus } from "@/bridge/events";
import {
  useOrdersStore,
  type Order,
  type OrderStatus,
  type OrderType,
} from "@/stores/useOrdersStore";
import { useTradingModeStore } from "@/stores/useTradingModeStore";

// Applies live order state from the sidecar's Order Synchronization Engine.
//
// Previously the renderer marked a live order EXECUTED the moment placeOrder
// resolved — i.e. the moment the broker returned an order id. An order id means
// the request was accepted for routing; it can still be rejected at the
// exchange, rest unfilled, or fill in parts. `order_update` events now carry the
// broker's real state and are the only thing that advances a live order past
// PENDING.
//
// Paper is untouched: its book arrives as `paper_state` snapshots (paperSync).

/** Map the sidecar's canonical lifecycle onto the ledger's status vocabulary.
 *  Several broker states collapse to one ledger state — the precise broker
 *  status is in orders.log, and the ledger only needs to drive the UI. */
const STATUS: Record<OrderUpdateStatus, OrderStatus> = {
  NEW: "PENDING",
  SUBMITTED: "PENDING",
  ACCEPTED: "PENDING",
  PENDING: "OPEN",
  PARTIAL: "PARTIAL",
  FILLED: "EXECUTED",
  COMPLETE: "EXECUTED",
  REJECTED: "REJECTED",
  CANCELLED: "CANCELLED",
  EXPIRED: "CANCELLED",
};

/** One row of GET /orders/sync — the Order Synchronization Engine's snapshot. */
interface SyncRow {
  orderId: string;
  underlying: string;
  expiry?: string;
  strike: number;
  optType: "CE" | "PE";
  side: "BUY" | "SELL";
  qty: number;
  filledQty: number;
  lotSize: number;
  price: number;
  avgPrice: number;
  orderType: OrderType;
  status: OrderUpdateStatus;
  ts: number;
}

/** Repaint the live order book from the sidecar.
 *
 *  Needed because `order_update` events are DELTAS. Without an initial read the
 *  live order book was empty on every launch and after every reload until the
 *  next event happened to arrive — so a working live order was invisible, and
 *  the page looked broken on a fresh start.
 *
 *  The Order Synchronization Engine is the authority on live order state, so
 *  this replaces the mirror wholesale rather than merging into it.
 */
export async function paintLiveBook() {
  if (useTradingModeStore.getState().mode !== "live") return;
  let snapshot: { orders?: SyncRow[] };
  try {
    snapshot = await bridge.get<{ orders?: SyncRow[] }>("/orders/sync");
  } catch {
    // Sidecar not up yet. bridge.onStatus below repaints on connect, so there is
    // nothing to recover here.
    return;
  }
  // A late reply must not overwrite a book the user has since switched away from.
  if (useTradingModeStore.getState().mode !== "live") return;
  const rows = snapshot.orders ?? [];
  const orders = rows.map((r): Order => {
    const lot = r.lotSize > 0 ? r.lotSize : 1;
    return {
      id: r.orderId,
      brokerOrderId: r.orderId,
      ts: r.ts,
      underlying: r.underlying,
      expiry: r.expiry,
      strike: r.strike,
      optType: r.optType,
      side: r.side,
      orderType: r.orderType,
      lots: Math.max(1, Math.round(r.qty / lot)),
      filledLots: Math.round((r.filledQty || 0) / lot),
      qty: r.qty,
      price: r.price,
      status: STATUS[r.status] ?? "PENDING",
      avgFill: r.avgPrice > 0 ? r.avgPrice : undefined,
    };
  });
  // Trades are not part of this snapshot: the live trade book is built from
  // confirmed fills by the position book, so inventing rows here would show
  // trades the broker never reported.
  useOrdersStore.getState().setFromSnapshot(orders, useOrdersStore.getState().trades);
}

let started = false;

export function startLiveOrderSync() {
  if (started) return;
  started = true;

  // Paint now, on every (re)connect, and whenever the user switches into Live.
  void paintLiveBook();
  bridge.onStatus((connected) => {
    if (connected) void paintLiveBook();
  });
  let lastMode = useTradingModeStore.getState().mode;
  useTradingModeStore.subscribe((s) => {
    if (s.mode === lastMode) return;
    lastMode = s.mode;
    if (s.mode === "live") void paintLiveBook();
  });

  bridge.on((e: BridgeEvent) => {
    if (e.type !== "order_update") return;
    // Paper's own book is authoritative in paper mode; ignoring these there
    // keeps the two engines from writing to the same ledger.
    if (useTradingModeStore.getState().mode !== "live") return;

    const status = STATUS[e.status];
    if (!status) return; // unknown state — leave the row as it is

    const store = useOrdersStore.getState();
    // Match on the broker's order id, falling back to the instrument+side row
    // this session created: placeOrder records a local id before the broker's
    // is known, so the first update has to adopt it.
    const existing =
      store.orders.find((o) => o.brokerOrderId === e.id) ??
      store.orders.find(
        (o) => !o.brokerOrderId && o.status === "PENDING" && o.side === e.side,
      );
    if (!existing) return;

    store.applyBrokerUpdate(existing.id, {
      brokerOrderId: e.id,
      status,
      filledQty: e.qty,
      avgFill: e.price > 0 ? e.price : undefined,
    });
  });
}
