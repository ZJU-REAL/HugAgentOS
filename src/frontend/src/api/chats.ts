import type { ChatItem, ReferencableChat } from '../types';
import type { EditionChatDetailFields } from '../editionModelTypes';
import { apiRequest, authFetch, chatTargetHeaders, type CreateSessionRequest, getApiUrl, isHybridDual, isLocalChat, isLocalProject, type JsonObject, type PaginatedData, registerLocalChat, type SessionListResponse, toChatItem, toTimestamp, unwrapData, type UpdateSessionRequest } from './core';

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
async function listLocalSessions(page: number = 1, pageSize: number = 50): Promise<ChatItem[]> {
  if (!isHybridDual()) return [];
  try {
    const wrapped = await apiRequest<unknown>(
      `/v1/chats?page=${page}&page_size=${pageSize}`,
      undefined,
      'local',
    );
    const data = unwrapData<PaginatedData<JsonObject>>(wrapped);
    const items = Array.isArray(data.items) ? data.items.map((item) => toChatItem(item)) : [];
    items.forEach((it) => { it.runTarget = 'local'; registerLocalChat(it.id); });
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

/** 可引用的历史会话（输入框 `/` 面板与拖拽落点共用）。
 *  在项目里只返回同项目的会话；只给标题级信息，正文由智能体按需调 read_chat 读取。 */
export async function listReferencableChats(params: {
  q?: string;
  projectId?: string;
  excludeChatId?: string;
  limit?: number;
} = {}): Promise<ReferencableChat[]> {
  const qs = new URLSearchParams();
  if (params.q) qs.set('q', params.q);
  if (params.projectId) qs.set('project_id', params.projectId);
  if (params.excludeChatId) qs.set('exclude_chat_id', params.excludeChatId);
  qs.set('limit', String(params.limit ?? 20));
  const wrapped = await apiRequest<unknown>(`/v1/chats/referencable?${qs.toString()}`);
  const data = unwrapData<{ items: ReferencableChat[] }>(wrapped);
  return Array.isArray(data.items) ? data.items : [];
}

export async function getSession(chatId: string): Promise<ChatItem> {
  const local = isLocalChat(chatId);
  const wrapped = await apiRequest<unknown>(
    `/v1/chats/${chatId}`,
    undefined,
    local ? 'local' : undefined,
  );
  const session = toChatItem(unwrapData<JsonObject>(wrapped));
  // Task identity includes its execution location in the dual desktop workspace.
  if (isHybridDual() && local && session.automationTaskId) {
    session.automationTaskId = `local:${session.automationTaskId}`;
  }
  return session;
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

interface ToolCallResultPayload {
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

interface ChatContextState {
  compaction_progress?: unknown;
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

type ChatSteerDeliveryMode = 'steer' | 'followUp' | 'nextRun';

interface ChatSteerQueueItem {
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

/** 一条正在跑的会话，用于侧边栏「运行中」指示。 */
export interface ActiveChatRunSummary {
  chat_id: string;
  run_id: string;
  status: string;
  started_at: string | null;
  /** 'local' when the run belongs to this computer's execution side (dual desktop mode). */
  target?: 'local';
}

/** A chat whose run started within the requested window; it may already have ended. */
export type RecentRunChat = Pick<ActiveChatRunSummary, 'chat_id' | 'target'>;

/**
 * 当前用户所有还在跑的会话。
 *
 * 单会话的 ``getActiveChatRun`` 只回答"我正打开的这段在不在跑"，换设备登录时
 * 侧边栏对没点开过的会话一无所知。这个接口一次问完，供侧边栏点亮小圆点。
 * ``recentSeconds`` 另要这段时间内起过 run 的会话，让别处发起、两次对账之间
 * 就已跑完的会话也能被发现。
 */
export async function listActiveChatRuns(
  recentSeconds = 0,
): Promise<{ items: ActiveChatRunSummary[]; recent: RecentRunChat[] }> {
  // 双模式下侧边栏同时挂着本机执行面的会话，那边跑起来的 run 云端并不知道，两边都要问。
  const targets: Array<'local' | undefined> = isHybridDual() ? [undefined, 'local'] : [undefined];
  const responses = await Promise.allSettled(
    targets.map((target) =>
      apiRequest<unknown>(`/v1/chats/active-runs?recent_seconds=${recentSeconds}`, undefined, target),
    ),
  );
  // 调用方把返回值当成服务端权威快照，没列进来的会话会被灭灯——所以任一面查询失败就整体
  // 抛错，让调用方保留现有状态等下一轮，绝不能把查不到的一面当成「那边没有在跑的了」。
  const failed = responses.find((response) => response.status === 'rejected');
  if (failed?.status === 'rejected') throw failed.reason;
  const items: ActiveChatRunSummary[] = [];
  const recent: RecentRunChat[] = [];
  responses.forEach((response, index) => {
    if (response.status !== 'fulfilled') return;
    const target = targets[index];
    const data = unwrapData<{ items?: ActiveChatRunSummary[]; recent_chat_ids?: string[] }>(response.value);
    for (const item of data.items ?? []) if (item?.chat_id) items.push({ ...item, target });
    for (const chatId of data.recent_chat_ids ?? []) recent.push({ chat_id: chatId, target });
  });
  return { items, recent };
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
