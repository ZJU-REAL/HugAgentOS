// Frontend defaults: serve as the fallback while the store has not yet loaded, aligned with the backend DEFAULT_PAGE_CONFIG
// After the backend seeds, frontend polling overrides these values. When changing values, keep content_blocks.py in sync.

export interface PageConfig {
  branding: {
    product_name: string;
    product_subtitle: string;
    logo_url: string;
    favicon_url: string;
    page_title: string;
    hero_title: string;
    hero_subtitle: string;
    disclaimer: string;
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
    };
    sidebar_items: string[];
    menu_items: string[];
  };
  texts: Record<string, string>;
  defaults: {
    /** Initial mode on each user login / new conversation: turbo / fast / medium / high / max */
    chat_mode: 'turbo' | 'fast' | 'medium' | 'high' | 'max';
    /** Legacy field, dual-written with chat_mode for old-frontend compatibility; true=thinking mode, false=fast mode */
    thinking_mode: boolean;
  };
  auth: {
    /** Whether the login page shows the "register" entry (Tab + register sub-page); when disabled, only login remains */
    allow_register: boolean;
  };
}

// "Projects" no longer appears in the main navigation by default: browsing/entering projects goes through the sidebar "Projects" group, and selecting/creating goes through the input-box
// project dropdown. LAYOUT_ITEMS still keeps the projects definition, and the backend page config can add it back as needed.
// ⚠️ 这两个数组只是**出厂默认值**，不是最终形态——运行时以 DB 里的
// `page_config.navigation.sidebar_items / menu_items` 为准（见 Sidebar.tsx 的 usePageConfig）。
// 管理员随时可在 /config →「页面配置」里把任意入口在「侧边栏 / 用户菜单 / 隐藏」三栏间拖动、
// 调序、或一键「恢复默认」回到这里的取值。新增入口只要进 LAYOUT_ITEMS，就自动出现在那个编排界面里。
//
// 当前默认的取舍：能力中心从底部菜单提到一级导航（其四类能力做成二级导航）；定时任务从应用中心、
// 站点从实验室各自拆出来成一级导航；知识库并入我的空间的 Tab、子智能体并入能力中心的二级导航，
// 因此都不再单列；应用中心与项目反向下沉到用户下拉菜单，主导航只留高频入口。
export const DEFAULT_SIDEBAR_ITEMS = ['ability_center', 'automation', 'sites', 'my_space'];
export const DEFAULT_MENU_ITEMS = ['settings', 'app_center', 'projects', 'lab'];

export const DEFAULT_PAGE_CONFIG: PageConfig = {
  branding: {
    product_name: 'HugAgentOS',
    product_subtitle: 'HugAgentOS AI 智能助手',
    logo_url: '/home/header.svg',
    favicon_url: '/icon.png',
    page_title: 'HugAgentOS',
    hero_title: '你好，我是 HugAgentOS',
    hero_subtitle: '基于 AI 能力的场景化智能工作平台',
    disclaimer: '本平台生成内容由AI大模型生成，不构成任何建议；涉及业务决策请以权威信息为准。',
  },
  navigation: {
    panel_titles: {
      ability_center: '能力中心',
      skills: '技能库',
      agents: '子智能体',
      mcp: '连接器',
      kb: '知识库',
      docs: '更新记录',
      app_center: '应用中心',
      lab: '实验室',
      settings: '系统设置',
      my_space: '我的空间',
      projects: '项目',
      project_detail: '项目详情',
      automation: '定时任务',
      sites: '站点',
    },
    panel_subtitles: {
      ability_center: '智能体基础能力管理，包含智能体、技能、连接器与插件',
      skills: '启用/停用技能，并查看详细介绍、输入输出与示例。',
      agents: '选择与启用子智能体，并查看其职责边界与路由提示。',
      mcp: '管理 MCP 连接器服务，并查看其作用范围与可靠性影响。',
      kb: '浏览知识库、查看文档列表，并支持文档内检索。',
      docs: '查看功能更新、能力中心与平台说明。',
      app_center: '基于 AI 能力的场景化智能应用',
      lab: 'AI 能力实验性应用',
      settings: '',
      my_space: '',
      projects: '把对话、文件和指令打包成专属工作空间',
      project_detail: '',
      automation: '按计划自动执行任务，也可随时手动触发。在任意对话中描述你想定期做的事，即可快速创建',
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
    },
    sidebar_items: [...DEFAULT_SIDEBAR_ITEMS],
    menu_items: [...DEFAULT_MENU_ITEMS],
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
  defaults: {
    chat_mode: 'fast',
    thinking_mode: false,
  },
  auth: {
    allow_register: true,
  },
};

export function mergePageConfig(remote: Partial<PageConfig> | null | undefined): PageConfig {
  if (!remote || typeof remote !== 'object') return DEFAULT_PAGE_CONFIG;
  return {
    branding: { ...DEFAULT_PAGE_CONFIG.branding, ...(remote.branding || {}) },
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
    },
    texts: { ...DEFAULT_PAGE_CONFIG.texts, ...(remote.texts || {}) },
    defaults: { ...DEFAULT_PAGE_CONFIG.defaults, ...(remote.defaults || {}) },
    auth: { ...DEFAULT_PAGE_CONFIG.auth, ...(remote.auth || {}) },
  };
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
  };
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
