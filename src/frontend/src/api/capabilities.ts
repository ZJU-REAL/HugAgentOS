import type { McpMarketListResult, McpMarketItem, McpMarketSubmission, MarketplaceListResult, MarketplaceSkillDetail, MarketplaceSubmission } from '../types';
import { readErrorMessage } from '../utils/apiError';
import { t } from '../i18n';
import { saveBlob } from '../utils/download';
import { apiRequest, authFetch, getApiUrl, unwrapData } from './core';

// ── Capability center: self-service adding of MCP servers / skills ──────────

interface CreateMcpServerInput {
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

/** Start browser consent for an OAuth-protected private MCP; poll it with getMcpMarketOAuthStatus. */
export async function startMyMcpOAuth(input: CreateMcpServerInput & {
  scopes?: string[];
  client_id?: string;
  client_secret?: string;
}): Promise<{ flow_id: string; authorization_url: string; status: string }> {
  const wrapped = await apiRequest<unknown>('/v1/me/mcp-servers/oauth/start', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return unwrapData<{ flow_id: string; authorization_url: string; status: string }>(wrapped);
}

export async function deleteMyMcpServer(serverId: string): Promise<void> {
  await apiRequest(`/v1/me/mcp-servers/${encodeURIComponent(serverId)}`, { method: 'DELETE' });
}

// ── MCP marketplace (user side) ─────────────────────────────────────────────

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
  authMethod?: string,
): Promise<{ server_id: string; action: string }> {
  const wrapped = await apiRequest<unknown>('/v1/mcp-market/install', {
    method: 'POST',
    body: JSON.stringify({
      slug,
      auth_method: authMethod,
      credentials,
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
}): Promise<{ flow_id: string; authorization_url: string; status: string }> {
  const wrapped = await apiRequest<unknown>('/v1/mcp-market/oauth/start', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return unwrapData<{ flow_id: string; authorization_url: string; status: string }>(wrapped);
}

export async function getMcpMarketOAuthStatus(flowId: string): Promise<{
  status: string;
  authorization_url?: string;
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
  // apiRequest 会强制 JSON Content-Type，multipart 用不了；改走 authFetch——它和
  // apiRequest 挂的是同一套混合路由判定，不会漏标成云端写入。
  const url = `${getApiUrl()}/v1/me/skills/upload`;
  const response = await authFetch(url, { method: 'POST', body: form });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(readErrorMessage(payload, t('上传失败：{status}', { status: response.status })));
  }
  return unwrapData<{ id: string; skipped?: unknown[] }>(payload);
}

interface CreateSkillInput {
  name: string;
  display_name: string;
  description: string;  // Required: a skill with an empty description cannot be loaded/registered (backend 422)
  instructions: string;
  tags?: string[];
  mcp_server_ids?: string[];
  user_intro?: string;
  icon?: string;        // preset:<key> / URL / data-URI
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

interface MySkillDetail {
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
  // multipart 走不了 apiRequest（它强制 JSON Content-Type），authFetch 带同一套路由判定
  const url = `${getApiUrl()}/v1/me/skills/${encodeURIComponent(skillId)}/files/upload`;
  const response = await authFetch(url, { method: 'POST', body: form });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(readErrorMessage(payload, t('上传失败：{status}', { status: response.status })));
  }
  return unwrapData<{ filename: string; size: number }>(payload);
}

// Export my private skill as a zip and trigger a browser download.
export async function exportMySkillZip(skillId: string): Promise<void> {
  const url = `${getApiUrl()}/v1/me/skills/${encodeURIComponent(skillId)}/export`;
  const response = await authFetch(url);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(readErrorMessage(payload, t('导出失败：{status}', { status: response.status })));
  }
  saveBlob(await response.blob(), `${skillId}.zip`);
}

// ── Skill marketplace (user side) ───────────────────────────────────────────

export async function getMarketplaceSkills(): Promise<MarketplaceListResult> {
  const wrapped = await apiRequest<unknown>('/v1/marketplace/skills');
  return unwrapData<MarketplaceListResult>(wrapped);
}

export async function getMarketplaceSkillDetail(slug: string): Promise<MarketplaceSkillDetail> {
  const wrapped = await apiRequest<unknown>(`/v1/marketplace/skills/${encodeURIComponent(slug)}`);
  return unwrapData<MarketplaceSkillDetail>(wrapped);
}

// ── Desktop capability store（桌面双模式：本机执行面的能力来源）────────────
// 全部走本机后端（x-hugagent-target: local）。云端能力中心负责「账号里有什么」；
// 这里只回答「这台机器上这条能力是本机的还是云端的」——没有任何手动管理动作：
// 登录时同步一次就全部准备好，云端能力被改动时前端调一次 syncDeviceCapabilities。

export type DeviceCapabilityKind = 'skill' | 'mcp' | 'agent' | 'plugin';

export interface DeviceCapabilityItem {
  install_id: string;
  runtime_name: string;
  kind: DeviceCapabilityKind;
  /** 'cloud' | 'local' | 'builtin' | 'plugin' */
  source: string;
  server_id?: string;
  display_name?: string;
  description?: string;
  enabled?: boolean;
  usable?: boolean;
  change_state?: 'new' | 'modified' | 'synced' | 'compare' | 'unavailable';
  created_at?: string | null;
  resolution?: { outcome: string; reason?: string | null };
}

export interface DeviceCapabilityListing {
  kind: DeviceCapabilityKind;
  profile_id: string | null;
  items: DeviceCapabilityItem[];
  discovery_errors?: { folder: string; code: string }[];
}

export async function getDeviceCapabilities(kind: DeviceCapabilityKind): Promise<DeviceCapabilityListing> {
  const wrapped = await apiRequest<unknown>(`/v1/desktop/capabilities/installations?kind=${kind}`, undefined, 'local');
  return unwrapData<DeviceCapabilityListing>(wrapped);
}

export async function checkDeviceCapabilitySync(): Promise<{ changed: boolean | null }> {
  return unwrapData(await apiRequest('/v1/desktop/capabilities/sync-check', undefined, 'local'));
}

export async function syncDeviceCapabilities(): Promise<Record<string, unknown>> {
  const wrapped = await apiRequest<unknown>('/v1/desktop/capabilities/sync', { method: 'POST' }, 'local');
  return unwrapData<Record<string, unknown>>(wrapped);
}

interface CapabilityFileChange {
  path: string;
  side: string;
  conflict: boolean;
  binary: boolean;
  local: string | null;
  cloud: string | null;
  base: string | null;
}
export interface CapabilityChangePreview {
  preview_id: string;
  changes: CapabilityFileChange[];
  can_edit: boolean;
  cloud_exists: boolean;
  sensitive_paths: string[];
}
export type CapabilityChangeChoices = Record<string, { side: string; content?: string; alternate_path?: string }>;
export async function previewCapabilityChanges(install_id: string): Promise<CapabilityChangePreview> {
  return unwrapData(await apiRequest('/v1/desktop/capabilities/changes/preview', {
    method: 'POST', body: JSON.stringify({ install_id }),
  }, 'local'));
}
export async function commitCapabilityChanges(body: {
  preview_id: string; choices: CapabilityChangeChoices; acknowledge_sensitive: boolean; fork_key?: string;
}): Promise<{ uploaded: boolean; local_applied: boolean; applied: boolean }> {
  return unwrapData(await apiRequest('/v1/desktop/capabilities/changes/commit', {
    method: 'POST', body: JSON.stringify(body),
  }, 'local'));
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
