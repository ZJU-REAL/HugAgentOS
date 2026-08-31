/**
 * API Client for HugAgentOS Backend.
 *
 * Uses v1 unified response envelope.
 */

import type { Catalog, ChatItem, ChatMessage, ChunkPreviewResult, PlanProgressState, EvolutionSummary, JobBrief, KBChunk, KBIndexMode, KBWikiStatus, WikiConfig, MemoryItem, MemoryProfile, MemoryGraphRelation, ResourceItem, AutomationTask, AutomationRun, AutomationNotification, FileConfirmInfo, FileConfirmDecision, DesignPickInfo, UserQuestionAnswer, UserQuestionRequest, OntologyAssetKind, OntologyTagOption } from './types';
import type { EditionAuthUserFields } from './editionApiTypes';
import type { EditionChatDetailFields, EditionCreateProjectFields } from './editionModelTypes';
import { createEditionAccessError } from './editionAccessError';
import { createApiResponseError, readErrorMessage } from './utils/apiError';
import { newOperationId } from './utils/operationId';
import { t } from './i18n';
import {
  normalizeSiteEditionFields,
  normalizeSiteVisibility,
  type SiteEditionFields,
  type SiteUpdateEditionFields,
  type SiteVisibility,
} from './editionSiteVisibility';

type JsonObject = Record<string, unknown>;

interface ApiEnvelope<T> {
  code: number;
  message: string;
  data: T;
  trace_id?: string;
  timestamp?: number;
}

interface Pagination {
  page: number;
  page_size: number;
  total_items: number;
  total_pages: number;
  has_previous: boolean;
  has_next: boolean;
}

interface PaginatedData<T> {
  items: T[];
  pagination: Pagination;
}

export interface CatalogItem {
  id: string;
  name: string;
  desc: string;
  enabled: boolean;
  tags?: string[];
  detail?: string;
  [key: string]: unknown;
}

export interface CatalogResponse {
  skills: CatalogItem[];
  agents: CatalogItem[];
  mcp: CatalogItem[];
  kb: CatalogItem[];
}

export interface KBDocumentsResponse {
  items: KBDocumentItem[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
  status_counts?: KBDocumentStatusCounts;
}

export interface KBDocumentStatusCounts {
  indexed: number;
  processing: number;
  failed: number;
}

export interface SessionListResponse {
  items: ChatItem[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
}

export interface CreateSessionRequest {
  title?: string;
  business_topic?: string;
}

export interface UpdateSessionRequest {
  title?: string;
  pinned?: boolean;
  favorite?: boolean;
  business_topic?: string;
}

export interface UserInfo {
  user_id: string;
  username: string;
  email?: string;
  avatar_url?: string;
}

export interface UserPreferences {
  default_model?: string;
  language?: string;
  theme?: string;
  enabled_skills?: string[];
  enabled_mcps?: string[];
}

export interface AddArtifactToKBResult {
  document_id: string;
  kb_id: string;
  title: string;
  filename: string;
  size_bytes: number;
  uploaded_at: string;
  already_exists?: boolean;
}

export interface HealthResponse {
  status: string;
  service: string;
  timestamp: string;
}

export const getApiUrl = () => import.meta.env.VITE_API_BASE_URL || '/api';

// ── Hybrid routing（桌面双模式：云端为主 + 本机执行本地项目）─────────────────
// 桌面壳的 Rust 反代按请求头分流：带 `x-hugagent-target: local` → 本机执行面
// （127.0.0.1:32101），否则 → 云端。前端唯一的「本地」判定真源是项目
// kind==='local'；聊天则继承其绑定项目。此处维护两个注册表（api.ts 内自洽，
// 不引 store，避免模块环）。仅 provision_mode==='dual' 时生效；web 上恒为空。

// 头名必须用全小写技术标识：大写 HugAgentOS 会被 CE 品牌变换改写成 HugAgentOS，
// 与桌面壳 Rust 反代匹配的小写常量（proxy.rs TARGET_HEADER）对不上，路由整体失效。
export const LOCAL_TARGET_HEADER = 'x-hugagent-target';

let _hybridDual = false;
/** 由 deploymentModeStore.refresh() 在探测到桌面双模式后开启。 */
export function setHybridDual(on: boolean) {
  _hybridDual = on;
}
export function isHybridDual(): boolean {
  return _hybridDual;
}

const _localProjects = new Set<string>();
const _localChats = new Set<string>();

export function registerLocalProject(projectId: string) {
  if (projectId) _localProjects.add(projectId);
}
export function registerLocalChat(chatId: string) {
  if (chatId) _localChats.add(chatId);
}
export function isLocalProject(projectId?: string | null): boolean {
  return !!projectId && _localProjects.has(projectId);
}
export function isLocalChat(chatId?: string | null): boolean {
  return !!chatId && _localChats.has(chatId);
}

function localHeader(): Record<string, string> {
  return { [LOCAL_TARGET_HEADER]: 'local' };
}
/** 项目作用域请求的路由头：本地项目 → 本机；否则空对象（云端默认）。 */
export function projectTargetHeaders(projectId?: string | null): Record<string, string> {
  return _hybridDual && isLocalProject(projectId) ? localHeader() : {};
}
/** 聊天作用域请求的路由头：本地项目的聊天 → 本机。 */
export function chatTargetHeaders(chatId?: string | null): Record<string, string> {
  return _hybridDual && isLocalChat(chatId) ? localHeader() : {};
}

/** 从 API URL 推断混合路由头（authFetch 自动兜底：预览/下载等散点直连调用）。 */
function inferTargetHeadersFromUrl(url: string): Record<string, string> {
  if (!_hybridDual) return {};
  const pm = url.match(/\/v1\/projects\/([^/?#]+)/);
  if (pm && isLocalProject(decodeURIComponent(pm[1]))) return localHeader();
  const cm = url.match(/\/v1\/chats\/([^/?#]+)/);
  if (cm && isLocalChat(decodeURIComponent(cm[1]))) return localHeader();
  return {};
}

/** <img>/<iframe>/<video> 等 src 场景无法带请求头：本地归属的 URL 追加
 *  ?hg_target=local（Rust 反代等价识别）。非本地/非双模式原样返回。 */
export function maybeLocalizeUrl(url: string): string {
  if (!url || Object.keys(inferTargetHeadersFromUrl(url)).length === 0) return url;
  if (url.includes('hg_target=local')) return url;
  return url.includes('?') ? `${url}&hg_target=local` : `${url}?hg_target=local`;
}

function isApiEnvelope<T>(payload: unknown): payload is ApiEnvelope<T> {
  return !!payload && typeof payload === 'object' && 'code' in payload && 'data' in payload;
}

export function unwrapData<T>(payload: unknown): T {
  if (isApiEnvelope<T>(payload)) {
    return payload.data;
  }
  return payload as T;
}

// User-friendly upload error messages.
// Recognizes nginx 413 first (HTML response, not parseable as JSON), then
// backend structured error codes.
// Baked in at build time from the same UPLOAD_MAX_MB env var that feeds the
// nginx client_max_body_size (see docker-compose.yml frontend build args).
export const UPLOAD_MAX_MB = Number(import.meta.env.VITE_UPLOAD_MAX_MB) || 50;
export const UPLOAD_MAX_BYTES = UPLOAD_MAX_MB * 1024 * 1024;
function uploadErrorMessage(status: number, payload: unknown): string {
  if (status === 413) {
    return t('文件过大，单个文件不能超过 {n} MB', { n: UPLOAD_MAX_MB });
  }
  if (payload && typeof payload === 'object') {
    const record = payload as JsonObject;
    const code = typeof record.code === 'number' ? record.code : null;
    // 21001 FileTooLargeError, 21002 InvalidFileTypeError — see core/infra/exceptions.py
    if (code === 21001) {
      const data = (record.data ?? {}) as JsonObject;
      const max = typeof data.max_size === 'number' ? data.max_size : null;
      const mb = max ? Math.floor(max / 1024 / 1024) : UPLOAD_MAX_MB;
      return t('文件过大，单个文件不能超过 {n} MB', { n: mb });
    }
    if (code === 21002) {
      const data = (record.data ?? {}) as JsonObject;
      const allowed = Array.isArray(data.allowed_types) ? data.allowed_types.join('、') : '';
      return allowed ? t('不支持的文件格式，仅支持：{allowed}', { allowed }) : t('不支持的文件格式');
    }
  }
  return readErrorMessage(payload, t('上传失败 ({status})', { status }));
}

function toTimestamp(value: unknown): number {
  if (typeof value !== 'string' || !value) return Date.now();
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? Date.now() : parsed;
}

/** 会话 metadata 里的计划快照 → 计划栏状态。服务端是真源：`settled` 表示"不会再有哪一轮
 *  来更新它了"（不等于每一步都完成——停在 2/5 的计划收尾后仍显示 2/5，只是不再转圈）。 */
export function toPlanProgress(raw: unknown): PlanProgressState | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const o = raw as JsonObject;
  const steps = (Array.isArray(o.steps) ? o.steps : [])
    .map((s) => {
      const st = s as JsonObject;
      const title = typeof st?.title === 'string' ? st.title.trim() : '';
      const status = st?.status === 'in_progress' || st?.status === 'completed' || st?.status === 'failed'
        ? st.status : 'pending';
      return title ? { title, status: status as PlanProgressState['steps'][number]['status'] } : null;
    })
    .filter((s): s is PlanProgressState['steps'][number] => !!s);
  if (steps.length === 0) return undefined;
  const ts = typeof o.updated_at === 'string' ? Date.parse(o.updated_at) : NaN;
  return {
    source: 'agent',
    title: typeof o.title === 'string' ? o.title : '',
    steps,
    done: o.settled === true ? true : undefined,
    updatedAt: Number.isFinite(ts) ? ts : Date.now(),
  };
}

function toChatItem(raw: JsonObject): ChatItem {
  const metadata = (raw.metadata ?? {}) as JsonObject;
  return {
    id: String(raw.chat_id ?? raw.id ?? ''),
    title: String(raw.title ?? '新对话'),
    createdAt: toTimestamp(raw.created_at),
    updatedAt: toTimestamp(raw.updated_at),
    messages: [],
    favorite: Boolean(raw.favorite),
    pinned: Boolean(raw.pinned),
    businessTopic: typeof raw.business_topic === 'string' ? raw.business_topic : undefined,
    agentId: typeof metadata.agent_id === 'string' ? metadata.agent_id : undefined,
    agentName: typeof metadata.agent_name === 'string' ? metadata.agent_name : undefined,
    planChat: metadata.plan_chat === true ? true : undefined,
    batchChat: metadata.batch_chat === true ? true : undefined,
    workflowChat: metadata.workflow_chat === true ? true : undefined,
    planProgress: toPlanProgress(metadata.plan_progress),
    projectId: typeof raw.project_id === 'string' && raw.project_id ? raw.project_id : undefined,
  };
}

// ── Global 401 handler ──────────────────────────────────────────────────
let _on401: ((loginUrl: string) => void) | null = null;

/** Register a callback invoked on any 401 with login_url. */
export function onUnauthorized(handler: (loginUrl: string) => void) {
  _on401 = handler;
}

/** Only 401 (session expired) triggers the login flow and throws Session expired;
 * 403 is insufficient permission (e.g. backend 31001 Access Denied) — let the caller
 * surface the backend message instead of misreporting it as session expiry and
 * masking the real cause. */
function throwIfSessionExpired(status: number, payload: unknown): void {
  if (status !== 401 || !_on401) return;
  const pickLoginUrl = (obj: unknown): string => {
    if (!obj || typeof obj !== 'object') return '';
    const data = (obj as Record<string, unknown>).data;
    if (!data || typeof data !== 'object') return '';
    const url = (data as Record<string, unknown>).login_url;
    return typeof url === 'string' ? url : '';
  };
  const record = payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : {};
  const loginUrl = pickLoginUrl(record) || pickLoginUrl(record.detail);
  _on401(loginUrl);
  throw new Error('Session expired');
}

export async function apiRequest<T>(
  path: string,
  options?: RequestInit,
  target?: 'local',
): Promise<T> {
  const url = `${getApiUrl()}${path}`;
  const response = await fetch(url, {
    ...options,
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      // 混合路由：显式声明本地目标时打头（web / 非双模式下反代忽略该头）。
      ...(target === 'local' ? localHeader() : {}),
      // 兜底：与 authFetch 同源推断——路径含本地项目/本地会话 id 时自动打头，
      // 否则 file-confirm / pending-confirm 等会话作用域请求会被误发云端，
      // 云端无此会话而报「会话不存在或无权访问」。
      ...inferTargetHeadersFromUrl(path),
      ...(options?.headers ?? {}),
    },
  });

  if (response.status === 204) {
    return undefined as T;
  }

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    // 401 → session expired, show login; 403 → insufficient permission, fall through to the generic branch below to surface the backend message
    throwIfSessionExpired(response.status, payload);
    const editionError = createEditionAccessError(response.status, payload, readErrorMessage);
    if (editionError) throw editionError;
    throw createApiResponseError(response.status, payload, `API Error: ${response.status}`);
  }
  return payload as T;
}

export interface ModelCapabilities {
  /** Whether the main model supports multiple reasoning_effort levels (high/max). When false the frontend hides the "Thinking: high/max" options. */
  supports_reasoning_effort: boolean;
  /** Whether the admin backend allows end users to switch the chat model. */
  user_model_switch_enabled: boolean;
  /** Active chat models selectable on the user side; excludes sensitive info like URL / API Key. */
  user_selectable_models: UserSelectableModel[];
  /** Real context window (tokens) of the main chat model; 0/undefined when unconfigured. */
  main_context_length?: number;
  /** Legacy capacity reserve; not a live context-usage measurement. */
  system_prompt_tokens?: number;
  /** Maximum text characters automatically previewed per newly attached file. */
  attachment_preview_chars?: number;
  /** 当前部署能不能读图：主模型原生多模态，或配了「图像理解（视觉桥）」角色。 */
  can_read_image?: boolean;
  /** 读图是走视觉桥转写（true）还是主模型直接看（false）。仅用于提示文案措辞。 */
  vision_bridge_enabled?: boolean;
}

export interface UserSelectableModel {
  provider_id: string;
  display_name: string;
  model_name: string;
  provider: string;
  is_default: boolean;
  supports_reasoning_effort: boolean;
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
  const wrapped = await apiRequest<unknown>('/v1/chat-modes');
  const data = unwrapData<JsonObject>(wrapped);
  return (Array.isArray(data?.modes) ? data.modes : []) as ChatModeOption[];
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
        context_length: typeof row.context_length === 'number' ? row.context_length : 0,
      };
    })
    .filter((item): item is UserSelectableModel => item !== null);
  return {
    supports_reasoning_effort: !!main.supports_reasoning_effort,
    user_model_switch_enabled: !!switchInfo.enabled,
    user_selectable_models: models,
    main_context_length: typeof main.context_length === 'number' ? main.context_length : 0,
    system_prompt_tokens: typeof main.system_prompt_tokens === 'number' ? main.system_prompt_tokens : 0,
    can_read_image: !!vision.can_read_image,
    vision_bridge_enabled: !!vision.bridge_enabled,
    attachment_preview_chars: typeof main.attachment_preview_chars === 'number'
      ? main.attachment_preview_chars
      : 0,
  };
}

export async function getCatalog(): Promise<Catalog> {
  const wrapped = await apiRequest<unknown>('/v1/catalog');
  const data = unwrapData<JsonObject>(wrapped);
  return {
    skills: (Array.isArray(data.skills) ? data.skills : []) as CatalogItem[],
    agents: (Array.isArray(data.agents) ? data.agents : []) as CatalogItem[],
    mcp: (Array.isArray(data.mcp) ? data.mcp : []) as CatalogItem[],
    kb: (Array.isArray(data.kb) ? data.kb : []) as CatalogItem[],
  };
}

export async function updateCatalogItem(
  kind: 'skills' | 'agents' | 'mcp' | 'kb',
  itemId: string,
  enabled: boolean
): Promise<void> {
  await apiRequest(`/v1/catalog/${kind}/${itemId}`, {
    method: 'PATCH',
    body: JSON.stringify({ enabled }),
  });
}

export async function listSessions(page: number = 1, pageSize: number = 50): Promise<SessionListResponse> {
  const wrapped = await apiRequest<unknown>(`/v1/chats?page=${page}&page_size=${pageSize}`);
  const data = unwrapData<PaginatedData<JsonObject>>(wrapped);
  const items = Array.isArray(data.items) ? data.items.map((item) => toChatItem(item)) : [];
  const pagination = data.pagination;
  const localItems = await listLocalSessions(page, pageSize);
  return {
    items: [...items, ...localItems],
    total: (pagination?.total_items ?? items.length) + localItems.length,
    page: pagination?.page ?? page,
    page_size: pagination?.page_size ?? pageSize,
    has_more: Boolean(pagination?.has_next),
  };
}

/** 双模式：从本机执行面拉「本地项目的会话」，并登记 chat→local 路由。其余场景返回空。 */
export async function listLocalSessions(page: number = 1, pageSize: number = 50): Promise<ChatItem[]> {
  if (!isHybridDual()) return [];
  try {
    const wrapped = await apiRequest<unknown>(
      `/v1/chats?page=${page}&page_size=${pageSize}`,
      undefined,
      'local',
    );
    const data = unwrapData<PaginatedData<JsonObject>>(wrapped);
    const items = Array.isArray(data.items) ? data.items.map((item) => toChatItem(item)) : [];
    items.forEach((it) => registerLocalChat(it.id));
    return items;
  } catch {
    return []; // 本机服务未就绪：不阻塞云端会话列表
  }
}

export interface SearchResultItem extends ChatItem {
  match_type?: 'title' | 'content';
  matched_snippet?: string;
}

export async function searchSessions(
  query: string,
  page = 1,
  pageSize = 20,
): Promise<{ items: SearchResultItem[]; total: number }> {
  const wrapped = await apiRequest<unknown>(
    `/v1/chats/search?q=${encodeURIComponent(query)}&scope=all&page=${page}&page_size=${pageSize}`,
  );
  const data = unwrapData<{ items: JsonObject[]; total: number }>(wrapped);
  return {
    items: (data.items || []).map((raw) => ({
      ...toChatItem(raw),
      match_type: (raw.match_type as 'title' | 'content') || 'title',
      matched_snippet: typeof raw.matched_snippet === 'string' ? raw.matched_snippet : undefined,
    })),
    total: data.total ?? 0,
  };
}

export async function getSession(chatId: string): Promise<ChatItem> {
  const wrapped = await apiRequest<unknown>(
    `/v1/chats/${chatId}`,
    undefined,
    isLocalChat(chatId) ? 'local' : undefined,
  );
  const data = unwrapData<JsonObject>(wrapped);
  return toChatItem(data);
}

export async function createSession(data: CreateSessionRequest): Promise<ChatItem> {
  const projectId = (data as { project_id?: string }).project_id;
  const local = isLocalProject(projectId);
  const wrapped = await apiRequest<unknown>(
    '/v1/chats',
    { method: 'POST', body: JSON.stringify(data) },
    local ? 'local' : undefined,
  );
  const payload = unwrapData<JsonObject>(wrapped);
  const item = toChatItem(payload);
  if (local) registerLocalChat(item.id);
  return item;
}

export async function updateSession(chatId: string, data: UpdateSessionRequest): Promise<ChatItem> {
  const wrapped = await apiRequest<unknown>(
    `/v1/chats/${chatId}`,
    { method: 'PATCH', body: JSON.stringify(data) },
    isLocalChat(chatId) ? 'local' : undefined,
  );
  const payload = unwrapData<JsonObject>(wrapped);
  return {
    id: chatId,
    title: String(payload.title ?? '新对话'),
    createdAt: Date.now(),
    updatedAt: toTimestamp(payload.updated_at),
    messages: [],
    favorite: Boolean(payload.favorite),
    pinned: Boolean(payload.pinned),
    businessTopic: undefined,
  };
}

/** 侧边栏手动拖拽顺序（chat_id 序列，空数组＝按默认「置顶 + 最近更新」排）。
 *  存在账号维度（users_shadow.metadata），换设备/换浏览器仍跟随账号。 */
export async function getSidebarChatOrder(): Promise<string[]> {
  const wrapped = await apiRequest<unknown>('/v1/chats/sidebar-order');
  const data = unwrapData<{ order?: unknown }>(wrapped);
  return Array.isArray(data.order) ? data.order.map(String) : [];
}

export async function saveSidebarChatOrder(order: string[]): Promise<void> {
  await apiRequest('/v1/chats/sidebar-order', {
    method: 'PUT',
    body: JSON.stringify({ order }),
  });
}

export async function deleteSession(chatId: string): Promise<void> {
  await apiRequest(
    `/v1/chats/${chatId}`,
    { method: 'DELETE' },
    isLocalChat(chatId) ? 'local' : undefined,
  );
}

export type ChatDetail = {
  chat_id: string;
  title: string;
  user_id: string;
  project_id: string | null;
  pinned?: boolean;
  favorite?: boolean;
  metadata?: Record<string, unknown>;
  /** 会话最后一次被写入的时间。后端每落一条消息都会推进它，所以它是"服务端这边又有新
   *  内容了"最省事的判据——比对消息条数不行：内部唤醒指令落库但不进消息列表。 */
  updated_at?: string;
} & EditionChatDetailFields;

/** Fetch chat detail, extended by the active edition's response contract. */
export async function getChatDetail(chatId: string): Promise<ChatDetail> {
  const wrapped = await apiRequest<unknown>(
    `/v1/chats/${encodeURIComponent(chatId)}`,
    undefined,
    isLocalChat(chatId) ? 'local' : undefined,
  );
  return unwrapData<ChatDetail>(wrapped);
}

export async function getChatMessages(chatId: string): Promise<ChatMessage[]> {
  const wrapped = await apiRequest<unknown>(
    `/v1/chats/${chatId}/messages`,
    undefined,
    isLocalChat(chatId) ? 'local' : undefined,
  );
  const data = unwrapData<PaginatedData<JsonObject>>(wrapped);
  const rawItems = Array.isArray(data.items) ? data.items : [];
  // 后台作业的唤醒指令（进度播报 / 终态交付）以 user 角色落库，好让模型在历史里看见它，
  // 但它是内部提示词、不是用户说的话。后端已经在消息列表接口里滤掉，这里再挡一道：
  // 老后端配新前端时也不会把「[系统] 进度播报：…」贴进对话。
  const items = rawItems.filter(
    (item) => !(item.metadata as JsonObject | undefined)?.hidden_in_chat,
  );
  return items.map((item) => ({
    role: String(item.role) === 'assistant' ? 'assistant' : 'user',
    content: String(item.content ?? ''),
    isMarkdown: Boolean((item.metadata as JsonObject | undefined)?.is_markdown),
    ts: toTimestamp(item.created_at),
    messageId: typeof item.message_id === 'string' ? item.message_id : undefined,
    citations: Array.isArray((item.metadata as JsonObject | undefined)?.citations)
      ? ((item.metadata as JsonObject).citations as ChatMessage['citations'])
      : undefined,
  }));
}

export interface ToolCallResultPayload {
  tool_id: string;
  tool_name?: string;
  status?: string;
  result: unknown;
  /** 后端在宽档上限下仍然截断了（极端大结果）——展开后正文里带说明。 */
  truncated: boolean;
}

/**
 * 按需取回某个工具调用的完整结果。
 *
 * 历史列表只下发梗概（后端 `_tool_calls_for_history`）：一页 100 条消息、每条挂着
 * 好几张工具卡，把完整结果一起搬进浏览器正是"打开长对话就卡死"的来源。用户真正
 * 展开哪张卡，才来取哪一张。
 */
export async function getToolCallResult(
  chatId: string,
  messageId: string,
  toolId: string,
): Promise<ToolCallResultPayload> {
  const wrapped = await apiRequest<unknown>(
    `/v1/chats/${encodeURIComponent(chatId)}/messages/${encodeURIComponent(messageId)}`
    + `/tool-calls/${encodeURIComponent(toolId)}`,
    undefined,
    isLocalChat(chatId) ? 'local' : undefined,
  );
  return unwrapData<ToolCallResultPayload>(wrapped);
}

export interface ChatContextState {
  context_usage?: unknown;
  context_compaction?: unknown;
}

/** Lightweight projection used to observe background compaction completion. */
export async function getChatContextState(chatId: string): Promise<ChatContextState> {
  const wrapped = await apiRequest<unknown>(
    `/v1/chats/${encodeURIComponent(chatId)}/context-usage`,
    undefined,
    isLocalChat(chatId) ? 'local' : undefined,
  );
  return unwrapData<ChatContextState>(wrapped);
}

/** Build "?key=value&..." string while skipping nullish values. */
function buildQuery(params: Record<string, string | number | undefined | null>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue;
    parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  }
  return parts.length > 0 ? `?${parts.join('&')}` : '';
}

/**
 * Cancel a running chat run. Truly kills the backend asyncio task.
 * `userId` is a fallback for non-cookie auth (mock/dev); production uses cookies.
 */
export async function cancelChatRun(runId: string, userId?: string, chatId?: string): Promise<void> {
  // runId 无法反查归属，路径推断兜底失效——本机会话需显式传 chatId 打路由头，
  // 否则取消请求落云端，本机任务杀不掉。
  await apiRequest<unknown>(`/v1/chat-runs/${runId}/cancel${buildQuery({ user_id: userId })}`, {
    method: 'POST',
    headers: { ...chatTargetHeaders(chatId) },
  });
}

export type ChatSteerDeliveryMode = 'steer' | 'followUp' | 'nextRun';

export interface ChatSteerQueueItem {
  queue_id: string;
  steer_id: string;
  run_id: string | null;
  chat_id: string;
  steer_seq: number;
  target_operation_seq: number | null;
  delivery_mode: 'steer' | 'follow_up' | 'next_run';
  status: 'accepted' | 'claimed' | 'applied' | 'cancelled' | 'superseded';
  message: string;
  delivery_attempt: number;
  superseded_by: string | null;
  applied_run_id: string | null;
  applied_source_run_id: string | null;
  applied_operation_seq: number | null;
  applied_user_message_id?: string | null;
  applied_run_message_id?: string | null;
  applied_run_status?: string | null;
}

/** Durably queue a steer, follow-up, or independent next-run instruction. */
export async function steerChatRun(
  runId: string,
  steerId: string,
  content: string,
  chatId?: string,
  options?: { deliveryMode?: ChatSteerDeliveryMode; replaceLatest?: boolean },
): Promise<ChatSteerQueueItem> {
  const wrapped = await apiRequest<unknown>(`/v1/chat-runs/${encodeURIComponent(runId)}/steer`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...chatTargetHeaders(chatId) },
    body: JSON.stringify({
      steer_id: steerId,
      message: content,
      delivery_mode: options?.deliveryMode ?? 'steer',
      replace_latest: options?.replaceLatest ?? true,
    }),
  });
  return unwrapData<ChatSteerQueueItem>(wrapped);
}

