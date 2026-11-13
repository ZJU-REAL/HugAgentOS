// Frontend defaults: serve as the fallback while the store has not yet loaded, aligned with the backend DEFAULT_PAGE_CONFIG
// After the backend seeds, frontend polling overrides these values. When changing values, keep content_blocks.py in sync.

import type { SidebarLayout } from './sidebarLayout';

export interface PageConfig {
  branding: {
    product_name: string;
    product_subtitle: string;
    logo_url: string;
    favicon_url: string;
    /** Wordmark on the login page (/login) and first-run setup; empty = use logo_url. */
    login_logo_url: string;
    /** Full-screen background of the login page; empty = gradient only. */
    login_background_url: string;
    page_title: string;
    hero_title: string;
    hero_subtitle: string;
    disclaimer: string;
  };
  homepage: {
    /** Whether the transparent standalone brand mark is shown above the greeting. */
    show_logo: boolean;
    /** Transparent homepage mark; independent from the square sidebar logo. */
    logo_url: string;
    /** Whether the configurable suggestion list is shown below the main composer. */
    show_suggestions: boolean;
    /** Questions are displayed three at a time and can be rotated with “换一批”. */
    suggested_questions: string[];
  };
  navigation: {
    panel_titles: Record<string, string>;
    panel_subtitles: Record<string, string>;
    admin_header: {
      title: string;
      subtitle: string;
    };
    /**
     * Unified branding and per-page labels for the admin platform (content management /admin, system config /config, API docs /api-docs).
     * product_name determines the header branding and browser tab title for the three backend pages; the three *_label values are each tab's name.
     * Replaces the old admin_header (which now serves only as a historical fallback, see mergePageConfig).
     */
    admin_platform: {
      product_name: string;
      content_label: string;
      config_label: string;
      apidoc_label: string;
      /** Browser tab icon of the three admin pages; empty = follow branding.favicon_url. */
      favicon_url: string;
    };
    /** Legacy single layout; kept for the workbench policy bridge. The sidebars read `layouts`. */
    sidebar_items: string[];
    menu_items: string[];
    /** Module placement per sidebar layout: each user's chosen layout reads its own entry. */
    layouts: Record<SidebarLayout, LayoutNavigation>;
  };
  texts: Record<string, string>;
  auth: {
    /** Whether the login page shows the "register" entry (Tab + register sub-page); when disabled, only login remains */
    allow_register: boolean;
  };
}

/** Shell modules the sidebar can place. Applications and plugins never join this set. */
export const MODULE_KEYS = ['my_space', 'automation', 'ability_center', 'projects', 'sites', 'app_center', 'lab'] as const;
export type ModuleKey = typeof MODULE_KEYS[number];
export const SIDEBAR_LAYOUT_KEYS: readonly SidebarLayout[] = ['modern', 'classic'];
/** `sidebar_items`: primary navigation in this order. `more_items`: the 「更多」 menu in this order. Absent = hidden. */
export interface LayoutNavigation { sidebar_items: ModuleKey[]; more_items: ModuleKey[] }
export const DEFAULT_LAYOUT_NAVIGATION: Readonly<LayoutNavigation> = {
  sidebar_items: ['my_space', 'automation', 'ability_center'],
  more_items: ['projects', 'sites', 'app_center', 'lab'],
};
export const defaultLayoutNavigation = (): LayoutNavigation => ({
  sidebar_items: [...DEFAULT_LAYOUT_NAVIGATION.sidebar_items],
  more_items: [...DEFAULT_LAYOUT_NAVIGATION.more_items],
});

// Legacy single-layout placement. The workbench layout policy still mirrors these two arrays, but the
// sidebars no longer read them: each layout reads `navigation.layouts.<layout>` (DEFAULT_LAYOUT_NAVIGATION
// when unset), which administrators arrange per layout in /config →「页面配置」→「侧边栏布局」.
export const DEFAULT_SIDEBAR_ITEMS = ['ability_center', 'automation', 'sites', 'my_space'];
export const DEFAULT_MENU_ITEMS = ['settings', 'app_center', 'projects', 'lab'];

