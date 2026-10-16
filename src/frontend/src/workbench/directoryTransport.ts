import { apiRequest, authFetch, getApiUrl } from '../api';
import type { CatalogItem, LayoutPolicy } from './catalog';

export interface DirectorySnapshot { items: CatalogItem[]; policy: LayoutPolicy; revision: string }
export async function fetchDirectory(signal: AbortSignal, etag?: string): Promise<DirectorySnapshot | null> {
  const response = await authFetch(getApiUrl() + '/v1/workbench/catalog', {
    signal, cache: 'no-cache', headers: etag ? { 'If-None-Match': '"' + etag + '"' } : {},
  });
  if (response.status === 304) return null;
  if (!response.ok) throw new Error('Directory request failed: ' + response.status);
  const body = await response.json() as { data: DirectorySnapshot };
  if (!Array.isArray(body.data?.items) || !body.data.policy
    || typeof body.data.revision !== 'string') throw new Error('Invalid directory response');
  return body.data;
}
export async function waitDirectoryRevision(revision: string, signal: AbortSignal) {
  const response = await apiRequest<{ data: { revision: string; scope: string } }>(
    '/v1/workbench/catalog/events?revision=' + encodeURIComponent(revision), { signal });
  return response?.data?.revision ?? revision;
}
export const directoryPollDelay = (failures: number) => Math.min(60000 * 2 ** Math.min(failures, 4), 600000);
