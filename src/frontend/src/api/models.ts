import type { ReasoningLevel, ReasoningKey } from '../utils/reasoningEffort';
import type { Catalog } from '../types';
import { apiRequest, capabilitiesRouteLocal, type CatalogItem, type JsonObject, unwrapData } from './core';

export interface ModelCapabilities {
  /** Whether the main model supports multiple reasoning_effort levels (high/max). When false the frontend hides the "Thinking: high/max" options. */
  supports_reasoning_effort: boolean;
  reasoning_effort_levels?: ReasoningLevel[];
  default_reasoning_effort?: ReasoningKey;
  reasoning_effort_configured?: boolean;
  /** Whether the admin backend allows end users to switch the chat model. */
  user_model_switch_enabled: boolean;
  /** Active chat models selectable on the user side; excludes sensitive info like URL / API Key. */
  user_selectable_models: UserSelectableModel[];
  /** Real context window (tokens) of the main chat model; 0/undefined when unconfigured. */
  main_context_length?: number;
  /** Maximum text characters automatically previewed per newly attached file. */
  attachment_preview_chars?: number;
  /** 当前部署能不能读图：主模型原生多模态，或配了「图像理解（视觉桥）」角色。 */
  can_read_image?: boolean;
  /** 读图是走视觉桥转写（true）还是主模型直接看（false）。仅用于提示文案措辞。 */
  vision_bridge_enabled?: boolean;
}

interface UserSelectableModel {
  provider_id: string;
  display_name: string;
  model_name: string;
  provider: string;
  is_default: boolean;
  supports_reasoning_effort: boolean;
  reasoning_effort_levels?: ReasoningLevel[];
  default_reasoning_effort?: ReasoningKey;
  reasoning_effort_configured?: boolean;
  /** Real context window (tokens) for this model; 0/undefined when the admin hasn't configured it. */
  context_length?: number;
}

/** 对话模式（输入框上方那枚模式位）。装配细节留在服务端，这里只拿展示与策略。 */
export interface ChatModeOption {
  slug: string;
  name: string;
  description: string;
  is_builtin: boolean;
  is_private: boolean;
  /** 选中该模式时思考强度跟随切到这一档 */
  default_effort: 'fast' | 'medium' | 'high' | 'max';
  /** 锁死则用户改不了强度（极速模式即如此） */
  effort_locked: boolean;
  /**
   * 智能路由（slug=auto）：不是一套装配，而是每轮先识别意图、再从其余候选模式里挑一个。
   * 有 can_use_intent_router 且总开关开启时服务端才把它放在名单最前面。
   */
  is_router?: boolean;
}

/** 模式的完整可写形态（Config 台 / 用户设置页编辑用）。 */
export interface ChatModeDetail extends ChatModeOption {
  id: string;
  enabled: boolean;
  sort_order: number;
  tool_scope: 'all' | 'restricted';
  mcp_server_ids: string[];
  skill_ids: string[];
  plugin_ids: string[];
  agent_ids: string[];
  manual_invoke_enabled: boolean;
  code_exec_enabled: boolean;
  max_iters: number | null;
  prompt_kind: string | null;
  prompt_text: string | null;
}

/** 当前用户可用的模式：启用的官方模式 + 本人私有模式。 */
export async function listChatModes(): Promise<ChatModeOption[]> {
  return (await getChatModeMenu()).modes;
}

/** 模式菜单 + 本人的模式策略：新对话的默认模式、能否切换（不能时服务端也会锁定）。 */
export interface ChatModeMenu {
  modes: ChatModeOption[];
  defaultSlug: string;
  canSwitch: boolean;
}

export async function getChatModeMenu(): Promise<ChatModeMenu> {
  const wrapped = await apiRequest<unknown>('/v1/chat-modes');
  const data = unwrapData<JsonObject>(wrapped);
  return {
    modes: (Array.isArray(data?.modes) ? data.modes : []) as ChatModeOption[],
    defaultSlug: typeof data?.default_slug === 'string' && data.default_slug ? data.default_slug : 'standard',
    // 老后端不下发这一位：按可切换处理，行为不变。
    canSwitch: data?.can_switch !== false,
  };
}

/** 我自建的私有模式（需 can_manage_chat_modes）。 */
export async function listMyChatModes(): Promise<ChatModeDetail[]> {
  const wrapped = await apiRequest<unknown>('/v1/chat-modes/mine');
  const data = unwrapData<JsonObject>(wrapped);
  return (Array.isArray(data?.modes) ? data.modes : []) as ChatModeDetail[];
}

