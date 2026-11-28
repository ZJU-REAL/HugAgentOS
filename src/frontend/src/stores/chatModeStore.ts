import { create } from 'zustand';
import { getChatModeMenu, type ChatModeOption } from '../api';

/**
 * 对话模式名单（输入框上方那枚模式位的数据源）。
 *
 * 名单是服务端下发的：内置的标准 / 极速两条 + 管理员建的官方模式 + 本人建的私有模式。
 * 前端只拿到展示与策略字段（名称、说明、默认思考强度、强度是否锁死）——工具面、
 * 技能、插件、提示词这些装配细节留在服务端，前端没有理由知道。
 *
 * 加载失败不清空已有名单，也不阻塞输入框：模式是增强不是前置条件，
 * 拿不到就退回内置的两条兜底。
 */

/** 接口不可用时的兜底名单：至少让用户还能在标准 / 极速之间切。 */
const FALLBACK_MODES: ChatModeOption[] = [
  {
    slug: 'standard',
    name: '标准模式',
    description: '完整工具、技能与插件，思考强度可调，适合大部分任务',
    is_builtin: true,
    is_private: false,
    default_effort: 'fast',
    effort_locked: false,
  },
  {
    slug: 'turbo',
    name: '极速模式',
    description: '只留检索类工具、不思考，秒级直达结果',
    is_builtin: true,
    is_private: false,
    default_effort: 'fast',
    effort_locked: true,
  },
];

/** 智能路由的 slug（与后端约定一致）。 */
export const INTENT_ROUTER_SLUG = 'auto';

export function isRouterMode(mode: Pick<ChatModeOption, 'slug' | 'is_router'> | undefined): boolean {
  return !!mode && (mode.is_router === true || mode.slug === INTENT_ROUTER_SLUG);
}

/**
 * 找不到对应模式时的兜底：名单第一条可执行的模式。智能路由排在名单最前面，
 * 但它只是"替你挑一个"，不能拿来顶替一段记录丢了模式、或权限被收回的对话。
 */
export function defaultChatMode(modes: ChatModeOption[]): ChatModeOption {
  return modes.find((m) => !isRouterMode(m)) || FALLBACK_MODES[0];
}

interface ChatModeState {
  modes: ChatModeOption[];
  /** 新对话默认进入的模式（管理员按角色 / 团队 / 个人配置；未配置即标准模式）。 */
  defaultSlug: string;
  /** false：管理员收走了「模式切换」，只能用 defaultSlug，模式位锁定。 */
  canSwitch: boolean;
  loaded: boolean;
  fetching: boolean;
  fetchModes: (force?: boolean) => Promise<void>;
  /** 按 slug 取一条；取不到返回第一条可执行的模式（标准模式），不会落到智能路由上。 */
  modeOf: (slug: string) => ChatModeOption;
  /** 实际要用的模式：不能切换时一律是默认模式，否则就是传进来的这个。 */
  effectiveSlug: (slug: string | null | undefined) => string;
}

export const useChatModeStore = create<ChatModeState>((set, get) => ({
  modes: FALLBACK_MODES,
  defaultSlug: 'standard',
  canSwitch: true,
  loaded: false,
  fetching: false,
  fetchModes: async (force = false) => {
    const state = get();
    if (state.fetching || (state.loaded && !force)) return;
    set({ fetching: true });
    try {
      const { modes, defaultSlug, canSwitch } = await getChatModeMenu();
      // 空名单当异常处理：服务端至少会播种两条内置的，真空说明这次没拿到。
      if (modes.length > 0) set({ modes, defaultSlug, canSwitch, loaded: true });
      else set({ defaultSlug, canSwitch, loaded: true });
    } catch {
      // 静默：保留兜底名单，输入框照常可用
    } finally {
      set({ fetching: false });
    }
  },
  modeOf: (slug: string) => {
    const { modes } = get();
    return modes.find((m) => m.slug === slug) || defaultChatMode(modes);
  },
  effectiveSlug: (slug) => {
    const { canSwitch, defaultSlug } = get();
    return canSwitch ? (slug || defaultSlug) : defaultSlug;
  },
}));
