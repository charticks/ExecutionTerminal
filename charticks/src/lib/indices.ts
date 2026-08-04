// Catalog of supported indices (Phase 1 — mock data; sidecar will feed real quotes later).
// `step` = option strike interval, `lot` = option lot size, `spot` = mock base price.

export interface IndexDef {
  id: string; // stable key, used in stores / persistence
  name: string; // display name
  spot: number;
  step: number;
  lot: number;
  hasOptions: boolean; // appears in the Option Chain index dropdown
}

export const INDICES: IndexDef[] = [
  { id: "NIFTY", name: "NIFTY 50", spot: 25348.6, step: 50, lot: 65, hasOptions: true },
  { id: "BANKNIFTY", name: "BANKNIFTY", spot: 57210.4, step: 100, lot: 30, hasOptions: true },
  { id: "FINNIFTY", name: "FINNIFTY", spot: 26841.3, step: 50, lot: 60, hasOptions: true },
  { id: "MIDCPNIFTY", name: "MIDCPNIFTY", spot: 13092.7, step: 25, lot: 140, hasOptions: true },
  { id: "SENSEX", name: "SENSEX", spot: 83125.2, step: 100, lot: 20, hasOptions: true },
  { id: "BANKEX", name: "BANKEX", spot: 64310.9, step: 100, lot: 30, hasOptions: false },
  // MCX commodity, not an index: the "spot" it quotes is the front-month
  // CRUDEOIL future (see BrokerManager._nearest_crude_future). lot 100 matches
  // the contract's lotsize in the Angel instrument master.
  { id: "CRUDEOIL", name: "CRUDE OIL", spot: 5680.0, step: 50, lot: 100, hasOptions: true },
  { id: "NIFTYIT", name: "NIFTY IT", spot: 39880.5, step: 50, lot: 0, hasOptions: false },
  { id: "NIFTYAUTO", name: "NIFTY AUTO", spot: 26410.3, step: 50, lot: 0, hasOptions: false },
  { id: "NIFTYPHARMA", name: "NIFTY PHARMA", spot: 22960.8, step: 50, lot: 0, hasOptions: false },
  { id: "NIFTYFMCG", name: "NIFTY FMCG", spot: 61240.1, step: 50, lot: 0, hasOptions: false },
  { id: "NIFTYMETAL", name: "NIFTY METAL", spot: 10480.6, step: 25, lot: 0, hasOptions: false },
  { id: "NIFTYENERGY", name: "NIFTY ENERGY", spot: 42115.7, step: 50, lot: 0, hasOptions: false },
  { id: "NIFTYREALTY", name: "NIFTY REALTY", spot: 1092.4, step: 5, lot: 0, hasOptions: false },
  { id: "INDIAVIX", name: "INDIA VIX", spot: 13.42, step: 0, lot: 0, hasOptions: false },
];

export const INDEX_BY_ID: Record<string, IndexDef> = Object.fromEntries(
  INDICES.map((i) => [i.id, i]),
);

export const OPTION_INDICES = INDICES.filter((i) => i.hasOptions);

export const MAX_VISIBLE_INDICES = 8;
export const DEFAULT_VISIBLE = ["NIFTY", "BANKNIFTY", "SENSEX"];

/** Next `n` weekly expiries (Thursdays) as display strings, e.g. "23 Jul". */
export function mockExpiries(n = 4): string[] {
  const out: string[] = [];
  const d = new Date();
  while (out.length < n) {
    d.setDate(d.getDate() + 1);
    if (d.getDay() === 4) {
      out.push(d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" }));
    }
  }
  return out;
}
