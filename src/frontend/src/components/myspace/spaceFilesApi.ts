import { apiRequest, authFetch, getApiUrl, LOCAL_TARGET_HEADER } from '../../api';

export interface SpaceRoot {
  space_id: string;
  name: string;
  kind: 'personal' | 'team';
  permission: 'view' | 'edit' | 'admin';
  mount_path: string;
}
export interface SpaceEntry {
  name: string;
  path: string;
  is_directory: boolean;
  size: number;
  modified_at: string | null;
}
export interface SpacePage {
  space: SpaceRoot;
  path: string;
  entries: SpaceEntry[];
  next_cursor: string | null;
}
const base = '/v1/myspace';
export const spaceQuery = (space: string, path: string) => new URLSearchParams({ space, path });
async function request<T>(path: string, options?: RequestInit): Promise<T> {
  return (await apiRequest<{ data: T }>(base + path, options, 'cloud')).data;
}
export async function listSpaceRoots(signal?: AbortSignal) {
  return request<{ items: SpaceRoot[]; revision: string }>('/mounts', { signal });
}
export async function listSpaceEntries(space: string, path: string, cursor?: string, signal?: AbortSignal, keyword?: string) {
  const query = spaceQuery(space, path);
  if (cursor) query.set('cursor', cursor);
  if (keyword) query.set('keyword', keyword);
  return request<SpacePage>('/entries?' + query, { signal });
}
export async function uploadSpaceFile(space: string, path: string, file: File) {
  return request('/content?' + spaceQuery(space, path), {
    method: 'PUT', body: file, headers: { 'Content-Type': 'application/octet-stream' },
  });
}
export async function downloadSpaceFile(space: string, entry: SpaceEntry) {
  const response = await authFetch(getApiUrl() + base + '/content?' + spaceQuery(space, entry.path), { headers: { [LOCAL_TARGET_HEADER]: 'cloud' } });
  if (!response.ok) throw new Error('HTTP ' + response.status);
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = entry.name;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export async function createSpaceDirectory(space: string, path: string) {
  return request('/directories', { method: 'POST', body: JSON.stringify({ space, path }) });
}
export async function renameSpaceFile(space: string, source: string, destination: string) {
  return request('/moves', { method: 'POST', body: JSON.stringify({ space, source, destination }) });
}
export async function deleteSpaceEntry(space: string, entry: SpaceEntry) {
  const query = spaceQuery(space, entry.path);
  query.set('directory', String(entry.is_directory));
  return request('/entries?' + query, { method: 'DELETE' });
}

export async function referenceSpaceFile(space: string, path: string) {
  return request<import('../../stores/composerStore').ImportedSpaceFile>('/references', {
    method:'POST', body:JSON.stringify({space,path}),
  });
}

export async function searchSpaceFiles(keyword: string, limit = 20, signal?: AbortSignal): Promise<import('../../types').ResourceItem[]> {
  const roots = await listSpaceRoots(signal);
  const found: import('../../types').ResourceItem[] = [];
  const queue = roots.items.map(root => ({ space: root.space_id, path: '' }));
  const needle = keyword.toLowerCase();
  while (queue.length && found.length < limit) {
    const current = queue.shift()!;
    let cursor: string | undefined;
    do {
      const page = await listSpaceEntries(current.space, current.path, cursor, signal);
      for (const entry of page.entries) {
        if (entry.is_directory) queue.push({ space: current.space, path: entry.path });
        else if (entry.name.toLowerCase().includes(needle)) found.push({
          id: current.space + ':' + entry.path, file_id: current.space + ':' + entry.path,
          name: entry.name, type: 'document', size: entry.size, created_at: entry.modified_at || '',
          origin: 'cloud', download_url: '/v1/myspace/content?' + spaceQuery(current.space, entry.path),
        });
        if (found.length >= limit) break;
      }
      cursor = page.next_cursor || undefined;
    } while (cursor && found.length < limit);
  }
  return found;
}
