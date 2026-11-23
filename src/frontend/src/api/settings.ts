import { apiRequest, unwrapData } from './core';

// ── Personal API keys ────────────────────────────────────────────────────

export interface ApiKeyItem {
  id: string;
  name: string;
  key_prefix: string;
  enabled: boolean;
  expires_at?: string | null;
  last_used_at?: string | null;
  created_at?: string | null;
  /** Whether the plaintext can be retrieved again (false for legacy keys with no stored ciphertext; frontend hides the "Copy" action) */
  revealable?: boolean;
  /** Plaintext is only returned by the create / reveal endpoints; null in list responses */
  api_key?: string | null;
}

export async function listApiKeys(): Promise<ApiKeyItem[]> {
  const wrapped = await apiRequest<unknown>('/v1/me/api-keys');
  const data = unwrapData<{ items?: ApiKeyItem[] }>(wrapped);
  return Array.isArray(data.items) ? data.items : [];
}

export async function createApiKey(name: string, expiresInDays: number | null, forGateway = false): Promise<ApiKeyItem> {
  const wrapped = await apiRequest<unknown>('/v1/me/api-keys', {
    method: 'POST',
    body: JSON.stringify({ name, expires_in_days: expiresInDays, for_gateway: forGateway }),
  });
  return unwrapData<ApiKeyItem>(wrapped);
}

/** Retrieve the full plaintext of an API key again (for copying). Backend returns 400 for legacy keys with no stored ciphertext. */
export async function revealApiKey(keyId: string): Promise<string> {
  const wrapped = await apiRequest<unknown>(`/v1/me/api-keys/${encodeURIComponent(keyId)}/reveal`);
  const data = unwrapData<ApiKeyItem>(wrapped);
  return data.api_key ?? '';
}

export async function toggleApiKey(keyId: string, enabled: boolean): Promise<void> {
  await apiRequest(`/v1/me/api-keys/${encodeURIComponent(keyId)}`, {
    method: 'PATCH',
    body: JSON.stringify({ enabled }),
  });
}

export async function revokeApiKey(keyId: string): Promise<void> {
  await apiRequest(`/v1/me/api-keys/${encodeURIComponent(keyId)}`, { method: 'DELETE' });
}

// ── Personal system settings (delegated to users on CE: model providers / service configs / my logs) ──

interface SystemAccessInfo {
  allowed: boolean;
  edition: string;
}

interface OntologyGovernanceAccessInfo {
  allowed: boolean;
  edition: string;
}

/** Probe: whether the current user can manage personal system settings (the frontend shows/hides the "System management" entry based on this). */
export async function getMySystemAccess(): Promise<SystemAccessInfo> {
  const wrapped = await apiRequest<unknown>('/v1/me/system/access');
  return unwrapData<SystemAccessInfo>(wrapped);
}

/** Probe: whether the current CE user may manage the instance-wide Domain Packs from Settings. */
export async function getOntologyGovernanceAccess(): Promise<OntologyGovernanceAccessInfo> {
  return { allowed: false, edition: 'ce' };
}

export interface ServiceConfigItem {
  config_key: string;
  config_value: string | null;
  display_name: string;
  description: string;
  group_key: string;
  is_secret: boolean;
  updated_at?: string | null;
  updated_by?: string | null;
}

export interface ServiceConfigGroup {
  group_key: string;
  label: string;
  testable: boolean;
  items: ServiceConfigItem[];
}

export async function getMyServiceConfigs(): Promise<ServiceConfigGroup[]> {
  const wrapped = await apiRequest<unknown>('/v1/me/system/service-configs');
  const data = unwrapData<ServiceConfigGroup[]>(wrapped);
  return Array.isArray(data) ? data : [];
}

export async function updateMyServiceConfigs(
  items: Array<{ key: string; value: string | null }>,
): Promise<void> {
  await apiRequest('/v1/me/system/service-configs', {
    method: 'PUT',
    body: JSON.stringify({ items }),
  });
}

interface ServiceTestResult {
  success: boolean;
  latency_ms: number;
  error: string | null;
}

export async function testMyServiceConfig(groupKey: string): Promise<ServiceTestResult> {
  const wrapped = await apiRequest<unknown>(
    `/v1/me/system/service-configs/test/${encodeURIComponent(groupKey)}`,
    { method: 'POST' },
  );
  return unwrapData<ServiceTestResult>(wrapped);
}

// ── Model provider management (/v1/models, gate = require_system_settings) ──

export interface ModelProviderItem {
  provider_id: string;
  display_name: string;
  provider_type: 'chat' | 'embedding' | 'reranker' | 'audio';
  provider: string;
  base_url: string;
  api_key: string; // masked
  model_name: string;
  extra_config: Record<string, unknown>;
  is_active: boolean;
  last_tested_at?: string | null;
  last_test_status?: string | null;
}

