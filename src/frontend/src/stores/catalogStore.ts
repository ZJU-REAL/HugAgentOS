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

/**
 * 「当前停在哪个面板」是**每个标签页各自的视图状态**，不是跨标签共享的用户偏好，
 * 所以存 sessionStorage 而不是 localStorage。
 *
 * 原来存 localStorage 会串台（问题 25）：A 窗口停在历史对话、B 窗口切到站点，
 * B 一写就把共享的 key 改成了 'sites'，此时刷新 A，A 也跳去站点——两个窗口互相干扰。
 * sessionStorage 同样能扛住「刷新页面后回到原面板」这个本来的诉求（它随标签页存活、
 * 刷新不丢），但每个标签页一份，互不影响。
 *
 * 顺带把能力中心停在哪个二级 Tab 也一并记住：以前只记面板不记 Tab，在「能力中心 →
 * 插件」按刷新会掉回子智能体。
 */
const ABILITY_TAB_STORAGE_KEY = 'hugagent_ability_tab';
const VALID_ABILITY_TABS: readonly AbilityTabKey[] = ['agents', 'skills', 'mcp', 'plugins'];

function readSession(key: string): string | null {
  if (typeof window === 'undefined') return null;
  try { return window.sessionStorage.getItem(key); } catch { return null; }
}

function writeSession(key: string, value: string) {
  if (typeof window === 'undefined') return;
  try { window.sessionStorage.setItem(key, value); } catch { /* sessionStorage 不可用 */ }
}

function loadActivePanel(): PanelKey {
  if (typeof window === 'undefined') return 'chat';
  // On a fresh login (SSO ticket exchange), force the user to land on home.
  // On a plain browser refresh the flag is absent, so we restore the last panel.
  if (readSession(LOGIN_LANDING_KEY) === '1') return 'chat';
  const saved = readSession(PANEL_STORAGE_KEY);
  if (saved && (VALID_PANELS as readonly string[]).includes(saved)) {
    return saved as PanelKey;
  }
  // 迁移：老版本把它写在 localStorage 里，读一次让本次刷新不至于突然跳回首页，
  // 读完就清掉，之后一律走 sessionStorage。
  try {
    const legacy = window.localStorage.getItem(PANEL_STORAGE_KEY);
    window.localStorage.removeItem(PANEL_STORAGE_KEY);
    if (legacy && (VALID_PANELS as readonly string[]).includes(legacy)) {
      writeSession(PANEL_STORAGE_KEY, legacy);
      return legacy as PanelKey;
    }
  } catch { /* localStorage unavailable */ }
  return 'chat';
}

function saveActivePanel(panel: PanelKey) {
  writeSession(PANEL_STORAGE_KEY, panel);
}

function loadAbilityTab(): AbilityTabKey {
  const saved = readSession(ABILITY_TAB_STORAGE_KEY);
  return saved && (VALID_ABILITY_TABS as readonly string[]).includes(saved)
    ? (saved as AbilityTabKey)
    : 'agents';
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
  abilityTab: loadAbilityTab(),
  // 恢复出来的 Tab 也要算作「已访问」，否则刷新后能力中心不会挂载它对应的 pane、一片空白
  visitedAbilityTabs: [loadAbilityTab()],

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
  setAbilityTab: (tab) => set((state) => {
    // 记住当前 Tab，刷新后能回到同一个二级页（问题 25）
    writeSession(ABILITY_TAB_STORAGE_KEY, tab);
    return {
      abilityTab: tab,
      manageQuery: '',
      visitedAbilityTabs: state.visitedAbilityTabs.includes(tab)
        ? state.visitedAbilityTabs
        : [...state.visitedAbilityTabs, tab],
    };
  }),

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
