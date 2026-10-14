import { apiRequest, projectTargetHeaders, unwrapData, type ApiKeyItem } from '../api';

export interface ProjectApiKey extends ApiKeyItem { project_id: string }
const base = (projectId: string) => `/v1/projects/${encodeURIComponent(projectId)}/api-keys`;
const options = (projectId: string, signal?: AbortSignal) => ({
  headers: projectTargetHeaders(projectId), signal,
});

export async function listProjectApiKeys(projectId: string, signal?: AbortSignal): Promise<ProjectApiKey[]> {
  const data = unwrapData<{ items: ProjectApiKey[] }>(await apiRequest(base(projectId), options(projectId, signal)));
  return data.items;
}
export async function createProjectApiKey(projectId: string, name: string, expiresInDays: number | null, signal?: AbortSignal): Promise<ProjectApiKey> {
  return unwrapData(await apiRequest(base(projectId), {
    ...options(projectId, signal), method: 'POST', body: JSON.stringify({ name, expires_in_days: expiresInDays }),
  }));
}
export async function toggleProjectApiKey(projectId: string, keyId: string, enabled: boolean, signal?: AbortSignal): Promise<ProjectApiKey> {
  return unwrapData(await apiRequest(`${base(projectId)}/${encodeURIComponent(keyId)}`, {
    ...options(projectId, signal), method: 'PATCH', body: JSON.stringify({ enabled }),
  }));
}
export async function revokeProjectApiKey(projectId: string, keyId: string, signal?: AbortSignal): Promise<void> {
  await apiRequest(`${base(projectId)}/${encodeURIComponent(keyId)}`, { ...options(projectId, signal), method: 'DELETE' });
}
export async function revealProjectApiKey(projectId: string, keyId: string, signal?: AbortSignal): Promise<string> {
  const key = unwrapData<ProjectApiKey>(await apiRequest(
    `${base(projectId)}/${encodeURIComponent(keyId)}/reveal`, options(projectId, signal),
  ));
  return key.api_key || '';
}
