import { create } from "zustand";

// User-added "custom" brokers — brokers not in the predefined angel/kotak/dhan
// set. These are UI + persistence only for now (no backend SDK integration),
// so they live entirely in the renderer, persisted to localStorage like the
// other ck.* stores. A broker account created against one stores this `key`
// string in its `broker` field.

const KEY = "ck.customBrokers";

export interface CustomBroker {
  key: string; // e.g. "custom:my-broker"
  label: string; // display name as the user typed it
}

function slugify(label: string): string {
  const s = label.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  return `custom:${s || "broker"}`;
}

function load(): CustomBroker[] {
  try {
    const raw = localStorage.getItem(KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr.filter((b) => b && b.key && b.label) : [];
  } catch {
    return [];
  }
}

function persist(list: CustomBroker[]) {
  try {
    localStorage.setItem(KEY, JSON.stringify(list));
  } catch {
    /* storage full / unavailable — in-memory copy still works this session */
  }
}

interface CustomBrokersState {
  brokers: CustomBroker[];
  /** Add a custom broker by display name. Returns the created broker, or an
   *  error string on validation failure (empty / duplicate). Duplicate check
   *  is case-insensitive and against the caller-supplied reserved labels
   *  (the predefined broker labels) plus existing custom labels. */
  add: (label: string, reservedLabels: string[]) => CustomBroker | { error: string };
  labelFor: (key: string) => string | undefined;
}

export const useCustomBrokersStore = create<CustomBrokersState>((set, get) => ({
  brokers: load(),
  add: (rawLabel, reservedLabels) => {
    const label = rawLabel.trim();
    if (!label) return { error: "Broker name cannot be empty." };
    const existing = get().brokers;
    const taken = new Set(
      [...reservedLabels, ...existing.map((b) => b.label)].map((l) => l.toLowerCase()),
    );
    if (taken.has(label.toLowerCase())) {
      return { error: "A broker with this name already exists." };
    }
    // Ensure a unique key even if two labels slugify identically.
    let key = slugify(label);
    const keys = new Set(existing.map((b) => b.key));
    let i = 2;
    while (keys.has(key)) key = `${slugify(label)}-${i++}`;
    const broker: CustomBroker = { key, label };
    const next = [...existing, broker];
    persist(next);
    set({ brokers: next });
    return broker;
  },
  labelFor: (key) => get().brokers.find((b) => b.key === key)?.label,
}));
