/**
 * API Client for HugAgentOS Backend.
 *
 * Uses v1 unified response envelope.
 */

import type { Catalog, ChatItem, ChatMessage, ChunkPreviewResult, KBChunk, MemoryItem, MemoryProfile, MemoryGraphRelation, ResourceItem, AutomationTask, AutomationRun, AutomationNotification, FileConfirmInfo, FileConfirmDecision, DesignPickInfo, OntologyAssetKind, OntologyTagOption } from './types';
import type { TeamRole } from './utils/roles';
import { createApiResponseError, LicenseError, licenseErrorMessage, readErrorMessage } from './utils/apiError';
import { t } from './i18n';

export { LicenseError } from './utils/apiError';

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

function isApiEnvelope<T>(payload: unknown): payload is ApiEnvelope<T> {
  return !!payload && typeof payload === 'object' && 'code' in payload && 'data' in payload;
}

function unwrapData<T>(payload: unknown): T {
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
const UPLOAD_MAX_MB = Number(import.meta.env.VITE_UPLOAD_MAX_MB) || 50;
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

async function apiRequest<T>(path: string, options?: RequestInit): Promise<T> {
  const url = `${getApiUrl()}${path}`;
  const response = await fetch(url, {
    ...options,
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
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
    if (response.status === 402) {
      throw new LicenseError(licenseErrorMessage(payload));
    }
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
}

export interface UserSelectableModel {
  provider_id: string;
  display_name: string;
  model_name: string;
  provider: string;
  is_default: boolean;
  supports_reasoning_effort: boolean;
}

export async function getMainModelCapabilities(): Promise<ModelCapabilities> {
  const wrapped = await apiRequest<unknown>('/v1/models/capabilities');
  const data = unwrapData<JsonObject>(wrapped);
  const main = (data?.main_agent as JsonObject | undefined) || {};
  const switchInfo = (data?.user_model_switch as JsonObject | undefined) || {};
  const modelsRaw = Array.isArray(switchInfo.models) ? switchInfo.models : [];
  const models: UserSelectableModel[] = modelsRaw
    .map((item) => {
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
      };
    })
    .filter((item): item is UserSelectableModel => item !== null);
  return {
    supports_reasoning_effort: !!main.supports_reasoning_effort,
    user_model_switch_enabled: !!switchInfo.enabled,
    user_selectable_models: models,
  };
}

export interface EditionInfo {
  /** Deployment edition: ce (community) / ee (commercial). */
  edition: string;
  /** License state machine: internal / licensed / grace / expired / invalid / missing / ce. */
  mode: string;
  /** Feature-flag boolean map (multi_tenancy / audit / billing ...); all false on CE. */
  features: Record<string, boolean>;
}

export async function getEditionInfo(): Promise<EditionInfo> {
  const wrapped = await apiRequest<unknown>('/v1/meta/edition');
  const data = unwrapData<JsonObject>(wrapped);
  return {
    edition: String(data?.edition || 'ee'),
    mode: String(data?.mode || 'internal'),
    features: (data?.features as Record<string, boolean> | undefined) || {},
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
  return {
    items,
    total: pagination?.total_items ?? items.length,
    page: pagination?.page ?? page,
    page_size: pagination?.page_size ?? pageSize,
    has_more: Boolean(pagination?.has_next),
  };
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
  const wrapped = await apiRequest<unknown>(`/v1/chats/${chatId}`);
  const data = unwrapData<JsonObject>(wrapped);
  return toChatItem(data);
}

export async function createSession(data: CreateSessionRequest): Promise<ChatItem> {
  const wrapped = await apiRequest<unknown>('/v1/chats', {
    method: 'POST',
    body: JSON.stringify(data),
  });
  const payload = unwrapData<JsonObject>(wrapped);
  return toChatItem(payload);
}

export async function updateSession(chatId: string, data: UpdateSessionRequest): Promise<ChatItem> {
  const wrapped = await apiRequest<unknown>(`/v1/chats/${chatId}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
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

export async function deleteSession(chatId: string): Promise<void> {
  await apiRequest(`/v1/chats/${chatId}`, {
    method: 'DELETE',
  });
}

export interface ChatDetail {
  chat_id: string;
  title: string;
  user_id: string;
  project_id: string | null;
  share_scope: 'private' | 'team_read' | 'team_edit';
  owner_user_id: string;
  is_owner: boolean;
  access_level: 'admin' | 'edit' | 'read';
  /** Whether this chat belongs to a team project — determines whether the owner can set it to shared. */
  is_team_project?: boolean;
  pinned?: boolean;
  favorite?: boolean;
  metadata?: Record<string, unknown>;
}

/** Fetch chat detail (carries share_scope / is_owner / access_level in shared scenarios). */
export async function getChatDetail(chatId: string): Promise<ChatDetail> {
  const wrapped = await apiRequest<unknown>(`/v1/chats/${encodeURIComponent(chatId)}`);
  return unwrapData<ChatDetail>(wrapped);
}

export async function getChatMessages(chatId: string): Promise<ChatMessage[]> {
  const wrapped = await apiRequest<unknown>(`/v1/chats/${chatId}/messages`);
  const data = unwrapData<PaginatedData<JsonObject>>(wrapped);
  const items = Array.isArray(data.items) ? data.items : [];
  return items.map((item) => ({
    role: String(item.role) === 'assistant' ? 'assistant' : 'user',
    content: String(item.content ?? ''),
    isMarkdown: Boolean((item.metadata as JsonObject | undefined)?.is_markdown),
    ts: toTimestamp(item.created_at),
    citations: Array.isArray((item.metadata as JsonObject | undefined)?.citations)
      ? ((item.metadata as JsonObject).citations as ChatMessage['citations'])
      : undefined,
  }));
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
export async function cancelChatRun(runId: string, userId?: string): Promise<void> {
  await apiRequest<unknown>(`/v1/chat-runs/${runId}/cancel${buildQuery({ user_id: userId })}`, { method: 'POST' });
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
): Promise<Response> {
  const qs = buildQuery({ from: fromOffset, user_id: userId });
  const url = `${getApiUrl()}/v1/chats/stream/${encodeURIComponent(runId)}${qs || '?from=0'}`;
  return authFetch(url, { method: 'GET', signal });
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
    headers: { 'Content-Type': 'application/json' },
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
    headers: { 'Content-Type': 'application/json' },
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
  title: string;
  desc?: string;
  word_count?: number;
  indexing_status?: string;
  enabled?: boolean;
  data_source_type?: string;
  created_at?: number;
  content?: string;
}

export async function getKBDocuments(
  kbId: string,
  page = 1,
  pageSize = 20,
): Promise<KBDocumentsResponse> {
  try {
    const wrapped = await apiRequest<unknown>(
      `/v1/catalog/kb/${kbId}/documents?page=${page}&page_size=${pageSize}`,
    );
    const data = unwrapData<PaginatedData<KBDocumentItem>>(wrapped);
    const items = Array.isArray(data.items) ? data.items : [];
    const pagination = data.pagination;
    return {
      items,
      total: typeof pagination?.total_items === 'number' ? pagination.total_items : items.length,
      page: typeof pagination?.page === 'number' ? pagination.page : page,
      page_size: typeof pagination?.page_size === 'number' ? pagination.page_size : pageSize,
      has_more: Boolean(pagination?.has_next),
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
): Promise<Record<string, unknown>> {
  const wrapped = await apiRequest<unknown>('/v1/catalog/kb', {
    method: 'POST',
    body: JSON.stringify({
      name,
      description: description || undefined,
      chunk_method: chunkMethod || 'semantic',
      indexing_config: indexingConfig || undefined,
      visibility: visibility || 'private',
    }),
  });
  return unwrapData<Record<string, unknown>>(wrapped);
}

export async function updateKBSpace(
  kbId: string,
  payload: { name?: string; description?: string },
): Promise<Record<string, unknown>> {
  const wrapped = await apiRequest<unknown>(`/v1/catalog/kb/${kbId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
  return unwrapData<Record<string, unknown>>(wrapped);
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

export async function deleteMemory(memoryId: string): Promise<void> {
  await apiRequest(`/v1/memories/${memoryId}`, { method: 'DELETE' });
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
  ontology_enabled: boolean;
  ontology_pack_ids: string[];
  available: boolean;
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

export interface TeamMembershipBrief {
  team_id: string;
  name: string;
  role: TeamRole;
  source?: 'manual' | 'sso_auto';
  sso_department?: string | null;
  description?: string | null;
  member_count?: number;
}

export interface AuthUser {
  user_id: string;
  username: string;
  email?: string;
  avatar_url?: string;
  nickname?: string | null;
  real_name?: string | null;
  department?: string | null;
  teams?: TeamMembershipBrief[];
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
  /** Whether the user may create private knowledge bases (default false; visible only to the owner) */
  can_create_private_kb?: boolean;
  /** Whether the user may create public knowledge bases (default false; visible to everyone by default, can be further restricted by grants) */
  can_create_public_kb?: boolean;
  /** Whether the user may self-service create channel bots (inbound bots such as Feishu, default false) */
  can_create_channel_bot?: boolean;
  /** Whether the user may switch the currently available model from the chat input box (default false) */
  can_switch_model?: boolean;
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

export interface TeamMemberBrief {
  user_id: string;
  username: string;
  avatar_url?: string | null;
  role: TeamRole;
  joined_at?: string | null;
  is_self?: boolean;
}

export interface UserSearchResult {
  user_id: string;
  username: string;
  real_name?: string | null;
  avatar_url?: string | null;
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

export async function getMyTeams(): Promise<TeamMembershipBrief[]> {
  const wrapped = await apiRequest<unknown>('/v1/me/teams');
  const d = unwrapData<{ items?: TeamMembershipBrief[] }>(wrapped);
  return Array.isArray(d?.items) ? d.items : [];
}

export async function getTeamMembers(teamId: string): Promise<{ items: TeamMemberBrief[]; my_role: string }> {
  const wrapped = await apiRequest<unknown>(`/v1/me/teams/${encodeURIComponent(teamId)}/members`);
  const d = unwrapData<{ items: TeamMemberBrief[]; my_role: string }>(wrapped);
  return { items: d?.items || [], my_role: d?.my_role || 'member' };
}

export async function inviteTeamMember(
  teamId: string,
  body: { user_id?: string; username?: string; role?: Exclude<TeamRole, 'owner'> },
): Promise<void> {
  await apiRequest(`/v1/me/teams/${encodeURIComponent(teamId)}/members`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function removeTeamMember(teamId: string, memberUserId: string): Promise<void> {
  await apiRequest(`/v1/me/teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(memberUserId)}`, {
    method: 'DELETE',
  });
}

export async function searchUsers(q: string, limit = 10): Promise<UserSearchResult[]> {
  const wrapped = await apiRequest<unknown>(`/v1/me/users/search?q=${encodeURIComponent(q)}&limit=${limit}`);
  const d = unwrapData<{ items?: UserSearchResult[] }>(wrapped);
  return Array.isArray(d?.items) ? d.items : [];
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
  return fetch(input, {
    ...init,
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

// ── Team folders / team files API ────────────────────────────────
import type {
  MyTeamItem,
  TeamFolderNode,
  TeamFolderFlat,
  TeamMemberPermission,
  TeamFilePermission,
} from './types/teamFiles';

export async function listMyTeamsWithPermissions(): Promise<MyTeamItem[]> {
  const wrapped = await apiRequest<unknown>('/v1/my-teams');
  const data = unwrapData<{ items: MyTeamItem[] }>(wrapped);
  return data.items || [];
}

export async function listTeamFolderTree(teamId: string): Promise<TeamFolderNode[]> {
  const wrapped = await apiRequest<unknown>(`/v1/teams/${encodeURIComponent(teamId)}/folders?as=tree`);
  const data = unwrapData<{ tree: TeamFolderNode[] }>(wrapped);
  return data.tree || [];
}

export async function listTeamFoldersFlat(teamId: string): Promise<TeamFolderFlat[]> {
  const wrapped = await apiRequest<unknown>(`/v1/teams/${encodeURIComponent(teamId)}/folders?as=flat`);
  const data = unwrapData<{ items: TeamFolderFlat[] }>(wrapped);
  return data.items || [];
}

export async function createTeamFolder(
  teamId: string,
  name: string,
  parentFolderId: string | null,
): Promise<{ folder_id: string }> {
  const wrapped = await apiRequest<unknown>(`/v1/teams/${encodeURIComponent(teamId)}/folders`, {
    method: 'POST',
    body: JSON.stringify({ name, parent_folder_id: parentFolderId }),
  });
  return unwrapData<{ folder_id: string }>(wrapped);
}

export async function renameTeamFolder(
  teamId: string,
  folderId: string,
  name: string,
): Promise<void> {
  await apiRequest(`/v1/teams/${encodeURIComponent(teamId)}/folders/${encodeURIComponent(folderId)}`, {
    method: 'PATCH',
    body: JSON.stringify({ name }),
  });
}

export async function moveTeamFolder(
  teamId: string,
  folderId: string,
  newParentFolderId: string | null,
): Promise<void> {
  await apiRequest(`/v1/teams/${encodeURIComponent(teamId)}/folders/${encodeURIComponent(folderId)}`, {
    method: 'PATCH',
    body: JSON.stringify({ parent_folder_id: newParentFolderId }),
  });
}

export async function deleteTeamFolder(
  teamId: string,
  folderId: string,
): Promise<{ artifacts_affected: number }> {
  const wrapped = await apiRequest<unknown>(
    `/v1/teams/${encodeURIComponent(teamId)}/folders/${encodeURIComponent(folderId)}`,
    { method: 'DELETE' },
  );
  return unwrapData<{ artifacts_affected: number }>(wrapped);
}

export async function getFolderAffectedCount(
  teamId: string,
  folderId: string,
): Promise<number> {
  const wrapped = await apiRequest<unknown>(
    `/v1/teams/${encodeURIComponent(teamId)}/folders/${encodeURIComponent(folderId)}/affected-count`,
  );
  const data = unwrapData<{ count: number }>(wrapped);
  return data.count || 0;
}

export async function listTeamFiles(params: {
  teamId: string;
  folderId: string | null;
  type?: 'document' | 'image';
  keyword?: string;
  page?: number;
  page_size?: number;
}): Promise<{ items: ResourceItem[]; total: number; has_more: boolean }> {
  const qs = new URLSearchParams();
  if (params.folderId) qs.set('folder_id', params.folderId);
  if (params.type) qs.set('type', params.type);
  if (params.keyword) qs.set('keyword', params.keyword);
  if (params.page) qs.set('page', String(params.page));
  if (params.page_size) qs.set('page_size', String(params.page_size));
  const q = qs.toString();
  const wrapped = await apiRequest<unknown>(
    `/v1/teams/${encodeURIComponent(params.teamId)}/files${q ? '?' + q : ''}`,
  );
  return unwrapData<{ items: ResourceItem[]; total: number; has_more: boolean }>(wrapped);
}

export async function uploadTeamFile(
  teamId: string,
  folderId: string | null,
  file: File,
): Promise<ResourceItem> {
  const url = `${getApiUrl()}/v1/teams/${encodeURIComponent(teamId)}/files/upload`;
  const form = new FormData();
  form.append('file', file);
  if (folderId) form.append('folder_id', folderId);
  const response = await fetch(url, { method: 'POST', credentials: 'include', body: form });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(readErrorMessage(payload, `Upload failed: ${response.status}`));
  }
  const payload = await response.json();
  return unwrapData<ResourceItem>(payload);
}

export async function deleteTeamFile(teamId: string, artifactId: string): Promise<void> {
  await apiRequest(
    `/v1/teams/${encodeURIComponent(teamId)}/files/${encodeURIComponent(artifactId)}`,
    { method: 'DELETE' },
  );
}

export async function moveTeamFile(
  teamId: string,
  artifactId: string,
  targetFolderId: string | null,
): Promise<ResourceItem> {
  const wrapped = await apiRequest<unknown>(
    `/v1/teams/${encodeURIComponent(teamId)}/files/${encodeURIComponent(artifactId)}/move`,
    { method: 'POST', body: JSON.stringify({ folder_id: targetFolderId }) },
  );
  return unwrapData<ResourceItem>(wrapped);
}

export async function moveArtifactToTeam(
  artifactId: string,
  teamId: string,
  folderId: string | null,
): Promise<ResourceItem> {
  const wrapped = await apiRequest<unknown>(
    `/v1/artifacts/${encodeURIComponent(artifactId)}/move-to-team`,
    { method: 'POST', body: JSON.stringify({ team_id: teamId, folder_id: folderId }) },
  );
  return unwrapData<ResourceItem>(wrapped);
}

/** Copy a personal file into a team folder (non-destructive; keeps the personal original). */
export async function copyArtifactToTeam(
  artifactId: string,
  teamId: string,
  folderId: string | null,
): Promise<ResourceItem> {
  const wrapped = await apiRequest<unknown>(
    `/v1/artifacts/${encodeURIComponent(artifactId)}/copy-to-team`,
    { method: 'POST', body: JSON.stringify({ team_id: teamId, folder_id: folderId }) },
  );
  return unwrapData<ResourceItem>(wrapped);
}

/** Recursively copy a personal folder into a team folder (keeps the personal originals). Returns {folders, files} counts. */
export async function copyFolderToTeam(
  personalFolderId: string,
  teamId: string,
  folderId: string | null,
): Promise<{ folders: number; files: number }> {
  const wrapped = await apiRequest<unknown>(
    `/v1/myspace/folders/${encodeURIComponent(personalFolderId)}/copy-to-team`,
    { method: 'POST', body: JSON.stringify({ team_id: teamId, folder_id: folderId }) },
  );
  return unwrapData<{ folders: number; files: number }>(wrapped);
}

export async function listTeamMemberPermissions(teamId: string): Promise<TeamMemberPermission[]> {
  const wrapped = await apiRequest<unknown>(`/v1/teams/${encodeURIComponent(teamId)}/members/permissions`);
  const data = unwrapData<{ items: TeamMemberPermission[] }>(wrapped);
  return data.items || [];
}

export async function setTeamMemberPermission(
  teamId: string,
  userId: string,
  permission: TeamFilePermission,
): Promise<void> {
  await apiRequest(
    `/v1/teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(userId)}/permission`,
    { method: 'PUT', body: JSON.stringify({ file_permission: permission }) },
  );
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
  attachments?: Array<{ name: string; content: string; mime_type: string; file_id: string; download_url: string }>,
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
  getFollowUpQuestions,
  getCurrentUser,
  getUserPreferences,
  updateUserPreferences,
  healthCheck,
  getMemories,
  deleteMemory,
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

export interface SkillDistillResultMeta {
  proposed_skill_id?: string;
  display_name?: string;
  description?: string;
  tags?: string[];
  confidence?: number;
  digest_text?: string;
  sampled_ratio?: number;
  partial?: boolean;
  session_count?: number;
  useful_digests?: number;
}

export interface SkillDistillJob {
  job_id: string;
  kind: string;
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';
  progress_done: number;
  progress_total: number;
  cost_usd: number;
  scope: { chat_ids?: string[] | null; hint?: string; include_project_memories?: boolean };
  result_meta: SkillDistillResultMeta;
  saved_skill_id?: string | null;
  error?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  result_skill_content?: string | null;
}

export async function createSkillDistillJob(params: {
  chat_ids: string[] | 'all';
  hint?: string;
  include_project_memories?: boolean;
}): Promise<SkillDistillJob> {
  const res = await apiRequest<unknown>('/v1/lab/skill-distill/jobs', {
    method: 'POST',
    body: JSON.stringify(params),
  });
  return unwrapData<SkillDistillJob>(res);
}

export async function listSkillDistillJobs(limit = 20): Promise<SkillDistillJob[]> {
  const res = await apiRequest<unknown>(`/v1/lab/skill-distill/jobs?limit=${limit}`);
  const data = unwrapData<{ items: SkillDistillJob[] }>(res);
  return Array.isArray(data.items) ? data.items : [];
}

export async function getSkillDistillJob(jobId: string): Promise<SkillDistillJob> {
  const res = await apiRequest<unknown>(`/v1/lab/skill-distill/jobs/${jobId}`);
  return unwrapData<SkillDistillJob>(res);
}

export async function saveSkillDistillJob(
  jobId: string,
  params: { skill_content?: string; enable?: boolean },
): Promise<{ skill_id: string; display_name: string; is_enabled: boolean; job: SkillDistillJob }> {
  const res = await apiRequest<unknown>(`/v1/lab/skill-distill/jobs/${jobId}/save`, {
    method: 'POST',
    body: JSON.stringify(params),
  });
  return unwrapData<{ skill_id: string; display_name: string; is_enabled: boolean; job: SkillDistillJob }>(res);
}

export async function cancelSkillDistillJob(jobId: string): Promise<SkillDistillJob> {
  const res = await apiRequest<unknown>(`/v1/lab/skill-distill/jobs/${jobId}/cancel`, {
    method: 'POST',
  });
  return unwrapData<SkillDistillJob>(res);
}

export async function deleteSkillDistillJob(jobId: string): Promise<void> {
  await apiRequest<unknown>(`/v1/lab/skill-distill/jobs/${jobId}`, { method: 'DELETE' });
}

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
  TeamForProjectCreation,
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
  const wrapped = await apiRequest<unknown>(`/v1/projects${qs ? `?${qs}` : ''}`);
  return unwrapData<ProjectListResponse>(wrapped);
}

export async function listMyTeamsForProjects(): Promise<TeamForProjectCreation[]> {
  const wrapped = await apiRequest<unknown>('/v1/projects/teams');
  const data = unwrapData<{ teams: TeamForProjectCreation[] }>(wrapped);
  return data?.teams || [];
}

export async function createProject(body: {
  name: string;
  description?: string;
  kind: 'personal' | 'team';
  team_id?: string;
  linked_folder_id?: string;
  linked_team_folder_id?: string;
}): Promise<ProjectDetail> {
  const wrapped = await apiRequest<unknown>('/v1/projects', {
    method: 'POST',
    body: JSON.stringify(body),
  });
  return unwrapData<ProjectDetail>(wrapped);
}

export async function getProject(projectId: string): Promise<ProjectDetail> {
  const wrapped = await apiRequest<unknown>(`/v1/projects/${encodeURIComponent(projectId)}`);
  return unwrapData<ProjectDetail>(wrapped);
}

export async function updateProject(
  projectId: string,
  patch: Partial<Pick<ProjectItem, 'name' | 'description' | 'instructions' | 'pinned' | 'icon_color' | 'memory_enabled' | 'memory_write_enabled'>>,
): Promise<ProjectDetail> {
  const wrapped = await apiRequest<unknown>(`/v1/projects/${encodeURIComponent(projectId)}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
  return unwrapData<ProjectDetail>(wrapped);
}

export async function deleteProject(projectId: string): Promise<void> {
  await apiRequest<unknown>(`/v1/projects/${encodeURIComponent(projectId)}`, { method: 'DELETE' });
}

export async function toggleProjectFavorite(projectId: string, on: boolean): Promise<void> {
  await apiRequest<unknown>(
    `/v1/projects/${encodeURIComponent(projectId)}/favorite`,
    { method: on ? 'POST' : 'DELETE' },
  );
}

export async function updateProjectInstructions(projectId: string, instructions: string): Promise<ProjectDetail> {
  const wrapped = await apiRequest<unknown>(
    `/v1/projects/${encodeURIComponent(projectId)}/instructions`,
    {
      method: 'PATCH',
      body: JSON.stringify({ instructions }),
    },
  );
  return unwrapData<ProjectDetail>(wrapped);
}

export async function listProjectFiles(projectId: string): Promise<{
  items: ProjectFileItem[];
  total: number;
  capacity_used: number;
  capacity_limit: number;
}> {
  const wrapped = await apiRequest<unknown>(`/v1/projects/${encodeURIComponent(projectId)}/files`);
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
  );
  const data = unwrapData<{ items: ProjectChatSummary[]; pagination: { total_items: number } }>(wrapped);
  return { items: data?.items || [], total: data?.pagination?.total_items || 0 };
}

/**
 * Set the current chat's share scope within a team project.
 * Only the chat owner may call this; the chat must belong to a ``kind='team'``
 * project to be set to anything other than private.
 */
export async function updateChatShareScope(
  chatId: string,
  shareScope: 'private' | 'team_read' | 'team_edit',
): Promise<unknown> {
  const wrapped = await apiRequest<unknown>(
    `/v1/chats/${encodeURIComponent(chatId)}/share`,
    {
      method: 'POST',
      body: JSON.stringify({ share_scope: shareScope }),
    },
  );
  return unwrapData<unknown>(wrapped);
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
  patch: { display_name?: string; enabled?: boolean; resource_scope?: { kb_ids?: string[]; skill_ids?: string[] }; agent_id?: string | null },
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

export async function getYidaStatus(): Promise<YidaStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/yida/status');
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
  body: { model_name?: string; evaluator_model?: string; worker_max_iters?: number; hitl_enabled?: boolean; enable_thinking?: boolean; chat_mode?: string } = {},
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
  body: { model_name?: string; evaluator_model?: string; worker_max_iters?: number; hitl_enabled?: boolean; enable_thinking?: boolean; chat_mode?: string } = {},
  signal?: AbortSignal,
): Promise<Response> {
  return authFetch(`${getApiUrl()}/v1/loops/${encodeURIComponent(loopId)}/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });
}

export async function cancelLoop(loopId: string): Promise<boolean> {
  const wrapped = await apiRequest<unknown>(`/v1/loops/${encodeURIComponent(loopId)}/cancel`, {
    method: 'POST',
  });
  return (unwrapData<{ cancelled: boolean }>(wrapped) || { cancelled: false }).cancelled;
}

// ── Sites (site hosting) ────────────────────────────────────────────────

export interface SiteItem {
  site_id: string;
  slug: string;
  /** In-app relative access URL, of the form /site/<slug>/ */
  url: string;
  title: string;
  description: string | null;
  visibility: 'public' | 'private' | 'team';
  team_id: string | null;
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
    visibility: raw.visibility === 'private' ? 'private' : raw.visibility === 'team' ? 'team' : 'public',
    team_id: typeof raw.team_id === 'string' ? raw.team_id : null,
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

export async function listSites(page = 1, pageSize = 50): Promise<{ items: SiteItem[]; total: number }> {
  const wrapped = await apiRequest<unknown>(`/v1/sites?page=${page}&page_size=${pageSize}`);
  const data = unwrapData<JsonObject>(wrapped);
  const items = Array.isArray(data.items) ? (data.items as JsonObject[]).map(toSiteItem) : [];
  const pagination = (data.pagination ?? {}) as JsonObject;
  return { items, total: Number(pagination.total_items ?? items.length) };
}

export async function updateSite(
  siteId: string,
  data: { title?: string; visibility?: 'public' | 'private' | 'team'; team_id?: string; slug?: string; description?: string },
): Promise<SiteItem> {
  const wrapped = await apiRequest<unknown>(`/v1/sites/${encodeURIComponent(siteId)}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
  return toSiteItem(unwrapData<JsonObject>(wrapped));
}

export async function deleteSite(siteId: string): Promise<void> {
  await apiRequest(`/v1/sites/${encodeURIComponent(siteId)}`, { method: 'DELETE' });
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

export async function getSiteDetail(siteId: string): Promise<SiteItem & { versions: SiteVersionItem[] }> {
  const wrapped = await apiRequest<unknown>(`/v1/sites/${encodeURIComponent(siteId)}`);
  const data = unwrapData<JsonObject>(wrapped);
  return {
    ...toSiteItem(data),
    versions: Array.isArray(data.versions) ? (data.versions as SiteVersionItem[]) : [],
  };
}

export async function rollbackSite(siteId: string, version: number): Promise<SiteItem> {
  const wrapped = await apiRequest<unknown>(`/v1/sites/${encodeURIComponent(siteId)}/rollback`, {
    method: 'POST',
    body: JSON.stringify({ version }),
  });
  return toSiteItem(unwrapData<JsonObject>(wrapped));
}

export async function listSiteSubmissions(
  siteId: string, page = 1, pageSize = 50,
): Promise<{ items: SiteSubmissionItem[]; total: number }> {
  const wrapped = await apiRequest<unknown>(
    `/v1/sites/${encodeURIComponent(siteId)}/submissions?page=${page}&page_size=${pageSize}`,
  );
  const data = unwrapData<JsonObject>(wrapped);
  const pagination = (data.pagination ?? {}) as JsonObject;
  return {
    items: Array.isArray(data.items) ? (data.items as SiteSubmissionItem[]) : [],
    total: Number(pagination.total_items ?? 0),
  };
}

export async function exportSiteSubmissions(
  siteId: string,
): Promise<{ artifact_id: string; filename: string; rows: number; download_url: string }> {
  const wrapped = await apiRequest<unknown>(
    `/v1/sites/${encodeURIComponent(siteId)}/submissions/export`,
    { method: 'POST' },
  );
  return unwrapData<{ artifact_id: string; filename: string; rows: number; download_url: string }>(wrapped);
}

export async function clearSiteSubmissions(siteId: string): Promise<number> {
  const wrapped = await apiRequest<unknown>(
    `/v1/sites/${encodeURIComponent(siteId)}/submissions`, { method: 'DELETE' },
  );
  return Number(unwrapData<JsonObject>(wrapped).cleared ?? 0);
}

export async function listSiteKv(siteId: string): Promise<{ items: SiteKvItem[]; total: number }> {
  const wrapped = await apiRequest<unknown>(`/v1/sites/${encodeURIComponent(siteId)}/kv`);
  const data = unwrapData<JsonObject>(wrapped);
  return {
    items: Array.isArray(data.items) ? (data.items as SiteKvItem[]) : [],
    total: Number(data.total ?? 0),
  };
}

export async function deleteSiteKvKey(siteId: string, key: string): Promise<void> {
  await apiRequest(
    `/v1/sites/${encodeURIComponent(siteId)}/kv/${encodeURIComponent(key)}`,
    { method: 'DELETE' },
  );
}

export async function clearSiteKv(siteId: string): Promise<number> {
  const wrapped = await apiRequest<unknown>(
    `/v1/sites/${encodeURIComponent(siteId)}/kv`, { method: 'DELETE' },
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
  group_key: string;
  total_requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

async function fetchLogPage<T>(path: string, q: MyLogQuery): Promise<MyLogPage<T>> {
  const wrapped = await apiRequest<unknown>(`${path}?${logQueryString(q)}`);
  const data = unwrapData<PaginatedData<T>>(wrapped);
  return { items: data.items ?? [], pagination: data.pagination };
}

export function getMyToolLogs(q: MyLogQuery = {}): Promise<MyLogPage<MyToolLogItem>> {
  return fetchLogPage<MyToolLogItem>('/v1/me/logs/tools', q);
}

export async function getMyToolLog(logId: string): Promise<MyToolLogItem> {
  const wrapped = await apiRequest<unknown>(`/v1/me/logs/tools/${encodeURIComponent(logId)}`);
  return unwrapData<MyToolLogItem>(wrapped);
}

export function getMySkillLogs(q: MyLogQuery = {}): Promise<MyLogPage<MySkillLogItem>> {
  return fetchLogPage<MySkillLogItem>('/v1/me/logs/skills', q);
}

export async function getMySkillLog(logId: string): Promise<MySkillLogItem> {
  const wrapped = await apiRequest<unknown>(`/v1/me/logs/skills/${encodeURIComponent(logId)}`);
  return unwrapData<MySkillLogItem>(wrapped);
}

export function getMySubagentLogs(q: MyLogQuery = {}): Promise<MyLogPage<MySubagentLogItem>> {
  return fetchLogPage<MySubagentLogItem>('/v1/me/logs/subagents', q);
}

export async function getMySubagentLog(logId: string): Promise<MySubagentLogDetail> {
  const wrapped = await apiRequest<unknown>(
    `/v1/me/logs/subagents/${encodeURIComponent(logId)}`,
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
  return Array.isArray(data) ? data : [];
}
