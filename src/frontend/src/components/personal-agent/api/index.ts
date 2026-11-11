import { apiRequest, unwrapData } from '../../../api';
import { ApiResponseError } from '../../../utils/apiError';
import { t } from '../../../i18n';
import type { ArtifactRef } from '../../chat/DeliveredFiles';
/**
 * What the approval card shows before the owner decides. target: "cloud", "device:<id>" or null; place: the same in words;
 * chat_title: the chat a session action changes; lines: every field of the action as "label：value"; body: all of the text that would go out.
 */
export interface ApprovalDetail { target?: string | null; place?: string | null; chat_title?: string | null; lines?: string[]; body?: string }
/** proactive: a proactive run started the task; its actions are approved one by one, never for the whole task. */
export interface PersonalTask { id: string; chat_id?: string; goal: string; title?: string; progress?: string | null; status: string; run_id?: string | null; waiting_reason?: string; result?: string; uncertain_actions?: string[]; proactive?: boolean; pending_approval?: { id: string; action: Record<string, unknown>; at?: number | string | null; detail?: ApprovalDetail | null }; question?: string | null; outcome?: string | null; updated_at?: string }
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
export type ChannelType = 'dingtalk' | 'lark' | 'weixin' | 'wecom' | 'slack';
export interface PersonalChannel { channel_id: string; channel_type: ChannelType; display_name?: string; enabled: boolean; status?: string; personal_agent: boolean; access?: ChannelAccess }
/** One IM account: listed (added_at) or a recent private sender not listed (last_at). */
export interface ChannelAccount { key: string; sender_id: string; sender_name: string; added_at?: number; last_at?: number }
/** Who may instruct one bot: the allowed list the owner keeps, and recent private senders to pick from. */
export interface ChannelAccess { allowed: ChannelAccount[]; recent: ChannelAccount[] }
export interface ComputerPreview { image: string; taken_at: number }
export interface PersonalSchedule { id: string; title: string; instruction: string; cron?: string | null; timezone: string; next_run?: string | null; run_at?: string | null; enabled?: boolean; revision?: number }
export type PersonalSchedulePatch = Partial<Pick<PersonalSchedule, "title" | "instruction" | "timezone" | "enabled" | "revision">> & { cron?: string; run_at?: string };
export type MessageIntent = "auto" | "new" | "update" | "status" | "pause" | "resume" | "cancel";
/** A message the agent is still answering, or a finished task it is still reporting. */
export interface PersonalTurn { message_id: string; status: string; work_item_ids: string[]; heartbeat?: boolean }
/** Outcome of the last proactive run. "skipped" comes with a reason and made no model call. */
export type HeartbeatOutcome = 'quiet' | 'alert' | 'delegated' | 'duplicate' | 'failed' | 'stale' | 'skipped' | 'reported' | 'nothing' | 'task_failed';
/** A checklist topic that cannot be served now: no mailbox connected, or no direct chat to send to. */
export type HeartbeatUnavailable = 'email' | 'channel';
/**
 * Proactive service settings and state. It has no switch: it runs for every Personal Agent.
 * active_hours.start is the morning run and .end the evening run.
 * channel_id: empty sends to the bot of the latest direct chat, "none" keeps results on the web, else a bot id.
 */