/** Query durable queue state for refresh/recovery UI. */
export async function getChatRunSteers(
  runId: string,
  chatId?: string,
): Promise<ChatSteerQueueItem[]> {
  const wrapped = await apiRequest<unknown>(
    `/v1/chat-runs/${encodeURIComponent(runId)}/steers`,
    { headers: { ...chatTargetHeaders(chatId) } },
  );
  return unwrapData<{ items?: ChatSteerQueueItem[] }>(wrapped)?.items ?? [];
}

/** Withdraw an instruction that has not yet reached a tool boundary. */
export async function withdrawChatRunSteer(
  runId: string,
  steerId: string,
  chatId?: string,
): Promise<boolean> {
  const wrapped = await apiRequest<unknown>(
    `/v1/chat-runs/${encodeURIComponent(runId)}/steer/${encodeURIComponent(steerId)}`,
    { method: 'DELETE', headers: { ...chatTargetHeaders(chatId) } },
  );
  const data = unwrapData<{ removed?: boolean }>(wrapped);
  return data?.removed === true;
}

/**
 * Discover whether a chat has an in-flight backend run (for resume after refresh).
 * Returns null if no active run.
 */
export interface ActiveChatRun {
  run_id: string;
  message_id: string;
  status: string;
  started_at: string | null;
  last_event_offset: number;
  kind: string;
  plan_id?: string | null;
  /** Thinking mode of the original run — lets the resume SSE parser start in
   *  the correct phase so reasoning isn't flattened into the answer body. */
  enable_thinking?: boolean;
}

export async function getActiveChatRun(
  chatId: string,
  userId?: string,
): Promise<ActiveChatRun | null> {
  const res = await apiRequest<unknown>(`/v1/chats/${chatId}/active-run${buildQuery({ user_id: userId })}`);
  const data = unwrapData<unknown>(res);
  if (!data || typeof data !== 'object') return null;
  return data as ActiveChatRun;
}

/**
 * Open the resume SSE stream for an existing run. Returns a fetch Response
 * whose body is an SSE stream — the caller pipes it through the same
 * SSE handler as the live chat stream (see hooks/useStreaming.ts).
 */
export async function followChatRun(
  runId: string,
  fromOffset: number = 0,
  signal?: AbortSignal,
  userId?: string,
  chatId?: string,
): Promise<Response> {
  const qs = buildQuery({ from: fromOffset, user_id: userId });
  const url = `${getApiUrl()}/v1/chats/stream/${encodeURIComponent(runId)}${qs || '?from=0'}`;
  return authFetch(url, { method: 'GET', signal, headers: { ...chatTargetHeaders(chatId) } });
}

/**
 * Regenerate an assistant message. Returns a fetch Response with SSE body.
 */
export async function regenerateMessage(
  chatId: string,
  messageIndex: number,
  signal?: AbortSignal,
): Promise<Response> {
  return authFetch(`${getApiUrl()}/v1/chats/${chatId}/regenerate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...chatTargetHeaders(chatId) },
    body: JSON.stringify({ message_index: messageIndex }),
    signal,
  });
}

/** §13 MySpace write confirmation: out-of-band approve/reject of a single write
 *  operation on "MySpace". Also carries the site-building 3-way design pick
 *  (decision: 'choice' + optionId / 'skip') — both go through the same backend
 *  endpoint and the same stale/chat_interrupted invalidation contract. */
export async function confirmFileWrite(
  chatId: string,
  confirmId: string,
  decision: FileConfirmDecision | 'choice' | 'skip',
  optionId?: string,
): Promise<{
  ok: boolean;
  // Confirmation has expired (backend timeout reclaim / process restart): backend
  // returns 200 + stale. Not the user's fault — the frontend silently dismisses
  // the confirm bar plus a friendly hint, not treated as an error.
  stale?: boolean;
  // Plan F mid-term: True means this stale was caused by a server restart killing
  // the agent task, not an ordinary timeout — the user must send a new message to
  // continue. The frontend shows a prominent notice for this instead of silently
  // dismissing the bar.
  chat_interrupted?: boolean;
  message?: string;
  decision?: string;
  op?: string;
  logical_path?: string;
  cascaded?: string[];
}> {
  const wrapped = await apiRequest<unknown>(`/v1/chats/${chatId}/file-confirm`, {
    method: 'POST',
    body: JSON.stringify({
      confirm_id: confirmId,
      decision,
      ...(optionId ? { option_id: optionId } : {}),
    }),
  });
  return unwrapData(wrapped);
}

/** §13: backend shape (snake_case) → frontend FileConfirmInfo. SSE events and REST
 *  payloads share this mapping so the field contract can't drift in two places. */
export function toFileConfirmInfo(r: Record<string, unknown>): FileConfirmInfo {
  return {
    confirmId: String(r.confirm_id ?? ''),
    op: String(r.op ?? ''),
    logicalPath: String(r.logical_path ?? ''),
    message: typeof r.message === 'string' ? r.message : undefined,
    kind: typeof r.kind === 'string' ? r.kind : undefined,
  };
}

/** Site-building 3-way design pick: backend shape (snake_case) → frontend DesignPickInfo.
 *  The SSE design_pick event and pending-confirm recovery share this mapping. */
export function toDesignPickInfo(r: Record<string, unknown>): DesignPickInfo {
  const rawOpts = Array.isArray(r.options)
    ? (r.options as Record<string, unknown>[])
    : [];
  return {
    confirmId: String(r.confirm_id ?? ''),
    question: typeof r.question === 'string' ? r.question : '',
    options: rawOpts
      .map((o) => ({
        id: String(o.id ?? ''),
        title: String(o.title ?? ''),
        brief: typeof o.brief === 'string' && o.brief ? o.brief : undefined,
        imageFileId: String(o.image_file_id ?? ''),
      }))
      .filter((o) => o.id && o.imageFileId),
  };
}

/** Site-building 3-way design pick: submit the user's choice out-of-band (optionId null = let the assistant decide). */
export function submitDesignPick(
  chatId: string,
  confirmId: string,
  optionId: string | null,
) {
  return confirmFileWrite(
    chatId, confirmId, optionId ? 'choice' : 'skip', optionId ?? undefined,
  );
}

/** §13: fetch ALL pending confirmations for a single chat (restores the whole
 *  queue on refresh / tab switch-back). One round of parallel tool calls can
 *  concurrently register N distinct pending items, so all must be retrieved and
 *  queued. The result is split by kind: write-confirm queue + site-building
 *  design picker (the latter has single-value semantics — take the latest). */
export async function getPendingConfirm(
  chatId: string,
): Promise<{ confirms: FileConfirmInfo[]; designPick: DesignPickInfo | null }> {
  const wrapped = await apiRequest<{
    pendings?: Record<string, unknown>[];
  }>(`/v1/chats/${chatId}/pending-confirm`);
  const { pendings } = unwrapData<{
    pendings?: Record<string, unknown>[];
  }>(wrapped);
  const list = Array.isArray(pendings) ? pendings : [];
  const picks = list.filter((r) => String(r.kind ?? '') === 'design_pick');
  const confirms = list
    .filter((r) => String(r.kind ?? '') !== 'design_pick')
    .map(toFileConfirmInfo);
  const pick = picks.length ? toDesignPickInfo(picks[picks.length - 1]) : null;
  return { confirms, designPick: pick && pick.confirmId ? pick : null };
}

/** §13: batch-fetch all of the current user's chats that have pending
 *  confirmations (lights up the sidebar blue dot on first load / refresh).
 *  Split by kind: write confirms go into the pendingConfirm queue (kept
 *  homogeneous), design_pick goes into the single pendingDesignPick slot —
 *  both contribute to the sidebar blue dot. */
export async function listPendingConfirms(): Promise<{
  confirms: Array<{ chatId: string; info: FileConfirmInfo }>;
  designPicks: Array<{ chatId: string; info: DesignPickInfo }>;
}> {
  const wrapped = await apiRequest<{ items: Record<string, unknown>[] }>(
    '/v1/chats/pending-confirms',
  );
  const { items } = unwrapData<{ items: Record<string, unknown>[] }>(wrapped);
  const confirms: Array<{ chatId: string; info: FileConfirmInfo }> = [];
  const designPicks: Array<{ chatId: string; info: DesignPickInfo }> = [];
  for (const it of items || []) {
    const chatId = String(it.chat_id ?? '');
    if (!chatId) continue;
    if (String(it.kind ?? '') === 'design_pick') {
      const pick = toDesignPickInfo(it);
      if (pick.confirmId) designPicks.push({ chatId, info: pick });
    } else {
      confirms.push({ chatId, info: toFileConfirmInfo(it) });
    }
  }
  return { confirms, designPicks };
}

/** DSH recommendation suffix → display label plus badge state.
 * The original label stays authoritative in the backend and is what the model
 * receives in `answers[].selected`; this helper only cleans up presentation. */
export function parseRecommendedLabel(label: string): {
  label: string;
  recommended: boolean;
} {
  const suffix = /\s*(?:\((?:recommended|推荐)\)|（(?:recommended|推荐)）)\s*$/i;
  return suffix.test(label)
    ? { label: label.replace(suffix, ''), recommended: true }
    : { label, recommended: false };
}

/** Backend internal transport → resident-composer state. */
export function toUserQuestionRequest(raw: Record<string, unknown>): UserQuestionRequest {
  const rawQuestions = Array.isArray(raw.questions)
    ? raw.questions as Record<string, unknown>[]
    : [];
  return {
    requestId: String(raw.request_id ?? ''),
    createdAt: typeof raw.created_at === 'number' ? raw.created_at : undefined,
    expiresAt: typeof raw.expires_at === 'number' ? raw.expires_at : undefined,
    questions: rawQuestions
      .map((question) => {
        const rawOptions = Array.isArray(question.options)
          ? question.options as Record<string, unknown>[]
          : [];
        return {
          id: String(question.id ?? ''),
          header: typeof question.header === 'string' && question.header
            ? question.header : undefined,
          question: String(question.question ?? ''),
          description: typeof question.description === 'string' && question.description
            ? question.description : undefined,
          multiSelect: question.multi_select === true,
          options: rawOptions
            .map((option) => {
              const display = parseRecommendedLabel(String(option.label ?? ''));
              return {
                id: String(option.id ?? ''),
                label: display.label,
                description: typeof option.description === 'string' && option.description
                  ? option.description : undefined,
                recommended: option.recommended === true || display.recommended,
              };
            })
            .filter((option) => option.id && option.label),
        };
      })
      .filter((question) => question.id && question.question),
  };
}

/** Merge a pending-GET snapshot without dropping requests delivered by SSE
 * after that GET started. Resolved-event tombstones are applied by uiStore
 * during hydration, so this helper only protects the newer-arrival side. */
export function mergePendingUserQuestionRecovery(
  snapshot: UserQuestionRequest[],
  current: UserQuestionRequest[],
  requestIdsAtStart: ReadonlySet<string>,
): UserQuestionRequest[] {
  const merged = [...snapshot];
  for (const request of current) {
    if (requestIdsAtStart.has(request.requestId)) continue;
    if (!merged.some((item) => item.requestId === request.requestId)) {
      merged.push(request);
    }
  }
  return merged;
}

export async function getPendingUserQuestions(chatId: string): Promise<UserQuestionRequest[]> {
  const wrapped = await apiRequest<{ requests?: Record<string, unknown>[] }>(
    `/v1/chats/${chatId}/pending-user-questions`,
  );
  const { requests } = unwrapData<{ requests?: Record<string, unknown>[] }>(wrapped);
  return (Array.isArray(requests) ? requests : [])
    .map(toUserQuestionRequest)
    .filter((request) => request.requestId && request.questions.length);
}

export async function listPendingUserQuestions(): Promise<
  Array<{ chatId: string; request: UserQuestionRequest }>
> {
  const wrapped = await apiRequest<{ items?: Record<string, unknown>[] }>(
    '/v1/chats/pending-user-questions',
  );
  const { items } = unwrapData<{ items?: Record<string, unknown>[] }>(wrapped);
  const out: Array<{ chatId: string; request: UserQuestionRequest }> = [];
  for (const item of Array.isArray(items) ? items : []) {
    const chatId = String(item.chat_id ?? '');
    const request = toUserQuestionRequest(item);
    if (chatId && request.requestId && request.questions.length) {
      out.push({ chatId, request });
    }
  }
  return out;
}

export async function answerUserQuestion(
  chatId: string,
  requestId: string,
  answers: UserQuestionAnswer[],
): Promise<{
  ok: boolean;
  outcome?: string;
  stale?: boolean;
  chat_interrupted?: boolean;
  message?: string;
}> {
  const wrapped = await apiRequest<unknown>(
    `/v1/chats/${chatId}/user-questions/${encodeURIComponent(requestId)}/answer`,
    { method: 'POST', body: JSON.stringify({ answers }) },
  );
  return unwrapData(wrapped);
}

export async function cancelUserQuestion(
  chatId: string,
  requestId: string,
): Promise<{
  ok: boolean;
  outcome?: string;
  stale?: boolean;
  chat_interrupted?: boolean;
  message?: string;
}> {
  const wrapped = await apiRequest<unknown>(
    `/v1/chats/${chatId}/user-questions/${encodeURIComponent(requestId)}/cancel`,
    { method: 'POST' },
  );
  return unwrapData(wrapped);
}

/**
 * Edit a user message and regenerate. Returns a fetch Response with SSE body.
 */
export async function editAndRegenerate(
  chatId: string,
  messageIndex: number,
  newContent: string,
  signal?: AbortSignal,
): Promise<Response> {
  return authFetch(`${getApiUrl()}/v1/chats/${chatId}/edit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...chatTargetHeaders(chatId) },
    body: JSON.stringify({ message_index: messageIndex, new_content: newContent }),
    signal,
  });
}

export async function getFollowUpQuestions(
  chatId: string,
  messageId: string,
): Promise<string[]> {
  try {
    const wrapped = await apiRequest<unknown>(
      `/v1/chats/${chatId}/messages/${messageId}/followups`,
    );
    const data = unwrapData<{ follow_up_questions?: string[] }>(wrapped);
    return Array.isArray(data?.follow_up_questions) ? data.follow_up_questions : [];
  } catch {
    return [];
  }
}

export async function getCurrentUser(): Promise<UserInfo> {
  const wrapped = await apiRequest<unknown>('/v1/me');
  const data = unwrapData<JsonObject>(wrapped);
  return {
    user_id: String(data.user_id ?? ''),
    username: String(data.username ?? ''),
    email: typeof data.email === 'string' ? data.email : undefined,
    avatar_url: typeof data.avatar === 'string' ? data.avatar : undefined,
  };
}

export async function getUserPreferences(userId: string): Promise<UserPreferences> {
  const wrapped = await apiRequest<unknown>(`/v1/users/${userId}/preferences`);
  return unwrapData<UserPreferences>(wrapped);
}

export async function updateUserPreferences(userId: string, preferences: UserPreferences): Promise<void> {
  await apiRequest(`/v1/users/${userId}/preferences`, {
    method: 'PUT',
    body: JSON.stringify(preferences),
  });
}

export async function healthCheck(): Promise<HealthResponse> {
  return await apiRequest<HealthResponse>('/health');
}

export interface KBDocumentItem {
  id: string;
  document_id?: string;
  title: string;
  name?: string;
  filename?: string;
  desc?: string;
  word_count?: number;
  size?: number;
  size_bytes?: number;
  indexing_status?: string;
  enabled?: boolean;
  data_source_type?: string;
  created_at?: number | string;
  uploaded_at?: number | string;
  createdAt?: number | string;
  uploadedAt?: number | string;
  content?: string;
}

