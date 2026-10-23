/**
 * Zustand store template.
 *
 * Replace ${Feature}, ${feature} with actual names.
 * Create as stores/${feature}Store.ts and re-export it from stores/index.ts.
 *
 * Rules:
 *   - Server data comes from API functions (api.ts or src/api/<domain>.ts), never raw fetch here
 *   - Business data cached in localStorage MUST be keyed per account with userScopedKey(base, userId)
 *     (a null key means "not signed in yet": do not read or write)
 *   - Clear account-scoped state on logout / account switch (reset())
 */

import { create } from 'zustand';
import { loadJsonPref, removeLocal, saveJsonPref, userScopedKey } from '../storage';
// import { list${Feature}s, create${Feature}, delete${Feature} } from '../api/${feature}';

interface ${Feature}Item {
  id: string;
  name: string;
}

interface ${Feature}State {
  userId: string | null;
  items: ${Feature}Item[];
  loading: boolean;
  selectedId: string | null;
  error: string | null;

  bindUser: (userId: string | null) => void;
  selectItem: (id: string | null) => void;
  fetchItems: (signal?: AbortSignal) => Promise<void>;
  createItem: (name: string) => Promise<${Feature}Item>;
  deleteItem: (id: string) => Promise<void>;
  reset: () => void;
}

const STORAGE_BASE = 'hugagent_ui_${feature}_v1';

function persist(userId: string | null, items: ${Feature}Item[]) {
  const key = userScopedKey(STORAGE_BASE, userId);
  if (key) saveJsonPref(key, { items });
}

export const use${Feature}Store = create<${Feature}State>((set, get) => ({
  userId: null,
  items: [],
  loading: false,
  selectedId: null,
  error: null,

  bindUser: (userId) => {
    const key = userScopedKey(STORAGE_BASE, userId);
    const cached = key ? loadJsonPref<{ items: ${Feature}Item[] }>(key, { items: [] }) : { items: [] };
    set({ userId, items: cached.items, selectedId: null, error: null });
  },

  selectItem: (id) => set({ selectedId: id }),

  fetchItems: async (signal) => {
    set({ loading: true, error: null });
    try {
      const items: ${Feature}Item[] = []; // = await list${Feature}s(signal);
      void signal;
      set({ items });
      persist(get().userId, items);
    } catch (e) {
      if ((e as Error).name === 'AbortError') return;
      set({ error: (e as Error).message });
    } finally {
      set({ loading: false });
    }
  },

  createItem: async (name) => {
    const created: ${Feature}Item = { id: '', name }; // = await create${Feature}(name);
    const items = [created, ...get().items];
    set({ items });
    persist(get().userId, items);
    return created;
  },

  // Optimistic delete with rollback: the list stays truthful if the request fails.
  deleteItem: async (id) => {
    const previous = get().items;
    const items = previous.filter((item) => item.id !== id);
    set({ items, selectedId: get().selectedId === id ? null : get().selectedId });
    try {
      // await delete${Feature}(id);
      persist(get().userId, items);
    } catch (e) {
      set({ items: previous });
      throw e;
    }
  },

  reset: () => {
    const key = userScopedKey(STORAGE_BASE, get().userId);
    if (key) removeLocal(key);
    set({ userId: null, items: [], loading: false, selectedId: null, error: null });
  },
}));
