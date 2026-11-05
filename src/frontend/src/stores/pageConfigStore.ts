import { create } from 'zustand';
import { mergePageConfig, type PageConfig } from '../utils/pageConfigDefaults';
import { loadJsonPref, removeLocal, saveJsonPref } from '../storage';

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
 *
 * 注意：这个列表同时是 **权限位注册表**——Config 权限配置（用户/团队/角色）的
 * "应用可见范围" 勾选项就来自它（见 hooks/useAppsCatalog.ts）。所以有些条目并不
 * 陈列在应用中心宫格里（automation 走侧边栏一级入口、prompt_hub 是输入框上的入口），
 * 留在这里纯粹是为了让管理员能按用户/团队开关它。
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
    name: '定时任务',
    description:
      '设置定时或周期性 AI 任务，支持自然语言提示词和计划模式的自动执行，适用于定期报告、数据监控等场景',
    url: '',
    icon: '/home/new-icons/automation.svg',
  },
  {
    id: 'prompt_hub',
    enabled: true,
    name: '提示词中心',
    description:
      '输入框上的提示词模板库入口，按场景挑选提示词直接填入对话；关掉后该入口对用户隐藏',
    url: '',
    icon: '/home/prompt.svg',
  },
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

function composeVersionKey(pageVer: string | null, appVer: string | null): string | null {
  if (!pageVer && !appVer) return null;
  return `${pageVer || ''}|${appVer || ''}`;
}

/**
 * The last page config the server returned. The store starts from it, so a reload paints the configured
 * logo and names at once instead of the defaults until `/content/docs` answers. index.html reads the
 * same key to set the tab title and icon before any script loads — keep the two in sync.
 */
export const PAGE_CONFIG_CACHE_KEY = 'hugagent_page_config_cache';

function cachePageConfig(config: object | null): void {
  if (config) saveJsonPref(PAGE_CONFIG_CACHE_KEY, config);
  else removeLocal(PAGE_CONFIG_CACHE_KEY);
}

/** index.html starts the `/content/docs` request before any script loads; the first fetch takes it over. */
function takeEarlyRequest(): Promise<unknown> | null {
  if (typeof window === 'undefined' || API_BASE !== '/api') return null;
  const host = window as Window & { __jxPageConfig?: Promise<unknown> };
  const early = host.__jxPageConfig ?? null;
  delete host.__jxPageConfig;
  return early;
}

interface DocsResponse {
  data?: {
    page_config?: Partial<PageConfig> | null;
    app_config?: unknown;
    page_config_updated_at?: string | null;
    app_config_updated_at?: string | null;
  };
}

async function requestDocs(): Promise<DocsResponse | null> {
  const early = (await takeEarlyRequest()) as DocsResponse | null;
  if (early) return early;
  const res = await fetch(`${API_BASE}/v1/content/docs`);
  return res.ok ? res.json() : null;
}

interface PageConfigState {
  config: PageConfig;
  appConfig: AppConfig;
  updatedAt: string | null;
  appConfigUpdatedAt: string | null;
  loaded: boolean;
  fetching: boolean;
  fetchConfig: () => Promise<void>;
  fetchVersion: () => Promise<string | null>;
  getVersionKey: () => string | null;
  setConfig: (config: PageConfig, updatedAt?: string | null) => void;
}

export const usePageConfigStore = create<PageConfigState>((set, get) => ({
  config: mergePageConfig(loadJsonPref(PAGE_CONFIG_CACHE_KEY, {})),
  appConfig: DEFAULT_APP_CONFIG,
  updatedAt: null,
  appConfigUpdatedAt: null,
  loaded: false,
  fetching: false,

  fetchConfig: async () => {
    if (get().fetching) return;
    set({ fetching: true });
    try {
      const body = await requestDocs();
      if (!body) return;
      const data = body.data || {};
      const remote = data.page_config || null;
      const remoteApp = data.app_config || null;
      cachePageConfig(remote);
      set({
        config: mergePageConfig(remote),
        appConfig: mergeAppConfig(remoteApp),
        updatedAt: data.page_config_updated_at || null,
        appConfigUpdatedAt: data.app_config_updated_at || null,
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
      return composeVersionKey(pageVer, appVer);
    } catch {
      return null;
    }
  },

  getVersionKey: () => composeVersionKey(get().updatedAt, get().appConfigUpdatedAt),

  setConfig: (config, updatedAt) => {
    cachePageConfig(config);
    set({
      config: mergePageConfig(config),
      updatedAt: updatedAt ?? new Date().toISOString(),
      loaded: true,
    });
  },
}));
