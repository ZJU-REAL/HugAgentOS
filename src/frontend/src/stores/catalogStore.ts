import { create } from 'zustand';
import type { Catalog, KbTabKey, PanelKey } from '../types';
import { getCatalog, updateCatalogItem } from '../api';
import { defaultCatalog } from '../storage';
import { navigateTo, pathForPanel } from '../routing/navigation';
import { useDeploymentModeStore } from './deploymentModeStore';
import { pathForKbTab } from '../routing/subPages';

/**
 * 「当前停在哪个面板 / 哪个二级页」一律由地址栏决定（`/my-space/kb/public`、
 * `/ability-center/connectors` …），store 与浏览器存储里都不再留第二份。
 * 组件用 usePanel / useAbilityTab / useMySpaceTab / useKbTab 从地址实时算。
 */

interface CatalogState {
  catalog: Catalog;
  catalogLoading: boolean;
  /** 每进入一次顶层面板 +1。它不是「当前在哪个面板」的副本，而是「又进了一次」这个动作，
   *  各页据此重放入场动画 / 重新拉列表。 */
  panelEntryNonce: number;
  /** Search query within catalog management */
  manageQuery: string;
  /** Selected catalog item id */
  selectedId: string | null;

  setCatalog: (catalog: Catalog) => void;
  setCatalogLoading: (v: boolean) => void;
  /** 切换面板 = 跳到该面板的地址；`sub` 是要直接落到的二级页（如能力中心的类别）。
   *  二级页必须随这一次跳转一起给出——先跳类别再跳面板会把类别冲掉。 */
  setPanel: (panel: PanelKey, sub?: string) => void;
  setManageQuery: (query: string) => void;
  setSelectedId: (id: string | null) => void;
  setKbTab: (tab: KbTabKey) => void;

  /** Fetch catalog from backend (the only source of enabled state). */
  fetchCatalog: () => Promise<void>;
  /** Toggle item enabled/disabled (optimistic update + backend sync) */
  toggleItem: (kind: 'skills' | 'agents' | 'mcp' | 'kb', itemId: string, enabled: boolean) => Promise<void>;
}

export const useCatalogStore = create<CatalogState>((set, get) => ({
  // 首屏占位：只有形状，没有启停状态。启停的真源是后端，浏览器里不留第二份——
  // 留了就会出现「界面显示的和实际生效的不是同一份」。
  catalog: structuredClone(defaultCatalog),
  catalogLoading: true,
  panelEntryNonce: 0,
  manageQuery: '',
  selectedId: null,

  setCatalog: (catalog) => set({ catalog }),
  setCatalogLoading: (v) => set({ catalogLoading: v }),
  setPanel: (panel, sub) => {
    set((state) => ({
      panelEntryNonce: state.panelEntryNonce + 1,
      selectedId: null,
      manageQuery: '',
    }));
    navigateTo(pathForPanel(panel, sub));
  },
  setManageQuery: (query) => set({ manageQuery: query }),
  setSelectedId: (id) => set({ selectedId: id }),
  setKbTab: (tab) => {
    set({ manageQuery: '' });
    navigateTo(pathForKbTab(tab));
  },

  fetchCatalog: async () => {
    try {
      set({ catalogLoading: true });
      set({ catalog: await getCatalog(), catalogLoading: false });
    } catch (e) {
      console.error('Failed to fetch catalog:', e);
      set({ catalogLoading: false });
    }
  },

  toggleItem: async (kind, itemId, enabled) => {
    const previous = get().catalog;
    const apply = (value: boolean) => ({
      ...get().catalog,
      [kind]: get().catalog[kind].map((item) =>
        item.id === itemId ? { ...item, enabled: value } : item,
      ),
    });
    set({ catalog: apply(enabled) });
    try {
      await updateCatalogItem(kind, itemId, enabled);
    } catch (e) {
      // 后端没写成就把开关拨回去：开关显示的必须是真正生效的那个状态。
      console.error('Failed to sync catalog toggle:', e);
      set({ catalog: previous });
      throw e;
    }
  },
}));

// 首轮能力同步落完的那一刻，能力目录的来源从云端切到本机（见 api.ts 的
// capabilityTargetHeaders）。重新拉一次，界面显示的才是真正生效的那份。
useDeploymentModeStore.subscribe((next, previous) => {
  if (next.capabilitiesReady && !previous.capabilitiesReady) void useCatalogStore.getState().fetchCatalog();
});
