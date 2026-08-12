import { bridge } from "@/bridge/client";
import type { BridgeEvent, OrderUpdateStatus } from "@/bridge/events";
import { useOrdersStore, type OrderStatus } from "@/stores/useOrdersStore";
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

let started = false;

export function startLiveOrderSync() {
  if (started) return;
  started = true;

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