export interface PersonalHeartbeat {
  active_hours: { start: string; end: string }; channel_id?: string | null;
  next_run?: string | null; last?: { at?: string | null; outcome: HeartbeatOutcome; reason?: string | null } | null;
  stats?: Record<string, number>; /** backoff: 0 every run, 1 no idle runs, 2 the morning run only (the owner left the last summaries unanswered). */
  proactive?: { backoff?: number; ignored?: number; reported_at?: number | null } | null;
  unavailable?: HeartbeatUnavailable[];
}
export interface PersonalDevice { id: string; name: string; platform: string; online: boolean; control_enabled: boolean; sessions_enabled: boolean }
/** This computer's own link, served by the desktop execution plane. */
export interface DeviceLink { device_id: string; name: string; control_enabled: boolean; sessions_enabled: boolean }
export interface DeviceLinkStatus {
  platform: string; default_name: string; capabilities: string[]; control_supported: boolean;
  permissions: { screen: boolean; input: boolean } | null; link: DeviceLink | null;
  /** The worse of what the heartbeat and the mailbox pass last saw. */
  /** signed_out: the desktop's cloud sign-in is missing; only state and link are set then. */
  state: 'unlinked' | 'connecting' | 'connected' | 'offline' | 'error' | 'unavailable' | 'signed_out';
  error?: string | null;
  last_contact?: number | null;
  /** The action this computer is running for the agent right now, if any. */
  activity?: { kind: 'command' | 'session'; id: string; since: number } | null;
}
export type DeviceConsent = Partial<Pick<DeviceLink, 'control_enabled' | 'sessions_enabled'>>;
export interface PersonalOutput { id: string; name: string; mime_type: string; size_bytes: number; task_id?: string; url: string; created_at: string }
export interface OutputPage { total: number; images: number; documents: number; other: number; has_more: boolean; items: PersonalOutput[] }
export interface PersonalDreamingStatus {
  hour: number; timezone: string; next_run: number; run_id?: string | null;
  status?: 'pending' | 'running' | 'retry' | 'failed' | 'done' | null;
  started_at?: number | null; completed_at?: number | null; attempts?: number | null;
  next_retry?: number | null; error?: string | null; outcomes?: string[] | null;
}
/** One topic page of the memory wiki, as /memory/pages describes it. */
export type MemoryPageType = 'person' | 'org' | 'project' | 'topic' | 'procedure';
export interface MemoryPage {
  path: string; rel: string; name: string; type: MemoryPageType | string; description: string; aliases: string[];
  status: 'active' | 'archived' | string; updated: string | null; size: number; sources: number;
  pending: boolean; moved_to: string | null; links: string[];
}
export interface MemoryPages {
  available: boolean; pages?: MemoryPage[]; index_head?: string; where_to_start?: string;
  index_path?: string; schema_path?: string; report_path?: string; types?: { key: string; label: string }[]; busy?: boolean;
}
/** Who wrote a memory change: the agent's turn, Dreaming's two passes, the owner, or the platform. */
export type MemoryWriter = 'online' | 'analyzer' | 'organizer' | 'user' | 'system';
export interface MemoryVersion {
  id: string; path: string; file: string; writer: MemoryWriter | string; source_id: string | null; run_id: string | null;
  created: boolean; deleted: boolean; bytes_changed: number; reverted_by: string | null; created_at: string | null;
  revertable: boolean; before?: string | null; after?: string | null;
}
export interface MemoryVersionPage { items: MemoryVersion[]; page: number; page_size: number; has_more: boolean; available?: boolean }
export interface MemoryVersionQuery { path?: string; source_id?: string; writer?: string; page?: number; page_size?: number }
export interface MemorySnapshot { id: string; at: number | null; note: string; files: number }
export interface MemoryLintFinding { level: 'block' | 'warn' | string; rule: string; path: string; detail: string }
export interface MemoryLint { available: boolean; findings?: MemoryLintFinding[]; blocking?: number; warnings?: number }
/** A message the agent failed to answer, or a reply that did not reach its IM conversation. */
export interface AttentionTurn { message_id: string; chat_id: string; preview: string; kind: string; at: string }
export interface AttentionDelivery { message_id: string; chat_id: string; preview: string; channel_type?: ChannelType; at: string }
export interface PersonalAttention { turns: AttentionTurn[]; deliveries: AttentionDelivery[] }
export interface PersonalSearchMessage { message_id: string; chat_id: string; role: 'user' | 'assistant'; seq: number; preview: string; at: string; channel_type?: ChannelType; task_ids: string[] }
export interface PersonalSearchResult { messages: PersonalSearchMessage[]; tasks: PersonalTask[] }
export interface PersonalMonitor { attention?: PersonalAttention; devices: PersonalDevice[]; outputs: OutputPage; turns: PersonalTurn[]; agent: PersonalAgent | null; tasks: PersonalTask[]; schedules?: PersonalSchedule[]; heartbeat?: PersonalHeartbeat | null; email_connected?: boolean; file_memory?: { available: boolean; pending: number; retrying: number } }

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
  'HEARTBEAT.md has no items to check': '主动服务清单为空。请先在清单里写下要留意的事。',
  'Wait a minute before the next proactive check': '请稍等一分钟再运行。',
  'The previous proactive check has not ended': '上一次主动服务还没结束，请稍后再试。',
  'The daily limit of proactive checks is reached': '今天的主动服务次数已达上限，请明天再试。',
  'Personal Agent is not enabled for this account': '此账号没有开通 Personal Agent，请联系管理员。',
  'The morning and evening times must differ': '早上和晚上的时间不能相同，请换一个时间。',
  'Give a morning and an evening time': '请同时选择早上和晚上的时间。',
  'The bot is not connected to Personal Agent': '这个机器人还没有接入 Personal Agent，请先在频道里接入。',
  'The agent is answering a message; try again in a moment': '正在回复消息，请稍后再试。',
  'Personal Agent is paused': '智能体已暂停。',
  'Only a message that failed can be processed again': '这条消息已经在处理或已处理完成。',
  'Message not found': '这条消息已不存在，请刷新。',
  'Delivery not found': '这条回复已经重新发送或不存在，请刷新。',
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
  allowChannelAccount: (id: string, account: { key?: string; sender_id?: string }) => personalRequest<PersonalChannel[]>(base + '/channels/' + id + '/allowed', { method: 'POST', body: JSON.stringify(account) }),
  removeChannelAccount: (id: string, key: string) => personalRequest<PersonalChannel[]>(base + '/channels/' + id + '/allowed?key=' + encodeURIComponent(key), { method: 'DELETE' }),
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
  reprocess: (messageId: string) => personalRequest<PersonalTurn>(base + '/messages/' + encodeURIComponent(messageId) + '/reprocess', { method: 'POST' }),
  retryDelivery: (messageId: string) => personalRequest<{ message_id: string; status: string }>(base + '/deliveries/' + encodeURIComponent(messageId) + '/retry', { method: 'POST' }),
  search: (query: string, signal?: AbortSignal) => personalRequest<PersonalSearchResult>(base + '/search?q=' + encodeURIComponent(query), { signal }),
  cancel: (id: string) => personalRequest<PersonalTask>(base + '/work-items/' + id + '/cancel', { method: 'POST' }),
  dreaming: (signal?: AbortSignal) => personalRequest<PersonalDreamingStatus | null>(base + '/dreaming', { signal }),
  runDreaming: () => personalRequest<PersonalDreamingStatus>(base + '/dreaming/run', { method: 'POST' }),
  runHeartbeat: () => personalRequest<{ started: boolean; message_id: string }>(base + '/heartbeat/run', { method: 'POST' }),
  settings: (body: object) => personalRequest<PersonalAgent>(base + '/settings', { method: 'PATCH', body: JSON.stringify(body) }),
  memoryPages: (signal?: AbortSignal) => personalRequest<MemoryPages>(base + '/memory/pages', { signal }),
  memoryVersions: (query: MemoryVersionQuery = {}, signal?: AbortSignal) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== '') params.set(key, String(value));
    const text = params.toString();
    return personalRequest<MemoryVersionPage>(base + '/memory/versions' + (text ? '?' + text : ''), { signal });
  },
  memoryVersion: (id: string, signal?: AbortSignal) => personalRequest<MemoryVersion>(base + '/memory/versions/' + encodeURIComponent(id), { signal }),
  revertMemoryVersion: (id: string) => personalRequest<{ reverted: string; file: string }>(base + '/memory/versions/' + encodeURIComponent(id) + '/revert', { method: 'POST' }),
  memorySnapshots: (signal?: AbortSignal) => personalRequest<{ available: boolean; items?: MemorySnapshot[] }>(base + '/memory/snapshots', { signal }),
  restoreMemorySnapshot: (id: string) => personalRequest<{ restored: string; changed: string[] }>(base + '/memory/snapshots/' + encodeURIComponent(id) + '/restore', { method: 'POST' }),
  memoryLint: (signal?: AbortSignal) => personalRequest<MemoryLint>(base + '/memory/lint', { signal }),
  workspaceTree: () => personalRequest<WorkspaceTree>(base + '/workspace/tree'),
  workspaceFile: (path: string, signal?: AbortSignal) => personalRequest<WorkspaceFile>(base + '/workspace/file?path=' + encodeURIComponent(path), { signal }),
  saveWorkspaceFile: (path: string, content: string, etag?: string) => personalRequest<WorkspaceFile>(base + '/workspace/file', { method: 'PUT', body: JSON.stringify({ path, content, etag }) }),
};
