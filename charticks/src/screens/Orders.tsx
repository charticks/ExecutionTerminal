import { useState } from "react";
import {
  useOrdersStore,
  isWorking,
  type Order,
  type OrderStatus,
} from "@/stores/useOrdersStore";
import { limitPriceError } from "@/lib/orderValidation";
import { marketGate } from "@/lib/marketSession";

type Tab = "orders" | "trades";

const clock = (ts: number) =>
  new Date(ts).toLocaleTimeString("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });

const STATUS_CLASS: Record<OrderStatus, string> = {
  PENDING: "st-pending",
  OPEN: "st-pending",
  PARTIAL: "st-partial",
  EXECUTED: "st-exec",
  CANCELLED: "st-cancel",
  REJECTED: "st-reject",
};

const STATUS_LABEL: Record<OrderStatus, string> = {
  PENDING: "PENDING",
  OPEN: "PENDING",
  PARTIAL: "PARTIAL",
  EXECUTED: "EXECUTED",
  CANCELLED: "CANCELLED",
  REJECTED: "REJECTED",
};

/** One order-book row. Working (pending/partial) LIMIT orders expose a ✎ pencil
 *  to modify price + lots and a Cancel action; everything else is read-only. */
function OrderRow({ o }: { o: Order }) {
  const modifyOrder = useOrdersStore((s) => s.modifyOrder);
  const cancelOrder = useOrdersStore((s) => s.cancelOrder);
  const [editing, setEditing] = useState(false);
  const [priceDraft, setPriceDraft] = useState(String(o.price));
  const [lotsDraft, setLotsDraft] = useState(String(o.lots));

  const pending = isWorking(o);
  const editable = pending && o.orderType === "LIMIT";

  const startEdit = () => {
    setPriceDraft(o.price.toFixed(2));
    setLotsDraft(String(o.lots));
    setEditing(true);
  };
  const commit = () => {
    // Modifying an order is a trading action — blocked outside market hours.
    if (!marketGate(o.underlying)) return;
    const price = parseFloat(priceDraft);
    const lots = parseInt(lotsDraft, 10);
    const patch: { price?: number; lots?: number } = {};
    if (limitPriceError(price) === "") patch.price = +price.toFixed(2);
    if (!Number.isNaN(lots) && lots >= 1) patch.lots = lots;
    if (patch.price != null || patch.lots != null) modifyOrder(o.id, patch);
    setEditing(false);
  };

  return (
    <tr>
      <td className="num t-col">{clock(o.ts)}</td>
      <td className="inst-col">
        {o.underlying} {o.strike} {o.optType}
      </td>
      <td>
        <span className={`side ${o.side === "BUY" ? "b" : "s"}`}>
          {o.side === "BUY" ? "B" : "S"}
        </span>
      </td>
      <td>{o.orderType === "MARKET" ? "Market" : "Limit"}</td>
      <td className="c-num num">
        {editing ? (
          <input
            className="risk-input num"
            value={lotsDraft}
            inputMode="numeric"
            aria-label="Modify lots"
            onChange={(e) => setLotsDraft(e.target.value.replace(/[^\d]/g, ""))}
            onKeyDown={(e) => {
              if (e.key === "Enter") commit();
              if (e.key === "Escape") setEditing(false);
            }}
          />
        ) : (
          o.lots
        )}
      </td>
      <td className="c-num num">{o.qty}</td>
      <td className="c-num num">
        {editing ? (
          <input
            className="risk-input num"
            autoFocus
            value={priceDraft}
            inputMode="decimal"
            aria-label="Modify limit price"
            onChange={(e) => setPriceDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") commit();
              if (e.key === "Escape") setEditing(false);
            }}
          />
        ) : (
          o.price.toFixed(2)
        )}
      </td>
      <td>
        <span className={`ord-status ${STATUS_CLASS[o.status]}`}>
          {STATUS_LABEL[o.status]}
        </span>
      </td>
      <td className="c-num">
        {editing ? (
          <span className="wo-edit">
            <button className="wo-ok" title="Submit changes" onClick={commit}>✓</button>
            <button className="wo-cancel" title="Discard changes" onClick={() => setEditing(false)}>✕</button>
          </span>
        ) : (
          pending && (
            <span className="wo-actions">
              {editable && (
                <button className="risk-edit" title="Modify order" aria-label="Modify order" onClick={startEdit}>✎</button>
              )}
              <button
                className="closeb"
                title="Cancel order"
                onClick={() => { if (marketGate(o.underlying)) cancelOrder(o.id); }}
              >
                Cancel
              </button>
            </span>
          )
        )}
      </td>
    </tr>
  );
}

function OrderBook() {
  const orders = useOrdersStore((s) => s.orders);

  if (orders.length === 0) return <div className="empty">No orders yet</div>;

  return (
    <table className="ledger-grid">
      <thead>
        <tr>
          <th>Time</th>
          <th>Instrument</th>
          <th>Side</th>
          <th>Type</th>
          <th className="c-num">Lots</th>
          <th className="c-num">Qty</th>
          <th className="c-num">Price</th>
          <th>Status</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {orders.map((o: Order) => (
          <OrderRow key={o.id} o={o} />
        ))}
      </tbody>
    </table>
  );
}

function TradeBook() {
  const trades = useOrdersStore((s) => s.trades);

  if (trades.length === 0) return <div className="empty">No trades yet</div>;

  return (
    <table className="ledger-grid">
      <thead>
        <tr>
          <th>Time</th>
          <th>Instrument</th>
          <th>Side</th>
          <th className="c-num">Qty</th>
          <th className="c-num">Price</th>
        </tr>
      </thead>
      <tbody>
        {trades.map((t) => (
          <tr key={t.id}>
            <td className="num t-col">{clock(t.ts)}</td>
            <td className="inst-col">
              {t.underlying} {t.strike} {t.optType}
            </td>
            <td>
              <span className={`side ${t.side === "BUY" ? "b" : "s"}`}>
                {t.side === "BUY" ? "B" : "S"}
              </span>
            </td>
            <td className="c-num num">{t.qty}</td>
            <td className="c-num num">{t.price.toFixed(2)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Orders module: Order Book (all submitted orders + live status) and Trade Book
 *  (executed fills). Both update in real time as orders are placed and filled. */
export function Orders() {
  const [tab, setTab] = useState<Tab>("orders");
  const orderCount = useOrdersStore((s) => s.orders.length);
  const tradeCount = useOrdersStore((s) => s.trades.length);

  return (
    <div className="orders-screen">
      <section className="panel orders-panel">
        <div className="phead orders-head">
          <div className="orders-tabs" role="tablist">
            <button
              role="tab"
              aria-selected={tab === "orders"}
              className={tab === "orders" ? "on" : ""}
              onClick={() => setTab("orders")}
            >
              Order Book <span className="tab-count">{orderCount}</span>
            </button>
            <button
              role="tab"
              aria-selected={tab === "trades"}
              className={tab === "trades" ? "on" : ""}
              onClick={() => setTab("trades")}
            >
              Trade Book <span className="tab-count">{tradeCount}</span>
            </button>
          </div>
        </div>
        <div className="pbody">
          {tab === "orders" ? <OrderBook /> : <TradeBook />}
        </div>
      </section>
    </div>
  );
}
