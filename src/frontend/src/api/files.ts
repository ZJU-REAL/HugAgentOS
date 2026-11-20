import { createApiResponseError, readErrorMessage } from '../utils/apiError';
import type { ResourceItem, PersonalFolderNode } from '../types';
import { type AddArtifactToKBResult, apiRequest, authFetch, chatUploadTarget, getApiUrl, LOCAL_TARGET_HEADER, throwIfSessionExpired, unwrapData } from './core';

// ── File upload API ─────────────────────────────────────────────

export interface UploadedFile {
  origin?: 'local' | 'cloud';
  file_id: string;
  name: string;
  size: number;
  mime_type: string;
  download_url: string;
}

export async function uploadFile(
  file: File,
  chatId?: string,
  folderId?: string | null,
  options: { apiUrl?: string; target?: 'local' | 'cloud'; projectId?: string; uploadKey?: string } = {},
): Promise<UploadedFile> {
  const origin = options.target ?? chatUploadTarget(chatId, options.projectId);
  const url = `${options.apiUrl ?? getApiUrl()}/v1/file/upload`;
  const formData = new FormData();
  formData.append('file', file, file.name.split('/').pop()!);
  if (options.uploadKey) formData.append('upload_key', options.uploadKey);
  if (chatId) formData.append('chat_id', chatId);
  if (folderId) formData.append('folder_id', folderId);

  const response = await authFetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: origin ? { [LOCAL_TARGET_HEADER]: origin } : {},
    body: formData,
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw createApiResponseError(response.status, payload, `Upload failed: ${response.status}`, response.headers.get("Retry-After"));
  }

  const payload = await response.json();
  return { ...unwrapData<UploadedFile>(payload), ...(origin ? { origin } : {}) };
}

/** Overwrite existing file content in-place (same file_id & URL). */
export async function overwriteFile(fileId: string, file: File, headers: Record<string, string> = {}): Promise<UploadedFile> {
  const url = `${getApiUrl()}/v1/file/${encodeURIComponent(fileId)}`;
  const formData = new FormData();
  formData.append('file', file);

  const response = await fetch(url, {
    method: 'PUT',
    credentials: 'include',
    headers,
    body: formData,
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throwIfSessionExpired(response.status, payload);
    throw new Error(readErrorMessage(payload, `Overwrite failed: ${response.status}`));
  }

  const payload = await response.json();
  return unwrapData<UploadedFile>(payload);
}

// ── MySpace API ─────────────────────────────────────────────────

export async function getArtifacts(params?: {
  type?: 'document' | 'image';
  source_kind?: 'user_upload' | 'ai_generated';
  keyword?: string;
  scope?: 'personal' | 'all';
  /** "__root__" = personal root directory; "<id>" = direct child files of that personal folder; omitted = all personal files */
  folder_id?: string;
  page?: number;
  page_size?: number;
}): Promise<{ items: ResourceItem[]; total: number; has_more: boolean }> {
  const qs = new URLSearchParams();
  if (params?.type) qs.set('type', params.type);
  if (params?.source_kind) qs.set('source_kind', params.source_kind);
  if (params?.keyword) qs.set('keyword', params.keyword);
  if (params?.scope) qs.set('scope', params.scope);
  if (params?.folder_id) qs.set('folder_id', params.folder_id);
  if (params?.page) qs.set('page', String(params.page));
  if (params?.page_size) qs.set('page_size', String(params.page_size));
  const query = qs.toString();
  const wrapped = await apiRequest<unknown>(`/v1/artifacts${query ? '?' + query : ''}`);
  return unwrapData<{ items: ResourceItem[]; total: number; has_more: boolean }>(wrapped);
}

export async function getFavoriteChats(params?: {
  keyword?: string;
  page?: number;
  page_size?: number;
}): Promise<{ items: ResourceItem[]; total: number; has_more: boolean }> {
  const qs = new URLSearchParams();
  if (params?.keyword) qs.set('keyword', params.keyword);
  if (params?.page) qs.set('page', String(params.page));
  if (params?.page_size) qs.set('page_size', String(params.page_size));
  const query = qs.toString();
  const wrapped = await apiRequest<unknown>(`/v1/artifacts/favorites${query ? '?' + query : ''}`);
  return unwrapData<{ items: ResourceItem[]; total: number; has_more: boolean }>(wrapped);
}

export async function deleteArtifact(id: string): Promise<void> {
  await apiRequest(`/v1/artifacts/${id}`, { method: 'DELETE' });
}

export async function addArtifactToKnowledgeBase(
  artifactId: string,
  kbId: string,
): Promise<AddArtifactToKBResult> {
  const wrapped = await apiRequest<unknown>(`/v1/artifacts/${encodeURIComponent(artifactId)}/knowledge-base`, {
    method: 'POST',
    body: JSON.stringify({ kb_id: kbId }),
  });
  return unwrapData<AddArtifactToKBResult>(wrapped);
}

// ── Personal folders (MySpace) API ───────────────────────────────

export async function listPersonalFolderTree(): Promise<PersonalFolderNode[]> {
  const wrapped = await apiRequest<unknown>('/v1/myspace/folders?as=tree');
  const data = unwrapData<{ tree: PersonalFolderNode[] }>(wrapped);
  return data.tree || [];
}

export async function createPersonalFolder(
  name: string,
  parentFolderId: string | null,
): Promise<{ folder_id: string }> {
  const wrapped = await apiRequest<unknown>('/v1/myspace/folders', {
    method: 'POST',
    body: JSON.stringify({ name, parent_folder_id: parentFolderId }),
  });
  return unwrapData<{ folder_id: string }>(wrapped);
}

export async function renamePersonalFolder(folderId: string, name: string): Promise<void> {
  await apiRequest(`/v1/myspace/folders/${encodeURIComponent(folderId)}`, {
    method: 'PATCH',
    body: JSON.stringify({ name }),
  });
}

export async function deletePersonalFolder(
  folderId: string,
): Promise<{ folder_id: string; artifacts_affected: number }> {
  const wrapped = await apiRequest<unknown>(
    `/v1/myspace/folders/${encodeURIComponent(folderId)}`,
    { method: 'DELETE' },
  );
  return unwrapData<{ folder_id: string; artifacts_affected: number }>(wrapped);
}

export async function moveArtifactToPersonalFolder(
  artifactId: string,
  folderId: string | null,
): Promise<void> {
  await apiRequest('/v1/myspace/folders/move-artifact', {
    method: 'POST',
    body: JSON.stringify({ artifact_id: artifactId, folder_id: folderId }),
  });
}

export async function copyArtifactToPersonalFolder(
  artifactId: string,
  folderId: string | null,
): Promise<void> {
  await apiRequest('/v1/myspace/folders/copy-artifact', {
    method: 'POST',
    body: JSON.stringify({ artifact_id: artifactId, folder_id: folderId }),
  });
}
