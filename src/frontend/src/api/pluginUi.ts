import type { PluginContributions } from '../plugin-ui/types';
import { apiRequest, getApiUrl, unwrapData } from './core';

// ── Plugin UI contributions (L0 views / L1 data proxy / L2 modules) ─────────
// Generic replacements for what used to be per-plugin endpoints: the browser
// names a plugin and a declared data-source id, and the backend resolves the
// upstream URL and its credentials.

export async function listPluginUiContributions(): Promise<PluginContributions[]> {
  const wrapped = await apiRequest<unknown>('/v1/plugins/ui-contributions');
  return unwrapData<{ items: PluginContributions[] }>(wrapped).items || [];
}

export async function callPluginDataSource(
  slug: string,
  sourceId: string,
  params: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<unknown> {
  const wrapped = await apiRequest<unknown>(
    `/v1/plugins/${encodeURIComponent(slug)}/data/${encodeURIComponent(sourceId)}`,
    { method: 'POST', body: JSON.stringify(params), signal },
  );
  return unwrapData<unknown>(wrapped);
}

/** URL of an asset inside a plugin package's own ``web/`` directory (L2 modules). */
export function pluginWebAssetUrl(slug: string, entry: string): string {
  const relative = entry.replace(/^\/+/, '').replace(/^web\//, '');
  const path = relative.split('/').map(encodeURIComponent).join('/');
  return `${getApiUrl()}/v1/plugins/${encodeURIComponent(slug)}/web/${path}`;
}
