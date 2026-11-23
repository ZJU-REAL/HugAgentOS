import { apiRequest, isHybridDual, type PaginatedData, type Pagination, unwrapData } from './core';

// ── My logs (/v1/me/logs) ──────────────────────────────────────────────────

interface MyLogQuery {
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
