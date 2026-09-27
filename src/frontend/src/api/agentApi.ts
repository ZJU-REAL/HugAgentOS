import { apiRequest, unwrapData, type ApiKeyItem } from '../api';

export interface AgentApiKey extends ApiKeyItem { agent_id: string }
export type AgentApiCallStatus = 'running' | 'completed' | 'failed' | 'cancelled' | 'needs_attention';
export interface AgentApiCall {
  id: string;
  key_id: string;
  key_name: string;
  key_prefix: string;
  agent_id: string;
  chat_id: string | null;
  run_id: string | null;
  stream: boolean;
  status: AgentApiCallStatus;
  http_status: number | null;
  error_code: string | null;
  created_at: string;
  completed_at: string | null;
  duration_ms: number | null;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}
export interface AgentApiCallPage {
  items: AgentApiCall[];
  pagination: {
    page: number; page_size: number; total_items: number; total_pages: number;
    has_previous: boolean; has_next: boolean;
  };
}
const base = (agentId: string) => `/v1/agents/${encodeURIComponent(agentId)}`;

export async function listAgentApiKeys(agentId: string, signal?: AbortSignal): Promise<AgentApiKey[]> {
  const data = unwrapData<{ items: AgentApiKey[] }>(await apiRequest(
    `${base(agentId)}/api-keys`, { signal },
  ));
  return data.items;
}
export async function createAgentApiKey(
  agentId: string, name: string, expiresInDays: number | null, signal?: AbortSignal,
): Promise<AgentApiKey> {
  return unwrapData(await apiRequest(`${base(agentId)}/api-keys`, {
    method: 'POST', body: JSON.stringify({ name, expires_in_days: expiresInDays }), signal,
  }));
}
export async function toggleAgentApiKey(agentId: string, keyId: string, enabled: boolean, signal?: AbortSignal) {
  return unwrapData<AgentApiKey>(await apiRequest(`${base(agentId)}/api-keys/${encodeURIComponent(keyId)}`, {
    method: 'PATCH', body: JSON.stringify({ enabled }), signal,
  }));
}
export async function revokeAgentApiKey(agentId: string, keyId: string, signal?: AbortSignal) {
  await apiRequest(`${base(agentId)}/api-keys/${encodeURIComponent(keyId)}`, { method: 'DELETE', signal });
}
export async function revealAgentApiKey(agentId: string, keyId: string, signal?: AbortSignal): Promise<string> {
  const data = unwrapData<AgentApiKey>(await apiRequest(
    `${base(agentId)}/api-keys/${encodeURIComponent(keyId)}/reveal`, { signal },
  ));
  return data.api_key || '';
}
export async function listAgentApiCalls(
  agentId: string,
  query: { page: number; page_size: number; key_id?: string; status?: AgentApiCallStatus },
  signal?: AbortSignal,
): Promise<AgentApiCallPage> {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.page_size) });
  if (query.key_id) params.set('key_id', query.key_id);
  if (query.status) params.set('status', query.status);
  return unwrapData(await apiRequest(`${base(agentId)}/api-calls?${params}`, { signal }));
}
