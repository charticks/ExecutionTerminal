import { create } from "zustand";
import { DEFAULT_VISIBLE, INDEX_BY_ID, MAX_VISIBLE_INDICES } from "@/lib/indices";

// Which index cards show on the dashboard, and in what order. Persisted.

const KEY = "ck.indices.visible";

function load(): string[] {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) {
      const ids = (JSON.parse(raw) as string[]).filter((id) => id in INDEX_BY_ID);
      if (ids.length) return ids.slice(0, MAX_VISIBLE_INDICES);
    }
  } catch {
    /* fall through to defaults */
  }
  return [...DEFAULT_VISIBLE];
}

function persist(ids: string[]) {
  localStorage.setItem(KEY, JSON.stringify(ids));
}

interface IndicesState {
  visible: string[];
  /** Returns false when the 8-card limit is hit (caller shows the message). */
  add: (id: string) => boolean;
  remove: (id: string) => void;
  /** Add if hidden, remove if visible. Returns false only when adding hit the
   *  8-card limit (caller shows the message); removing always returns true. */
  toggle: (id: string) => boolean;
  move: (from: number, to: number) => void;
}

export const useIndicesStore = create<IndicesState>((set, get) => ({
  visible: load(),
  add: (id) => {
    const v = get().visible;
    if (v.includes(id)) return true;
    if (v.length >= MAX_VISIBLE_INDICES) return false;
    const visible = [...v, id];
    persist(visible);
    set({ visible });
    return true;
  },
  remove: (id) => {
    const visible = get().visible.filter((x) => x !== id);
    persist(visible);
    set({ visible });
  },
  toggle: (id) => {
    if (get().visible.includes(id)) {
      get().remove(id);
      return true;
    }
    return get().add(id);
  },
  move: (from, to) => {
    const visible = [...get().visible];
    if (from < 0 || from >= visible.length || to < 0 || to >= visible.length) return;
    const [x] = visible.splice(from, 1);
    visible.splice(to, 0, x);
    persist(visible);
    set({ visible });
  },
}));
