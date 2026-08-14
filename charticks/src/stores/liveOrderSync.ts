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
  /** Set when the order closes a position — see TrackedOrder.exit_for. */
  exitFor?: string;
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
      origin: r.exitFor ? "exit" : undefined,
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

    if (!existing) {
      // An order the renderer did not place. This used to `return` — and that is
      // why the Order Book only ever showed entries.
      //
      // Every order the SIDECAR originates arrives this way and no other: a
      // stop-loss exit, a target exit, a trailing-stop exit, a portfolio-trail
      // square-off, a manual close, a partial exit, the second leg of a roll,
      // an auto-hedge. None of them has a local row to match, so all of them
      // were silently discarded, and the Order Book contradicted the Positions
      // tab it was supposed to explain.
      //
      // The Order Book is meant to be every broker order and its lifecycle, so
      // the row is CREATED from the event. This needs the structured contract,
      // which is why order_update now carries it.
      const created = orderFromEvent(e, status);
      if (created) store.addBrokerOrder(created);
      return;
    }

    store.applyBrokerUpdate(existing.id, {
      brokerOrderId: e.id,
      status,
      filledQty: e.filledQty ?? e.qty,
      avgFill: (e.avgPrice ?? e.price) > 0 ? (e.avgPrice ?? e.price) : undefined,
    });
  });
}

/** Build an Order row from an order_update the renderer did not originate.
 *
 *  Returns null when the event carries no structured contract — an older
 *  sidecar, or a row we genuinely cannot place in the book. Inventing a strike
 *  by parsing the display symbol is exactly what the structured fields replaced.
 */
function orderFromEvent(
  e: Extract<BridgeEvent, { type: "order_update" }>,
  status: OrderStatus,
): Order | null {
  if (!e.underlying || !e.optType || e.strike == null) return null;
  const lot = e.lotSize && e.lotSize > 0 ? e.lotSize : 1;
  const qty = e.requestedQty ?? e.qty;
  return {
    id: e.id,
    brokerOrderId: e.id,
    ts: e.ts,
    underlying: e.underlying,
    expiry: e.expiry,
    strike: e.strike,
    optType: e.optType,
    side: e.side,
    orderType: (e.orderType as OrderType) ?? "MARKET",
    lots: Math.max(1, Math.round(qty / lot)),
    filledLots: Math.round((e.filledQty ?? 0) / lot),
    qty,
    price: e.limitPrice ?? 0,
    status,
    avgFill: (e.avgPrice ?? 0) > 0 ? e.avgPrice : undefined,
    // Why this order exists. An exit that says so reads as the close of a
    // position rather than as an unexplained sell nobody remembers placing.
    origin: e.exitFor ? "exit" : "engine",
  };
}
