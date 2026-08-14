import { create } from 'zustand';
import { listChatModes, type ChatModeOption } from '../api';

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

interface ChatModeState {
  modes: ChatModeOption[];
  loaded: boolean;
  fetching: boolean;
  fetchModes: (force?: boolean) => Promise<void>;
  /** 按 slug 取一条；取不到返回名单第一条（标准模式）。 */
  modeOf: (slug: string) => ChatModeOption;
}

export const useChatModeStore = create<ChatModeState>((set, get) => ({
  modes: FALLBACK_MODES,
  loaded: false,
  fetching: false,
  fetchModes: async (force = false) => {
    const state = get();
    if (state.fetching || (state.loaded && !force)) return;
    set({ fetching: true });
    try {
      const modes = await listChatModes();
      // 空名单当异常处理：服务端至少会播种两条内置的，真空说明这次没拿到。
      if (modes.length > 0) set({ modes, loaded: true });
      else set({ loaded: true });
    } catch {
      // 静默：保留兜底名单，输入框照常可用
    } finally {
      set({ fetching: false });
    }
  },
  modeOf: (slug: string) => {
    const { modes } = get();
    return modes.find((m) => m.slug === slug) || modes[0] || FALLBACK_MODES[0];
  },
}));