export async function getKBDocuments(
  kbId: string,
  page = 1,
  pageSize = 20,
  keyword?: string,
): Promise<KBDocumentsResponse> {
  try {
    const kw = keyword?.trim() ? `&keyword=${encodeURIComponent(keyword.trim())}` : '';
    const wrapped = await apiRequest<unknown>(
      `/v1/catalog/kb/${kbId}/documents?page=${page}&page_size=${pageSize}${kw}`,
    );
    const data = unwrapData<PaginatedData<KBDocumentItem> & { status_counts?: KBDocumentStatusCounts }>(wrapped);
    const items = Array.isArray(data.items) ? data.items : [];
    const pagination = data.pagination;
    return {
      items,
      total: typeof pagination?.total_items === 'number' ? pagination.total_items : items.length,
      page: typeof pagination?.page === 'number' ? pagination.page : page,
      page_size: typeof pagination?.page_size === 'number' ? pagination.page_size : pageSize,
      has_more: Boolean(pagination?.has_next),
      status_counts: data.status_counts,
    };
  } catch {
    return {
      items: [],
      total: 0,
      page,
      page_size: pageSize,
      has_more: false,
    };
  }
}

export async function getKBDocumentDetail(
  kbId: string,
  _documentId: string,
): Promise<{ title: string; content: string; desc?: string }> {
  const wrapped = await apiRequest<unknown>(
    `/v1/catalog/kb/${kbId}/documents/${_documentId}`,
  );
  const data = unwrapData<{ title?: string; content?: string; desc?: string }>(wrapped);
  const rawTitle = typeof data.title === 'string' ? data.title.trim() : '';
  return {
    title: rawTitle,
    content: data.content || '',
    desc: data.desc,
  };
}

// ── Private knowledge base management API ──────────────────────────────────

export interface IndexingConfig {
  parent_chunk_size?: number;
  child_chunk_size?: number;
  overlap_tokens?: number;
  parent_child_indexing?: boolean;
  auto_keywords_count?: number;
  auto_questions_count?: number;
  // Parent-chunk separator hierarchy (only effective for recursive chunking and the recursive fallback of semantic chunking); empty uses the built-in defaults
  separators?: string[];
  // Child-chunk separator hierarchy (in parent-child chunking, child chunks are split by this, then packed by child_size); empty falls back to fixed-length sliding window
  child_separators?: string[];
}

export async function createKBSpace(
  name: string,
  description?: string,
  chunkMethod?: string,
  indexingConfig?: IndexingConfig,
  visibility?: 'private' | 'public',
  indexModes?: KBIndexMode[],
  wikiConfig?: WikiConfig,
): Promise<Record<string, unknown>> {
  const wrapped = await apiRequest<unknown>('/v1/catalog/kb', {
    method: 'POST',
    body: JSON.stringify({
      name,
      description: description || undefined,
      chunk_method: chunkMethod || 'semantic',
      indexing_config: indexingConfig || undefined,
      visibility: visibility || 'private',
      index_modes: indexModes && indexModes.length ? indexModes : undefined,
      wiki_config: wikiConfig || undefined,
    }),
  });
  return unwrapData<Record<string, unknown>>(wrapped);
}

export async function updateKBSpace(
  kbId: string,
  payload: {
    name?: string;
    description?: string;
    index_modes?: KBIndexMode[];
    wiki_config?: WikiConfig;
  },
): Promise<Record<string, unknown>> {
  const wrapped = await apiRequest<unknown>(`/v1/catalog/kb/${kbId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
  return unwrapData<Record<string, unknown>>(wrapped);
}

/** 单库 Wiki 状态：是否具备 Wiki 能力 + 生成进度。无权访问时返回 supports_wiki:false。 */
export async function getKBWikiStatus(kbId: string): Promise<KBWikiStatus> {
  try {
    const wrapped = await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/wiki/capability`);
    return unwrapData<KBWikiStatus>(wrapped);
  } catch {
    return { kb_id: kbId, supports_wiki: false };
  }
}

/** 把该库全部已索引文档重新排进 Wiki 生成队列（建库后才勾 Wiki、或想换粒度重来）。 */
export async function rebuildKBWiki(kbId: string): Promise<{ job_id: string; documents: number }> {
  const wrapped = await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/wiki/rebuild`, {
    method: 'POST',
  });
  return unwrapData<{ job_id: string; documents: number }>(wrapped);
}

export async function polishKBDescription(
  name: string,
  description?: string,
): Promise<string> {
  const wrapped = await apiRequest<unknown>('/v1/catalog/kb/polish-description', {
    method: 'POST',
    body: JSON.stringify({
      name,
      description: description || undefined,
    }),
  });
  const data = unwrapData<{ description?: string }>(wrapped);
  return typeof data.description === 'string' ? data.description : '';
}

export async function uploadKBDocument(
  kbId: string,
  file: File,
  title?: string,
  indexingConfig?: IndexingConfig,
  chunkMethod?: string,
): Promise<Record<string, unknown>> {
  const url = `${getApiUrl()}/v1/catalog/kb/${kbId}/documents`;
  const formData = new FormData();
  formData.append('file', file);
  if (title) formData.append('title', title);
  if (indexingConfig) formData.append('indexing_config', JSON.stringify(indexingConfig));
  if (chunkMethod) formData.append('chunk_method', chunkMethod);

  const response = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    body: formData,
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throwIfSessionExpired(response.status, payload);
    throw new Error(uploadErrorMessage(response.status, payload));
  }

  const payload = await response.json();
  return unwrapData<Record<string, unknown>>(payload);
}

export async function deleteKBSpace(kbId: string): Promise<void> {
  await apiRequest(`/v1/catalog/kb/${kbId}`, { method: 'DELETE' });
}

export async function deleteKBDocument(kbId: string, documentId: string): Promise<void> {
  await apiRequest(`/v1/catalog/kb/${kbId}/documents/${documentId}`, { method: 'DELETE' });
}

export async function getKBChunks(
  kbId: string,
  docId: string,
  page = 1,
  pageSize = 100,
): Promise<KBChunk[]> {
  try {
    const wrapped = await apiRequest<unknown>(
      `/v1/catalog/kb/${kbId}/chunks?document_id=${docId}&page=${page}&page_size=${pageSize}`,
    );
    const data = unwrapData<{ items?: KBChunk[] }>(wrapped);
    return Array.isArray(data.items) ? data.items : [];
  } catch {
    return [];
  }
}

export interface KBChunkChild {
  chunk_id: string;
  chunk_index: number;
  content: string;
}

/** Fetch a parent chunk's child chunks from the vector store (parent-child chunking mode; flat mode returns an empty array). */
export async function getKBChunkChildren(
  kbId: string,
  chunkId: string,
): Promise<KBChunkChild[]> {
  const wrapped = await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/chunks/${chunkId}/children`);
  const data = unwrapData<{ children?: KBChunkChild[] }>(wrapped);
  return Array.isArray(data.children) ? data.children : [];
}

