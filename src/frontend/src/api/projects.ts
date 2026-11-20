import type { ProjectItem, ProjectKind, ProjectDetail, ProjectFileItem, ProjectChatSummary } from '../types';
import type { EditionCreateProjectFields, EditionProjectUpdateFields } from '../editionModelTypes';
import { apiRequest, getApiUrl, isHybridDual, isLocalProject, projectTargetHeaders, registerLocalProject, unwrapData, uploadErrorMessage } from './core';

// ── Skill Distillation (Lab personal skill distillation) ────────────────

// ─── Projects (Claude-style workspaces) ───────────────────────────────────

interface ProjectListResponse {
  items: ProjectItem[];
  pagination: { page: number; page_size: number; total_items: number; total_pages: number; has_previous: boolean; has_next: boolean };
}

export async function listProjects(opts: { q?: string; sort?: string; page?: number; pageSize?: number } = {}): Promise<ProjectListResponse> {
  const params = new URLSearchParams();
  if (opts.q) params.set('q', opts.q);
  if (opts.sort) params.set('sort', opts.sort);
  if (opts.page) params.set('page', String(opts.page));
  if (opts.pageSize) params.set('page_size', String(opts.pageSize));
  const qs = params.toString();
  const path = `/v1/projects${qs ? `?${qs}` : ''}`;
  if (!isHybridDual()) return unwrapData<ProjectListResponse>(await apiRequest<unknown>(path));
  // 双模式：云端项目 + 本机的本地文件夹项目合并为一张列表（本地项目登记路由表）。
  // 两端并行请求；本机执行面尚未就绪时只列云端项目。
  const [cloudResult, localResult] = await Promise.allSettled([
    apiRequest<unknown>(path),
    apiRequest<unknown>(path, undefined, 'local'),
  ]);
  if (cloudResult.status === 'rejected') throw cloudResult.reason;
  const cloud = unwrapData<ProjectListResponse>(cloudResult.value);
  if (localResult.status === 'rejected') return cloud;
  const local = unwrapData<ProjectListResponse>(localResult.value);
  const localItems = (local?.items || []).filter((p) => (p.kind as string) === 'local');
  localItems.forEach((p) => registerLocalProject(p.project_id));
  if (!localItems.length) return cloud;
  const seen = new Set((cloud.items || []).map((p) => p.project_id));
  return {
    ...cloud,
    items: [...(cloud.items || []), ...localItems.filter((p) => !seen.has(p.project_id))],
  };
}

export async function createProject(body: {
  name: string;
  description?: string;
  kind: ProjectKind;
  linked_folder_id?: string;
} & EditionCreateProjectFields): Promise<ProjectDetail> {
  const wrapped = await apiRequest<unknown>('/v1/projects', {
    method: 'POST',
    body: JSON.stringify(body),
  });
  return unwrapData<ProjectDetail>(wrapped);
}

/**
 * Create a desktop local-folder project (ticket #03). Only works inside the
 * desktop local backend (the route is gated on DEPLOY_PROFILE=local); on the
 * cloud/web deployment this returns 403 and is never invoked.
 */
export async function createLocalProject(body: {
  name: string;
  local_path: string;
  local_machine?: string;
  description?: string;
}): Promise<ProjectDetail> {
  const wrapped = await apiRequest<unknown>(
    '/v1/projects',
    { method: 'POST', body: JSON.stringify({ kind: 'local', ...body }) },
    'local',
  );
  const detail = unwrapData<ProjectDetail>(wrapped);
  registerLocalProject(detail.project_id);
  return detail;
}

// ── Desktop local permissions (ticket #06) ────────────────────────────────
export interface LocalGrant {
  path: string;
  mode: 'read' | 'readwrite';
}
export type LocalDisposition = 'block' | 'confirm' | 'allow';
export interface LocalPolicy {
  out_of_scope?: LocalDisposition;
  delete?: LocalDisposition;
  system_write?: LocalDisposition;
  network?: LocalDisposition;
  privilege?: LocalDisposition;
}