export interface ModelProviderInput {
  display_name: string;
  provider_type: 'chat' | 'embedding' | 'reranker' | 'audio';
  provider?: string;
  base_url?: string;
  api_key?: string;
  model_name: string;
  extra_config?: Record<string, unknown>;
  is_active?: boolean;
}

export interface ModelRoleAssignment {
  role_key: string;
  label?: string;
  description?: string;
  type?: string;
  required_type?: string;
  /** 该角色额外要求供应商声明的 extra_config 能力位（如 vision 角色的 supports_vision）；
   *  为空表示只按 provider_type 匹配即可。 */
  requires_capability?: string | null;
  provider_id: string | null;
  provider_name?: string | null;
  [key: string]: unknown;
}

interface ProviderSchemaField {
  key: string;
  label?: string;
  required?: boolean;
  secret?: boolean;
  placeholder?: string;
  [key: string]: unknown;
}

export interface ProviderSchema {
  id: string;
  label?: string;
  engine?: string;
  supports_types?: string[];
  base_url_template?: string;
  autofill_base_url?: boolean;
  api_key_required?: boolean;
  fields?: ProviderSchemaField[];
  [key: string]: unknown;
}

/** 上下文窗口自动探测的输入（未保存的表单值即可探测）。 */
interface ContextProbeInput {
  provider: string;
  provider_type?: string;
  base_url?: string;
  api_key?: string;
  model_name: string;
  /** 编辑已保存供应商时 API Key 框为空（表示不修改），传 provider_id 让后端复用已存密钥。 */
  provider_id?: string;
  /** 允许多花一次「超限报错」探测（上游校验阶段即拒绝，不产生推理费用）。 */
  allow_error_probe?: boolean;
}

export interface ContextProbeResult {
  /** 探到的上下文窗口（token）；0 表示没探到，需人工填写。 */
  context_length: number;
  /** models_endpoint | ollama_show | max_tokens_probe | name_heuristic */
  source: string;
  source_label: string;
  /** high = 上游自报；medium = 报错回报；low = 按模型名推断。 */
  confidence: 'high' | 'medium' | 'low' | 'none';
  detail: string;
  /** 逐级说明每个来源看到了什么，供管理员判断该手工填多少。 */
  notes: string[];
}

export async function detectModelContextLength(
  input: ContextProbeInput,
): Promise<ContextProbeResult> {
  const wrapped = await apiRequest<unknown>('/v1/models/providers/detect-context', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return unwrapData<ContextProbeResult>(wrapped);
}

export async function listModelProviders(): Promise<ModelProviderItem[]> {
  const wrapped = await apiRequest<unknown>('/v1/models/providers');
  const data = unwrapData<ModelProviderItem[]>(wrapped);
  return Array.isArray(data) ? data : [];
}

export async function createModelProvider(input: ModelProviderInput): Promise<ModelProviderItem> {
  const wrapped = await apiRequest<unknown>('/v1/models/providers', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return unwrapData<ModelProviderItem>(wrapped);
}

export async function updateModelProvider(
  providerId: string,
  input: Partial<ModelProviderInput>,
): Promise<ModelProviderItem> {
  const wrapped = await apiRequest<unknown>(
    `/v1/models/providers/${encodeURIComponent(providerId)}`,
    { method: 'PUT', body: JSON.stringify(input) },
  );
  return unwrapData<ModelProviderItem>(wrapped);
}

export async function deleteModelProvider(providerId: string): Promise<void> {
  await apiRequest(`/v1/models/providers/${encodeURIComponent(providerId)}`, {
    method: 'DELETE',
  });
}

export async function testModelProvider(providerId: string): Promise<ServiceTestResult> {
  const wrapped = await apiRequest<unknown>(
    `/v1/models/providers/${encodeURIComponent(providerId)}/test`,
    { method: 'POST' },
  );
  return unwrapData<ServiceTestResult>(wrapped);
}

export async function listModelRoles(): Promise<ModelRoleAssignment[]> {
  const wrapped = await apiRequest<unknown>('/v1/models/roles');
  const data = unwrapData<ModelRoleAssignment[]>(wrapped);
  return Array.isArray(data) ? data : [];
}

export async function assignModelRole(roleKey: string, providerId: string): Promise<void> {
  await apiRequest(`/v1/models/roles/${encodeURIComponent(roleKey)}`, {
    method: 'PUT',
    body: JSON.stringify({ provider_id: providerId }),
  });
}

export async function unassignModelRole(roleKey: string): Promise<void> {
  await apiRequest(`/v1/models/roles/${encodeURIComponent(roleKey)}`, { method: 'DELETE' });
}

export async function getModelProviderSchemas(): Promise<ProviderSchema[]> {
  const wrapped = await apiRequest<unknown>('/v1/models/provider-schemas');
  const data = unwrapData<ProviderSchema[]>(wrapped);
  return Array.isArray(data) ? data : [];
}