export async function updateKBChunk(
  kbId: string,
  chunkId: string,
  data: { content?: string; tags?: string[]; questions?: string[] },
): Promise<void> {
  await apiRequest(`/v1/catalog/kb/${kbId}/chunks/${chunkId}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
}

export async function reindexKBDocument(
  kbId: string,
  docId: string,
  indexingConfig?: IndexingConfig,
  chunkMethod?: string,
): Promise<void> {
  const body: Record<string, unknown> = {};
  if (indexingConfig) body.indexing_config = { ...indexingConfig };
  if (chunkMethod) body.chunk_method = chunkMethod;
  await apiRequest(`/v1/catalog/kb/${kbId}/documents/${docId}/reindex`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ── Chunk preview API ───────────────────────────────────────────

export async function previewChunks(
  file: File,
  chunkMethod = 'structured',
  parentChunkSize = 1024,
  childChunkSize = 128,
  overlapTokens = 20,
  parentChildIndexing = true,
  separators?: string[],
  childSeparators?: string[],
): Promise<ChunkPreviewResult> {
  const url = `${getApiUrl()}/v1/catalog/kb/preview-chunks`;
  const formData = new FormData();
  formData.append('file', file);
  formData.append('chunk_method', chunkMethod);
  formData.append('parent_chunk_size', String(parentChunkSize));
  formData.append('child_chunk_size', String(childChunkSize));
  formData.append('overlap_tokens', String(overlapTokens));
  formData.append('parent_child_indexing', String(parentChildIndexing));
  if (separators && separators.length) formData.append('separators', JSON.stringify(separators));
  if (childSeparators && childSeparators.length) formData.append('child_separators', JSON.stringify(childSeparators));

  const response = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    body: formData,
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throwIfSessionExpired(response.status, payload);
    throw new Error(uploadErrorMessage(response.status, payload));
  }

  const payload = await response.json();
  return unwrapData<ChunkPreviewResult>(payload);
}

// ── Memory management API ───────────────────────────────────────

export async function getMemories(
  projectId?: string,
): Promise<{ enabled: boolean; items: MemoryItem[]; count: number }> {
  const url = projectId
    ? `/v1/memories?project_id=${encodeURIComponent(projectId)}`
    : '/v1/memories';
  const wrapped = await apiRequest<unknown>(url);
  return unwrapData<{ enabled: boolean; items: MemoryItem[]; count: number }>(wrapped);
}

export async function deleteMemory(memoryId: string, messageId?: string): Promise<void> {
  // `messageId` lets the backend drop the entry from the turn card that reported
  // it, so the transcript never keeps offering actions on a deleted memory.
  const qs = messageId ? `?message_id=${encodeURIComponent(messageId)}` : '';
  await apiRequest(`/v1/memories/${memoryId}${qs}`, { method: 'DELETE' });
}

export async function deleteGraphRelation(relationId: string, messageId?: string): Promise<void> {
  const qs = messageId ? `?message_id=${encodeURIComponent(messageId)}` : '';
  await apiRequest(`/v1/memories/graph/${relationId}${qs}`, { method: 'DELETE' });
}

export async function updateMemory(
  memoryId: string,
  text: string,
  messageId?: string,
): Promise<void> {
  await apiRequest(`/v1/memories/${memoryId}`, {
    method: 'PATCH',
    body: JSON.stringify({
      text,
      message_id: messageId,
      operation_id: newOperationId(),
    }),
  });
}

/** L1 profile entries are addressed by field key rather than by a store id. */
export async function updateProfileField(
  key: string,
  text: string,
  messageId?: string,
): Promise<void> {
  await apiRequest('/v1/memories/profile/field', {
    method: 'PATCH',
    body: JSON.stringify({
      key,
      text,
      message_id: messageId,
      operation_id: newOperationId(),
    }),
  });
}

export async function deleteProfileField(key: string, messageId?: string): Promise<void> {
  const params = new URLSearchParams({ key });
  if (messageId) params.set('message_id', messageId);
  await apiRequest(`/v1/memories/profile/field?${params.toString()}`, { method: 'DELETE' });
}

export async function clearAllMemories(): Promise<void> {
  await apiRequest('/v1/memories', { method: 'DELETE' });
}

export async function getMemoryProfile(workspaceId: string = 'default'): Promise<MemoryProfile> {
  const wrapped = await apiRequest<unknown>(`/v1/memories/profile?workspace_id=${encodeURIComponent(workspaceId)}`);
  return unwrapData<MemoryProfile>(wrapped);
}

export async function getMemoryGraph(
  limit: number = 30,
): Promise<{ enabled: boolean; relations: MemoryGraphRelation[]; count: number }> {
  const wrapped = await apiRequest<unknown>(`/v1/memories/graph?limit=${limit}`);
  return unwrapData<{ enabled: boolean; relations: MemoryGraphRelation[]; count: number }>(wrapped);
}

export interface UserSettings {
  memory_enabled: boolean;
  memory_write_enabled: boolean;
  mem0_available: boolean;
  embedding_available: boolean;
  memory_available: boolean;
  reranker_enabled: boolean;
  reranker_available: boolean;
}

export async function getMemorySettings(): Promise<UserSettings> {
  const wrapped = await apiRequest<unknown>('/v1/memories/settings');
  return unwrapData<UserSettings>(wrapped);
}

export async function updateMemorySettings(memoryEnabled: boolean): Promise<void> {
  await apiRequest('/v1/memories/settings', {
    method: 'PATCH',
    body: JSON.stringify({ memory_enabled: memoryEnabled }),
  });
}

export async function updateMemoryWriteSettings(memoryWriteEnabled: boolean): Promise<void> {
  await apiRequest('/v1/memories/settings', {
    method: 'PATCH',
    body: JSON.stringify({ memory_write_enabled: memoryWriteEnabled }),
  });
}

export async function updateRerankerSettings(rerankerEnabled: boolean): Promise<void> {
  await apiRequest('/v1/memories/settings', {
    method: 'PATCH',
    body: JSON.stringify({ reranker_enabled: rerankerEnabled }),
  });
}

export interface OntologySettings {
  allowed: boolean;
  ontology_enabled: boolean;
  ontology_pack_ids: string[];
  available: boolean;
  plugin_import_build_validation_forced: boolean;
  active_packs: Array<{ pack_id: string; version_id: string; version: string }>;
}

export async function getOntologySettings(): Promise<OntologySettings> {
  const wrapped = await apiRequest<unknown>('/v1/ontologies/settings');
  return unwrapData<OntologySettings>(wrapped);
}

export async function updateOntologySettings(ontologyEnabled: boolean): Promise<void> {
  await apiRequest('/v1/ontologies/settings', {
    method: 'PATCH',
    body: JSON.stringify({ ontology_enabled: ontologyEnabled }),
  });
}

/** Tags from active Domain Packs that actually trigger workflows for this asset kind. */
export async function getOntologyTagOptions(assetKind: OntologyAssetKind): Promise<OntologyTagOption[]> {
  const wrapped = await apiRequest<unknown>(
    `/v1/ontologies/tags?asset_kind=${encodeURIComponent(assetKind)}`,
  );
  const data = unwrapData<{ items?: OntologyTagOption[] }>(wrapped);
  return Array.isArray(data.items) ? data.items : [];
}

// ── Personal API keys ────────────────────────────────────────────────────

export interface ApiKeyItem {
  id: string;
  name: string;
  key_prefix: string;
  enabled: boolean;
  expires_at?: string | null;
  last_used_at?: string | null;
  created_at?: string | null;
  /** Whether the plaintext can be retrieved again (false for legacy keys with no stored ciphertext; frontend hides the "Copy" action) */
  revealable?: boolean;
  /** Plaintext is only returned by the create / reveal endpoints; null in list responses */
  api_key?: string | null;
}

export async function listApiKeys(): Promise<ApiKeyItem[]> {
  const wrapped = await apiRequest<unknown>('/v1/me/api-keys');
  const data = unwrapData<{ items?: ApiKeyItem[] }>(wrapped);
  return Array.isArray(data.items) ? data.items : [];
}

export async function createApiKey(name: string, expiresInDays: number | null, forGateway = false): Promise<ApiKeyItem> {
  const wrapped = await apiRequest<unknown>('/v1/me/api-keys', {
    method: 'POST',
    body: JSON.stringify({ name, expires_in_days: expiresInDays, for_gateway: forGateway }),
  });
  return unwrapData<ApiKeyItem>(wrapped);
}

/** Retrieve the full plaintext of an API key again (for copying). Backend returns 400 for legacy keys with no stored ciphertext. */
export async function revealApiKey(keyId: string): Promise<string> {
  const wrapped = await apiRequest<unknown>(`/v1/me/api-keys/${encodeURIComponent(keyId)}/reveal`);
  const data = unwrapData<ApiKeyItem>(wrapped);
  return data.api_key ?? '';
}

export async function toggleApiKey(keyId: string, enabled: boolean): Promise<void> {
  await apiRequest(`/v1/me/api-keys/${encodeURIComponent(keyId)}`, {
    method: 'PATCH',
    body: JSON.stringify({ enabled }),
  });
}

export async function revokeApiKey(keyId: string): Promise<void> {
  await apiRequest(`/v1/me/api-keys/${encodeURIComponent(keyId)}`, { method: 'DELETE' });
}

// ── Capability center: self-service adding of MCP servers / skills ──────────

export interface CreateMcpServerInput {
  display_name: string;
  description?: string;
  user_intro?: string;
  transport: 'streamable_http' | 'sse';
  url: string;
  headers?: Record<string, string>;
  icon?: string;
}

export async function createMyMcpServer(input: CreateMcpServerInput): Promise<{ server_id: string }> {
  const wrapped = await apiRequest<unknown>('/v1/me/mcp-servers', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return unwrapData<{ server_id: string }>(wrapped);
}

export async function deleteMyMcpServer(serverId: string): Promise<void> {
  await apiRequest(`/v1/me/mcp-servers/${encodeURIComponent(serverId)}`, { method: 'DELETE' });
}

// ── MCP marketplace (user side) ─────────────────────────────────────────────
import type { McpMarketItem, McpMarketListResult, McpMarketSubmission } from './types';

export async function getMcpMarketItems(): Promise<McpMarketListResult> {
  const wrapped = await apiRequest<unknown>('/v1/mcp-market/items');
  return unwrapData<McpMarketListResult>(wrapped);
}

export async function getMcpMarketItem(slug: string): Promise<McpMarketItem> {
  const wrapped = await apiRequest<unknown>(`/v1/mcp-market/items/${encodeURIComponent(slug)}`);
  return unwrapData<McpMarketItem>(wrapped);
}

export async function installMcpMarketItem(
  slug: string,
  credentials: Record<string, string> = {},
  confirmHighRisk = false,
  authMethod?: string,
): Promise<{ server_id: string; action: string }> {
  const wrapped = await apiRequest<unknown>('/v1/mcp-market/install', {
    method: 'POST',
    body: JSON.stringify({
      slug,
      auth_method: authMethod,
      credentials,
      confirm_high_risk: confirmHighRisk,
    }),
  });
  return unwrapData<{ server_id: string; action: string }>(wrapped);
}

export async function startMcpMarketOAuth(input: {
  slug: string;
  auth_method: string;
  credentials?: Record<string, string>;
  client_id?: string;
  client_secret?: string;
  confirm_high_risk?: boolean;
}): Promise<{ flow_id: string; authorization_url: string; status: string }> {
  const wrapped = await apiRequest<unknown>('/v1/mcp-market/oauth/start', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return unwrapData<{ flow_id: string; authorization_url: string; status: string }>(wrapped);
}

export async function getMcpMarketOAuthStatus(flowId: string): Promise<{
  status: string;
  error?: string;
  result?: { server_id?: string; action?: string };
}> {
  const wrapped = await apiRequest<unknown>(`/v1/mcp-market/oauth/status/${encodeURIComponent(flowId)}`);
  return unwrapData(wrapped);
}

export async function cancelMcpMarketOAuth(flowId: string): Promise<void> {
  await apiRequest<unknown>(`/v1/mcp-market/oauth/cancel/${encodeURIComponent(flowId)}`, {
    method: 'POST',
  });
}

export async function submitMcpToMarketplace(input: {
  source_server_id: string;
  category: string;
  version: string;
  summary?: string;
  note?: string;
  tags?: string[];
}): Promise<McpMarketSubmission> {
  const wrapped = await apiRequest<unknown>('/v1/mcp-market/submissions', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return unwrapData<McpMarketSubmission>(wrapped);
}

export async function getMyMcpMarketSubmissions(): Promise<McpMarketSubmission[]> {
  const wrapped = await apiRequest<unknown>('/v1/mcp-market/submissions');
  return unwrapData<{ items: McpMarketSubmission[] }>(wrapped).items || [];
}

export async function withdrawMcpMarketSubmission(submissionId: string): Promise<void> {
  await apiRequest(`/v1/mcp-market/submissions/${encodeURIComponent(submissionId)}`, {
    method: 'DELETE',
  });
}

export async function uploadMySkill(file: File): Promise<{ id: string; skipped?: unknown[] }> {
  const form = new FormData();
  form.append('file', file);
  // Cannot use apiRequest (it forces a JSON Content-Type); multipart goes through fetch directly.
  const url = `${getApiUrl()}/v1/me/skills/upload`;
  const response = await fetch(url, { method: 'POST', credentials: 'include', body: form });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(readErrorMessage(payload, t('上传失败：{status}', { status: response.status })));
  }
  return unwrapData<{ id: string; skipped?: unknown[] }>(payload);
}

export interface CreateSkillInput {
  name: string;
  display_name: string;
  description: string;  // Required: a skill with an empty description cannot be loaded/registered (backend 422)
  instructions: string;
  tags?: string[];
  mcp_server_ids?: string[];
  user_intro?: string;
  icon?: string;        // preset:<key> / URL / data-URI
}

// Set the icon of my private skill (empty string = restore default)
export async function setMySkillIcon(skillId: string, icon: string): Promise<void> {
  await apiRequest(`/v1/me/skills/${encodeURIComponent(skillId)}/icon`, {
    method: 'PUT',
    body: JSON.stringify({ icon }),
  });
}

export async function createMySkill(input: CreateSkillInput): Promise<{ id: string }> {
  const wrapped = await apiRequest<unknown>('/v1/me/skills', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return unwrapData<{ id: string }>(wrapped);
}

export interface MySkillFileInfo {
  filename: string;
  size: number;
  is_binary?: boolean;
}

export interface MySkillDetail {
  id: string;
  display_name: string;
  description: string;
  instructions: string;
  tags: string[];
  mcp_server_ids: string[];
  allowed_tools?: string[];
  user_intro?: string | null;
  icon?: string | null;
  extra_files?: MySkillFileInfo[];
}

// Fetch the editable fields of my private skill (including the body) to prefill the edit form.
export async function getMySkill(skillId: string): Promise<MySkillDetail> {
  const wrapped = await apiRequest<unknown>(`/v1/me/skills/${encodeURIComponent(skillId)}`);
  return unwrapData<MySkillDetail>(wrapped);
}

export async function deleteMySkill(skillId: string): Promise<void> {
  await apiRequest(`/v1/me/skills/${encodeURIComponent(skillId)}`, { method: 'DELETE' });
}

// ── My skills: managing files inside the skill folder + zip export ──────────

// Filenames may contain subdirectory slashes: encode segment by segment, keep `/`, matching the backend {filename:path} parameter.
function encodeSkillFilePath(filename: string): string {
  return filename.split('/').map(encodeURIComponent).join('/');
}

export async function getMySkillFile(
  skillId: string,
  filename: string,
): Promise<{ filename: string; content: string; is_binary?: boolean }> {
  const wrapped = await apiRequest<unknown>(
    `/v1/me/skills/${encodeURIComponent(skillId)}/files/${encodeSkillFilePath(filename)}`,
  );
  return unwrapData<{ filename: string; content: string; is_binary?: boolean }>(wrapped);
}

export async function saveMySkillFile(skillId: string, filename: string, content: string): Promise<void> {
  await apiRequest(
    `/v1/me/skills/${encodeURIComponent(skillId)}/files/${encodeSkillFilePath(filename)}`,
    { method: 'PUT', body: JSON.stringify({ content }) },
  );
}

export async function deleteMySkillFile(skillId: string, filename: string): Promise<void> {
  await apiRequest(
    `/v1/me/skills/${encodeURIComponent(skillId)}/files/${encodeSkillFilePath(filename)}`,
    { method: 'DELETE' },
  );
}

// Upload a single file (binary-safe) to my private skill. path may include subdirectories.
export async function uploadMySkillFile(
  skillId: string,
  file: File,
  path?: string,
): Promise<{ filename: string; size: number }> {
  const form = new FormData();
  form.append('file', file);
  if (path) form.append('path', path);
  // multipart cannot go through apiRequest (it forces a JSON Content-Type)
  const url = `${getApiUrl()}/v1/me/skills/${encodeURIComponent(skillId)}/files/upload`;
  const response = await fetch(url, { method: 'POST', credentials: 'include', body: form });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(readErrorMessage(payload, t('上传失败：{status}', { status: response.status })));
  }
  return unwrapData<{ filename: string; size: number }>(payload);
}

// Export my private skill as a zip and trigger a browser download.
export async function exportMySkillZip(skillId: string): Promise<void> {
  const url = `${getApiUrl()}/v1/me/skills/${encodeURIComponent(skillId)}/export`;
  const response = await fetch(url, { credentials: 'include' });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(readErrorMessage(payload, t('导出失败：{status}', { status: response.status })));
  }
  const blob = await response.blob();
  const href = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = href;
  a.download = `${skillId}.zip`;
  a.click();
  URL.revokeObjectURL(href);
}

// ── Skill marketplace (user side) ───────────────────────────────────────────
import type { MarketplaceListResult, MarketplaceSkillDetail, MarketplaceSubmission } from './types';

export async function getMarketplaceSkills(): Promise<MarketplaceListResult> {
  const wrapped = await apiRequest<unknown>('/v1/marketplace/skills');
  return unwrapData<MarketplaceListResult>(wrapped);
}

export async function getMarketplaceSkillDetail(slug: string): Promise<MarketplaceSkillDetail> {
  const wrapped = await apiRequest<unknown>(`/v1/marketplace/skills/${encodeURIComponent(slug)}`);
  return unwrapData<MarketplaceSkillDetail>(wrapped);
}

export async function installMarketplaceSkill(
  slug: string,
  secrets: Record<string, string> = {},
): Promise<{ id: string; action?: string }> {
  const wrapped = await apiRequest<unknown>('/v1/marketplace/install', {
    method: 'POST',
    body: JSON.stringify({ slug, secrets }),
  });
  return unwrapData<{ id: string; action?: string }>(wrapped);
}

// Submit my private skill for listing on the skill marketplace (pending admin review).
export async function submitSkillToMarketplace(input: {
  skill_id: string;
  note?: string;
  category?: string;
  summary?: string;
}): Promise<MarketplaceSubmission> {
  const wrapped = await apiRequest<unknown>('/v1/marketplace/submissions', {
    method: 'POST',
    body: JSON.stringify({
      skill_id: input.skill_id,
      note: input.note || '',
      category: input.category || '',
      summary: input.summary || '',
    }),
  });
  return unwrapData<MarketplaceSubmission>(wrapped);
}

export async function getMySkillSubmissions(): Promise<MarketplaceSubmission[]> {
  const wrapped = await apiRequest<unknown>('/v1/marketplace/submissions');
  return unwrapData<{ items: MarketplaceSubmission[] }>(wrapped).items || [];
}

export async function withdrawSkillSubmission(submissionId: string): Promise<void> {
  await apiRequest(`/v1/marketplace/submissions/${encodeURIComponent(submissionId)}`, { method: 'DELETE' });
}

// ── Sub-Agent Marketplace ───────────────────────────────────────────────────
import type {
  MarketplaceAgentListResult,
  MarketplaceAgentDetail,
  AgentMarketInstallResult,
  AgentMarketSubmission,
} from './types';

export async function getMarketplaceAgents(): Promise<MarketplaceAgentListResult> {
  const wrapped = await apiRequest<unknown>('/v1/agent-marketplace/agents');
  return unwrapData<MarketplaceAgentListResult>(wrapped);
}

export async function getMarketplaceAgentDetail(slug: string): Promise<MarketplaceAgentDetail> {
  const wrapped = await apiRequest<unknown>(`/v1/agent-marketplace/agents/${encodeURIComponent(slug)}`);
  return unwrapData<MarketplaceAgentDetail>(wrapped);
}

// Installing a marketplace sub-agent = cloning it as a private sub-agent under my account (bound skills/tools are installed along with it).
export async function installMarketplaceAgent(slug: string): Promise<AgentMarketInstallResult> {
  const wrapped = await apiRequest<unknown>('/v1/agent-marketplace/install', {
    method: 'POST',
    body: JSON.stringify({ slug }),
  });
  return unwrapData<AgentMarketInstallResult>(wrapped);
}

// Submit my sub-agent for listing on the marketplace (pending admin review).
export async function submitAgentToMarketplace(input: {
  agent_id: string;
  note?: string;
  category?: string;
  summary?: string;
}): Promise<AgentMarketSubmission> {
  const wrapped = await apiRequest<unknown>('/v1/agent-marketplace/submissions', {
    method: 'POST',
    body: JSON.stringify({
      agent_id: input.agent_id,
      note: input.note || '',
      category: input.category || '',
      summary: input.summary || '',
    }),
  });
  return unwrapData<AgentMarketSubmission>(wrapped);
}

export async function getMyAgentSubmissions(): Promise<AgentMarketSubmission[]> {
  const wrapped = await apiRequest<unknown>('/v1/agent-marketplace/submissions');
  return unwrapData<{ items: AgentMarketSubmission[] }>(wrapped).items || [];
}

export async function withdrawAgentSubmission(submissionId: string): Promise<void> {
  await apiRequest(`/v1/agent-marketplace/submissions/${encodeURIComponent(submissionId)}`, { method: 'DELETE' });
}

// ── Plugins ─────────────────────────────────────────────────────────────────
import type {
  PluginListItem,
  PluginDetail,
  InstalledPluginItem,
  InstalledPluginDetail,
  PluginInstallResult,
} from './types';

export async function listPlugins(): Promise<PluginListItem[]> {
  const wrapped = await apiRequest<unknown>('/v1/plugins');
  return unwrapData<{ items: PluginListItem[] }>(wrapped).items || [];
}

export async function listInstalledPlugins(): Promise<InstalledPluginItem[]> {
  const wrapped = await apiRequest<unknown>('/v1/plugins/installed');
  return unwrapData<{ items: InstalledPluginItem[] }>(wrapped).items || [];
}

export async function getPluginDetail(slug: string): Promise<PluginDetail> {
  const wrapped = await apiRequest<unknown>(`/v1/plugins/${encodeURIComponent(slug)}`);
  return unwrapData<PluginDetail>(wrapped);
}

export async function getInstalledPluginDetail(installId: string): Promise<InstalledPluginDetail> {
  const wrapped = await apiRequest<unknown>(
    `/v1/plugins/installed/${encodeURIComponent(installId)}/detail`,
  );
  return unwrapData<InstalledPluginDetail>(wrapped);
}

export async function installPlugin(
  slug: string,
  secrets: Record<string, string> = {},
): Promise<PluginInstallResult> {
  const wrapped = await apiRequest<unknown>(`/v1/plugins/${encodeURIComponent(slug)}/install`, {
    method: 'POST',
    body: JSON.stringify({ secrets }),
  });
  return unwrapData<PluginInstallResult>(wrapped);
}

// Upload a .zip to import an external plugin (native / Claude Code / Codex). FormData upload; does not go through apiRequest (to avoid setting Content-Type).
export async function importPlugin(
  file: File,
  secrets: Record<string, string> = {},
): Promise<PluginInstallResult> {
  const url = `${getApiUrl()}/v1/plugins/import`;
  const formData = new FormData();
  formData.append('file', file);
  if (secrets && Object.keys(secrets).length > 0) {
    formData.append('secrets', JSON.stringify(secrets));
  }
  const response = await fetch(url, { method: 'POST', credentials: 'include', body: formData });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throwIfSessionExpired(response.status, payload);
    throw new Error(readErrorMessage(payload, `导入失败：${response.status}`));
  }
  return unwrapData<PluginInstallResult>(payload);
}

export async function uninstallPlugin(installId: string): Promise<void> {
  await apiRequest(`/v1/plugins/installed/${encodeURIComponent(installId)}`, { method: 'DELETE' });
}

export async function setPluginEnabled(installId: string, enabled: boolean): Promise<void> {
  await apiRequest(`/v1/plugins/installed/${encodeURIComponent(installId)}/enable`, {
    method: 'PATCH',
    body: JSON.stringify({ enabled }),
  });
}

// Edit my imported/private plugin's display metadata (name/category/icon are UI
// config — the Agent Plugins standard plugin.json carries no display fields).
export async function setInstalledPluginMeta(
  installId: string,
  meta: { display_name?: string; category?: string; icon?: string },
): Promise<void> {
  await apiRequest(`/v1/plugins/installed/${encodeURIComponent(installId)}/meta`, {
    method: 'PATCH',
    body: JSON.stringify(meta),
  });
}

export interface LarkAppInitStatus {
  configured: boolean;
  app_id: string | null;
  status: 'idle' | 'pending' | 'configured' | 'error' | string;
  verification_url: string | null;
  qr_data_uri: string | null;
  error: string | null;
}

export async function getLarkAppInitStatus(): Promise<LarkAppInitStatus> {
  const wrapped = await apiRequest<unknown>('/v1/plugins/feishu-cli/app/status');
  return unwrapData<LarkAppInitStatus>(wrapped);
}

export async function startLarkAppInit(): Promise<LarkAppInitStatus> {
  const wrapped = await apiRequest<unknown>('/v1/plugins/feishu-cli/app/init', { method: 'POST' });
  return unwrapData<LarkAppInitStatus>(wrapped);
}

export async function resetLarkAppInit(): Promise<LarkAppInitStatus> {
  const wrapped = await apiRequest<unknown>('/v1/plugins/feishu-cli/app/reset', { method: 'POST' });
  return unwrapData<LarkAppInitStatus>(wrapped);
}

// ── Auth API (SSO session) ──────────────────────────────────────────────

export interface AuthUser extends EditionAuthUserFields {
  user_id: string;
  username: string;
  email?: string;
  avatar_url?: string;
  nickname?: string | null;
  real_name?: string | null;
  department?: string | null;
  expires_at?: string;
  sso_token?: string | null;
  /** null/undefined = all enabled apps visible by default; array = only the app IDs in the list are visible */
  allowed_apps?: string[] | null;
  /** undefined/true = Lab enabled by default; false = hide and forbid access to the Lab module */
  lab_enabled?: boolean;
  /** Whether API keys may be used (default false; controlled by the Config admin platform) */
  can_use_api_key?: boolean;
  /** Whether the user may self-service add skills in the capability center (default false) */
  can_add_skill?: boolean;
  /** Whether the user may self-service add MCP tools in the capability center (default false) */
  can_add_mcp?: boolean;
  /** Whether the user may install/import plugins in the capability center (default false; controlled by the Config admin platform) */
  can_import_plugin?: boolean;
  /** Whether the user may build their own sub-agents / install from and list on the sub-agent marketplace (default false) */
  can_add_agent?: boolean;
  /** Whether the user may run the autonomous loop (the "autonomous loop" toggle in chat mode, default true) */
  can_run_autonomous_loop?: boolean;
  /** Whether the user may create their own private chat modes (设置 → 模式选择, default false) */
  can_manage_chat_modes?: boolean;
  /** Whether the user may create private knowledge bases (default false; visible only to the owner) */
  can_create_private_kb?: boolean;
  /** Whether the user may create public knowledge bases (default false; visible to everyone by default, can be further restricted by grants) */
  can_create_public_kb?: boolean;
  /** Whether the user may self-service create channel bots (inbound bots such as Feishu, default false) */
  can_create_channel_bot?: boolean;
  /** Whether the user may switch the currently available model from the chat input box (default false) */
  can_switch_model?: boolean;
  /** Whether the user may configure and use ontology validation (default false) */
  can_use_ontology_validation?: boolean;
  /** Whether the user may enter the /config system settings console without a token (default false) */
  can_system_config?: boolean;
  /** Whether the user may enter the /admin content management console without a token (default false) */
  can_content_manage?: boolean;
  /** CE bootstrap accounts must replace the temporary default password before normal use. */
  must_change_password?: boolean;
  /** CE bootstrap owner must finish the browser first-run setup before entering the app shell. */
  onboarding_required?: boolean;
}

export interface MyProfile extends AuthUser {
  user_center_id?: string;
  phone?: string | null;
  auth_source?: 'local' | 'external';
}

export async function getMyProfile(): Promise<MyProfile> {
  const wrapped = await apiRequest<unknown>('/v1/me');
  return unwrapData<MyProfile>(wrapped);
}

export interface UpdateMyProfilePayload {
  nickname?: string;
  real_name?: string;
  phone?: string;
}

export async function updateMyProfile(payload: UpdateMyProfilePayload): Promise<MyProfile> {
  const wrapped = await apiRequest<unknown>('/v1/me', {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
  return unwrapData<MyProfile>(wrapped);
}

export async function changeMyPassword(
  oldPassword: string,
  newPassword: string,
): Promise<{ user_id: string; must_change_password: boolean }> {
  const wrapped = await apiRequest<unknown>('/v1/me/password', {
    method: 'PUT',
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  });
  return unwrapData<{ user_id: string; must_change_password: boolean }>(wrapped);
}

export async function completeFirstRunSetup(): Promise<{
  user_id: string;
  onboarding_required: boolean;
  onboarding_completed_version: number;
}> {
  const wrapped = await apiRequest<unknown>('/v1/me/onboarding/complete', { method: 'POST' });
  return unwrapData<{
    user_id: string;
    onboarding_required: boolean;
    onboarding_completed_version: number;
  }>(wrapped);
}

export interface AvatarUpdateResult {
  user_id: string;
  avatar_url: string | null;
}

/** Upload a custom avatar (multipart). Returns the new avatar_url (with a timestamp cache-busting parameter). */
export async function uploadMyAvatar(blob: Blob, filename = 'avatar.png'): Promise<AvatarUpdateResult> {
  const url = `${getApiUrl()}/v1/me/avatar`;
  const formData = new FormData();
  formData.append('file', blob, filename);

  const response = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    body: formData,
  });

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throwIfSessionExpired(response.status, payload);
    throw new Error(readErrorMessage(payload, t('头像上传失败: {status}', { status: response.status })));
  }
  return unwrapData<AvatarUpdateResult>(payload);
}

/** Set the avatar to a built-in default avatar URL (whitelist: /icons/avatar/avatar-{1-8}.png etc.). */
export async function setMyAvatarUrl(avatarUrl: string): Promise<AvatarUpdateResult> {
  const wrapped = await apiRequest<unknown>('/v1/me/avatar', {
    method: 'PUT',
    body: JSON.stringify({ avatar_url: avatarUrl }),
  });
  return unwrapData<AvatarUpdateResult>(wrapped);
}

/** Clear the custom avatar and revert to the system default. */
export async function clearMyAvatar(): Promise<AvatarUpdateResult> {
  const wrapped = await apiRequest<unknown>('/v1/me/avatar', { method: 'DELETE' });
  return unwrapData<AvatarUpdateResult>(wrapped);
}

export interface ChatShareRecord {
  share_id: string;
  chat_id: string;
  origin_message_ts?: number | null;
  title: string;
  preview_url: string;
  created_at: string;
  expires_at?: string | null;
  expiry_option?: '3d' | '15d' | '3m' | 'permanent';
  created_by: string;
  created_by_username?: string;
  status: 'valid' | 'expired';
  view_count: number;
  revoked?: boolean;
}

export async function listChatShares(): Promise<ChatShareRecord[]> {
  const wrapped = await apiRequest<unknown>('/v1/chat-shares');
  const data = unwrapData<{ items?: ChatShareRecord[] }>(wrapped);
  return Array.isArray(data?.items) ? data.items : [];
}

export async function revokeChatShare(shareId: string): Promise<void> {
  await apiRequest(`/v1/chat-shares/${encodeURIComponent(shareId)}/revoke`, {
    method: 'POST',
  });
}

export async function restoreChatShare(shareId: string): Promise<void> {
  await apiRequest(`/v1/chat-shares/${encodeURIComponent(shareId)}/restore`, {
    method: 'POST',
  });
}

export async function deleteChatShare(shareId: string): Promise<void> {
  await apiRequest(`/v1/chat-shares/${encodeURIComponent(shareId)}`, {
    method: 'DELETE',
  });
}

/** Exchange a one-time SSO credential for a session cookie + user info.
 * Real OAuth2 SSO delivers the credential as `?code=`; local mock-SSO delivers
 * it as `?ticket=` — both are submitted to the backend in the `code` field. */
export async function exchangeSsoCredential(
  body: { code?: string },
): Promise<AuthUser> {
  const wrapped = await apiRequest<unknown>('/v1/auth/ticket/exchange', {
    method: 'POST',
    body: JSON.stringify(body),
  });
  return unwrapData<AuthUser>(wrapped);
}

/** Desktop plan B: exchange the current cookie session for a one-time handoff ticket.
 * Called only on the system-browser side after a successful login — once the ticket is
 * obtained the browser jumps to `hugagent://auth/callback?ticket=` to wake the desktop
 * app, which then uses the ticket against the backend directly to exchange it for the
 * real session token. */
export async function desktopHandoff(): Promise<string> {
  const wrapped = await apiRequest<unknown>('/v1/auth/desktop/handoff', {
    method: 'POST',
  });
  const data = unwrapData<{ handoff_ticket?: string }>(wrapped);
  if (!data?.handoff_ticket) {
    throw new Error('Missing handoff ticket');
  }
  return data.handoff_ticket;
}

/** Fetch the real OAuth authorize URL from the SSO login provider. Used by
 * the new provincial SSO flow where `/oa/login` returns
 * `{authorizeUrl: "..."}` and the browser must hop to that authorize URL. */
export async function getSsoAuthorizeUrl(): Promise<string | undefined> {
  const wrapped = await apiRequest<unknown>('/v1/auth/sso/authorize-url');
  const data = unwrapData<{ authorize_url?: string }>(wrapped);
  return data?.authorize_url || undefined;
}

/** Check if the current cookie session is still valid. */
export async function checkSession(): Promise<AuthUser> {
  const wrapped = await apiRequest<unknown>('/v1/auth/session/check');
  return unwrapData<AuthUser>(wrapped);
}

/** Revoke current session and clear the cookie.
 *
 * If `SSO_LOGOUT_URL` is configured on the backend, it returns an external
 * SSO logout URL via `logout_url`; the caller should redirect there so the
 * provider tears down its own session and bounces back via `redirect_uri`.
 * Otherwise the standard `login_url` fallback applies.
 */
export async function logout(): Promise<string | undefined> {
  const res = await apiRequest<unknown>('/v1/auth/logout', { method: 'POST' });
  const data = unwrapData<{ login_url?: string; logout_url?: string }>(res);
  return data?.logout_url || data?.login_url || undefined;
}

/**
 * Convenience wrapper: adds `credentials: 'include'` and handles 401.
 * Use in App.tsx for direct fetch() calls that bypass apiRequest().
 */
export function authFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  // 混合路由兜底：按 URL 中的项目/聊天归属自动打本机头（调用方显式头优先）。
  const inferred = inferTargetHeadersFromUrl(String(input));
  const mergedInit: RequestInit | undefined =
    Object.keys(inferred).length > 0
      ? { ...init, headers: { ...inferred, ...(init?.headers ?? {}) } }
      : init;
  return fetch(input, {
    ...mergedInit,
    credentials: 'include',
  }).then(async (response) => {
    if (response.status === 401 && _on401) {
      let loginUrl = '';
      try {
        const payload = await response.clone().json();
        loginUrl =
          payload?.data?.login_url ||
          payload?.detail?.data?.login_url ||
          '';
      } catch {
        // ignore parse errors
      }
      _on401(loginUrl);
    }
    return response;
  });
}

// ── Plugin UI contributions (L0 views / L1 data proxy / L2 modules) ─────────
// Generic replacements for what used to be per-plugin endpoints: the browser
// names a plugin and a declared data-source id, and the backend resolves the
// upstream URL and its credentials.

import type { PluginContributions } from './plugin-ui/types';

export async function listPluginUiContributions(): Promise<PluginContributions[]> {
  const wrapped = await apiRequest<unknown>('/v1/plugins/ui-contributions');
  return unwrapData<{ items: PluginContributions[] }>(wrapped).items || [];
}

export async function callPluginDataSource(
  slug: string,
  sourceId: string,
  params: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<unknown> {
  const wrapped = await apiRequest<unknown>(
    `/v1/plugins/${encodeURIComponent(slug)}/data/${encodeURIComponent(sourceId)}`,
    { method: 'POST', body: JSON.stringify(params), signal },
  );
  return unwrapData<unknown>(wrapped);
}

/** URL of an asset inside a plugin package's own ``web/`` directory (L2 modules). */
export function pluginWebAssetUrl(slug: string, entry: string): string {
  const relative = entry.replace(/^\/+/, '').replace(/^web\//, '');
  const path = relative.split('/').map(encodeURIComponent).join('/');
  return `${getApiUrl()}/v1/plugins/${encodeURIComponent(slug)}/web/${path}`;
}

// ── File upload API ─────────────────────────────────────────────

export interface UploadedFile {
  file_id: string;
  name: string;
  size: number;
  mime_type: string;
  download_url: string;
}

export async function uploadFile(
  file: File,
  chatId?: string,
  folderId?: string | null,
): Promise<UploadedFile> {
  const url = `${getApiUrl()}/v1/file/upload`;
  const formData = new FormData();
  formData.append('file', file);
  if (chatId) formData.append('chat_id', chatId);
  if (folderId) formData.append('folder_id', folderId);

  const response = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    body: formData,
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throwIfSessionExpired(response.status, payload);
    throw new Error(readErrorMessage(payload, `Upload failed: ${response.status}`));
  }

  const payload = await response.json();
  return unwrapData<UploadedFile>(payload);
}

/** Overwrite existing file content in-place (same file_id & URL). */
export async function overwriteFile(fileId: string, file: File): Promise<UploadedFile> {
  const url = `${getApiUrl()}/v1/file/${encodeURIComponent(fileId)}`;
  const formData = new FormData();
  formData.append('file', file);

  const response = await fetch(url, {
    method: 'PUT',
    credentials: 'include',
    body: formData,
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throwIfSessionExpired(response.status, payload);
    throw new Error(readErrorMessage(payload, `Overwrite failed: ${response.status}`));
  }

  const payload = await response.json();
  return unwrapData<UploadedFile>(payload);
}

// ── MySpace API ─────────────────────────────────────────────────

export async function getArtifacts(params?: {
  type?: 'document' | 'image';
  source_kind?: 'user_upload' | 'ai_generated';
  keyword?: string;
  scope?: 'personal' | 'all';
  /** "__root__" = personal root directory; "<id>" = direct child files of that personal folder; omitted = all personal files */
  folder_id?: string;
  page?: number;
  page_size?: number;
}): Promise<{ items: ResourceItem[]; total: number; has_more: boolean }> {
  const qs = new URLSearchParams();
  if (params?.type) qs.set('type', params.type);
  if (params?.source_kind) qs.set('source_kind', params.source_kind);
  if (params?.keyword) qs.set('keyword', params.keyword);
  if (params?.scope) qs.set('scope', params.scope);
  if (params?.folder_id) qs.set('folder_id', params.folder_id);
  if (params?.page) qs.set('page', String(params.page));
  if (params?.page_size) qs.set('page_size', String(params.page_size));
  const query = qs.toString();
  const wrapped = await apiRequest<unknown>(`/v1/artifacts${query ? '?' + query : ''}`);
  return unwrapData<{ items: ResourceItem[]; total: number; has_more: boolean }>(wrapped);
}

export async function getFavoriteChats(params?: {
  keyword?: string;
  page?: number;
  page_size?: number;
}): Promise<{ items: ResourceItem[]; total: number; has_more: boolean }> {
  const qs = new URLSearchParams();
  if (params?.keyword) qs.set('keyword', params.keyword);
  if (params?.page) qs.set('page', String(params.page));
  if (params?.page_size) qs.set('page_size', String(params.page_size));
  const query = qs.toString();
  const wrapped = await apiRequest<unknown>(`/v1/artifacts/favorites${query ? '?' + query : ''}`);
  return unwrapData<{ items: ResourceItem[]; total: number; has_more: boolean }>(wrapped);
}

export async function deleteArtifact(id: string): Promise<void> {
  await apiRequest(`/v1/artifacts/${id}`, { method: 'DELETE' });
}

export async function addArtifactToKnowledgeBase(
  artifactId: string,
  kbId: string,
): Promise<AddArtifactToKBResult> {
  const wrapped = await apiRequest<unknown>(`/v1/artifacts/${encodeURIComponent(artifactId)}/knowledge-base`, {
    method: 'POST',
    body: JSON.stringify({ kb_id: kbId }),
  });
  return unwrapData<AddArtifactToKBResult>(wrapped);
}

// ── Personal folders (MySpace) API ───────────────────────────────

import type { PersonalFolderNode } from './types';

export async function listPersonalFolderTree(): Promise<PersonalFolderNode[]> {
  const wrapped = await apiRequest<unknown>('/v1/myspace/folders?as=tree');
  const data = unwrapData<{ tree: PersonalFolderNode[] }>(wrapped);
  return data.tree || [];
}

export async function createPersonalFolder(
  name: string,
  parentFolderId: string | null,
): Promise<{ folder_id: string }> {
  const wrapped = await apiRequest<unknown>('/v1/myspace/folders', {
    method: 'POST',
    body: JSON.stringify({ name, parent_folder_id: parentFolderId }),
  });
  return unwrapData<{ folder_id: string }>(wrapped);
}

export async function renamePersonalFolder(folderId: string, name: string): Promise<void> {
  await apiRequest(`/v1/myspace/folders/${encodeURIComponent(folderId)}`, {
    method: 'PATCH',
    body: JSON.stringify({ name }),
  });
}

export async function movePersonalFolder(
  folderId: string,
  newParentFolderId: string | null,
): Promise<void> {
  await apiRequest(`/v1/myspace/folders/${encodeURIComponent(folderId)}`, {
    method: 'PATCH',
    body: JSON.stringify({ parent_folder_id: newParentFolderId }),
  });
}

export async function deletePersonalFolder(
  folderId: string,
): Promise<{ folder_id: string; artifacts_affected: number }> {
  const wrapped = await apiRequest<unknown>(
    `/v1/myspace/folders/${encodeURIComponent(folderId)}`,
    { method: 'DELETE' },
  );
  return unwrapData<{ folder_id: string; artifacts_affected: number }>(wrapped);
}

export async function getPersonalFolderAffectedCount(folderId: string): Promise<number> {
  const wrapped = await apiRequest<unknown>(
    `/v1/myspace/folders/${encodeURIComponent(folderId)}/affected-count`,
  );
  const data = unwrapData<{ count: number }>(wrapped);
  return data.count || 0;
}

export async function moveArtifactToPersonalFolder(
  artifactId: string,
  folderId: string | null,
): Promise<void> {
  await apiRequest('/v1/myspace/folders/move-artifact', {
    method: 'POST',
    body: JSON.stringify({ artifact_id: artifactId, folder_id: folderId }),
  });
}

export async function copyArtifactToPersonalFolder(
  artifactId: string,
  folderId: string | null,
): Promise<void> {
  await apiRequest('/v1/myspace/folders/copy-artifact', {
    method: 'POST',
    body: JSON.stringify({ artifact_id: artifactId, folder_id: folderId }),
  });
}

// ── Plan Mode API ─────────────────────────────────────────────────────────

import type { Plan } from './types';

export async function generatePlanStream(
  taskDescription: string,
  modelName: string = 'qwen',
  signal?: AbortSignal,
  enabledMcpIds?: string[],
  enabledSkillIds?: string[],
  enabledKbIds?: string[],
  chatId?: string,
  historyMessages?: Array<{ role: string; content: string }>,
  attachments?: Array<{ name: string; mime_type: string; file_id: string }>,
  enabledAgentIds?: string[],
  projectId?: string,
  modelProviderId?: string | null,
  // Set to true when the main agent auto-enters plan mode via enter_plan_mode:
  // task_description is an AI-expanded internal prompt, and the backend uses this
  // flag to NOT persist it as a user message, so it isn't exposed on the page after
  // a refresh.
  suppressUserEcho?: boolean,
): Promise<Response> {
  const url = `${getApiUrl()}/v1/plans/generate`;
  return authFetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      task_description: taskDescription,
      model_name: modelName,
      ...(modelProviderId ? { model_provider_id: modelProviderId } : {}),
      ...(enabledMcpIds ? { enabled_mcp_ids: enabledMcpIds } : {}),
      ...(enabledSkillIds ? { enabled_skill_ids: enabledSkillIds } : {}),
      ...(enabledKbIds ? { enabled_kb_ids: enabledKbIds } : {}),
      ...(enabledAgentIds ? { enabled_agent_ids: enabledAgentIds } : {}),
      ...(chatId ? { chat_id: chatId } : {}),
      ...(historyMessages && historyMessages.length > 0 ? { history_messages: historyMessages } : {}),
      ...(attachments && attachments.length > 0 ? { attachments } : {}),
      ...(projectId ? { project_id: projectId } : {}),
      ...(suppressUserEcho ? { suppress_user_echo: true } : {}),
    }),
    signal,
  });
}

