import { create } from 'zustand';
import type { Catalog, PanelKey } from '../types';
import { getCatalog, updateCatalogItem } from '../api';
import { loadCatalog, saveCatalog } from '../storage';

const PANEL_STORAGE_KEY = 'hugagent_active_panel';
// Keep in sync with authStore.LOGIN_LANDING_KEY (inlined to avoid a circular import).
const LOGIN_LANDING_KEY = 'hugagent_login_landing';

const VALID_PANELS: readonly PanelKey[] = [
  'chat', 'skills', 'agents', 'mcp', 'kb', 'docs',
  'app_center', 'settings', 'share_records', 'my_space',
  'ability_center', 'lab', 'projects', 'project_detail',
] as const;

function loadActivePanel(): PanelKey {
  if (typeof window === 'undefined') return 'chat';
  // On a fresh login (SSO ticket exchange), force the user to land on home.
  // On a plain browser refresh the flag is absent, so we restore the last panel.
  try {
    if (window.sessionStorage.getItem(LOGIN_LANDING_KEY) === '1') return 'chat';
  } catch { /* sessionStorage unavailable */ }
  try {
    const saved = window.localStorage.getItem(PANEL_STORAGE_KEY);
    if (saved && (VALID_PANELS as readonly string[]).includes(saved)) {
      return saved as PanelKey;
    }
  } catch { /* localStorage unavailable */ }
  return 'chat';
}

function saveActivePanel(panel: PanelKey) {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(PANEL_STORAGE_KEY, panel);
}

interface CatalogState {
  catalog: Catalog;
  catalogLoading: boolean;
  /** Current panel view */
  panel: PanelKey;
  /** Incremented whenever a top-level panel is entered */
  panelEntryNonce: number;
  /** Search query within catalog management */
  manageQuery: string;
  /** Selected catalog item id */
  selectedId: string | null;

  setCatalog: (catalog: Catalog) => void;
  setCatalogLoading: (v: boolean) => void;
  setPanel: (panel: PanelKey) => void;
  setManageQuery: (query: string) => void;
  setSelectedId: (id: string | null) => void;

  /** Fetch catalog from backend, merge with localStorage enabled state */
  fetchCatalog: () => Promise<void>;
  /** Toggle item enabled/disabled (optimistic update + backend sync) */
  toggleItem: (kind: 'skills' | 'agents' | 'mcp' | 'kb', itemId: string, enabled: boolean) => Promise<void>;
}

export const useCatalogStore = create<CatalogState>((set, get) => ({
  catalog: loadCatalog(),
  catalogLoading: true,
  panel: loadActivePanel(),
  panelEntryNonce: 0,
  manageQuery: '',
  selectedId: null,

  setCatalog: (catalog) => {
    set({ catalog });
    saveCatalog(catalog);
  },
  setCatalogLoading: (v) => set({ catalogLoading: v }),
  setPanel: (panel) => {
    saveActivePanel(panel);
    set((state) => ({
      panel,
      panelEntryNonce: state.panelEntryNonce + 1,
      selectedId: null,
      manageQuery: '',
    }));
  },
  setManageQuery: (query) => set({ manageQuery: query }),
  setSelectedId: (id) => set({ selectedId: id }),

  fetchCatalog: async () => {
    try {
      set({ catalogLoading: true });
      const remote = await getCatalog();
      // Merge local enabled states onto remote catalog
      const local = loadCatalog();
      const mergeEnabled = <T extends { id: string; enabled: boolean }>(
        remoteItems: T[],
        localItems: { id: string; enabled: boolean }[],
      ): T[] => {
        const localMap = new Map(localItems.map((i) => [i.id, i.enabled]));
        return remoteItems.map((item) => ({
          ...item,
          enabled: localMap.has(item.id) ? localMap.get(item.id)! : item.enabled,
        }));
      };
      const merged: Catalog = {
        skills: mergeEnabled(remote.skills, local.skills),
        agents: mergeEnabled(remote.agents, local.agents),
        mcp: mergeEnabled(remote.mcp, local.mcp),
        kb: mergeEnabled(remote.kb, local.kb),
      };
      set({ catalog: merged, catalogLoading: false });
      saveCatalog(merged);
    } catch (e) {
      console.error('Failed to fetch catalog:', e);
      set({ catalogLoading: false });
    }
  },

  toggleItem: async (kind, itemId, enabled) => {
    const { catalog } = get();
    // Optimistic update
    const updated = {
      ...catalog,
      [kind]: catalog[kind].map((item) =>
        item.id === itemId ? { ...item, enabled } : item,
      ),
    };
    set({ catalog: updated });
    saveCatalog(updated);
    // Sync to backend
    try {
      await updateCatalogItem(kind, itemId, enabled);
    } catch (e) {
      console.error('Failed to sync catalog toggle:', e);
    }
  },
}));
