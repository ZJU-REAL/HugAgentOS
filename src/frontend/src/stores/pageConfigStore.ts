import { create } from 'zustand';
import { DEFAULT_PAGE_CONFIG, mergePageConfig, type PageConfig } from '../utils/pageConfigDefaults';

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string) || '/api';

export interface AppItem {
  id: string;
  enabled: boolean;
  name: string;
  description: string;
  url: string;
  icon?: string;
}

export interface AppConfig {
  apps: AppItem[];
}

/**
 * Built-in apps (not in app_config, but participate in user app-permission filtering).
 */
export const BUILTIN_APPS: AppItem[] = [
  {
    id: 'plan_mode',
    enabled: true,
    name: '计划模式',
    description:
      '描述复杂任务，AI 自动分解为多步骤并逐步执行，适用于数据分析、报告生成、政策解读等场景',
    url: '',
    icon: '/home/random-icons/Frame 450.svg',
  },
  {
    id: 'automation',
    enabled: true,
    name: '自动化',
    description:
      '设置定时或周期性 AI 任务，支持自然语言提示词和计划模式的自动执行，适用于定期报告、数据监控等场景',
    url: '',
    icon: '/home/new-icons/automation.svg',
  },
  {
    id: 'batch_runner',
    enabled: true,
    name: '批量执行',
    description:
      '对一组对象（Excel 行 / 多份文档 / 文本枚举）批量执行同一任务，AI 自动生成可确认的执行计划并逐条处理',
    url: '',
    icon: '/home/random-icons/Frame 460.svg',
  },
];

export interface HomepageShortcut {
  id: string;
  enabled: boolean;
  label: string;
  icon: string;
  url: string;
}

export const DEFAULT_HOMEPAGE_SHORTCUTS: HomepageShortcut[] = [
  { id: 'knowledge', enabled: true, label: '知识检索', icon: '/home/company-research.svg', url: '' },
  { id: 'portrait',  enabled: true, label: '企业画像', icon: '/home/company-profile.svg', url: '' },
  { id: 'policy',    enabled: true, label: '政策对比', icon: '/home/icon3.svg',            url: '' },
  { id: 'compare',   enabled: true, label: '材料对比', icon: '/home/icon1.svg',            url: '' },
  { id: 'data',      enabled: true, label: '数据分析', icon: '/home/icon2.svg',            url: '' },
];

// By default no external sub-apps are built in — administrators add/remove them in "App Configuration".
// (Historically "Enterprise Profile / Enterprise Research" were preset; after deletion a refresh re-injected them, so this is left empty.)
export const DEFAULT_APP_CONFIG: AppConfig = {
  apps: [],
};

function normalizeAppItem(item: unknown): AppItem | null {
  if (!item || typeof item !== 'object') return null;
  const raw = item as Record<string, unknown>;
  const id = typeof raw.id === 'string' ? raw.id.trim() : '';
  if (!id) return null;
  return {
    id,
    enabled: raw.enabled !== false,
    name: typeof raw.name === 'string' && raw.name ? raw.name : id,
    description: typeof raw.description === 'string' ? raw.description : '',
    url: typeof raw.url === 'string' ? raw.url : '',
    icon: typeof raw.icon === 'string' ? raw.icon : undefined,
  };
}

function mergeAppConfig(remote: unknown): AppConfig {
  if (!remote || typeof remote !== 'object') {
    return { apps: [...DEFAULT_APP_CONFIG.apps] };
  }
  const raw = remote as Record<string, unknown>;
  const list = Array.isArray(raw.apps) ? raw.apps : null;
  if (list) {
    const apps = list.map(normalizeAppItem).filter((a): a is AppItem => a !== null);
    return { apps };
  }
  return { apps: [...DEFAULT_APP_CONFIG.apps] };
}

function normalizeShortcut(raw: unknown): HomepageShortcut | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const id = typeof r.id === 'string' ? r.id.trim() : '';
  if (!id) return null;
  return {
    id,
    enabled: r.enabled !== false,
    label: typeof r.label === 'string' && r.label ? r.label : id,
    icon: typeof r.icon === 'string' ? r.icon : '',
    url: typeof r.url === 'string' ? r.url : '',
  };
}

function mergeHomepageShortcuts(remote: unknown): HomepageShortcut[] {
  if (!Array.isArray(remote)) return [...DEFAULT_HOMEPAGE_SHORTCUTS];
  const list = remote.map(normalizeShortcut).filter((c): c is HomepageShortcut => c !== null);
  return list.length > 0 ? list : [...DEFAULT_HOMEPAGE_SHORTCUTS];
}

function composeVersionKey(
  pageVer: string | null,
  appVer: string | null,
  shortcutsVer: string | null,
): string | null {
  if (!pageVer && !appVer && !shortcutsVer) return null;
  return `${pageVer || ''}|${appVer || ''}|${shortcutsVer || ''}`;
}

interface PageConfigState {
  config: PageConfig;
  appConfig: AppConfig;
  homepageShortcuts: HomepageShortcut[];
  updatedAt: string | null;
  appConfigUpdatedAt: string | null;
  homepageShortcutsUpdatedAt: string | null;
  loaded: boolean;
  fetching: boolean;
  fetchConfig: () => Promise<void>;
  fetchVersion: () => Promise<string | null>;
  getVersionKey: () => string | null;
  setConfig: (config: PageConfig, updatedAt?: string | null) => void;
}

export const usePageConfigStore = create<PageConfigState>((set, get) => ({
  config: DEFAULT_PAGE_CONFIG,
  appConfig: DEFAULT_APP_CONFIG,
  homepageShortcuts: [...DEFAULT_HOMEPAGE_SHORTCUTS],
  updatedAt: null,
  appConfigUpdatedAt: null,
  homepageShortcutsUpdatedAt: null,
  loaded: false,
  fetching: false,

  fetchConfig: async () => {
    if (get().fetching) return;
    set({ fetching: true });
    try {
      const res = await fetch(`${API_BASE}/v1/content/docs`);
      if (!res.ok) return;
      const body = await res.json();
      const data = body?.data || {};
      const remote = data.page_config || null;
      const remoteApp = data.app_config || null;
      const remoteShortcuts = data.homepage_shortcuts || null;
      set({
        config: mergePageConfig(remote),
        appConfig: mergeAppConfig(remoteApp),
        homepageShortcuts: mergeHomepageShortcuts(remoteShortcuts),
        updatedAt: data.page_config_updated_at || null,
        appConfigUpdatedAt: data.app_config_updated_at || null,
        homepageShortcutsUpdatedAt: data.homepage_shortcuts_updated_at || null,
        loaded: true,
      });
    } catch {
      // leave defaults
    } finally {
      set({ fetching: false });
    }
  },

  fetchVersion: async () => {
    try {
      const res = await fetch(`${API_BASE}/v1/content/docs/version`);
      if (!res.ok) return null;
      const body = await res.json();
      const pageVer = (body?.data?.page_config as string | null) || null;
      const appVer = (body?.data?.app_config as string | null) || null;
      const shortcutsVer = (body?.data?.homepage_shortcuts as string | null) || null;
      return composeVersionKey(pageVer, appVer, shortcutsVer);
    } catch {
      return null;
    }
  },

  getVersionKey: () => composeVersionKey(
    get().updatedAt,
    get().appConfigUpdatedAt,
    get().homepageShortcutsUpdatedAt,
  ),

  setConfig: (config, updatedAt) =>
    set({
      config: mergePageConfig(config),
      updatedAt: updatedAt ?? new Date().toISOString(),
      loaded: true,
    }),
}));
