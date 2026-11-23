import type { MarketplaceAgentListResult, MarketplaceAgentDetail, AgentMarketInstallResult, AgentMarketSubmission, PluginListItem, InstalledPluginItem, InstalledPluginDetail, PluginInstallResult } from '../types';
import { readErrorMessage } from '../utils/apiError';
import { apiRequest, authFetch, getApiUrl, type RequestTarget, throwIfSessionExpired, unwrapData } from './core';

// ── Sub-Agent Marketplace ───────────────────────────────────────────────────

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

export async function listPlugins(): Promise<PluginListItem[]> {
  const wrapped = await apiRequest<unknown>('/v1/plugins');
  return unwrapData<{ items: PluginListItem[] }>(wrapped).items || [];
}

export async function listInstalledPlugins(): Promise<InstalledPluginItem[]> {
  const wrapped = await apiRequest<unknown>('/v1/plugins/installed');
  return unwrapData<{ items: InstalledPluginItem[] }>(wrapped).items || [];
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
  const response = await authFetch(url, { method: 'POST', body: formData });
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

// Edit my imported/private plugin's display metadata (name/category/icon are UI
// config — the Agent Plugins standard plugin.json carries no display fields).
export async function setInstalledPluginMeta(
  installId: string,
  meta: { display_name?: string; category?: string; icon?: string },
): Promise<void> {
  await apiRequest(`/v1/plugins/installed/${encodeURIComponent(installId)}/meta`, {
    method: 'PATCH',
    body: JSON.stringify(meta),
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

export async function getLarkAppInitStatus(target?: RequestTarget): Promise<LarkAppInitStatus> {
  const wrapped = await apiRequest<unknown>('/v1/plugins/feishu-cli/app/status', undefined, target);
  return unwrapData<LarkAppInitStatus>(wrapped);
}

export async function startLarkAppInit(target?: RequestTarget): Promise<LarkAppInitStatus> {
  const wrapped = await apiRequest<unknown>('/v1/plugins/feishu-cli/app/init', { method: 'POST' }, target);
  return unwrapData<LarkAppInitStatus>(wrapped);
}

export async function resetLarkAppInit(target?: RequestTarget): Promise<LarkAppInitStatus> {
  const wrapped = await apiRequest<unknown>('/v1/plugins/feishu-cli/app/reset', { method: 'POST' }, target);
  return unwrapData<LarkAppInitStatus>(wrapped);
}
