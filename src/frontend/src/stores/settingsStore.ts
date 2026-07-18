import { create } from 'zustand';
import type { MemoryItem, MemoryProfile, MemoryGraphRelation } from '../types';
import {
  getMemories,
  deleteMemory,
  clearAllMemories,
  getMemorySettings,
  updateMemorySettings,
  updateMemoryWriteSettings,
  updateRerankerSettings,
  getMemoryProfile,
  getMemoryGraph,
} from '../api';
import { message } from 'antd';
import { t } from '../i18n';


async function _safeLoad<T>(
  set: (partial: Partial<SettingsState>) => void,
  fetcher: () => Promise<T>,
  apply: (data: T) => Partial<SettingsState>,
  errorMsg: string,
): Promise<void> {
  try {
    set(apply(await fetcher()));
  } catch {
    message.error(errorMsg);
  }
}

interface SettingsState {
  settingsOpen: boolean;
  memoryEnabled: boolean;
  memoryWriteEnabled: boolean;
  memoryItems: MemoryItem[];
  memoryPanelOpen: boolean;
  memoryLoading: boolean;
  rerankerEnabled: boolean;
  rerankerAvailable: boolean;
  /** Hint for rolling back a failed optimistic Switch update: which switch + the failure timestamp (the UI uses it as a shake-animation trigger) */
  lastToggleError: { key: 'memory' | 'memoryWrite'; ts: number } | null;

  // Layered memory
  memoryProfile: MemoryProfile | null;
  memoryGraph: MemoryGraphRelation[];
  memoryGraphEnabled: boolean;

  setSettingsOpen: (v: boolean) => void;
  setMemoryEnabled: (v: boolean) => void;
  setMemoryWriteEnabled: (v: boolean) => void;
  setMemoryItems: (items: MemoryItem[]) => void;
  setMemoryPanelOpen: (v: boolean) => void;
  setMemoryLoading: (v: boolean) => void;
  setRerankerEnabled: (v: boolean) => void;
  setRerankerAvailable: (v: boolean) => void;

  loadMemorySettings: () => Promise<void>;
  toggleMemory: (enabled: boolean) => Promise<void>;
  toggleMemoryWrite: (enabled: boolean) => Promise<void>;
  toggleReranker: (enabled: boolean) => Promise<void>;

  /** Load memory of all layers (for batch refresh of the tabbed panel) */
  loadMemoryAllLayers: () => Promise<void>;
  loadMemories: () => Promise<void>;
  loadMemoryProfile: () => Promise<void>;
  loadMemoryGraph: () => Promise<void>;

  removeMemory: (id: string) => Promise<void>;
  clearMemories: () => Promise<void>;
}

export const useSettingsStore = create<SettingsState>((set, get) => ({
  settingsOpen: false,
  memoryEnabled: localStorage.getItem('hugagent_memory_enabled') === 'true',
  memoryWriteEnabled: localStorage.getItem('hugagent_memory_write_enabled') === 'true',
  memoryItems: [],
  memoryPanelOpen: false,
  memoryLoading: false,
  rerankerEnabled: false,
  rerankerAvailable: false,
  lastToggleError: null,

  memoryProfile: null,
  memoryGraph: [],
  memoryGraphEnabled: false,

  setSettingsOpen: (v) => set({ settingsOpen: v }),
  setMemoryEnabled: (v) => {
    localStorage.setItem('hugagent_memory_enabled', String(v));
    set({ memoryEnabled: v });
  },
  setMemoryWriteEnabled: (v) => {
    localStorage.setItem('hugagent_memory_write_enabled', String(v));
    set({ memoryWriteEnabled: v });
  },
  setMemoryItems: (items) => set({ memoryItems: items }),
  setMemoryPanelOpen: (v) => set({ memoryPanelOpen: v }),
  setMemoryLoading: (v) => set({ memoryLoading: v }),
  setRerankerEnabled: (v) => set({ rerankerEnabled: v }),
  setRerankerAvailable: (v) => set({ rerankerAvailable: v }),

  loadMemorySettings: async () => {
    try {
      const settings = await getMemorySettings();
      set({
        memoryEnabled: settings.memory_enabled,
        memoryWriteEnabled: settings.memory_write_enabled,
        rerankerEnabled: settings.reranker_enabled,
        rerankerAvailable: settings.reranker_available,
      });
      localStorage.setItem('hugagent_memory_enabled', String(settings.memory_enabled));
      localStorage.setItem('hugagent_memory_write_enabled', String(settings.memory_write_enabled));
    } catch (e) {
      console.error('Failed to load memory settings:', e);
    }
  },

  toggleMemory: async (enabled) => {
    const prev = get().memoryEnabled;
    set({ memoryEnabled: enabled });
    localStorage.setItem('hugagent_memory_enabled', String(enabled));
    try {
      await updateMemorySettings(enabled);
    } catch {
      set({ memoryEnabled: prev, lastToggleError: { key: 'memory', ts: Date.now() } });
      localStorage.setItem('hugagent_memory_enabled', String(prev));
      message.error(t('记忆设置更新失败'));
    }
  },

  toggleMemoryWrite: async (enabled) => {
    const prev = get().memoryWriteEnabled;
    set({ memoryWriteEnabled: enabled });
    localStorage.setItem('hugagent_memory_write_enabled', String(enabled));
    try {
      await updateMemoryWriteSettings(enabled);
    } catch {
      set({ memoryWriteEnabled: prev, lastToggleError: { key: 'memoryWrite', ts: Date.now() } });
      localStorage.setItem('hugagent_memory_write_enabled', String(prev));
      message.error(t('写入记忆设置更新失败'));
    }
  },

  toggleReranker: async (enabled) => {
    const prev = get().rerankerEnabled;
    set({ rerankerEnabled: enabled });
    try {
      await updateRerankerSettings(enabled);
    } catch {
      set({ rerankerEnabled: prev });
      message.error(t('重排序设置更新失败'));
    }
  },

  loadMemories: () => _safeLoad(
    set, getMemories, (data) => ({ memoryItems: data.items }), t('加载事实记忆失败'),
  ),
  loadMemoryProfile: () => _safeLoad(
    set, getMemoryProfile, (data) => ({ memoryProfile: data }), t('加载档案记忆失败'),
  ),
  loadMemoryGraph: () => _safeLoad(
    set, () => getMemoryGraph(30),
    (data) => ({ memoryGraph: data.relations, memoryGraphEnabled: data.enabled }),
    t('加载图谱记忆失败'),
  ),

  loadMemoryAllLayers: async () => {
    set({ memoryLoading: true });
    try {
      await Promise.all([
        get().loadMemoryProfile(),
        get().loadMemories(),
        get().loadMemoryGraph(),
      ]);
    } finally {
      set({ memoryLoading: false });
    }
  },

  removeMemory: async (id) => {
    try {
      await deleteMemory(id);
      set((s) => ({ memoryItems: s.memoryItems.filter((m) => m.id !== id) }));
    } catch {
      message.error(t('删除记忆失败'));
    }
  },

  clearMemories: async () => {
    try {
      await clearAllMemories();
      set({ memoryItems: [] });
      message.success(t('已清除所有记忆'));
    } catch {
      message.error(t('清除记忆失败'));
    }
  },
}));
