import { create } from 'zustand';
import type { AbilityTabKey, Catalog, PanelKey } from '../types';
import { getCatalog, updateCatalogItem } from '../api';
import { loadCatalog, saveCatalog } from '../storage';

const PANEL_STORAGE_KEY = 'hugagent_active_panel';
// Keep in sync with authStore.LOGIN_LANDING_KEY (inlined to avoid a circular import).
const LOGIN_LANDING_KEY = 'hugagent_login_landing';

// 可以从 localStorage 恢复的面板。'skills' / 'agents' / 'mcp' 已并入能力中心的二级导航、
// 不再是独立面板，故不在此列——残留的旧值会退回 'chat'。
const VALID_PANELS: readonly PanelKey[] = [
  'chat', 'kb', 'docs',
  'app_center', 'settings', 'share_records', 'my_space',
  'ability_center', 'lab', 'projects', 'project_detail',
  'automation', 'sites',
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
  /** 能力中心当前展开的能力类别——由侧边栏的二级导航项驱动，页面本身不再有 Tab 栏 */
  abilityTab: AbilityTabKey;
  /** 本次会话中访问过的能力类别。能力中心据此只挂载访问过的 pane——四个 pane 各自会在挂载时
   *  拉一份列表，全挂等于一次打出四份请求。累积在这里而不是页面内部：切换动作发生在侧边栏，
   *  放在 setAbilityTab 里是唯一不需要靠 effect 追赶的位置。 */
  visitedAbilityTabs: readonly AbilityTabKey[];

  setCatalog: (catalog: Catalog) => void;
  setCatalogLoading: (v: boolean) => void;
  setPanel: (panel: PanelKey) => void;
  setManageQuery: (query: string) => void;
  setSelectedId: (id: string | null) => void;
  setAbilityTab: (tab: AbilityTabKey) => void;

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
  abilityTab: 'agents',
  visitedAbilityTabs: ['agents'],

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
  setAbilityTab: (tab) => set((state) => ({
    abilityTab: tab,
    manageQuery: '',
    visitedAbilityTabs: state.visitedAbilityTabs.includes(tab)
      ? state.visitedAbilityTabs
      : [...state.visitedAbilityTabs, tab],
  })),

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
