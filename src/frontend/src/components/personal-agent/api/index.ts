import { apiRequest, unwrapData } from '../../../api';
import { ApiResponseError } from '../../../utils/apiError';
import { t } from '../../../i18n';
import type { ArtifactRef } from '../../chat/DeliveredFiles';
export interface PersonalTask { id: string; chat_id?: string; goal: string; title?: string; progress?: string | null; status: string; run_id?: string | null; waiting_reason?: string; result?: string; uncertain_actions?: string[]; pending_approval?: { id: string; action: Record<string, unknown> }; updated_at?: string }
export interface PersonalMessage { id: string; seq: number; role: 'user' | 'assistant'; content: string; origin?: string; channel_type?: ChannelType; work_item_id?: string; work_item_ids: string[]; task_links: { work_item_id: string; relation: string }[]; attachments?: { id: string; name: string; url: string }[]; artifacts?: ArtifactRef[]; reply_to?: string | null; streaming?: boolean; activity?: string | null; heartbeat?: boolean }
export interface PersonalAgent { id: string; chat_id: string; status: string; computer_status: string; generation: number; config: { capability_selection?: { skill_ids: string[]; mcp_ids: string[]; plugin_ids: string[] }; concurrency: number; email_enabled: boolean; skill_ids: string[]; mcp_ids: string[]; plugins?: { id: string; name: string; skill_ids: string[]; mcp_ids: string[] }[]; model_provider_id?: string | null; thinking_effort?: 'fast' | 'low' | 'medium' | 'high' | 'xhigh' | 'max'; timezone?: string; name?: string; avatar?: string; channel_ids?: string[]; approval_mode?: ApprovalMode } }
export interface PersonalCapabilityOptions {
  plugin_component_ids?: { skill_ids: string[]; mcp_ids: string[] };
  skill_ids: string[];
  mcp_ids: string[];
  plugins: { id: string; name: string; skill_ids: string[]; mcp_ids: string[] }[];
}
/** ask: every action waits for approval; auto: only email and deletions ask; full: nothing asks. */
export type ApprovalMode = 'ask' | 'auto' | 'full';
export interface WorkspaceEntry { path: string; name: string; type: 'dir' | 'file' | 'link' | 'other'; size: number | null; modified: number; role?: string | null; editable: boolean }
export interface WorkspaceTree { available: boolean; root?: string; exists?: boolean; entries?: WorkspaceEntry[]; truncated?: boolean }
export interface WorkspaceFile { available: boolean; path: string; size: number; modified: number; binary: boolean; truncated: boolean; content: string | null; etag: string; editable: boolean; role?: string | null }
export type ChannelType = 'dingtalk' | 'lark' | 'weixin' | 'wecom';
export interface PersonalChannel { channel_id: string; channel_type: ChannelType; display_name?: string; enabled: boolean; status?: string; personal_agent: boolean }
export interface ComputerPreview { image: string; taken_at: number }
export interface PersonalSchedule { id: string; title: string; instruction: string; cron?: string | null; timezone: string; next_run?: string | null; run_at?: string | null; enabled?: boolean; revision?: number }
export type PersonalSchedulePatch = Partial<Pick<PersonalSchedule, "title" | "instruction" | "timezone" | "enabled" | "revision">> & { cron?: string; run_at?: string };
export type MessageIntent = "auto" | "new" | "update" | "status" | "pause" | "resume" | "cancel";
/** A message the agent is still answering, or a finished task it is still reporting. */
export interface PersonalTurn { message_id: string; status: string; work_item_ids: string[]; heartbeat?: boolean }
/** Outcome of the last heartbeat check. "skipped" comes with a reason and made no model call. */
export type HeartbeatOutcome = 'quiet' | 'alert' | 'delegated' | 'duplicate' | 'failed' | 'stale' | 'skipped';
export interface PersonalHeartbeat {
  enabled: boolean; every_minutes: number; active_hours: { start: string; end: string }; channel_id?: string | null;
  next_run?: string | null; last?: { at?: string | null; outcome: HeartbeatOutcome; reason?: string | null } | null;
  stats: Partial<Record<HeartbeatOutcome, number>>; min_minutes: number; daily_max: number;
}
export interface PersonalDevice { id: string; name: string; platform: string; online: boolean; control_enabled: boolean; sessions_enabled: boolean }
/** This computer's own link, served by the desktop execution plane. */
export interface DeviceLink { device_id: string; name: string; control_enabled: boolean; sessions_enabled: boolean }
export interface DeviceLinkStatus {
  platform: string; default_name: string; capabilities: string[]; control_supported: boolean;
  permissions: { screen: boolean; input: boolean } | null; link: DeviceLink | null;
  state: 'unlinked' | 'connected' | 'offline' | 'unavailable';
}
export type DeviceConsent = Partial<Pick<DeviceLink, 'control_enabled' | 'sessions_enabled'>>;
export interface PersonalOutput { id: string; name: string; mime_type: string; size_bytes: number; task_id?: string; url: string; created_at: string }
export interface OutputPage { total: number; images: number; documents: number; other: number; has_more: boolean; items: PersonalOutput[] }
export interface PersonalDreamingStatus {
  enabled: boolean; hour: number; timezone: string; next_run: number; run_id?: string | null;
  status?: 'pending' | 'running' | 'retry' | 'failed' | 'done' | null;
  started_at?: number | null; completed_at?: number | null; attempts?: number | null;
  next_retry?: number | null; error?: string | null; outcomes?: string[] | null;
}
export interface PersonalMonitor { devices: PersonalDevice[]; outputs: OutputPage; turns: PersonalTurn[]; agent: PersonalAgent | null; tasks: PersonalTask[]; schedules?: PersonalSchedule[]; heartbeat?: PersonalHeartbeat | null; email_connected?: boolean; file_memory?: { available: boolean; pending: number; retrying: number } }