export const DEFAULT_PAGE_CONFIG: PageConfig = {
  branding: {
    product_name: 'HugAgentOS',
    product_subtitle: 'HugAgentOS AI 智能助手',
    logo_url: '/home/header.svg',
    favicon_url: '/icon.png',
    login_logo_url: '/home/hugagentos-logo.png',
    login_background_url: '/home/mock-sso-bg-original.png',
    page_title: 'HugAgentOS',
    hero_title: '你好，我是 HugAgentOS',
    hero_subtitle: '今天想从哪里开始？',
    disclaimer: '本平台生成内容由AI大模型生成，不构成任何建议；涉及业务决策请以权威信息为准。',
  },
  homepage: {
    show_logo: true,
    logo_url: '/icon.png',
    show_suggestions: true,
    suggested_questions: [
      '分析新能源汽车产业链的关键环节与代表企业',
      '对比两份产业政策，提炼支持方向和申报条件',
      '查询一家企业的基本情况、经营风险和产业链位置',
      '从这份材料中提炼核心观点和待办事项',
      '分析这组经营数据的趋势、异常和可能原因',
      '检索相关政策依据，并给出可核验的来源',
    ],
  },
  navigation: {
    panel_titles: {
      ability_center: '能力中心',
      skills: '技能',
      agents: '智能体',
      mcp: '连接器',
      kb: '知识库',
      docs: '更新记录',
      app_center: '应用中心',
      lab: '实验室',
      settings: '系统设置',
      my_space: '我的空间',
      projects: '项目',
      automation: '定时任务',
      sites: '站点',
    },
    panel_subtitles: {
      ability_center: '智能体基础能力管理，包含智能体、技能、连接器与插件',
      skills: '启用/停用技能，并查看详细介绍、输入输出与示例。',
      agents: '选择与启用智能体，并查看其职责边界与路由提示。',
      mcp: '管理 MCP连接器服务，并查看其作用范围与可靠性影响。',
      kb: '浏览知识库、查看文档列表，并支持文档内检索。',
      docs: '查看功能更新、能力中心与平台说明。',
      app_center: '基于 AI 能力的场景化智能应用',
      lab: 'AI 能力实验性应用',
      settings: '',
      my_space: '',
      projects: '把对话、文件和指令打包成专属工作空间',
      automation: '按计划自动执行任务，也可随时手动触发；在对话中描述即可快速创建',
      sites: '在对话里描述需求，AI 生成完整网站并一键发布，由平台托管、凭链接即可访问',
    },
    admin_header: {
      title: 'HugAgentOS — 后台管理',
      subtitle: '后台管理',
    },
    admin_platform: {
      product_name: 'HugAgentOS',
      content_label: '内容管理',
      config_label: '系统配置',
      apidoc_label: '接口文档',
      favicon_url: '',
    },
    sidebar_items: [...DEFAULT_SIDEBAR_ITEMS],
    menu_items: [...DEFAULT_MENU_ITEMS],
    layouts: { modern: defaultLayoutNavigation(), classic: defaultLayoutNavigation() },
  },
  texts: {
    input_placeholder: '请输入您的问题…',
    input_placeholder_agent: '请输入您的问题…',
    search_placeholder: '搜索对话',
    btn_new_chat: '新建对话',
    btn_logout: '退出',
    history_label: '历史对话',
    sidebar_empty_state: '暂无对话记录',
    search_no_results: '无匹配结果',
    dialog_logout_confirm_title: '确认退出登录？',
    dialog_logout_confirm_content: '退出登录不会丢失任何数据，重新登录后可继续使用。',
    dialog_logout_confirm_ok: '退出登录',
    recommend_banner_text: '',
  },
  auth: {
    allow_register: true,
  },
};

/** The login / first-run wordmark: its own image, or the sidebar logo when left empty (as the backend's resolve_login_branding). */
export function resolveLoginLogoUrl(branding: Pick<PageConfig['branding'], 'login_logo_url' | 'logo_url'>): string {
  return branding.login_logo_url.trim() || branding.logo_url.trim() || DEFAULT_PAGE_CONFIG.branding.logo_url;
}