/* ── 批量作业（工作流模式）──────────────────────────────────────────
   状态条的数据源。只读聚合，逐项明细不走这里——几百上千项读进浏览器毫无意义。 */

/** 会话的作业列表。默认只给未结束的（状态条轮询口径）；`live=false` 连终态一起给——
 *  刷新后要靠它把「上次没善终的作业」找回来，否则重新挂载时那条告警就凭空消失了。 */
export async function listChatJobs(chatId: string, live = true): Promise<JobBrief[]> {
  const res = await apiRequest<unknown>(
    `/v1/jobs?chat_id=${encodeURIComponent(chatId)}&live=${live ? 'true' : 'false'}`,
  );
  const data = unwrapData<{ jobs?: JobBrief[] }>(res);
  return data?.jobs ?? [];
}

export async function getJobApi(jobId: string): Promise<JobBrief> {
  const res = await apiRequest<unknown>(`/v1/jobs/${encodeURIComponent(jobId)}`);
  return unwrapData<JobBrief>(res);
}

export async function cancelJobApi(jobId: string): Promise<void> {
  await apiRequest<unknown>(`/v1/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
}

export async function listPlans(): Promise<Plan[]> {
  const res = await apiRequest<unknown>('/v1/plans');
  return unwrapData<Plan[]>(res);
}

export async function getPlanApi(planId: string): Promise<Plan> {
  const res = await apiRequest<unknown>(`/v1/plans/${planId}`);
  return unwrapData<Plan>(res);
}

export async function updatePlanApi(
  planId: string,
  updates: { status?: string; title?: string; steps?: Record<string, unknown>[] },
): Promise<Plan> {
  const res = await apiRequest<unknown>(`/v1/plans/${planId}`, {
    method: 'PATCH',
    body: JSON.stringify(updates),
  });
  return unwrapData<Plan>(res);
}

export async function deletePlanApi(planId: string): Promise<void> {
  await apiRequest<unknown>(`/v1/plans/${planId}`, { method: 'DELETE' });
}

export async function executePlanStream(
  planId: string,
  signal?: AbortSignal,
  enabledMcpIds?: string[],
  enabledSkillIds?: string[],
  enabledKbIds?: string[],
  chatId?: string,
  historyMessages?: Array<{ role: string; content: string }>,
  enabledAgentIds?: string[],
  projectId?: string,
): Promise<Response> {
  const url = `${getApiUrl()}/v1/plans/${planId}/execute`;
  return authFetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ...(enabledMcpIds ? { enabled_mcp_ids: enabledMcpIds } : {}),
      ...(enabledSkillIds ? { enabled_skill_ids: enabledSkillIds } : {}),
      ...(enabledKbIds ? { enabled_kb_ids: enabledKbIds } : {}),
      ...(enabledAgentIds ? { enabled_agent_ids: enabledAgentIds } : {}),
      ...(chatId ? { chat_id: chatId } : {}),
      ...(historyMessages && historyMessages.length > 0 ? { history_messages: historyMessages } : {}),
      ...(projectId ? { project_id: projectId } : {}),
    }),
    signal,
  });
}

export async function cancelPlanApi(planId: string): Promise<void> {
  await apiRequest<unknown>(`/v1/plans/${planId}/cancel`, { method: 'POST' });
}

export const api = {
  getCatalog,
  updateCatalogItem,
  getKBDocuments,
  getKBDocumentDetail,
  createKBSpace,
  polishKBDescription,
  updateKBSpace,
  uploadKBDocument,
  deleteKBSpace,
  deleteKBDocument,
  getKBChunks,
  updateKBChunk,
  reindexKBDocument,
  previewChunks,
  listSessions,
  searchSessions,
  getSession,
  createSession,
  updateSession,
  deleteSession,
  getChatMessages,
  getChatContextState,
  getFollowUpQuestions,
  getCurrentUser,
  getUserPreferences,
  updateUserPreferences,
  healthCheck,
  getMemories,
  deleteMemory,
  deleteGraphRelation,
  updateMemory,
  updateProfileField,
  deleteProfileField,
  clearAllMemories,
  getMemorySettings,
  updateMemorySettings,
  updateMemoryWriteSettings,
  updateRerankerSettings,
  getOntologySettings,
  updateOntologySettings,
  exchangeSsoCredential,
  checkSession,
  logout,
  listChatShares,
  authFetch,
  listPluginUiContributions,
  callPluginDataSource,
  pluginWebAssetUrl,
  uploadFile,
  overwriteFile,
  getArtifacts,
  getFavoriteChats,
  deleteArtifact,
  addArtifactToKnowledgeBase,
  listPersonalFolderTree,
  createPersonalFolder,
  renamePersonalFolder,
  movePersonalFolder,
  deletePersonalFolder,
  getPersonalFolderAffectedCount,
  moveArtifactToPersonalFolder,
  copyArtifactToPersonalFolder,
};

export default api;

// ── Automation API ──────────────────────────────────────────────

export interface CreateAutomationRequest {
  task_type: 'prompt' | 'plan';
  prompt?: string;
  plan_id?: string;
  cron_expression: string;
  schedule_type?: 'recurring' | 'once' | 'manual';
  name?: string;
  description?: string;
  timezone?: string;
  enabled_mcp_ids?: string[];
  enabled_skill_ids?: string[];
  enabled_kb_ids?: string[];
  enabled_agent_ids?: string[];
  max_runs?: number;
  /** Optional: deliver the results on schedule to an external channel conversation (Feishu etc.) */
  channel_id?: string;
  conversation_id?: string;
}

export interface ChannelConversation {
  channel_id: string;
  /** Bot name (display_name), used to compose a distinguishable conversation label */
  bot_name?: string | null;
  /** Real Feishu conversation ID: group = chat_id / direct chat = the speaker's open_id */
  conversation_id: string;
  /** Taken from the first message's content (e.g. "hello"); duplicates happen, so not usable as a unique display name */
  title: string;
  chat_type: string | null;
  last_message_at: string | null;
}

export async function listChannelConversations(): Promise<ChannelConversation[]> {
  const wrapped = await apiRequest<unknown>('/v1/channels/conversations');
  const data = unwrapData<{ conversations?: ChannelConversation[] }>(wrapped);
  return data?.conversations ?? [];
}

export interface UpdateAutomationRequest {
  name?: string;
  description?: string;
  cron_expression?: string;
  schedule_type?: 'recurring' | 'once' | 'manual';
  prompt?: string;
  enabled_mcp_ids?: string[];
  enabled_skill_ids?: string[];
  enabled_kb_ids?: string[];
  enabled_agent_ids?: string[];
  /** Change the delivery target: passing channel_id+conversation_id = rebind to a channel conversation; passing null = switch back to in-app only. */
  channel_id?: string | null;
  conversation_id?: string | null;
}

export async function createAutomation(data: CreateAutomationRequest): Promise<AutomationTask> {
  const res = await apiRequest<unknown>('/v1/automations', {
    method: 'POST',
    body: JSON.stringify(data),
  });
  return unwrapData<AutomationTask>(res);
}

export async function listAutomations(status?: string): Promise<AutomationTask[]> {
  const qs = status ? `?status=${status}` : '';
  const res = await apiRequest<unknown>(`/v1/automations${qs}`);
  return unwrapData<AutomationTask[]>(res);
}

export async function getAutomation(taskId: string): Promise<AutomationTask> {
  const res = await apiRequest<unknown>(`/v1/automations/${taskId}`);
  return unwrapData<AutomationTask>(res);
}

export async function updateAutomation(taskId: string, data: UpdateAutomationRequest): Promise<AutomationTask> {
  const res = await apiRequest<unknown>(`/v1/automations/${taskId}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
  return unwrapData<AutomationTask>(res);
}

export async function deleteAutomation(taskId: string): Promise<void> {
  await apiRequest<unknown>(`/v1/automations/${taskId}`, { method: 'DELETE' });
}

export async function pauseAutomation(taskId: string): Promise<void> {
  await apiRequest<unknown>(`/v1/automations/${taskId}/pause`, { method: 'POST' });
}

export async function resumeAutomation(taskId: string): Promise<void> {
  await apiRequest<unknown>(`/v1/automations/${taskId}/resume`, { method: 'POST' });
}

export async function triggerAutomation(taskId: string): Promise<void> {
  await apiRequest<unknown>(`/v1/automations/${taskId}/trigger`, { method: 'POST' });
}

export async function getAutomationRuns(taskId: string, limit?: number): Promise<AutomationRun[]> {
  const res = await apiRequest<unknown>(`/v1/automations/${taskId}/runs?limit=${limit || 10}`);
  return unwrapData<AutomationRun[]>(res);
}

export async function activateAutomationSidebar(taskId: string): Promise<AutomationTask> {
  const res = await apiRequest<unknown>(`/v1/automations/${taskId}/activate-sidebar`, { method: 'POST' });
  return unwrapData<AutomationTask>(res);
}

export async function listSidebarAutomations(): Promise<AutomationTask[]> {
  const res = await apiRequest<unknown>('/v1/automations?sidebar_activated=true');
  return unwrapData<AutomationTask[]>(res);
}

export async function getAutomationNotifications(): Promise<AutomationNotification[]> {
  const res = await apiRequest<unknown>('/v1/automations/notifications/list');
  return unwrapData<AutomationNotification[]>(res);
}

export async function markNotificationsRead(ids: string[]): Promise<void> {
  await apiRequest<unknown>('/v1/automations/notifications/read', {
    method: 'POST',
    body: JSON.stringify({ ids }),
  });
}

export async function deleteNotifications(ids: string[]): Promise<void> {
  await apiRequest<unknown>('/v1/automations/notifications/delete', {
    method: 'POST',
    body: JSON.stringify({ ids }),
  });
}

// ── Skill Distillation (Lab personal skill distillation) ────────────────


// ── Batch execution API ────────────────────────────────────────────────────

export interface BatchPlanDetail {
  plan_id: string;
  chat_id?: string | null;
  source_type: 'xlsx' | 'word_files' | 'text_list';
  instruction?: string | null;
  items_total: number;
  items_preview: Record<string, unknown>[];
  placeholder_keys: string[];
  prompt_template: string;
  max_retries: number;
  status: string;
  progress: { done: number; success: number; failed: number };
  /** Only populated by GET /v1/batch/{plan_id} (not the listing endpoint).
   *  Each entry mirrors a batch_item_done event payload. */
  item_results?: Array<{
    index: number;
    status: 'success' | 'skipped';
    content?: string;
    error?: string;
    retry_count: number;
    item_summary?: string;
    tool_calls?: unknown[];
    artifacts?: unknown[];
    citations?: unknown[];
  }>;
  created_at?: string | null;
  updated_at?: string | null;
  expires_at?: string | null;
}

export async function getBatchPlan(planId: string): Promise<BatchPlanDetail> {
  const wrapped = await apiRequest<unknown>(`/v1/batch/${encodeURIComponent(planId)}`);
  return unwrapData<BatchPlanDetail>(wrapped);
}

export async function listActiveBatchPlans(chatId: string): Promise<BatchPlanDetail[]> {
  const wrapped = await apiRequest<unknown>(
    `/v1/batch/active?chat_id=${encodeURIComponent(chatId)}`,
  );
  const data = unwrapData<{ plans: BatchPlanDetail[] }>(wrapped);
  return data.plans || [];
}

export async function confirmBatchPlan(
  planId: string,
  payload: { prompt_template: string; max_retries?: number },
): Promise<BatchPlanDetail> {
  const wrapped = await apiRequest<unknown>(
    `/v1/batch/${encodeURIComponent(planId)}/confirm`,
    {
      method: 'POST',
      body: JSON.stringify(payload),
    },
  );
  return unwrapData<BatchPlanDetail>(wrapped);
}

export async function cancelBatchPlan(planId: string): Promise<void> {
  await apiRequest<unknown>(
    `/v1/batch/${encodeURIComponent(planId)}/cancel`,
    { method: 'POST' },
  );
}

/** Open the SSE batch execution stream. Calls back with each parsed event.
 *  Returns an AbortController so callers can cancel mid-stream.
 */
export function openBatchStream(
  planId: string,
  onEvent: (event: Record<string, unknown>) => void,
  onError?: (err: Error) => void,
): AbortController {
  const ctrl = new AbortController();
  const url = `${getApiUrl()}/v1/batch/${encodeURIComponent(planId)}/stream`;

  (async () => {
    try {
      const resp = await fetch(url, {
        method: 'GET',
        credentials: 'include',
        headers: { Accept: 'text/event-stream' },
        signal: ctrl.signal,
      });
      if (!resp.ok || !resp.body) {
        throw new Error(`batch stream failed: ${resp.status}`);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      try {
        // eslint-disable-next-line no-constant-condition
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let idx = buffer.indexOf('\n\n');
          while (idx >= 0) {
            const block = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);
            const dataLine = block.split('\n').find(l => l.startsWith('data: '));
            if (dataLine) {
              const payload = dataLine.slice(6).trim();
              if (payload === '[DONE]') return;
              try {
                onEvent(JSON.parse(payload));
              } catch {
                // skip malformed event
              }
            }
            idx = buffer.indexOf('\n\n');
          }
        }
      } finally {
        // Always release the underlying body reader on early return /
        // abort / error so the connection isn't kept open for the
        // browser to GC later.
        try { await reader.cancel(); } catch { /* already closed */ }
      }
    } catch (err) {
      if ((err as Error).name === 'AbortError') return;
      onError?.(err as Error);
    }
  })();

  return ctrl;
}

