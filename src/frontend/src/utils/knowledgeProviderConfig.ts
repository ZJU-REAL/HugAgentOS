export type ExternalKnowledgeProvider = 'dify' | 'fastgpt' | 'weknora';
export type KnowledgeProviderConfigField = 'url' | 'api_key' | 'allowed_dataset_ids';

const EXTERNAL_PROVIDERS = new Set<ExternalKnowledgeProvider>([
  'dify',
  'fastgpt',
  'weknora',
]);

const LEGACY_SHARED_CONFIG_KEYS = new Set([
  'knowledge_base.url',
  'knowledge_base.api_key',
  'knowledge_base.allowed_dataset_ids',
]);

const PROVIDER_CONFIG_KEY_RE = /^knowledge_base\.(dify|fastgpt|weknora)\.(url|api_key|allowed_dataset_ids)$/;

export function normalizeKnowledgeProvider(value: string | null | undefined): string {
  return (value || '').trim().toLowerCase() || 'dify';
}

export function isLocalKnowledgeProvider(value: string | null | undefined): boolean {
  const provider = normalizeKnowledgeProvider(value);
  return provider === 'custom' || provider === 'local';
}

export function getKnowledgeProviderLabel(value: string | null | undefined): string {
  const provider = normalizeKnowledgeProvider(value);
  if (provider === 'fastgpt') return 'FastGPT';
  if (provider === 'weknora') return 'WeKnora';
  return 'Dify';
}

export function getKnowledgeProviderConfigKey(
  provider: ExternalKnowledgeProvider,
  field: KnowledgeProviderConfigField,
): string {
  return `knowledge_base.${provider}.${field}`;
}

export function isKnowledgeProviderConfigVisible(
  configKey: string,
  selectedProvider: string | null | undefined,
): boolean {
  if (LEGACY_SHARED_CONFIG_KEYS.has(configKey)) return false;

  const match = PROVIDER_CONFIG_KEY_RE.exec(configKey);
  if (!match) return true;

  const provider = normalizeKnowledgeProvider(selectedProvider);
  return EXTERNAL_PROVIDERS.has(provider as ExternalKnowledgeProvider)
    && match[1] === provider;
}
