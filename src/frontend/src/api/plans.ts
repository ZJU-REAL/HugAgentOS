import type { JobBrief, CommandTaskBrief, Plan } from '../types';
import { apiRequest, authFetch, chatTargetHeaders, getApiUrl, projectTargetHeaders, unwrapData } from './core';

// ── Plan Mode API ─────────────────────────────────────────────────────────

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
    headers: { 'Content-Type': 'application/json', ...projectTargetHeaders(projectId), ...chatTargetHeaders(chatId) },
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

/** Sandbox command tasks of a chat: live ones first, then the latest finished. */
export async function listChatCommands(chatId: string): Promise<CommandTaskBrief[]> {
  const res = await apiRequest<unknown>(
    `/v1/commands?chat_id=${encodeURIComponent(chatId)}`,
    { headers: chatTargetHeaders(chatId) },
  );
  return unwrapData<CommandTaskBrief[]>(res) ?? [];
}

export async function cancelCommandApi(taskId: string, chatId?: string): Promise<void> {
  await apiRequest<unknown>(`/v1/commands/${encodeURIComponent(taskId)}/cancel`, {
    method: 'POST', headers: chatTargetHeaders(chatId),
  });
}

export async function listPlans(target?: 'local'): Promise<Plan[]> {
  const res = await apiRequest<unknown>('/v1/plans', undefined, target);
  return unwrapData<Plan[]>(res);
}

export async function getPlanApi(planId: string, chatId?: string, target?: 'local'): Promise<Plan> {
  const res = await apiRequest<unknown>(`/v1/plans/${planId}`, { headers: chatTargetHeaders(chatId) }, target);
  return unwrapData<Plan>(res);
}

export async function updatePlanApi(
  planId: string,
  updates: { status?: string; title?: string; steps?: Record<string, unknown>[] },
  chatId?: string,
): Promise<Plan> {
  const res = await apiRequest<unknown>(`/v1/plans/${planId}`, {
    method: 'PATCH',
    headers: chatTargetHeaders(chatId),
    body: JSON.stringify(updates),
  });
  return unwrapData<Plan>(res);
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
    headers: { 'Content-Type': 'application/json', ...projectTargetHeaders(projectId), ...chatTargetHeaders(chatId) },
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

export async function cancelPlanApi(planId: string, chatId?: string): Promise<void> {
  await apiRequest<unknown>(`/v1/plans/${planId}/cancel`, { method: 'POST', headers: chatTargetHeaders(chatId) });
}