// ─── Projects (Claude-style workspaces) ───────────────────────────────────

import type {
  ProjectChatSummary,
  ProjectDetail,
  ProjectFileItem,
  ProjectItem,
  ProjectKind,
} from './types';

export interface ProjectListResponse {
  items: ProjectItem[];
  pagination: { page: number; page_size: number; total_items: number; total_pages: number; has_previous: boolean; has_next: boolean };
}

export async function listProjects(opts: { q?: string; sort?: string; page?: number; pageSize?: number } = {}): Promise<ProjectListResponse> {
  const params = new URLSearchParams();
  if (opts.q) params.set('q', opts.q);
  if (opts.sort) params.set('sort', opts.sort);
  if (opts.page) params.set('page', String(opts.page));
  if (opts.pageSize) params.set('page_size', String(opts.pageSize));
  const qs = params.toString();
  const path = `/v1/projects${qs ? `?${qs}` : ''}`;
  const wrapped = await apiRequest<unknown>(path);
  const cloud = unwrapData<ProjectListResponse>(wrapped);
  if (!isHybridDual()) return cloud;
  // 双模式：云端项目 + 本机的本地文件夹项目合并为一张列表（本地项目登记路由表）。
  // 本机服务未就绪/无本地项目时静默退回云端列表。
  try {
    const localWrapped = await apiRequest<unknown>(path, undefined, 'local');
    const local = unwrapData<ProjectListResponse>(localWrapped);
    const localItems = (local?.items || []).filter((p) => (p.kind as string) === 'local');
    localItems.forEach((p) => registerLocalProject(p.project_id));
    if (!localItems.length) return cloud;
    const seen = new Set((cloud.items || []).map((p) => p.project_id));
    return {
      ...cloud,
      items: [...(cloud.items || []), ...localItems.filter((p) => !seen.has(p.project_id))],
    };
  } catch {
    return cloud;
  }
}

export async function createProject(body: {
  name: string;
  description?: string;
  kind: ProjectKind;
  linked_folder_id?: string;
} & EditionCreateProjectFields): Promise<ProjectDetail> {
  const wrapped = await apiRequest<unknown>('/v1/projects', {
    method: 'POST',
    body: JSON.stringify(body),
  });
  return unwrapData<ProjectDetail>(wrapped);
}

/**
 * Create a desktop local-folder project (ticket #03). Only works inside the
 * desktop local backend (the route is gated on DEPLOY_PROFILE=local); on the
 * cloud/web deployment this returns 403 and is never invoked.
 */
export async function createLocalProject(body: {
  name: string;
  local_path: string;
  local_machine?: string;
  description?: string;
}): Promise<ProjectDetail> {
  const wrapped = await apiRequest<unknown>(
    '/v1/projects',
    { method: 'POST', body: JSON.stringify({ kind: 'local', ...body }) },
    'local',
  );
  const detail = unwrapData<ProjectDetail>(wrapped);
  registerLocalProject(detail.project_id);
  return detail;
}

// ── Desktop local permissions (ticket #06) ────────────────────────────────
export interface LocalGrant {
  path: string;
  mode: 'read' | 'readwrite';
}
export type LocalDisposition = 'block' | 'confirm' | 'allow';
export interface LocalPolicy {
  out_of_scope?: LocalDisposition;
  delete?: LocalDisposition;
  system_write?: LocalDisposition;
  network?: LocalDisposition;
  privilege?: LocalDisposition;
}

export async function listLocalGrants(): Promise<LocalGrant[]> {
  const wrapped = await apiRequest<{ items: LocalGrant[] }>('/v1/local/grants', undefined, 'local');
  return unwrapData<{ items: LocalGrant[] }>(wrapped).items || [];
}
export async function addLocalGrant(path: string, mode: 'read' | 'readwrite' = 'readwrite'): Promise<void> {
  await apiRequest('/v1/local/grants', { method: 'POST', body: JSON.stringify({ path, mode }) }, 'local');
}
export async function removeLocalGrant(path: string): Promise<void> {
  await apiRequest(`/v1/local/grants?path=${encodeURIComponent(path)}`, { method: 'DELETE' }, 'local');
}
export async function getLocalPolicy(): Promise<LocalPolicy> {
  const wrapped = await apiRequest<LocalPolicy>('/v1/local/policy', undefined, 'local');
  return unwrapData<LocalPolicy>(wrapped) || {};
}
export async function setLocalPolicy(policy: LocalPolicy): Promise<void> {
  await apiRequest('/v1/local/policy', { method: 'PUT', body: JSON.stringify(policy) }, 'local');
}

export interface LocalSnapshotFile {
  path: string;
  count: number;
}
// ── 工具执行权限档（逐项确认 / 替我批准 / 完全放开）──────────────────────
// 网页端与桌面端共用这一档，每个用户一份存在服务端；桌面端的本机策略由它翻译
// 而来，不再另存一份「本机操作权限档」。上面的授权目录管的是"本机哪些目录能动"，
// 与档位是两件事，只在桌面端有。
export type ToolApprovalMode = 'ask' | 'auto' | 'full';
export async function getToolApprovalMode(): Promise<ToolApprovalMode> {
  const wrapped = await apiRequest<{ mode: ToolApprovalMode }>('/v1/tool-approval');
  return unwrapData<{ mode: ToolApprovalMode }>(wrapped).mode || 'ask';
}
export async function setToolApprovalMode(mode: ToolApprovalMode): Promise<void> {
  await apiRequest('/v1/tool-approval', { method: 'PUT', body: JSON.stringify({ mode }) });
}

export async function listLocalSnapshots(): Promise<LocalSnapshotFile[]> {
  const wrapped = await apiRequest<{ items: LocalSnapshotFile[] }>('/v1/local/snapshots', undefined, 'local');
  return unwrapData<{ items: LocalSnapshotFile[] }>(wrapped).items || [];
}
export async function rollbackLocalFile(path: string): Promise<void> {
  await apiRequest('/v1/local/rollback', { method: 'POST', body: JSON.stringify({ path }) }, 'local');
}

export async function getProject(projectId: string): Promise<ProjectDetail> {
  const wrapped = await apiRequest<unknown>(
    `/v1/projects/${encodeURIComponent(projectId)}`,
    undefined,
    isLocalProject(projectId) ? 'local' : undefined,
  );
  return unwrapData<ProjectDetail>(wrapped);
}

export async function updateProject(
  projectId: string,
  patch: Partial<Pick<ProjectItem, 'name' | 'description' | 'instructions' | 'pinned' | 'icon_color' | 'memory_enabled' | 'memory_write_enabled'>>,
): Promise<ProjectDetail> {
  const wrapped = await apiRequest<unknown>(
    `/v1/projects/${encodeURIComponent(projectId)}`,
    { method: 'PATCH', body: JSON.stringify(patch) },
    isLocalProject(projectId) ? 'local' : undefined,
  );
  return unwrapData<ProjectDetail>(wrapped);
}

export async function deleteProject(projectId: string): Promise<void> {
  await apiRequest<unknown>(
    `/v1/projects/${encodeURIComponent(projectId)}`,
    { method: 'DELETE' },
    isLocalProject(projectId) ? 'local' : undefined,
  );
}

export async function toggleProjectFavorite(projectId: string, on: boolean): Promise<void> {
  await apiRequest<unknown>(
    `/v1/projects/${encodeURIComponent(projectId)}/favorite`,
    { method: on ? 'POST' : 'DELETE' },
    isLocalProject(projectId) ? 'local' : undefined,
  );
}

export async function updateProjectInstructions(projectId: string, instructions: string): Promise<ProjectDetail> {
  const wrapped = await apiRequest<unknown>(
    `/v1/projects/${encodeURIComponent(projectId)}/instructions`,
    { method: 'PATCH', body: JSON.stringify({ instructions }) },
    isLocalProject(projectId) ? 'local' : undefined,
  );
  return unwrapData<ProjectDetail>(wrapped);
}

export async function listProjectFiles(projectId: string): Promise<{
  items: ProjectFileItem[];
  total: number;
  capacity_used: number;
  capacity_limit: number;
}> {
  const wrapped = await apiRequest<unknown>(
    `/v1/projects/${encodeURIComponent(projectId)}/files`,
    undefined,
    isLocalProject(projectId) ? 'local' : undefined,
  );
  const data = unwrapData<{ items: ProjectFileItem[]; total: number; capacity_used: number; capacity_limit: number }>(wrapped);
  return {
    items: data?.items || [],
    total: data?.total || 0,
    capacity_used: data?.capacity_used || 0,
    capacity_limit: data?.capacity_limit || 0,
  };
}

export async function uploadProjectFile(projectId: string, file: File): Promise<ProjectFileItem> {
  const form = new FormData();
  // For folder uploads, <input webkitdirectory> attaches webkitRelativePath to the
  // File object (e.g. ``finance/2024/q1.xlsx``). The backend keeps it as the filename
  // so the source subdirectory is visible at a glance inside the project. For plain
  // file uploads webkitRelativePath = '' and file.name is used.
  const relPath = (file as File & { webkitRelativePath?: string }).webkitRelativePath || '';
  const namedFile = relPath ? new File([file], relPath, { type: file.type }) : file;
  form.append('file', namedFile);
  const url = `${getApiUrl()}/v1/projects/${encodeURIComponent(projectId)}/files/upload`;
  const resp = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: { ...projectTargetHeaders(projectId) },
    body: form,
  });
  const payload = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(uploadErrorMessage(resp.status, payload));
  }
  return unwrapData<ProjectFileItem>(payload);
}

export async function removeProjectFile(projectId: string, artifactId: string): Promise<void> {
  // A project file = an artifact under a MySpace folder. Deleting simply soft-deletes the artifact (it disappears on the MySpace side too).
  await apiRequest<unknown>(
    `/v1/projects/${encodeURIComponent(projectId)}/files/${encodeURIComponent(artifactId)}`,
    { method: 'DELETE' },
    isLocalProject(projectId) ? 'local' : undefined,
  );
}

export async function listProjectChats(
  projectId: string,
  page: number = 1,
  pageSize: number = 50,
  scope: 'all' | 'mine' | 'shared' = 'all',
): Promise<{ items: ProjectChatSummary[]; total: number }> {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize), scope });
  const wrapped = await apiRequest<unknown>(
    `/v1/projects/${encodeURIComponent(projectId)}/chats?${params.toString()}`,
    undefined,
    isLocalProject(projectId) ? 'local' : undefined,
  );
  const data = unwrapData<{ items: ProjectChatSummary[]; pagination: { total_items: number } }>(wrapped);
  return { items: data?.items || [], total: data?.pagination?.total_items || 0 };
}

// ── Third-party integration: DingTalk account connection (dingtalk skill / dws CLI) ──
export interface DingTalkStatus {
  status: 'disconnected' | 'pending' | 'connected' | 'error';
  dingtalk_user_id: string | null;
  dingtalk_name: string | null;
  corp_id: string | null;
  granted_scopes: string[];
  verification_url: string | null;
  verification_url_complete: string | null;
  qr_data_uri: string | null;
  user_code: string | null;
  last_verified_at: string | null;
  last_error: string | null;
  raw_output?: string;
}

function _coerceDingTalkStatus(data: JsonObject): DingTalkStatus {
  return {
    status: (data?.status as DingTalkStatus['status']) || 'disconnected',
    dingtalk_user_id: (data?.dingtalk_user_id as string) ?? null,
    dingtalk_name: (data?.dingtalk_name as string) ?? null,
    corp_id: (data?.corp_id as string) ?? null,
    granted_scopes: Array.isArray(data?.granted_scopes) ? (data.granted_scopes as string[]) : [],
    verification_url: (data?.verification_url as string) ?? null,
    verification_url_complete: (data?.verification_url_complete as string) ?? null,
    qr_data_uri: (data?.qr_data_uri as string) ?? null,
    user_code: (data?.user_code as string) ?? null,
    last_verified_at: (data?.last_verified_at as string) ?? null,
    last_error: (data?.last_error as string) ?? null,
    raw_output: (data?.raw_output as string) ?? undefined,
  };
}

export async function getDingTalkStatus(probe = false): Promise<DingTalkStatus> {
  const wrapped = await apiRequest<unknown>(`/v1/integrations/dingtalk/status${probe ? '?probe=true' : ''}`);
  return _coerceDingTalkStatus(unwrapData<JsonObject>(wrapped));
}

export async function startDingTalkLogin(): Promise<DingTalkStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/dingtalk/login', { method: 'POST' });
  return _coerceDingTalkStatus(unwrapData<JsonObject>(wrapped));
}

export async function pollDingTalkLogin(): Promise<DingTalkStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/dingtalk/login/poll', { method: 'POST' });
  return _coerceDingTalkStatus(unwrapData<JsonObject>(wrapped));
}

export async function disconnectDingTalk(): Promise<DingTalkStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/dingtalk/disconnect', { method: 'POST' });
  return _coerceDingTalkStatus(unwrapData<JsonObject>(wrapped));
}

// ── Third-party integration: Feishu account connection (feishu-cli plugin / lark-cli), QR device flow, same structure as DingTalk ──
export interface LarkStatus {
  status: 'disconnected' | 'pending' | 'connected' | 'error';
  lark_open_id: string | null;
  lark_name: string | null;
  tenant_key: string | null;
  granted_scopes: string[];
  verification_url: string | null;
  verification_url_complete: string | null;
  qr_data_uri: string | null;
  user_code: string | null;
  last_verified_at: string | null;
  last_error: string | null;
}

function _coerceLarkStatus(data: JsonObject): LarkStatus {
  return {
    status: (data?.status as LarkStatus['status']) || 'disconnected',
    lark_open_id: (data?.lark_open_id as string) ?? null,
    lark_name: (data?.lark_name as string) ?? null,
    tenant_key: (data?.tenant_key as string) ?? null,
    granted_scopes: Array.isArray(data?.granted_scopes) ? (data.granted_scopes as string[]) : [],
    verification_url: (data?.verification_url as string) ?? null,
    verification_url_complete: (data?.verification_url_complete as string) ?? null,
    qr_data_uri: (data?.qr_data_uri as string) ?? null,
    user_code: (data?.user_code as string) ?? null,
    last_verified_at: (data?.last_verified_at as string) ?? null,
    last_error: (data?.last_error as string) ?? null,
  };
}

export async function getLarkStatus(probe = false): Promise<LarkStatus> {
  const wrapped = await apiRequest<unknown>(`/v1/integrations/lark/status${probe ? '?probe=true' : ''}`);
  return _coerceLarkStatus(unwrapData<JsonObject>(wrapped));
}

export async function startLarkLogin(): Promise<LarkStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/lark/login', { method: 'POST' });
  return _coerceLarkStatus(unwrapData<JsonObject>(wrapped));
}

export async function pollLarkLogin(): Promise<LarkStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/lark/login/poll', { method: 'POST' });
  return _coerceLarkStatus(unwrapData<JsonObject>(wrapped));
}

export async function disconnectLark(): Promise<LarkStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/lark/disconnect', { method: 'POST' });
  return _coerceLarkStatus(unwrapData<JsonObject>(wrapped));
}

// ── Inbound channel bots (owner service-account model): user-created external IM bots that run under the owner's identity ──
// Orthogonal to the "Feishu account connection" above: that is outbound (the agent operates Feishu as me), this is inbound (Feishu pushes messages to my agent).
export interface ChannelBot {
  channel_id: string;
  channel_type: string;
  display_name: string;
  transport: 'long_conn' | 'webhook';
  app_id: string;
  status: 'disconnected' | 'pending' | 'connected' | 'error';
  enabled: boolean;
  /** Bound sub-agent ID; null = main agent (the owner's default capabilities) */
  agent_id: string | null;
  /**
   * 群聊旁听：`mention_only` 仅处理 @ 机器人的消息；`observe_all` 旁观群内其他消息作为上下文
   * （不回复）。`observe_all` 还需渠道应用本身具备平台的「读取群内全部消息」权限，否则平台
   * 根本不会把未 @ 的消息推过来。
   */
  group_listen_mode: 'mention_only' | 'observe_all';
  resource_scope: { kb_ids?: string[]; skill_ids?: string[] } | null;
  last_event_at: string | null;
  last_error: string | null;
  created_at: string | null;
  webhook_path?: string;
}

export interface ChannelAdapterInfo {
  channel_type: string;
  max_message_len: number;
  supports_markdown: boolean;
  supports_long_conn: boolean;
  /** 该渠道是否可能投递「未 @ 机器人」的群消息（即群聊旁听是否有意义） */
  supports_group_observe: boolean;
  bind_mode: 'credentials' | 'qr';
  credential_fields: string[];
}

export interface CreateChannelBotPayload {
  channel_type: string;
  app_id: string;
  app_secret: string;
  encrypt_key?: string;
  verification_token?: string;
  extra?: Record<string, string>;
  display_name?: string;
  transport?: 'long_conn' | 'webhook';
  resource_scope?: { kb_ids?: string[]; skill_ids?: string[] };
  /** Bind to a specific sub-agent; omitted = main agent */
  agent_id?: string;
  /** 群聊旁听模式；不传 = mention_only */
  group_listen_mode?: 'mention_only' | 'observe_all';
}

export async function listChannelAdapters(): Promise<ChannelAdapterInfo[]> {
  const wrapped = await apiRequest<unknown>('/v1/channels/adapters');
  const data = unwrapData<{ adapters?: ChannelAdapterInfo[] }>(wrapped);
  return data?.adapters ?? [];
}

/** List my bots. `agentId` → only those bound to that sub-agent; `mainOnly` → only the main agent's; neither → all. */
export async function listChannelBots(
  opts?: { agentId?: string; mainOnly?: boolean },
): Promise<ChannelBot[]> {
  const qs = new URLSearchParams();
  if (opts?.agentId) qs.set('agent_id', opts.agentId);
  if (opts?.mainOnly) qs.set('main_only', 'true');
  const suffix = qs.toString() ? `?${qs.toString()}` : '';
  const wrapped = await apiRequest<unknown>(`/v1/channels/bots${suffix}`);
  const data = unwrapData<{ bots?: ChannelBot[] }>(wrapped);
  return data?.bots ?? [];
}

