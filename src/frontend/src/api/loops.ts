import type { LoopGoalSpec, LoopBudget, LoopItem, LoopIterationItem } from '../types';
import { apiRequest, authFetch, chatTargetHeaders, getApiUrl, isHybridDual, isLocalChat, isLocalProject, projectTargetHeaders, registerLocalChat, registerLocalLoop, unwrapData } from './core';

// ── Autonomous Loop (long-running autonomous operation) ──────────

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
    headers: { ...projectTargetHeaders(data.project_id), ...chatTargetHeaders(data.chat_id) },
    body: JSON.stringify(data),
  });
  const loop = unwrapData<LoopItem>(wrapped);
  if (isHybridDual() && (isLocalChat(data.chat_id) || isLocalProject(data.project_id))) {
    registerLocalLoop(loop.loop_id);
  }
  return loop;
}

export async function listLoops(): Promise<LoopItem[]> {
  const wrapped = await apiRequest<unknown>('/v1/loops');
  const cloud = unwrapData<LoopItem[]>(wrapped) || [];
  if (!isHybridDual()) return cloud;
  try {
    const localWrapped = await apiRequest<unknown>('/v1/loops', undefined, 'local');
    const local = unwrapData<LoopItem[]>(localWrapped) || [];
    for (const loop of local) {
      registerLocalLoop(loop.loop_id);
      if (loop.chat_id) registerLocalChat(loop.chat_id);
    }
    return [...cloud, ...local];
  } catch {
    return cloud; // Local service readiness must not hide available cloud tasks.
  }
}

export async function getLoop(loopId: string, chatId?: string): Promise<LoopItem> {
  const wrapped = await apiRequest<unknown>(`/v1/loops/${encodeURIComponent(loopId)}`, { headers: chatTargetHeaders(chatId) });
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
  chatId?: string,
): Promise<Response> {
  return authFetch(`${getApiUrl()}/v1/loops/${encodeURIComponent(loopId)}/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...chatTargetHeaders(chatId) },
    body: JSON.stringify(body),
    signal,
  });
}

export async function resumeLoop(
  loopId: string,
  body: { model_name?: string; model_provider_id?: string; evaluator_model?: string; worker_max_iters?: number; hitl_enabled?: boolean; enable_thinking?: boolean; chat_mode?: string } = {},
  signal?: AbortSignal,
  chatId?: string,
): Promise<Response> {
  return authFetch(`${getApiUrl()}/v1/loops/${encodeURIComponent(loopId)}/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...chatTargetHeaders(chatId) },
    body: JSON.stringify(body),
    signal,
  });
}

/** 运行中追加一条用户指令：driver 下一轮 worker 开工前取走并以最高优先级注入 prompt。 */
export async function steerLoop(loopId: string, message: string, chatId?: string): Promise<boolean> {
  const wrapped = await apiRequest<unknown>(`/v1/loops/${encodeURIComponent(loopId)}/steer`, {
    method: 'POST',
    headers: chatTargetHeaders(chatId),
    body: JSON.stringify({ message }),
  });
  return (unwrapData<{ queued: boolean }>(wrapped) || { queued: false }).queued;
}

export async function cancelLoop(loopId: string, chatId?: string): Promise<boolean> {
  const wrapped = await apiRequest<unknown>(`/v1/loops/${encodeURIComponent(loopId)}/cancel`, {
    method: 'POST',
    headers: chatTargetHeaders(chatId),
  });
  return (unwrapData<{ cancelled: boolean }>(wrapped) || { cancelled: false }).cancelled;
}
