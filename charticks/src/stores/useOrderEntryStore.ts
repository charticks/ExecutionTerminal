import { create } from "zustand";

// Option Chain execution controls: the session's Order Type and Lots. Applies to
// future orders only — existing positions/orders are never modified. Persisted so
// a trader's working quantity survives a reload; defaults are Market / 1 lot.

export type OrderType = "MARKET" | "LIMIT";

const KEY_TYPE = "ck.orderEntry.type";
const KEY_LOTS = "ck.orderEntry.lots";

function loadType(): OrderType {
  return localStorage.getItem(KEY_TYPE) === "LIMIT" ? "LIMIT" : "MARKET";
}

function loadLots(): number {
  const n = parseInt(localStorage.getItem(KEY_LOTS) ?? "", 10);
  return Number.isInteger(n) && n >= 1 ? n : 1;
}

interface OrderEntryState {
  orderType: OrderType;
  lots: number;
  setOrderType: (t: OrderType) => void;
  setLots: (n: number) => void;
}

export const useOrderEntryStore = create<OrderEntryState>((set) => ({
  orderType: loadType(),
  lots: loadLots(),
  setOrderType: (orderType) => {
    localStorage.setItem(KEY_TYPE, orderType);
    set({ orderType });
  },
  setLots: (lots) => {
    localStorage.setItem(KEY_LOTS, String(lots));
    set({ lots });
  },
}));