export async function createChannelBot(payload: CreateChannelBotPayload): Promise<ChannelBot> {
  const wrapped = await apiRequest<unknown>('/v1/channels/bots', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  return unwrapData<ChannelBot>(wrapped);
}

export async function updateChannelBot(
  channelId: string,
  patch: {
    display_name?: string;
    enabled?: boolean;
    resource_scope?: { kb_ids?: string[]; skill_ids?: string[] };
    agent_id?: string | null;
    group_listen_mode?: 'mention_only' | 'observe_all';
  },
): Promise<ChannelBot> {
  const wrapped = await apiRequest<unknown>(`/v1/channels/bots/${channelId}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
  return unwrapData<ChannelBot>(wrapped);
}

export async function deleteChannelBot(channelId: string): Promise<void> {
  await apiRequest<unknown>(`/v1/channels/bots/${channelId}`, { method: 'DELETE' });
}

export async function testChannelBot(channelId: string): Promise<{ ok: boolean }> {
  const wrapped = await apiRequest<unknown>(`/v1/channels/bots/${channelId}/test`, { method: 'POST' });
  return unwrapData<{ ok: boolean }>(wrapped);
}

// ── WeChat QR binding (qr mode: iLink protocol, scan to host a personal WeChat account) ──
export interface WeixinBindStart {
  bind_id: string;
  qrcode_img: string; // base64 PNG (without the data: prefix)
}

export interface WeixinBindStatus {
  status: string; // waiting | scanned | confirmed | ...
  channel_id?: string;
}

export async function startWeixinBind(agentId?: string): Promise<WeixinBindStart> {
  const suffix = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : '';
  const wrapped = await apiRequest<unknown>(`/v1/channels/weixin/bind/start${suffix}`, { method: 'POST' });
  return unwrapData<WeixinBindStart>(wrapped);
}

export async function getWeixinBindStatus(bindId: string): Promise<WeixinBindStatus> {
  const wrapped = await apiRequest<unknown>(`/v1/channels/weixin/bind/${bindId}/status`);
  return unwrapData<WeixinBindStatus>(wrapped);
}

// ── Third-party integration: email account connection (email plugin / himalaya), IMAP/SMTP app password, synchronous binding ──
// Unlike DingTalk/Feishu: no device flow / no QR code / no poll; the connection completes via a credential form submitted to POST /connect.
export interface EmailStatus {
  status: 'disconnected' | 'connected' | 'error';
  email_address: string | null;
  display_name: string | null;
  provider: string | null;
  imap_host: string | null;
  imap_port: number | null;
  imap_security: string | null;
  smtp_host: string | null;
  smtp_port: number | null;
  smtp_security: string | null;
  last_verified_at: string | null;
  last_error: string | null;
}

export interface EmailServerOverrides {
  imap_host?: string;
  imap_port?: number;
  imap_security?: string;
  smtp_host?: string;
  smtp_port?: number;
  smtp_security?: string;
}

function _coerceEmailStatus(data: JsonObject): EmailStatus {
  return {
    status: (data?.status as EmailStatus['status']) || 'disconnected',
    email_address: (data?.email_address as string) ?? null,
    display_name: (data?.display_name as string) ?? null,
    provider: (data?.provider as string) ?? null,
    imap_host: (data?.imap_host as string) ?? null,
    imap_port: (data?.imap_port as number) ?? null,
    imap_security: (data?.imap_security as string) ?? null,
    smtp_host: (data?.smtp_host as string) ?? null,
    smtp_port: (data?.smtp_port as number) ?? null,
    smtp_security: (data?.smtp_security as string) ?? null,
    last_verified_at: (data?.last_verified_at as string) ?? null,
    last_error: (data?.last_error as string) ?? null,
  };
}

export async function getEmailStatus(probe = false): Promise<EmailStatus> {
  const wrapped = await apiRequest<unknown>(`/v1/integrations/email/status${probe ? '?probe=true' : ''}`);
  return _coerceEmailStatus(unwrapData<JsonObject>(wrapped));
}

export async function connectEmail(body: {
  email_address: string;
  secret: string;
  display_name?: string;
  server_overrides?: EmailServerOverrides;
}): Promise<EmailStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/email/connect', {
    method: 'POST',
    body: JSON.stringify(body),
  });
  return _coerceEmailStatus(unwrapData<JsonObject>(wrapped));
}

export async function disconnectEmail(): Promise<EmailStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/email/disconnect', { method: 'POST' });
  return _coerceEmailStatus(unwrapData<JsonObject>(wrapped));
}

// ── Third-party integration: Yida account connection (yida plugin / openyida CLI), QR login executed in the user's sandbox ──
// Unlike the DingTalk device flow: poll is a long poll (the backend runs agent-poll
// inside the sandbox waiting for the scan; a single call can block ~45s), so the
// frontend must use a sequential loop ("start the next only after the previous
// returns") rather than setInterval, to avoid pile-up.
// For multi-organization accounts, poll returns corp_selection + organizations;
// after the user picks one, re-poll with corp_id.
export interface YidaOrganization {
  corp_id: string;
  corp_name: string;
  main_org: boolean;
}

export interface YidaStatus {
  status: 'disconnected' | 'pending' | 'connected' | 'error' | 'corp_selection';
  corp_id: string | null;
  base_url: string | null;
  qr_data_uri: string | null;
  qr_url: string | null;
  organizations: YidaOrganization[];
  error: string | null;
  message: string | null;
}

function _coerceYidaStatus(data: JsonObject): YidaStatus {
  return {
    status: (data?.status as YidaStatus['status']) || 'disconnected',
    corp_id: (data?.corp_id as string) ?? null,
    base_url: (data?.base_url as string) ?? null,
    qr_data_uri: (data?.qr_data_uri as string) ?? null,
    qr_url: (data?.qr_url as string) ?? null,
    organizations: Array.isArray(data?.organizations)
      ? (data.organizations as YidaOrganization[])
      : [],
    error: (data?.error as string) ?? null,
    message: (data?.message as string) ?? null,
  };
}

export async function getYidaStatus(probe = false): Promise<YidaStatus> {
  const wrapped = await apiRequest<unknown>(`/v1/integrations/yida/status${probe ? '?probe=true' : ''}`);
  return _coerceYidaStatus(unwrapData<JsonObject>(wrapped));
}

export async function startYidaLogin(): Promise<YidaStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/yida/login', { method: 'POST' });
  return _coerceYidaStatus(unwrapData<JsonObject>(wrapped));
}

export async function pollYidaLogin(corpId?: string): Promise<YidaStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/yida/login/poll', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(corpId ? { corp_id: corpId } : {}),
  });
  return _coerceYidaStatus(unwrapData<JsonObject>(wrapped));
}

export async function disconnectYida(): Promise<YidaStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/yida/disconnect', { method: 'POST' });
  return _coerceYidaStatus(unwrapData<JsonObject>(wrapped));
}

// ── Autonomous Loop (long-running autonomous operation) ──────────
import type { LoopItem, LoopIterationItem, LoopGoalSpec, LoopBudget } from './types';

export async function createLoop(data: {
  title?: string;
  goal_spec: LoopGoalSpec;
  budget?: Partial<LoopBudget>;
  chat_id?: string;
  /** The project the user selected in the input box — the loop is fully bound to it (the worker operates in the project folder; publishing goes through publish_site). */
  project_id?: string;
}): Promise<LoopItem> {
  const wrapped = await apiRequest<unknown>('/v1/loops', {
    method: 'POST',
    body: JSON.stringify(data),
  });
  return unwrapData<LoopItem>(wrapped);
}

export async function listLoops(): Promise<LoopItem[]> {
  const wrapped = await apiRequest<unknown>('/v1/loops');
  return unwrapData<LoopItem[]>(wrapped) || [];
}

export async function getLoop(loopId: string): Promise<LoopItem> {
  const wrapped = await apiRequest<unknown>(`/v1/loops/${encodeURIComponent(loopId)}`);
  return unwrapData<LoopItem>(wrapped);
}

export async function getLoopIterations(loopId: string): Promise<LoopIterationItem[]> {
  const wrapped = await apiRequest<unknown>(`/v1/loops/${encodeURIComponent(loopId)}/iterations`);
  return unwrapData<LoopIterationItem[]>(wrapped) || [];
}

/** Start/continue a loop; returns a Response with an SSE body (event parsing is in LoopPanel).
 *  `chat_mode` passes the user-confirmed thinking level (fast/medium/high/max) through
 *  verbatim; the backend uses it to set the worker's reasoning_effort — enable_thinking
 *  is only a fallback boolean for legacy clients. */
export async function startLoop(
  loopId: string,
  body: { model_name?: string; model_provider_id?: string; evaluator_model?: string; worker_max_iters?: number; hitl_enabled?: boolean; enable_thinking?: boolean; chat_mode?: string } = {},
  signal?: AbortSignal,
): Promise<Response> {
  return authFetch(`${getApiUrl()}/v1/loops/${encodeURIComponent(loopId)}/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });
}

export async function resumeLoop(
  loopId: string,
  body: { model_name?: string; model_provider_id?: string; evaluator_model?: string; worker_max_iters?: number; hitl_enabled?: boolean; enable_thinking?: boolean; chat_mode?: string } = {},
  signal?: AbortSignal,
): Promise<Response> {
  return authFetch(`${getApiUrl()}/v1/loops/${encodeURIComponent(loopId)}/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });
}

/** 运行中追加一条用户指令：driver 下一轮 worker 开工前取走并以最高优先级注入 prompt。 */
export async function steerLoop(loopId: string, message: string): Promise<boolean> {
  const wrapped = await apiRequest<unknown>(`/v1/loops/${encodeURIComponent(loopId)}/steer`, {
    method: 'POST',
    body: JSON.stringify({ message }),
  });
  return (unwrapData<{ queued: boolean }>(wrapped) || { queued: false }).queued;
}

export async function cancelLoop(loopId: string): Promise<boolean> {
  const wrapped = await apiRequest<unknown>(`/v1/loops/${encodeURIComponent(loopId)}/cancel`, {
    method: 'POST',
  });
  return (unwrapData<{ cancelled: boolean }>(wrapped) || { cancelled: false }).cancelled;
}

// ── Sites (site hosting) ────────────────────────────────────────────────

export interface SiteItem extends SiteEditionFields {
  /** 混合架构：站点发布在云端还是本机执行面（桌面双模式合并视图）。 */
  origin?: 'cloud' | 'local';
  site_id: string;
  slug: string;
  /** In-app relative access URL, of the form /site/<slug>/ */
  url: string;
  title: string;
  description: string | null;
  visibility: SiteVisibility;
  entry_file: string;
  current_version: number;
  file_count: number;
  total_size_bytes: number;
  view_count: number;
  chat_id: string | null;
  /** Site source project (personal project) id; when set → the "Edit" action on the card can continue editing; null for legacy sites */
  project_id: string | null;
  /** Editable whenever project_id is set */
  editable: boolean;
  created_at: string | null;
  updated_at: string | null;
}

function toSiteItem(raw: JsonObject): SiteItem {
  return {
    site_id: String(raw.site_id ?? ''),
    slug: String(raw.slug ?? ''),
    url: String(raw.url ?? `/site/${raw.slug ?? ''}/`),
    title: String(raw.title ?? ''),
    description: typeof raw.description === 'string' ? raw.description : null,
    visibility: normalizeSiteVisibility(raw.visibility),
    ...normalizeSiteEditionFields(raw),
    entry_file: String(raw.entry_file ?? 'index.html'),
    current_version: Number(raw.current_version ?? 1),
    file_count: Number(raw.file_count ?? 0),
    view_count: Number(raw.view_count ?? 0),
    total_size_bytes: Number(raw.total_size_bytes ?? 0),
    chat_id: typeof raw.chat_id === 'string' ? raw.chat_id : null,
    project_id: typeof raw.project_id === 'string' ? raw.project_id : null,
    editable: Boolean(raw.editable),
    created_at: typeof raw.created_at === 'string' ? raw.created_at : null,
    updated_at: typeof raw.updated_at === 'string' ? raw.updated_at : null,
  };
}

/** 站点管理操作的路由目标：本机发布的站点 → 本机执行面。 */
function siteTarget(origin?: 'cloud' | 'local'): 'local' | undefined {
  return origin === 'local' ? 'local' : undefined;
}

export async function listSites(page = 1, pageSize = 50): Promise<{ items: SiteItem[]; total: number }> {
  const wrapped = await apiRequest<unknown>(`/v1/sites?page=${page}&page_size=${pageSize}`);
  const data = unwrapData<JsonObject>(wrapped);
  let items: SiteItem[] = Array.isArray(data.items)
    ? (data.items as JsonObject[]).map((r) => ({ ...toSiteItem(r), origin: 'cloud' as const }))
    : [];
  const pagination = (data.pagination ?? {}) as JsonObject;
  let total = Number(pagination.total_items ?? items.length);
  // 双模式：并入本机发布的站点（本机执行的建站会话把站点落在本机库），
  // 按更新时间倒序混排；本机未就绪时静默仅展示云端。
  if (isHybridDual()) {
    try {
      const lw = await apiRequest<unknown>(
        `/v1/sites?page=${page}&page_size=${pageSize}`, undefined, 'local',
      );
      const ld = unwrapData<JsonObject>(lw);
      const localItems = Array.isArray(ld.items)
        ? (ld.items as JsonObject[]).map((r) => ({ ...toSiteItem(r), origin: 'local' as const }))
        : [];
      const lp = (ld.pagination ?? {}) as JsonObject;
      total += Number(lp.total_items ?? localItems.length);
      items = [...items, ...localItems].sort((a, b) =>
        String(b.updated_at ?? '').localeCompare(String(a.updated_at ?? '')));
    } catch { /* local backend not ready */ }
  }
  return { items, total };
}

export async function updateSite(
  siteId: string,
  data: {
    title?: string;
    visibility?: SiteVisibility;
    slug?: string;
    description?: string;
  } & SiteUpdateEditionFields,
  origin?: 'cloud' | 'local',
): Promise<SiteItem> {
  const wrapped = await apiRequest<unknown>(`/v1/sites/${encodeURIComponent(siteId)}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  }, siteTarget(origin));
  return { ...toSiteItem(unwrapData<JsonObject>(wrapped)), origin };
}

export async function deleteSite(siteId: string, origin?: 'cloud' | 'local'): Promise<void> {
  await apiRequest(`/v1/sites/${encodeURIComponent(siteId)}`, { method: 'DELETE' }, siteTarget(origin));
}

export interface SiteVersionItem {
  version: number;
  file_count: number;
  total_size_bytes: number;
  created_at: string;
}

export interface SiteSubmissionItem {
  id: string;
  form_key: string;
  payload: Record<string, unknown>;
  created_at: string | null;
}

export interface SiteKvItem {
  key: string;
  value: string;
  updated_at: string | null;
}

export async function getSiteDetail(
  siteId: string, origin?: 'cloud' | 'local',
): Promise<SiteItem & { versions: SiteVersionItem[] }> {
  const wrapped = await apiRequest<unknown>(
    `/v1/sites/${encodeURIComponent(siteId)}`, undefined, siteTarget(origin),
  );
  const data = unwrapData<JsonObject>(wrapped);
  return {
    ...toSiteItem(data),
    origin,
    versions: Array.isArray(data.versions) ? (data.versions as SiteVersionItem[]) : [],
  };
}

export async function rollbackSite(
  siteId: string, version: number, origin?: 'cloud' | 'local',
): Promise<SiteItem> {
  const wrapped = await apiRequest<unknown>(`/v1/sites/${encodeURIComponent(siteId)}/rollback`, {
    method: 'POST',
    body: JSON.stringify({ version }),
  }, siteTarget(origin));
  return { ...toSiteItem(unwrapData<JsonObject>(wrapped)), origin };
}

export async function listSiteSubmissions(
  siteId: string, page = 1, pageSize = 50, origin?: 'cloud' | 'local',
): Promise<{ items: SiteSubmissionItem[]; total: number }> {
  const wrapped = await apiRequest<unknown>(
    `/v1/sites/${encodeURIComponent(siteId)}/submissions?page=${page}&page_size=${pageSize}`,
    undefined, siteTarget(origin),
  );
  const data = unwrapData<JsonObject>(wrapped);
  const pagination = (data.pagination ?? {}) as JsonObject;
  return {
    items: Array.isArray(data.items) ? (data.items as SiteSubmissionItem[]) : [],
    total: Number(pagination.total_items ?? 0),
  };
}

export async function exportSiteSubmissions(
  siteId: string, origin?: 'cloud' | 'local',
): Promise<{ artifact_id: string; filename: string; rows: number; download_url: string }> {
  const wrapped = await apiRequest<unknown>(
    `/v1/sites/${encodeURIComponent(siteId)}/submissions/export`,
    { method: 'POST' }, siteTarget(origin),
  );
  const res = unwrapData<{ artifact_id: string; filename: string; rows: number; download_url: string }>(wrapped);
  // 本机导出的产物在本机端，下载链接补路由标记（window.open 带不上请求头）。
  if (origin === 'local' && res.download_url && !res.download_url.includes('hg_target=local')) {
    res.download_url += res.download_url.includes('?') ? '&hg_target=local' : '?hg_target=local';
  }
  return res;
}

export async function clearSiteSubmissions(siteId: string, origin?: 'cloud' | 'local'): Promise<number> {
  const wrapped = await apiRequest<unknown>(
    `/v1/sites/${encodeURIComponent(siteId)}/submissions`, { method: 'DELETE' }, siteTarget(origin),
  );
  return Number(unwrapData<JsonObject>(wrapped).cleared ?? 0);
}

export async function listSiteKv(
  siteId: string, origin?: 'cloud' | 'local',
): Promise<{ items: SiteKvItem[]; total: number }> {
  const wrapped = await apiRequest<unknown>(
    `/v1/sites/${encodeURIComponent(siteId)}/kv`, undefined, siteTarget(origin),
  );
  const data = unwrapData<JsonObject>(wrapped);
  return {
    items: Array.isArray(data.items) ? (data.items as SiteKvItem[]) : [],
    total: Number(data.total ?? 0),
  };
}

export async function deleteSiteKvKey(
  siteId: string, key: string, origin?: 'cloud' | 'local',
): Promise<void> {
  await apiRequest(
    `/v1/sites/${encodeURIComponent(siteId)}/kv/${encodeURIComponent(key)}`,
    { method: 'DELETE' }, siteTarget(origin),
  );
}

export async function clearSiteKv(siteId: string, origin?: 'cloud' | 'local'): Promise<number> {
  const wrapped = await apiRequest<unknown>(
    `/v1/sites/${encodeURIComponent(siteId)}/kv`, { method: 'DELETE' }, siteTarget(origin),
  );
  return Number(unwrapData<JsonObject>(wrapped).cleared ?? 0);
}

// ── Personal system settings (delegated to users on CE: model providers / service configs / my logs) ──

export interface SystemAccessInfo {
  allowed: boolean;
  edition: string;
}

export interface OntologyGovernanceAccessInfo {
  allowed: boolean;
  edition: string;
}

/** Probe: whether the current user can manage personal system settings (the frontend shows/hides the "System management" entry based on this). */
export async function getMySystemAccess(): Promise<SystemAccessInfo> {
  const wrapped = await apiRequest<unknown>('/v1/me/system/access');
  return unwrapData<SystemAccessInfo>(wrapped);
}

/** Probe: whether the current CE user may manage the instance-wide Domain Packs from Settings. */
export async function getOntologyGovernanceAccess(): Promise<OntologyGovernanceAccessInfo> {
  const wrapped = await apiRequest<unknown>('/v1/ontologies/governance/access');
  return unwrapData<OntologyGovernanceAccessInfo>(wrapped);
}

export interface ServiceConfigItem {
  config_key: string;
  config_value: string | null;
  display_name: string;
  description: string;
  group_key: string;
  is_secret: boolean;
  updated_at?: string | null;
  updated_by?: string | null;
}

export interface ServiceConfigGroup {
  group_key: string;
  label: string;
  testable: boolean;
  items: ServiceConfigItem[];
}

export async function getMyServiceConfigs(): Promise<ServiceConfigGroup[]> {
  const wrapped = await apiRequest<unknown>('/v1/me/system/service-configs');
  const data = unwrapData<ServiceConfigGroup[]>(wrapped);
  return Array.isArray(data) ? data : [];
}

export async function updateMyServiceConfigs(
  items: Array<{ key: string; value: string | null }>,
): Promise<void> {
  await apiRequest('/v1/me/system/service-configs', {
    method: 'PUT',
    body: JSON.stringify({ items }),
  });
}

export interface ServiceTestResult {
  success: boolean;
  latency_ms: number;
  error: string | null;
}

export async function testMyServiceConfig(groupKey: string): Promise<ServiceTestResult> {
  const wrapped = await apiRequest<unknown>(
    `/v1/me/system/service-configs/test/${encodeURIComponent(groupKey)}`,
    { method: 'POST' },
  );
  return unwrapData<ServiceTestResult>(wrapped);
}

// ── Model provider management (/v1/models, gate = require_system_settings) ──

export interface ModelProviderItem {
  provider_id: string;
  display_name: string;
  provider_type: 'chat' | 'embedding' | 'reranker';
  provider: string;
  base_url: string;
  api_key: string; // masked
  model_name: string;
  extra_config: Record<string, unknown>;
  is_active: boolean;
  last_tested_at?: string | null;
  last_test_status?: string | null;
}

export interface ModelProviderInput {
  display_name: string;
  provider_type: 'chat' | 'embedding' | 'reranker';
  provider?: string;
  base_url?: string;
  api_key?: string;
  model_name: string;
  extra_config?: Record<string, unknown>;
  is_active?: boolean;
}

export interface ModelRoleAssignment {
  role_key: string;
  label?: string;
  description?: string;
  type?: string;
  required_type?: string;
  /** 该角色额外要求供应商声明的 extra_config 能力位（如 vision 角色的 supports_vision）；
   *  为空表示只按 provider_type 匹配即可。 */
  requires_capability?: string | null;
  provider_id: string | null;
  provider_name?: string | null;
  [key: string]: unknown;
}

export interface ProviderSchemaField {
  key: string;
  label?: string;
  required?: boolean;
  secret?: boolean;
  placeholder?: string;
  [key: string]: unknown;
}

export interface ProviderSchema {
  id: string;
  label?: string;
  engine?: string;
  supports_types?: string[];
  base_url_template?: string;
  autofill_base_url?: boolean;
  api_key_required?: boolean;
  fields?: ProviderSchemaField[];
  [key: string]: unknown;
}

/** 上下文窗口自动探测的输入（未保存的表单值即可探测）。 */
export interface ContextProbeInput {
  provider: string;
  provider_type?: string;
  base_url?: string;
  api_key?: string;
  model_name: string;
  /** 编辑已保存供应商时 API Key 框为空（表示不修改），传 provider_id 让后端复用已存密钥。 */
  provider_id?: string;
  /** 允许多花一次「超限报错」探测（上游校验阶段即拒绝，不产生推理费用）。 */
  allow_error_probe?: boolean;
}