export async function listLocalGrants(): Promise<LocalGrant[]> {
  const wrapped = await apiRequest<{ items: LocalGrant[] }>('/v1/local/grants', undefined, 'local');
  return unwrapData<{ items: LocalGrant[] }>(wrapped).items || [];
}
export async function addLocalGrant(path: string, mode: 'read' | 'readwrite' = 'readwrite'): Promise<void> {
  await apiRequest('/v1/local/grants', { method: 'POST', body: JSON.stringify({ path, mode }) }, 'local');
}
export async function removeLocalGrant(path: string): Promise<void> {
  await apiRequest(`/v1/local/grants?path=${encodeURIComponent(path)}`, { method: 'DELETE' }, 'local');
}
export async function getLocalPolicy(): Promise<LocalPolicy> {
  const wrapped = await apiRequest<LocalPolicy>('/v1/local/policy', undefined, 'local');
  return unwrapData<LocalPolicy>(wrapped) || {};
}
export async function setLocalPolicy(policy: LocalPolicy): Promise<void> {
  await apiRequest('/v1/local/policy', { method: 'PUT', body: JSON.stringify(policy) }, 'local');
}

export interface LocalSnapshotFile {
  path: string;
  count: number;
}
// ── 工具执行权限档（逐项确认 / 替我批准 / 完全放开）──────────────────────
// 网页端与桌面端共用这一档，每个用户一份存在服务端；桌面端的本机策略由它翻译
// 而来，不再另存一份「本机操作权限档」。上面的授权目录管的是"本机哪些目录能动"，
// 与档位是两件事，只在桌面端有。
export type ToolApprovalMode = 'ask' | 'auto' | 'full';
async function writeToolApprovalMode(mode: ToolApprovalMode, target?: 'local'): Promise<void> {
  await apiRequest('/v1/tool-approval', { method: 'PUT', body: JSON.stringify({ mode }) }, target);
}

async function readAndSyncToolApprovalMode(): Promise<ToolApprovalMode> {
  const wrapped = await apiRequest<{ mode: ToolApprovalMode }>('/v1/tool-approval');
  const mode = unwrapData<{ mode: ToolApprovalMode }>(wrapped).mode || 'ask';
  if (isHybridDual()) {
    // Cloud owns the account preference; repair older desktops that only saved it there.
    // Do not report a preset until the executor has accepted the same value.
    const local = await apiRequest<{ mode: ToolApprovalMode }>('/v1/tool-approval', undefined, 'local');
    if (unwrapData<{ mode: ToolApprovalMode }>(local).mode !== mode) {
      await writeToolApprovalMode(mode, 'local');
    }
  }
  return mode;
}

// Serialize startup repair and user changes so a delayed read cannot overwrite
// a newer choice. Failed attempts release the queue so the next retry can repair it.
let approvalSync: Promise<unknown> = Promise.resolve();
function queueApprovalSync<T>(operation: () => Promise<T>): Promise<T> {
  const result = approvalSync.then(operation);
  approvalSync = result.catch(() => undefined);
  return result;
}

export function getToolApprovalMode(): Promise<ToolApprovalMode> {
  return queueApprovalSync(readAndSyncToolApprovalMode);
}

export function setToolApprovalMode(mode: ToolApprovalMode): Promise<void> {
  return queueApprovalSync(async () => {
    await writeToolApprovalMode(mode);
    if (isHybridDual()) await writeToolApprovalMode(mode, 'local');
  });
}

export async function listLocalSnapshots(): Promise<LocalSnapshotFile[]> {
  const wrapped = await apiRequest<{ items: LocalSnapshotFile[] }>('/v1/local/snapshots', undefined, 'local');
  return unwrapData<{ items: LocalSnapshotFile[] }>(wrapped).items || [];
}
export async function rollbackLocalFile(path: string): Promise<void> {
  await apiRequest('/v1/local/rollback', { method: 'POST', body: JSON.stringify({ path }) }, 'local');
}

export async function getProject(projectId: string): Promise<ProjectDetail> {
  const wrapped = await apiRequest<unknown>(
    `/v1/projects/${encodeURIComponent(projectId)}`,
    undefined,
    isLocalProject(projectId) ? 'local' : undefined,
  );
  return unwrapData<ProjectDetail>(wrapped);
}

