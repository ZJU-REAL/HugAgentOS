import type { AutomationTask, AutomationRun, AutomationNotification } from '../types';
import { apiRequest, isHybridDual, registerLocalChat, unwrapData } from './core';

// ── Automation API ──────────────────────────────────────────────

interface CreateAutomationRequest {
  execution_location?: 'local' | 'cloud';
  project_id?: string;
  task_type: 'prompt';
  prompt: string;
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

export async function listChannelConversations(target?: 'local'): Promise<ChannelConversation[]> {
  const wrapped = await apiRequest<unknown>('/v1/channels/conversations', undefined, target);
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

// Prefixes are frontend-only identities; they survive navigation/reload and
// prevent equal IDs from two independent databases from sharing UI state.
function automationTarget(id: string): 'local' | undefined {
  if (!id.startsWith('local:')) return undefined;
  if (isHybridDual()) return 'local';
  const boot = typeof window === 'undefined' ? undefined : window.__HG_DESKTOP__;
  if (boot?.provision_mode === 'local_only') return undefined;
  throw new Error('此任务属于本机，请在提供本机服务的桌面模式中打开');
}
function automationPath(id: string, suffix = ''): string {
  const raw = id.startsWith('local:') ? id.slice(6) : id;
  return `/v1/automations/${encodeURIComponent(raw)}${suffix}`;
}
function locatedTask(task: AutomationTask, local: boolean): AutomationTask {
  return { ...task, task_id: local && isHybridDual() ? `local:${task.task_id}` : task.task_id,
    execution_location: local ? 'local' : task.execution_location || 'cloud' };
}
let automationUnavailable: string[] = [];
export function automationAvailabilityWarning(): string {
  return automationUnavailable.join('；');
}
async function availableAutomationSides<T>(fetchSide: (local: boolean) => Promise<T[]>): Promise<T[]> {
  const results = await Promise.allSettled([fetchSide(false), fetchSide(true)]);
  automationUnavailable = results.flatMap((result, index) => result.status === 'rejected'
    ? [index === 0 ? '云端暂不可用，当前仅显示本机结果' : '本机服务暂不可用，当前仅显示云端结果'] : []);
  if (results.every(result => result.status === 'rejected')) throw new Error('本机和云端服务均暂不可用');
  return results.flatMap(result => result.status === 'fulfilled' ? result.value : []);
}
async function automationList(query = ''): Promise<AutomationTask[]> {
  const fetchSide = async (local: boolean) => {
    const res = await apiRequest<unknown>(`/v1/automations${query}`, undefined, local ? 'local' : undefined);
    return unwrapData<AutomationTask[]>(res).map(task => locatedTask(task, local));
  };
  if (!isHybridDual()) { automationUnavailable = []; return fetchSide(false); }
  const items = await availableAutomationSides(fetchSide);
  return items.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
}

export async function createAutomation(data: CreateAutomationRequest): Promise<AutomationTask> {
  const local = data.execution_location === 'local';
  const res = await apiRequest<unknown>('/v1/automations', {
    method: 'POST', body: JSON.stringify(data),
  }, isHybridDual() && local ? 'local' : undefined);
  return locatedTask(unwrapData<AutomationTask>(res), local);
}
export async function listAutomations(status?: string): Promise<AutomationTask[]> {
  return automationList(status ? `?status=${encodeURIComponent(status)}` : '');
}
export async function getAutomation(taskId: string): Promise<AutomationTask> {
  const target = automationTarget(taskId);
  const res = await apiRequest<unknown>(automationPath(taskId), undefined, target);
  return locatedTask(unwrapData<AutomationTask>(res), target === 'local');
}
export async function updateAutomation(taskId: string, data: UpdateAutomationRequest): Promise<AutomationTask> {
  const target = automationTarget(taskId);
  const res = await apiRequest<unknown>(automationPath(taskId), {
    method: 'PATCH', body: JSON.stringify(data),
  }, target);
  return locatedTask(unwrapData<AutomationTask>(res), target === 'local');
}
export async function deleteAutomation(taskId: string): Promise<void> {
  await apiRequest(automationPath(taskId), { method: 'DELETE' }, automationTarget(taskId));
}
export async function pauseAutomation(taskId: string): Promise<void> {
  await apiRequest(automationPath(taskId, '/pause'), { method: 'POST' }, automationTarget(taskId));
}
export async function resumeAutomation(taskId: string): Promise<void> {
  await apiRequest(automationPath(taskId, '/resume'), { method: 'POST' }, automationTarget(taskId));
}
export async function triggerAutomation(taskId: string): Promise<void> {
  await apiRequest(automationPath(taskId, '/trigger'), { method: 'POST' }, automationTarget(taskId));
}
export async function getAutomationRuns(taskId: string, limit?: number): Promise<AutomationRun[]> {
  const target = automationTarget(taskId);
  const res = await apiRequest<unknown>(automationPath(taskId, `/runs?limit=${limit || 10}`), undefined, target);
  return unwrapData<AutomationRun[]>(res).map(run => {
    if (target === 'local' && run.chat_id) registerLocalChat(run.chat_id);
    return { ...run, task_id: taskId };
  });
}
export async function getAutomationNotifications(): Promise<AutomationNotification[]> {
  const fetchSide = async (local: boolean) => {
    const res = await apiRequest<unknown>('/v1/automations/notifications/list', undefined, local ? 'local' : undefined);
    return unwrapData<AutomationNotification[]>(res).map(item => {
      if (local && item.chat_id) registerLocalChat(item.chat_id);
      return local ? { ...item, id: `local:${item.id}`, task_id: `local:${item.task_id}` } : item;
    });
  };
  if (!isHybridDual()) { automationUnavailable = []; return fetchSide(false); }
  return availableAutomationSides(fetchSide);
}
async function changeNotifications(ids: string[], action: 'read' | 'delete'): Promise<void> {
  const groups = isHybridDual() ? [false, true] : [false];
  await Promise.all(groups.map(async local => {
    const selected = ids.filter(id => (automationTarget(id) === 'local') === local);
    if (!selected.length) return;
    await apiRequest(`/v1/automations/notifications/${action}`, {
      method: 'POST',
      body: JSON.stringify({ ids: selected.map(id => local ? id.slice(6) : id) }),
    }, local ? 'local' : undefined);
  }));
}
export async function markNotificationsRead(ids: string[]): Promise<void> {
  return changeNotifications(ids, 'read');
}
export async function deleteNotifications(ids: string[]): Promise<void> {
  return changeNotifications(ids, 'delete');
}