/** A readable message for the user. Temporary server outages use one short sentence. */
const SERVER_MESSAGES: Record<string, string> = {
  'Schedule changed; reopen settings and try again': '定时任务已被修改，请重新打开设置后再试。',
  'Scheduled task not found': '此定时任务已不存在，请刷新列表。',
  'Unknown timezone': '时区无效，请填写有效时区，例如 Asia/Shanghai。',
  'run_at must be in the future': '请选择未来的执行时间。',
  'Use a five-field cron expression': '请使用五段重复规则：分 时 日 月 周。',
  'Recurring tasks must be at least ten minutes apart': '重复任务的间隔不能少于 10 分钟。',
  'Cloud computer is currently in use': '另一个窗口或任务正在使用这台电脑，请稍后再试。',
  'Cloud computer is not ready': '云电脑还在准备中。',
  'Control session expired': '接管已超时，请重新接管。',
  'HEARTBEAT.md has no items to check': '心跳清单为空。请先在清单里写下要检查的事项。',
  'Wait a minute before the next heartbeat': '请稍等一分钟再运行。',
  'The agent is answering a message; try again in a moment': '正在回复消息，请稍后再试。',
  'Personal Agent is paused': '智能体已暂停。',
};

export function errorText(exc: unknown): string {
  if (exc instanceof Error && SERVER_MESSAGES[exc.message]) return t(SERVER_MESSAGES[exc.message]);
  if (exc instanceof ApiResponseError && exc.status >= 502 && exc.status <= 504) return t('服务暂时不可用，请稍后重试。');
  if (exc instanceof TypeError) return t('网络连接失败，请稍后重试。');
  if (exc instanceof Error) return exc.message.replace(/^API Error: \d+$/, t('操作失败，请稍后重试。'));
  return String(exc);
}
export async function personalRequest<T>(path: string, options?: RequestInit): Promise<T> {
  return unwrapData<T>(await apiRequest<unknown>(path, options, "cloud"));
}
async function localRequest<T>(path: string, options?: RequestInit): Promise<T> {
  return unwrapData<T>(await apiRequest<unknown>(path, options, 'local'));
}
const linkBase = '/v1/device-link';
export const deviceLinkApi = {
  status: () => localRequest<DeviceLinkStatus>(linkBase),
  link: (body: DeviceLink & { token: string }) => localRequest<DeviceLinkStatus>(linkBase, { method: 'PUT', body: JSON.stringify(body) }),
  consent: (body: DeviceConsent) => localRequest<DeviceLinkStatus>(linkBase, { method: 'PATCH', body: JSON.stringify(body) }),
  unlink: () => localRequest<DeviceLinkStatus>(linkBase, { method: 'DELETE' }),
  requestPermissions: () => localRequest<DeviceLinkStatus>(linkBase + '/permissions', { method: 'POST' }),
};
export interface PersonalConversation { chat_id: string; title: string; channel_id: string; channel_type: ChannelType; chat_type: 'p2p' | 'group'; bot_name: string; preview: string; last_message_at: string | null }
const base = '/v1/personal-agent';
const sessionQuery = (chatId?: string) => chatId ? 'chat_id=' + encodeURIComponent(chatId) : '';
const withSession = (path: string, chatId?: string) => path + (chatId ? (path.includes('?') ? '&' : '?') + sessionQuery(chatId) : '');
export const personalApi = {
  capabilities: (signal?: AbortSignal) => personalRequest<PersonalCapabilityOptions>(base + '/capabilities', { signal }),
  initialize: () => personalRequest<PersonalAgent>(base + '/initialize', { method: 'POST', body: JSON.stringify({ timezone: Intl.DateTimeFormat().resolvedOptions().timeZone }) }),
  monitor: (signal?: AbortSignal, chatId?: string) => personalRequest<PersonalMonitor>(withSession(base + '/monitor', chatId), { signal }),
  conversations: (signal?: AbortSignal) => personalRequest<PersonalConversation[]>(base + '/conversations', { signal }),
  messages: (after: number, signal?: AbortSignal, chatId?: string) => personalRequest<PersonalMessage[]>(withSession(base + '/messages?after_seq=' + after, chatId), { signal }),
  history: (before: number, chatId?: string) => personalRequest<PersonalMessage[]>(withSession(base + '/messages?before_seq=' + before, chatId)),
  send: (content: string, request_key: string, work_item_ids: string[] = [], artifact_ids: string[] = [], intent: MessageIntent = 'auto', reply_to_message_id?: string, chatId?: string) => personalRequest<PersonalTurn>(withSession(base + '/messages', chatId), { method: 'POST', body: JSON.stringify({ content, request_key, work_item_ids, artifact_ids, intent, reply_to_message_id }) }),
  approve: (id: string, approval_id: string, accept: boolean, scope: 'once' | 'task' = 'once') => personalRequest<PersonalTask>(base + '/work-items/' + id + '/approve', { method: 'POST', body: JSON.stringify({ approval_id, accept, scope }) }),
  channels: () => personalRequest<PersonalChannel[]>(base + '/channels'),
  routeChannel: (id: string, enabled: boolean) => personalRequest<PersonalChannel[]>(base + '/channels/' + id, { method: 'PUT', body: JSON.stringify({ enabled }) }),
  preview: (signal?: AbortSignal) => personalRequest<ComputerPreview>(base + '/computer/preview', { signal }),
  control: () => personalRequest<{ token: string }>(base + '/computer/control', { method: 'POST' }),
  // keepalive lets the release finish while the page is closing, so control is not held for its full lease.
  releaseControl: (token: string) => personalRequest<{ released: boolean }>(base + '/computer/control', { method: 'DELETE', body: JSON.stringify({ token }), keepalive: true }),
  streamTicket: (token?: string) => personalRequest<{ ticket: string }>(base + '/computer/stream', { method: 'POST', body: JSON.stringify(token ? { token } : {}) }),
  streamPath: (ticket: string) => base + '/computer/stream/ws?ticket=' + encodeURIComponent(ticket),
  outputs: (before?: string, signal?: AbortSignal) => personalRequest<OutputPage>(base + '/outputs' + (before ? '?before=' + encodeURIComponent(before) : ''), { signal }),
  conversation: <T,>(id: string, beforeSeq?: number, signal?: AbortSignal) => personalRequest<T>(base + '/work-items/' + id + '/conversation' + (beforeSeq ? '?before_seq=' + beforeSeq : ''), { signal }),
  pairDevice: (name: string, platform: string, capabilities: string[]) => personalRequest<{ id: string; token: string }>(base + '/devices', { method: 'POST', body: JSON.stringify({ name, platform, capabilities }) }),
  revokeDevice: (id: string) => personalRequest<unknown>(base + '/devices/' + id, { method: 'DELETE' }),
  sessionOperation: <T,>(body?: object, id?: string) => personalRequest<T>(base + '/session-manager/operations' + (id ? '/' + id : ''), body ? { method: 'POST', body: JSON.stringify(body) } : undefined),
  updateSchedule: (id: string, body: PersonalSchedulePatch) => personalRequest<PersonalSchedule>(base + '/schedules/' + encodeURIComponent(id), { method: 'PATCH', body: JSON.stringify(body) }),
  deleteSchedule: (id: string) => personalRequest<{ deleted: boolean }>(base + '/schedules/' + id, { method: 'DELETE' }),
  reconcile: (id: string, action_id: string, outcome: string) => personalRequest<PersonalTask>(base + "/work-items/" + id + "/reconcile", { method: "POST", body: JSON.stringify({ action_id, outcome }) }),
  retry: (id: string) => personalRequest<PersonalTask>(base + "/work-items/" + id + "/retry", { method: "POST" }),
  cancel: (id: string) => personalRequest<PersonalTask>(base + '/work-items/' + id + '/cancel', { method: 'POST' }),
  dreaming: (signal?: AbortSignal) => personalRequest<PersonalDreamingStatus | null>(base + '/dreaming', { signal }),
  runDreaming: () => personalRequest<PersonalDreamingStatus>(base + '/dreaming/run', { method: 'POST' }),
  runHeartbeat: () => personalRequest<{ started: boolean; message_id: string }>(base + '/heartbeat/run', { method: 'POST' }),
  settings: (body: object) => personalRequest<PersonalAgent>(base + '/settings', { method: 'PATCH', body: JSON.stringify(body) }),
  workspaceTree: () => personalRequest<WorkspaceTree>(base + '/workspace/tree'),
  workspaceFile: (path: string) => personalRequest<WorkspaceFile>(base + '/workspace/file?path=' + encodeURIComponent(path)),
  saveWorkspaceFile: (path: string, content: string, etag?: string) => personalRequest<WorkspaceFile>(base + '/workspace/file', { method: 'PUT', body: JSON.stringify({ path, content, etag }) }),
};
