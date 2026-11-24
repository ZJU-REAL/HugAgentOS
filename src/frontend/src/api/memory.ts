import type { MemoryItem, MemoryProfile, MemoryGraphRelation, OntologyAssetKind, OntologyTagOption } from '../types';
import { newOperationId } from '../utils/operationId';
import { apiRequest, unwrapData } from './core';

// ── Memory management API ───────────────────────────────────────

export async function getMemories(
  projectId?: string,
): Promise<{ enabled: boolean; items: MemoryItem[]; count: number }> {
  const url = projectId
    ? `/v1/memories?project_id=${encodeURIComponent(projectId)}`
    : '/v1/memories';
  const wrapped = await apiRequest<unknown>(url);
  return unwrapData<{ enabled: boolean; items: MemoryItem[]; count: number }>(wrapped);
}

export async function deleteMemory(memoryId: string, messageId?: string): Promise<void> {
  // `messageId` lets the backend drop the entry from the turn card that reported
  // it, so the transcript never keeps offering actions on a deleted memory.
  const qs = messageId ? `?message_id=${encodeURIComponent(messageId)}` : '';
  await apiRequest(`/v1/memories/${memoryId}${qs}`, { method: 'DELETE' });
}

export async function deleteGraphRelation(relationId: string, messageId?: string): Promise<void> {
  const qs = messageId ? `?message_id=${encodeURIComponent(messageId)}` : '';
  await apiRequest(`/v1/memories/graph/${relationId}${qs}`, { method: 'DELETE' });
}

export async function updateMemory(
  memoryId: string,
  text: string,
  messageId?: string,
): Promise<void> {
  await apiRequest(`/v1/memories/${memoryId}`, {
    method: 'PATCH',
    body: JSON.stringify({
      text,
      message_id: messageId,
      operation_id: newOperationId(),
    }),
  });
}

/** L1 profile entries are addressed by field key rather than by a store id. */
export async function updateProfileField(
  key: string,
  text: string,
  messageId?: string,
): Promise<void> {
  await apiRequest('/v1/memories/profile/field', {
    method: 'PATCH',
    body: JSON.stringify({
      key,
      text,
      message_id: messageId,
      operation_id: newOperationId(),
    }),
  });
}

export async function deleteProfileField(key: string, messageId?: string): Promise<void> {
  const params = new URLSearchParams({ key });
  if (messageId) params.set('message_id', messageId);
  await apiRequest(`/v1/memories/profile/field?${params.toString()}`, { method: 'DELETE' });
}

export async function clearAllMemories(): Promise<void> {
  await apiRequest('/v1/memories', { method: 'DELETE' });
}

export async function getMemoryProfile(workspaceId: string = 'default'): Promise<MemoryProfile> {
  const wrapped = await apiRequest<unknown>(`/v1/memories/profile?workspace_id=${encodeURIComponent(workspaceId)}`);
  return unwrapData<MemoryProfile>(wrapped);
}

export async function getMemoryGraph(
  limit: number = 30,
): Promise<{ enabled: boolean; relations: MemoryGraphRelation[]; count: number }> {
  const wrapped = await apiRequest<unknown>(`/v1/memories/graph?limit=${limit}`);
  return unwrapData<{ enabled: boolean; relations: MemoryGraphRelation[]; count: number }>(wrapped);
}

interface UserSettings {
  memory_enabled: boolean;
  memory_write_enabled: boolean;
  mem0_available: boolean;
  embedding_available: boolean;
  memory_available: boolean;
  reranker_enabled: boolean;
  reranker_available: boolean;
}

export async function getMemorySettings(): Promise<UserSettings> {
  const wrapped = await apiRequest<unknown>('/v1/memories/settings');
  return unwrapData<UserSettings>(wrapped);
}

export async function updateMemorySettings(memoryEnabled: boolean): Promise<void> {
  await apiRequest('/v1/memories/settings', {
    method: 'PATCH',
    body: JSON.stringify({ memory_enabled: memoryEnabled }),
  });
}

export async function updateMemoryWriteSettings(memoryWriteEnabled: boolean): Promise<void> {
  await apiRequest('/v1/memories/settings', {
    method: 'PATCH',
    body: JSON.stringify({ memory_write_enabled: memoryWriteEnabled }),
  });
}

export async function updateRerankerSettings(rerankerEnabled: boolean): Promise<void> {
  await apiRequest('/v1/memories/settings', {
    method: 'PATCH',
    body: JSON.stringify({ reranker_enabled: rerankerEnabled }),
  });
}

interface OntologySettings {
  allowed: boolean;
  ontology_enabled: boolean;
  ontology_pack_ids: string[];
  available: boolean;
  plugin_import_build_validation_forced: boolean;
  active_packs: Array<{ pack_id: string; version_id: string; version: string }>;
}

export async function getOntologySettings(): Promise<OntologySettings> {
  return {
    allowed: false,
    ontology_enabled: false,
    ontology_pack_ids: [],
    available: false,
    plugin_import_build_validation_forced: false,
    active_packs: [],
  };
}

export async function updateOntologySettings(_ontologyEnabled: boolean): Promise<void> {}

/** Tags from active Domain Packs that actually trigger workflows for this asset kind. */
export async function getOntologyTagOptions(_assetKind: OntologyAssetKind): Promise<OntologyTagOption[]> {
  return [];
}
