import { apiRequest, isHybridDual, type JsonObject, type RequestTarget, unwrapData } from './core';

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

export async function getDingTalkStatus(probe = false, target?: RequestTarget): Promise<DingTalkStatus> {
  const wrapped = await apiRequest<unknown>(`/v1/integrations/dingtalk/status${probe ? '?probe=true' : ''}`, undefined, target);
  return _coerceDingTalkStatus(unwrapData<JsonObject>(wrapped));
}

export async function startDingTalkLogin(target?: RequestTarget): Promise<DingTalkStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/dingtalk/login', { method: 'POST' }, target);
  return _coerceDingTalkStatus(unwrapData<JsonObject>(wrapped));
}

export async function pollDingTalkLogin(target?: RequestTarget): Promise<DingTalkStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/dingtalk/login/poll', { method: 'POST' }, target);
  return _coerceDingTalkStatus(unwrapData<JsonObject>(wrapped));
}

export async function disconnectDingTalk(target?: RequestTarget): Promise<DingTalkStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/dingtalk/disconnect', { method: 'POST' }, target);
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

export async function getLarkStatus(probe = false, target?: RequestTarget): Promise<LarkStatus> {
  const wrapped = await apiRequest<unknown>(`/v1/integrations/lark/status${probe ? '?probe=true' : ''}`, undefined, target);
  return _coerceLarkStatus(unwrapData<JsonObject>(wrapped));
}

export async function startLarkLogin(target?: RequestTarget): Promise<LarkStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/lark/login', { method: 'POST' }, target);
  return _coerceLarkStatus(unwrapData<JsonObject>(wrapped));
}

export async function pollLarkLogin(target?: RequestTarget): Promise<LarkStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/lark/login/poll', { method: 'POST' }, target);
  return _coerceLarkStatus(unwrapData<JsonObject>(wrapped));
}

export async function disconnectLark(target?: RequestTarget): Promise<LarkStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/lark/disconnect', { method: 'POST' }, target);
  return _coerceLarkStatus(unwrapData<JsonObject>(wrapped));
}

// ── Inbound channel bots (owner service-account model): user-created external IM bots that run under the owner's identity ──
// Orthogonal to the "Feishu account connection" above: that is outbound (the agent operates Feishu as me), this is inbound (Feishu pushes messages to my agent).
export interface ChannelBot {
  execution_location?: 'local' | 'cloud';
  device_id?: string | null;
  device_name?: string | null;
  device_online?: boolean | null;
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
  execution_location?: 'local' | 'cloud';
  local_binding_id?: string;
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

export async function prepareChannelLocalBinding(): Promise<{ binding_id: string; device_name: string }> {
  if (!isHybridDual()) throw new Error('本机机器人需要混合模式桌面端');
  const result = await apiRequest<unknown>('/v1/channels/desktop/local-binding', { method: 'POST' }, 'local');
  return unwrapData<{ binding_id: string; device_name: string }>(result);
}

export async function startWeixinBind(
  agentId?: string, localBindingId?: string,
): Promise<WeixinBindStart> {
  const params = new URLSearchParams();
  if (agentId) params.set('agent_id', agentId);
  if (localBindingId) {
    params.set('execution_location', 'local');
    params.set('local_binding_id', localBindingId);
  }
  const suffix = params.size ? `?${params}` : '';
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

// `target` pins the account to one plane — Personal Agent always reads the cloud account.
export async function getEmailStatus(probe = false, target?: RequestTarget): Promise<EmailStatus> {
  const wrapped = await apiRequest<unknown>(`/v1/integrations/email/status${probe ? '?probe=true' : ''}`, undefined, target);
  return _coerceEmailStatus(unwrapData<JsonObject>(wrapped));
}

export async function connectEmail(body: {
  email_address: string;
  secret: string;
  display_name?: string;
  server_overrides?: EmailServerOverrides;
}, target?: RequestTarget): Promise<EmailStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/email/connect', {
    method: 'POST',
    body: JSON.stringify(body),
  }, target);
  return _coerceEmailStatus(unwrapData<JsonObject>(wrapped));
}

export async function disconnectEmail(target?: RequestTarget): Promise<EmailStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/email/disconnect', { method: 'POST' }, target);
  return _coerceEmailStatus(unwrapData<JsonObject>(wrapped));
}

// ── Third-party integration: Yida account connection (yida plugin / openyida CLI), QR login executed in the user's sandbox ──
// Unlike the DingTalk device flow: poll is a long poll (the backend runs agent-poll
// inside the sandbox waiting for the scan; a single call can block ~45s), so the
// frontend must use a sequential loop ("start the next only after the previous
// returns") rather than setInterval, to avoid pile-up.
// For multi-organization accounts, poll returns corp_selection + organizations;
// after the user picks one, re-poll with corp_id.
interface YidaOrganization {
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

export async function getYidaStatus(probe = false, target?: RequestTarget): Promise<YidaStatus> {
  const wrapped = await apiRequest<unknown>(`/v1/integrations/yida/status${probe ? '?probe=true' : ''}`, undefined, target);
  return _coerceYidaStatus(unwrapData<JsonObject>(wrapped));
}

export async function startYidaLogin(target?: RequestTarget): Promise<YidaStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/yida/login', { method: 'POST' }, target);
  return _coerceYidaStatus(unwrapData<JsonObject>(wrapped));
}

export async function pollYidaLogin(corpId?: string, target?: RequestTarget): Promise<YidaStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/yida/login/poll', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(corpId ? { corp_id: corpId } : {}),
  }, target);
  return _coerceYidaStatus(unwrapData<JsonObject>(wrapped));
}

export async function disconnectYida(target?: RequestTarget): Promise<YidaStatus> {
  const wrapped = await apiRequest<unknown>('/v1/integrations/yida/disconnect', { method: 'POST' }, target);
  return _coerceYidaStatus(unwrapData<JsonObject>(wrapped));
}