export interface ContextProbeResult {
  /** 探到的上下文窗口（token）；0 表示没探到，需人工填写。 */
  context_length: number;
  /** models_endpoint | ollama_show | max_tokens_probe | name_heuristic */
  source: string;
  source_label: string;
  /** high = 上游自报；medium = 报错回报；low = 按模型名推断。 */
  confidence: 'high' | 'medium' | 'low' | 'none';
  detail: string;
  /** 逐级说明每个来源看到了什么，供管理员判断该手工填多少。 */
  notes: string[];
}

export async function detectModelContextLength(
  input: ContextProbeInput,
): Promise<ContextProbeResult> {
  const wrapped = await apiRequest<unknown>('/v1/models/providers/detect-context', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return unwrapData<ContextProbeResult>(wrapped);
}

export async function listModelProviders(): Promise<ModelProviderItem[]> {
  const wrapped = await apiRequest<unknown>('/v1/models/providers');
  const data = unwrapData<ModelProviderItem[]>(wrapped);
  return Array.isArray(data) ? data : [];
}

export async function createModelProvider(input: ModelProviderInput): Promise<ModelProviderItem> {
  const wrapped = await apiRequest<unknown>('/v1/models/providers', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return unwrapData<ModelProviderItem>(wrapped);
}

export async function updateModelProvider(
  providerId: string,
  input: Partial<ModelProviderInput>,
): Promise<ModelProviderItem> {
  const wrapped = await apiRequest<unknown>(
    `/v1/models/providers/${encodeURIComponent(providerId)}`,
    { method: 'PUT', body: JSON.stringify(input) },
  );
  return unwrapData<ModelProviderItem>(wrapped);
}

export async function deleteModelProvider(providerId: string): Promise<void> {
  await apiRequest(`/v1/models/providers/${encodeURIComponent(providerId)}`, {
    method: 'DELETE',
  });
}

export async function testModelProvider(providerId: string): Promise<ServiceTestResult> {
  const wrapped = await apiRequest<unknown>(
    `/v1/models/providers/${encodeURIComponent(providerId)}/test`,
    { method: 'POST' },
  );
  return unwrapData<ServiceTestResult>(wrapped);
}

export async function listModelRoles(): Promise<ModelRoleAssignment[]> {
  const wrapped = await apiRequest<unknown>('/v1/models/roles');
  const data = unwrapData<ModelRoleAssignment[]>(wrapped);
  return Array.isArray(data) ? data : [];
}

export async function assignModelRole(roleKey: string, providerId: string): Promise<void> {
  await apiRequest(`/v1/models/roles/${encodeURIComponent(roleKey)}`, {
    method: 'PUT',
    body: JSON.stringify({ provider_id: providerId }),
  });
}

export async function unassignModelRole(roleKey: string): Promise<void> {
  await apiRequest(`/v1/models/roles/${encodeURIComponent(roleKey)}`, { method: 'DELETE' });
}

export async function getModelProviderSchemas(): Promise<ProviderSchema[]> {
  const wrapped = await apiRequest<unknown>('/v1/models/provider-schemas');
  const data = unwrapData<ProviderSchema[]>(wrapped);
  return Array.isArray(data) ? data : [];
}

// ── My logs (/v1/me/logs) ──────────────────────────────────────────────────

export interface MyLogQuery {
  page?: number;
  pageSize?: number;
  dateFrom?: string;
  dateTo?: string;
  status?: string;
}

export interface MyLogPage<T> {
  items: T[];
  pagination: Pagination;
}

function logQueryString(q: MyLogQuery): string {
  const params = new URLSearchParams();
  params.set('page', String(q.page ?? 1));
  params.set('page_size', String(q.pageSize ?? 20));
  if (q.dateFrom) params.set('date_from', q.dateFrom);
  if (q.dateTo) params.set('date_to', q.dateTo);
  if (q.status) params.set('status', q.status);
  return params.toString();
}

export interface MyToolLogItem {
  /** 混合架构：该行来自云端还是本机执行面（桌面双模式合并视图）。 */
  origin?: 'cloud' | 'local';
  id: string;
  trace_id?: string | null;
  chat_id?: string | null;
  message_id?: string | null;
  session_title?: string | null;
  user_name?: string | null;
  tool_name: string;
  tool_display_name?: string | null;
  tool_call_id?: string | null;
  mcp_server?: string | null;
  sandbox_id?: string | null;
  tool_args?: unknown;
  tool_result?: unknown;
  result_truncated?: boolean;
  status: string;
  source: string;
  duration_ms?: number | null;
  error_message?: string | null;
  subagent_log_id?: string | null;
  skill_log_id?: string | null;
  started_at?: string | null;
  created_at?: string | null;
}

export interface MySkillLogItem {
  /** 混合架构：该行来自云端还是本机执行面（桌面双模式合并视图）。 */
  origin?: 'cloud' | 'local';
  id: string;
  trace_id?: string | null;
  chat_id?: string | null;
  message_id?: string | null;
  session_title?: string | null;
  user_name?: string | null;
  skill_id: string;
  skill_name?: string | null;
  skill_version?: string | null;
  skill_source?: string | null;
  invocation_type?: string | null;
  script_name?: string | null;
  script_language?: string | null;
  script_args?: unknown;
  script_stdin?: string | null;
  script_stdout?: string | null;
  script_stderr?: string | null;
  output_truncated?: boolean;
  exit_code?: number | null;
  status: string;
  source?: string | null;
  duration_ms?: number | null;
  error_message?: string | null;
  subagent_log_id?: string | null;
  started_at?: string | null;
  created_at?: string | null;
}

export interface MySubagentLogItem {
  /** 混合架构：该行来自云端还是本机执行面（桌面双模式合并视图）。 */
  origin?: 'cloud' | 'local';
  id: string;
  trace_id?: string | null;
  chat_id?: string | null;
  message_id?: string | null;
  session_title?: string | null;
  user_name?: string | null;
  subagent_id?: string | null;
  subagent_name: string;
  subagent_type?: string | null;
  plan_id?: string | null;
  step_id?: string | null;
  step_index?: number | null;
  step_title?: string | null;
  model?: string | null;
  input_messages?: unknown;
  output_content?: string | null;
  intermediate_steps?: unknown;
  token_usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
    llm_call_count?: number;
  } | null;
  tool_calls_count?: number;
  skill_calls_count?: number;
  status: string;
  error_message?: string | null;
  duration_ms?: number | null;
  parent_subagent_log_id?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  created_at?: string | null;
}

export interface MySubagentLogDetail extends MySubagentLogItem {
  child_steps: MySubagentLogItem[];
  tool_calls: MyToolLogItem[];
  skill_calls: MySkillLogItem[];
}

export interface MyUsageItem {
  /** 混合架构：该行来自云端还是本机执行面（桌面双模式合并视图）。 */
  origin?: 'cloud' | 'local';
  message_id: string;
  chat_id: string;
  session_title?: string | null;
  model?: string | null;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  has_error: boolean;
  created_at?: string | null;
}

export interface MyUsageSummaryItem {
  /** 混合架构：该行来自云端还是本机执行面（桌面双模式合并视图）。 */
  origin?: 'cloud' | 'local';
  group_key: string;
  total_requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

async function fetchLogPage<T>(path: string, q: MyLogQuery): Promise<MyLogPage<T>> {
  if (!isHybridDual()) {
    const wrapped = await apiRequest<unknown>(`${path}?${logQueryString(q)}`);
    const data = unwrapData<PaginatedData<T>>(wrapped);
    return {
      items: (data.items ?? []).map((it) => ({ ...it, origin: 'cloud' as const })),
      pagination: data.pagination,
    };
  }
  // 双模式合并分页：不能「云端第N页 + 本机第N页」直接拼——两个独立分页流拼出的
  // 页边界互相错位，且当页 total 会随本机当页有无数据来回跳（55→38 的翻页 bug）。
  // 正确做法：两端各取时间线前 page*size 条，全局按时间倒序归并后再切当前页窗口；
  // total 恒为两端 total 之和。本机未就绪时静默仅展示云端。
  const page = q.page ?? 1;
  const size = q.pageSize ?? 20;
  // 后端 page_size 上限 200（me_logs.py）：翻到 200 条之后的页仅能展示已取窗口，
  // 个人日志查看器可接受，不再逐页补拉。
  const windowSize = Math.min(page * size, 200);
  const wq = logQueryString({ ...q, page: 1, pageSize: windowSize });
  const [cloudRes, localRes] = await Promise.allSettled([
    apiRequest<unknown>(`${path}?${wq}`),
    apiRequest<unknown>(`${path}?${wq}`, undefined, 'local'),
  ]);
  if (cloudRes.status === 'rejected') throw cloudRes.reason;
  const cloud = unwrapData<PaginatedData<T>>(cloudRes.value);
  const cloudItems = (cloud.items ?? []).map((it) => ({ ...it, origin: 'cloud' as const }));
  let localItems: Array<T & { origin: 'cloud' | 'local' }> = [];
  let localTotal = 0;
  if (localRes.status === 'fulfilled') {
    const ld = unwrapData<PaginatedData<T>>(localRes.value);
    localItems = (ld.items ?? []).map((it) => ({ ...it, origin: 'local' as const }));
    localTotal = ld.pagination?.total_items ?? localItems.length;
  }
  const merged = [...cloudItems, ...localItems].sort((a, b) => {
    const ta = String((a as { created_at?: string }).created_at ?? '');
    const tb = String((b as { created_at?: string }).created_at ?? '');
    return tb.localeCompare(ta);
  });
  const totalItems = (cloud.pagination?.total_items ?? cloudItems.length) + localTotal;
  const totalPages = Math.max(1, Math.ceil(totalItems / size));
  return {
    items: merged.slice((page - 1) * size, page * size),
    pagination: {
      page,
      page_size: size,
      total_items: totalItems,
      total_pages: totalPages,
      has_previous: page > 1,
      has_next: page < totalPages,
    },
  };
}

export function getMyToolLogs(q: MyLogQuery = {}): Promise<MyLogPage<MyToolLogItem>> {
  return fetchLogPage<MyToolLogItem>('/v1/me/logs/tools', q);
}

export async function getMyToolLog(logId: string, origin?: 'cloud' | 'local'): Promise<MyToolLogItem> {
  const wrapped = await apiRequest<unknown>(
    `/v1/me/logs/tools/${encodeURIComponent(logId)}`,
    undefined,
    origin === 'local' ? 'local' : undefined,
  );
  return unwrapData<MyToolLogItem>(wrapped);
}

export function getMySkillLogs(q: MyLogQuery = {}): Promise<MyLogPage<MySkillLogItem>> {
  return fetchLogPage<MySkillLogItem>('/v1/me/logs/skills', q);
}

export async function getMySkillLog(logId: string, origin?: 'cloud' | 'local'): Promise<MySkillLogItem> {
  const wrapped = await apiRequest<unknown>(
    `/v1/me/logs/skills/${encodeURIComponent(logId)}`,
    undefined,
    origin === 'local' ? 'local' : undefined,
  );
  return unwrapData<MySkillLogItem>(wrapped);
}

export function getMySubagentLogs(q: MyLogQuery = {}): Promise<MyLogPage<MySubagentLogItem>> {
  return fetchLogPage<MySubagentLogItem>('/v1/me/logs/subagents', q);
}

export async function getMySubagentLog(logId: string, origin?: 'cloud' | 'local'): Promise<MySubagentLogDetail> {
  const wrapped = await apiRequest<unknown>(
    `/v1/me/logs/subagents/${encodeURIComponent(logId)}`,
    undefined,
    origin === 'local' ? 'local' : undefined,
  );
  return unwrapData<MySubagentLogDetail>(wrapped);
}

export function getMyUsage(q: MyLogQuery = {}): Promise<MyLogPage<MyUsageItem>> {
  return fetchLogPage<MyUsageItem>('/v1/me/logs/usage', q);
}

export async function getMyUsageSummary(
  groupBy: 'day' | 'model' = 'day',
): Promise<MyUsageSummaryItem[]> {
  const wrapped = await apiRequest<unknown>(`/v1/me/logs/usage/summary?group_by=${groupBy}`);
  const data = unwrapData<MyUsageSummaryItem[]>(wrapped);
  const cloud = (Array.isArray(data) ? data : []).map((it) => ({ ...it, origin: 'cloud' as 'cloud' | 'local' }));
  if (!isHybridDual()) return cloud;
  try {
    const lw = await apiRequest<unknown>(
      `/v1/me/logs/usage/summary?group_by=${groupBy}`,
      undefined,
      'local',
    );
    const ld = unwrapData<MyUsageSummaryItem[]>(lw);
    const local = (Array.isArray(ld) ? ld : []).map((it) => ({ ...it, origin: 'local' as 'cloud' | 'local' }));
    return [...cloud, ...local];
  } catch {
    return cloud;
  }
}

// ── Evolution: per-turn settlement (GCE ticket 06) ───────────────────────────
//
// The SSE push is the fast path for a live client; this is the durable one —
// used after a reload, or when the connection dropped before settlement
// finished. A turn still settling legitimately reports `pending`.
export async function getTurnSettlement(messageId: string): Promise<EvolutionSummary> {
  const wrapped = await apiRequest<unknown>(
    `/v1/evolution/turns/${encodeURIComponent(messageId)}`,
  );
  return unwrapData<EvolutionSummary>(wrapped);
}

// ── Evolution: contribution summary (settings panel detail card) ─────────────
//
// The separate contribution setting is gone from the UI: participation follows
// the single evolution switch in EvolutionPrefs. Only the summary — "what did
// my conversations actually produce" — remains its own endpoint.
export interface EvolutionContributionItem {
  candidate_id: string;
  target_kind: 'memory' | 'skill' | 'workflow' | 'ontology' | 'prompt';
  summary: string;
  status: string;
  risk_tier: string;
  your_episodes: number;
  total_evidence: number;
  created_at?: string | null;
}

export interface EvolutionContributions {
  episodes: number;
  contributed_episodes: number;
  private_episodes: number;
  memory_written: number;
  candidates: EvolutionContributionItem[];
}

export async function getEvolutionContributions(): Promise<EvolutionContributions> {
  return unwrapData<EvolutionContributions>(await apiRequest<unknown>('/v1/evolution/contributions'));
}

// ── Evolution: personal candidate approval (settings console) ────────────────
/** The concrete content of a proposed change — what approving it will do. */
export type EvolutionChangePreview =
  | {
      type: 'skill_document';
      display_name: string;
      description: string;
      allowed_tools: string[];
      content: string;
    }
  | {
      type: 'skill_sequence';
      display_name: string;
      description: string;
      allowed_tools: string[];
      steps: string[];
      ordering_constraints: Array<Record<string, unknown>>;
    }
  | {
      type: 'memory_ops';
      operations: Array<{
        operation: string;
        text: string;
        reason: string;
        before?: unknown;
        after?: unknown;
      }>;
    }
  | Record<string, never>;

export interface MyEvolutionCandidate {
  candidate_id: string;
  target_kind: string;
  operation: string;
  summary: string;
  status: string;
  risk_tier: string;
  your_episodes: number;
  total_evidence: number;
  own_share: number;
  tool_sequence: string[];
  /** What the button does. Rows the user cannot action are not returned at all. */
  action: string;
  action_label: string;
  action_effect: string;
  change: EvolutionChangePreview;
  created_at?: string | null;
}

export async function getMyEvolutionCandidates(): Promise<{
  candidates: MyEvolutionCandidate[];
  own_evidence_threshold: number;
}> {
  return unwrapData(await apiRequest<unknown>('/v1/evolution/my-candidates'));
}

export async function approveMyEvolutionCandidate(candidateId: string): Promise<{
  skill_id: string;
  scope: string;
  tools: string[];
}> {
  return unwrapData(
    await apiRequest<unknown>(
      `/v1/evolution/my-candidates/${encodeURIComponent(candidateId)}/approve`,
      { method: 'POST' },
    ),
  );
}

// ── Evolution: per-user preferences ─────────────────────────────────────────
export interface EvolutionPrefs {
  enabled: boolean;
  /** `none` is a first-class choice, not a degraded state. */
  ontology_mode: 'none' | 'any' | 'specific';
  ontology_pack_id: string;
  mechanisms: string[];
  min_support: number;
  auto_approve_low_risk: boolean;
}

export interface OntologyPackOption {
  pack_id: string;
  name: string;
  active: boolean;
}

export async function getEvolutionPrefs(): Promise<{
  prefs: EvolutionPrefs;
  ontology_packs: OntologyPackOption[];
  ontology_modes: string[];
}> {
  return unwrapData(await apiRequest<unknown>('/v1/evolution/prefs'));
}

export async function updateEvolutionPrefs(
  patch: Partial<EvolutionPrefs>,
): Promise<EvolutionPrefs> {
  return unwrapData(
    await apiRequest<unknown>('/v1/evolution/prefs', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }),
  );
}

import type {
  WikiCapability,
  WikiFolder,
  WikiGraphData,
  WikiIndexOverview,
  WikiPageBrief,
  WikiPageDetail,
  WikiSourceChunk,
  WikiStats,
} from './types';

// ── 知识库的 LLM Wiki / 概念图谱 ─────────────────────────────────────────────
//
// 两类知识库都可能有这层结构化产物：勾选了 Wiki 索引模式的自建库，以及提供该
// 能力的外接后端。前端先调 getWikiCapability 做平台级探测，再看单个知识库的
// capabilities.wiki——两级都为真才渲染入口，避免出现注定 404 的按钮。

export async function getWikiCapability(): Promise<WikiCapability> {
  try {
    return unwrapData<WikiCapability>(
      await apiRequest<unknown>('/v1/catalog/kb/wiki/capability'),
    );
  } catch {
    return { provider: '', supports_wiki: false };
  }
}

export async function getWikiStats(kbId: string): Promise<WikiStats> {
  return unwrapData<WikiStats>(
    await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/wiki/stats`),
  );
}

export async function getWikiPages(
  kbId: string,
  options: {
    page?: number;
    pageSize?: number;
    /** 逗号分隔多类型，如 entity,concept,synthesis,comparison */
    pageType?: string;
    /** 把范围收窄到某个目录节点 */
    categoryPath?: string;
    categoryDepth?: number;
  } = {},
): Promise<{ pages: WikiPageBrief[]; total: number }> {
  const { page = 1, pageSize = 50, pageType = '', categoryPath = '', categoryDepth = 0 } = options;
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  if (pageType) params.set('page_type', pageType);
  if (categoryPath) {
    params.set('category_path', categoryPath);
    params.set('category_depth', String(categoryDepth || 1));
  }
  const data = unwrapData<{ pages?: WikiPageBrief[]; total?: number }>(
    await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/wiki/pages?${params}`),
  );
  return { pages: data.pages || [], total: data.total || 0 };
}

/** 目录树的某一层；parentId 留空取根层 */
export async function getWikiFolders(
  kbId: string,
  parentId = '',
  pageTypes = '',
): Promise<WikiFolder[]> {
  const params = new URLSearchParams();
  if (parentId) params.set('parent_id', parentId);
  if (pageTypes) params.set('page_types', pageTypes);
  const qs = params.toString();
  const data = unwrapData<{ folders?: WikiFolder[] }>(
    await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/wiki/folders${qs ? `?${qs}` : ''}`),
  );
  return data.folders || [];
}

export async function getWikiIndexOverview(
  kbId: string,
  limit = 20,
): Promise<WikiIndexOverview> {
  const data = unwrapData<WikiIndexOverview>(
    await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/wiki/index?limit=${limit}`),
  );
  return { intro: data.intro || '', version: data.version, groups: data.groups || [] };
}

export async function searchWikiPages(
  kbId: string,
  query: string,
  limit = 20,
): Promise<{ pages: WikiPageBrief[]; total: number }> {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  const data = unwrapData<{ pages?: WikiPageBrief[]; total?: number }>(
    await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/wiki/search?${params}`),
  );
  return { pages: data.pages || [], total: data.total || 0 };
}

export async function getWikiPage(kbId: string, slug: string): Promise<WikiPageDetail> {
  return unwrapData<WikiPageDetail>(
    await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/wiki/page/${slug}`),
  );
}

export async function getWikiGraph(
  kbId: string,
  options: { mode?: 'overview' | 'ego'; center?: string; depth?: number; limit?: number; types?: string } = {},
): Promise<WikiGraphData> {
  const { mode = 'overview', center = '', depth = 1, limit = 60, types = '' } = options;
  const params = new URLSearchParams({ mode, limit: String(limit) });
  if (mode === 'ego') {
    params.set('center', center);
    params.set('depth', String(depth));
  }
  if (types) params.set('types', types);
  const data = unwrapData<WikiGraphData>(
    await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/wiki/graph?${params}`),
  );
  return { nodes: data.nodes || [], edges: data.edges || [], meta: data.meta };
}

export async function getWikiSourceChunks(
  kbId: string,
  slug: string,
  maxChunks = 6,
): Promise<{ page: WikiPageDetail; chunks: WikiSourceChunk[] }> {
  const data = unwrapData<{ page?: WikiPageDetail; chunks?: WikiSourceChunk[] }>(
    await apiRequest<unknown>(
      `/v1/catalog/kb/${kbId}/wiki/source/${slug}?max_chunks=${maxChunks}`,
    ),
  );
  return { page: data.page as WikiPageDetail, chunks: data.chunks || [] };
}