export function mergePageConfig(remote: Partial<PageConfig> | null | undefined): PageConfig {
  if (!remote || typeof remote !== 'object') return DEFAULT_PAGE_CONFIG;
  return {
    branding: { ...DEFAULT_PAGE_CONFIG.branding, ...(remote.branding || {}) },
    homepage: {
      ...DEFAULT_PAGE_CONFIG.homepage,
      ...(remote.homepage || {}),
      suggested_questions: normalizeSuggestedQuestions(
        remote.homepage?.suggested_questions,
        DEFAULT_PAGE_CONFIG.homepage.suggested_questions,
      ),
    },
    navigation: {
      panel_titles: {
        ...DEFAULT_PAGE_CONFIG.navigation.panel_titles,
        ...((remote.navigation?.panel_titles as Record<string, string>) || {}),
      },
      panel_subtitles: {
        ...DEFAULT_PAGE_CONFIG.navigation.panel_subtitles,
        ...((remote.navigation?.panel_subtitles as Record<string, string>) || {}),
      },
      admin_header: {
        ...DEFAULT_PAGE_CONFIG.navigation.admin_header,
        ...((remote.navigation?.admin_header as Record<string, string>) || {}),
      },
      admin_platform: mergeAdminPlatform(
        remote.navigation?.admin_platform,
        remote.navigation?.admin_header,
      ),
      sidebar_items: normalizeLayoutKeys(
        remote.navigation?.sidebar_items,
        DEFAULT_PAGE_CONFIG.navigation.sidebar_items,
      ),
      menu_items: normalizeLayoutKeys(
        remote.navigation?.menu_items,
        DEFAULT_PAGE_CONFIG.navigation.menu_items,
      ),
      layouts: normalizeLayouts(remote.navigation?.layouts),
    },
    texts: { ...DEFAULT_PAGE_CONFIG.texts, ...(remote.texts || {}) },
    auth: { ...DEFAULT_PAGE_CONFIG.auth, ...(remote.auth || {}) },
  };
}

function normalizeSuggestedQuestions(value: unknown, fallback: string[]): string[] {
  if (!Array.isArray(value)) return [...fallback];
  const seen = new Set<string>();
  const questions: string[] = [];
  for (const item of value) {
    if (typeof item !== 'string') continue;
    const question = item.trim();
    if (!question || seen.has(question)) continue;
    seen.add(question);
    questions.push(question);
  }
  return questions;
}

// Admin platform config normalization: prefer the new admin_platform field; when missing, split the brand out of the old admin_header.title
// (in the form "Brand — Admin Management") to use as the product_name fallback, ensuring already-deployed, renamed
// deployments do not lose their backend branding after upgrade (no backend backfill needed).
function mergeAdminPlatform(
  remote: unknown,
  remoteAdminHeader: unknown,
): PageConfig['navigation']['admin_platform'] {
  const def = DEFAULT_PAGE_CONFIG.navigation.admin_platform;
  const r = (remote && typeof remote === 'object' ? remote : {}) as Partial<
    PageConfig['navigation']['admin_platform']
  >;
  const headerTitle =
    remoteAdminHeader && typeof remoteAdminHeader === 'object'
      ? (remoteAdminHeader as Record<string, string>).title || ''
      : '';
  const derivedBrand = headerTitle.split(/\s+[—-]\s+/)[0].trim();
  const pick = (v: unknown, fallback: string): string =>
    (typeof v === 'string' && v.trim()) || fallback;
  return {
    product_name: pick(r.product_name, derivedBrand || def.product_name),
    content_label: pick(r.content_label, def.content_label),
    config_label: pick(r.config_label, def.config_label),
    apidoc_label: pick(r.apidoc_label, def.apidoc_label),
    favicon_url: pick(r.favicon_url, def.favicon_url),
  };
}

function moduleKeys(value: unknown, taken: ReadonlySet<string>): ModuleKey[] {
  return normalizeLayoutKeys(value, []).filter((key): key is ModuleKey =>
    (MODULE_KEYS as readonly string[]).includes(key) && !taken.has(key));
}

/** Each layout keeps its own order; a module sits in at most one place and unknown keys are dropped. */
export function normalizeLayoutNavigation(value: unknown): LayoutNavigation {
  const raw = value && typeof value === 'object' ? value as Record<string, unknown> : {};
  const sidebarSource = Array.isArray(raw.sidebar_items) ? raw.sidebar_items : DEFAULT_LAYOUT_NAVIGATION.sidebar_items;
  const sidebar = moduleKeys(sidebarSource, new Set());
  const moreSource = Array.isArray(raw.more_items) ? raw.more_items : DEFAULT_LAYOUT_NAVIGATION.more_items;
  return { sidebar_items: sidebar, more_items: moduleKeys(moreSource, new Set(sidebar)) };
}

function normalizeLayouts(value: unknown): Record<SidebarLayout, LayoutNavigation> {
  const raw = value && typeof value === 'object' ? value as Record<string, unknown> : {};
  return { modern: normalizeLayoutNavigation(raw.modern), classic: normalizeLayoutNavigation(raw.classic) };
}

function normalizeLayoutKeys(value: unknown, fallback: string[]): string[] {
  if (!Array.isArray(value)) return [...fallback];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const v of value) {
    if (typeof v !== 'string') continue;
    const k = v.trim();
    if (!k || seen.has(k)) continue;
    seen.add(k);
    out.push(k);
  }
  return out;
}