export async function updateProject(
  projectId: string,
  patch: Partial<Pick<ProjectItem, 'name' | 'description' | 'instructions' | 'pinned' | 'icon_color' | 'memory_enabled' | 'memory_write_enabled'>> & EditionProjectUpdateFields,
): Promise<ProjectDetail> {
  const wrapped = await apiRequest<unknown>(
    `/v1/projects/${encodeURIComponent(projectId)}`,
    { method: 'PATCH', body: JSON.stringify(patch) },
    isLocalProject(projectId) ? 'local' : undefined,
  );
  return unwrapData<ProjectDetail>(wrapped);
}

export async function deleteProject(projectId: string): Promise<void> {
  await apiRequest<unknown>(
    `/v1/projects/${encodeURIComponent(projectId)}`,
    { method: 'DELETE' },
    isLocalProject(projectId) ? 'local' : undefined,
  );
}

export async function toggleProjectFavorite(projectId: string, on: boolean): Promise<void> {
  await apiRequest<unknown>(
    `/v1/projects/${encodeURIComponent(projectId)}/favorite`,
    { method: on ? 'POST' : 'DELETE' },
    isLocalProject(projectId) ? 'local' : undefined,
  );
}

export async function updateProjectInstructions(projectId: string, instructions: string, instructionsRevision?: string): Promise<ProjectDetail> {
  const wrapped = await apiRequest<unknown>(
    `/v1/projects/${encodeURIComponent(projectId)}/instructions`,
    { method: 'PATCH', body: JSON.stringify({ instructions, instructions_revision: instructionsRevision }) },
    isLocalProject(projectId) ? 'local' : undefined,
  );
  return unwrapData<ProjectDetail>(wrapped);
}

export async function listProjectFiles(projectId: string): Promise<{
  items: ProjectFileItem[];
  total: number;
  capacity_used: number;
  capacity_limit: number;
}> {
  const wrapped = await apiRequest<unknown>(
    `/v1/projects/${encodeURIComponent(projectId)}/files`,
    undefined,
    isLocalProject(projectId) ? 'local' : undefined,
  );
  const data = unwrapData<{ items: ProjectFileItem[]; total: number; capacity_used: number; capacity_limit: number }>(wrapped);
  return {
    items: data?.items || [],
    total: data?.total || 0,
    capacity_used: data?.capacity_used || 0,
    capacity_limit: data?.capacity_limit || 0,
  };
}

export async function uploadProjectFile(projectId: string, file: File): Promise<ProjectFileItem> {
  const form = new FormData();
  // For folder uploads, <input webkitdirectory> attaches webkitRelativePath to the
  // File object (e.g. ``finance/2024/q1.xlsx``). The backend keeps it as the filename
  // so the source subdirectory is visible at a glance inside the project. For plain
  // file uploads webkitRelativePath = '' and file.name is used.
  const relPath = (file as File & { webkitRelativePath?: string }).webkitRelativePath || '';
  const namedFile = relPath ? new File([file], relPath, { type: file.type }) : file;
  form.append('file', namedFile);
  const url = `${getApiUrl()}/v1/projects/${encodeURIComponent(projectId)}/files/upload`;
  const resp = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: { ...projectTargetHeaders(projectId) },
    body: form,
  });
  const payload = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(uploadErrorMessage(resp.status, payload));
  }
  return unwrapData<ProjectFileItem>(payload);
}

export async function removeProjectFile(projectId: string, artifactId: string): Promise<void> {
  // A project file = an artifact under a MySpace folder. Deleting simply soft-deletes the artifact (it disappears on the MySpace side too).
  await apiRequest<unknown>(
    `/v1/projects/${encodeURIComponent(projectId)}/files/${encodeURIComponent(artifactId)}`,
    { method: 'DELETE' },
    isLocalProject(projectId) ? 'local' : undefined,
  );
}

export async function listProjectChats(
  projectId: string,
  page: number = 1,
  pageSize: number = 50,
  scope: 'all' | 'mine' | 'shared' = 'all',
): Promise<{ items: ProjectChatSummary[]; total: number }> {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize), scope });
  const wrapped = await apiRequest<unknown>(
    `/v1/projects/${encodeURIComponent(projectId)}/chats?${params.toString()}`,
    undefined,
    isLocalProject(projectId) ? 'local' : undefined,
  );
  const data = unwrapData<{ items: ProjectChatSummary[]; pagination: { total_items: number } }>(wrapped);
  return { items: data?.items || [], total: data?.pagination?.total_items || 0 };
}
