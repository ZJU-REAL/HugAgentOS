/**
 * Zustand store template.
 *
 * Replace ${Feature}, ${feature} with actual names.
 * Create as stores/${feature}Store.ts
 */

import { create } from 'zustand';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ${Feature}Item {
  id: string;
  name: string;
  // ... add fields
}

// ---------------------------------------------------------------------------
// State Interface (state + actions)
// ---------------------------------------------------------------------------

interface ${Feature}State {
  // --- State ---
  items: ${Feature}Item[];
  loading: boolean;
  selectedId: string | null;
  error: string | null;

  // --- Actions ---
  setItems: (items: ${Feature}Item[]) => void;
  setLoading: (loading: boolean) => void;
  selectItem: (id: string | null) => void;
  fetchItems: () => Promise<void>;
  createItem: (data: Partial<${Feature}Item>) => Promise<void>;
  deleteItem: (id: string) => Promise<void>;
  reset: () => void;
}

// ---------------------------------------------------------------------------
// localStorage persistence (optional)
// ---------------------------------------------------------------------------

const STORAGE_KEY = 'hugagent_ui_${feature}_v1';

function loadFromStorage(): ${Feature}Item[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function saveToStorage(items: ${Feature}Item[]) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const use${Feature}Store = create<${Feature}State>((set, get) => ({
  // --- Initial state ---
  items: loadFromStorage(),
  loading: false,
  selectedId: null,
  error: null,

  // --- Simple setters ---
  setItems: (items) => {
    set({ items });
    saveToStorage(items);
  },
  setLoading: (loading) => set({ loading }),
  selectItem: (id) => set({ selectedId: id }),

  // --- Async: fetch ---
  fetchItems: async () => {
    set({ loading: true, error: null });
    try {
      // const r = await authFetch(`${apiUrl}/v1/${feature}s`);
      // const { data } = await r.json();
      // set({ items: data.items });
      // saveToStorage(data.items);
    } catch (e) {
      set({ error: (e as Error).message });
      console.error('Failed to fetch ${feature}s:', e);
    } finally {
      set({ loading: false });
    }
  },

  // --- Async: create ---
  createItem: async (data) => {
    try {
      // const r = await authFetch(`${apiUrl}/v1/${feature}s`, {
      //   method: 'POST',
      //   headers: { 'Content-Type': 'application/json' },
      //   body: JSON.stringify(data),
      // });
      // const { data: created } = await r.json();
      // const items = [...get().items, created];
      // set({ items });
      // saveToStorage(items);
    } catch (e) {
      console.error('Failed to create ${feature}:', e);
      throw e;
    }
  },

  // --- Async: delete ---
  deleteItem: async (id) => {
    try {
      // await authFetch(`${apiUrl}/v1/${feature}s/${id}`, { method: 'DELETE' });
      const items = get().items.filter((i) => i.id !== id);
      set({ items, selectedId: get().selectedId === id ? null : get().selectedId });
      saveToStorage(items);
    } catch (e) {
      console.error('Failed to delete ${feature}:', e);
      throw e;
    }
  },

  // --- Reset ---
  reset: () => {
    set({ items: [], loading: false, selectedId: null, error: null });
    localStorage.removeItem(STORAGE_KEY);
  },
}));