export async function createMyChatMode(body: Partial<ChatModeDetail>): Promise<ChatModeDetail> {
  const wrapped = await apiRequest<unknown>('/v1/chat-modes/mine', {
    method: 'POST',
    body: JSON.stringify(body),
  });
  return unwrapData<ChatModeDetail>(wrapped);
}

export async function updateMyChatMode(id: string, body: Partial<ChatModeDetail>): Promise<ChatModeDetail> {
  const wrapped = await apiRequest<unknown>(`/v1/chat-modes/mine/${encodeURIComponent(id)}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  });
  return unwrapData<ChatModeDetail>(wrapped);
}

export async function deleteMyChatMode(id: string): Promise<void> {
  await apiRequest<unknown>(`/v1/chat-modes/mine/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

export async function getMainModelCapabilities(): Promise<ModelCapabilities> {
  const wrapped = await apiRequest<unknown>('/v1/models/capabilities');
  const data = unwrapData<JsonObject>(wrapped);
  const main = (data?.main_agent as JsonObject | undefined) || {};
  const switchInfo = (data?.user_model_switch as JsonObject | undefined) || {};
  const vision = (data?.vision as JsonObject | undefined) || {};
  const modelsRaw = Array.isArray(switchInfo.models) ? switchInfo.models : [];
  const models: UserSelectableModel[] = modelsRaw
    .map((item): UserSelectableModel | null => {
      const row = item as JsonObject;
      const providerId = typeof row.provider_id === 'string' ? row.provider_id : '';
      const displayName = typeof row.display_name === 'string' ? row.display_name : '';
      const modelName = typeof row.model_name === 'string' ? row.model_name : '';
      if (!providerId || !displayName) return null;
      return {
        provider_id: providerId,
        display_name: displayName,
        model_name: modelName,
        provider: typeof row.provider === 'string' ? row.provider : 'openai_compatible',
        is_default: !!row.is_default,
        supports_reasoning_effort: !!row.supports_reasoning_effort,
        reasoning_effort_levels: Array.isArray(row.reasoning_effort_levels) ? row.reasoning_effort_levels as unknown as ReasoningLevel[] : undefined,
        default_reasoning_effort: row.default_reasoning_effort as ReasoningKey | undefined,
        reasoning_effort_configured: !!row.reasoning_effort_configured,
        context_length: typeof row.context_length === 'number' ? row.context_length : 0,
      };
    })
    .filter((item): item is UserSelectableModel => item !== null);
  return {
    supports_reasoning_effort: !!main.supports_reasoning_effort,
    reasoning_effort_levels: Array.isArray(main.reasoning_effort_levels) ? main.reasoning_effort_levels as unknown as ReasoningLevel[] : undefined,
    default_reasoning_effort: main.default_reasoning_effort as ReasoningKey | undefined,
    reasoning_effort_configured: !!main.reasoning_effort_configured,
    user_model_switch_enabled: !!switchInfo.enabled,
    user_selectable_models: models,
    main_context_length: typeof main.context_length === 'number' ? main.context_length : 0,
    can_read_image: !!vision.can_read_image,
    vision_bridge_enabled: !!vision.bridge_enabled,
    attachment_preview_chars: typeof main.attachment_preview_chars === 'number'
      ? main.attachment_preview_chars
      : 0,
  };
}

export async function getCatalog(): Promise<Catalog> {
  const items = (value: unknown) => (Array.isArray(value) ? value : []) as CatalogItem[];
  const data = unwrapData<JsonObject>(await apiRequest<unknown>('/v1/catalog'));
  // 知识库不是「这台机器上装了什么」，它一直只存在于云端。混合模式下能力那三类
  // 已经改从本机读（读写必须同源），知识库就得单独再取云端那一份补上，否则界面上
  // 会整块消失。非混合模式下两次请求打的是同一个后端，取到的也是同一份。
  const kb = capabilitiesRouteLocal()
    ? items(unwrapData<JsonObject>(await apiRequest<unknown>('/v1/catalog', undefined, 'cloud')).kb)
    : items(data.kb);
  return {
    skills: items(data.skills),
    agents: items(data.agents),
    mcp: items(data.mcp),
    kb,
  };
}

export async function updateCatalogItem(
  kind: 'skills' | 'agents' | 'mcp' | 'kb',
  itemId: string,
  enabled: boolean
): Promise<void> {
  // `/v1/catalog/kb/**` 归知识库路由（改名、文档、分块…）；知识库开关走通用目录
  // 路由的 `knowledge_bases` 写法，两者不共用同一个 URL。
  const segment = kind === 'kb' ? 'knowledge_bases' : kind;
  await apiRequest(`/v1/catalog/${segment}/${itemId}`, {
    method: 'PATCH',
    body: JSON.stringify({ enabled }),
  });
}
