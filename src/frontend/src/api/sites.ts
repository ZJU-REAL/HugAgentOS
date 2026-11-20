import { type SiteEditionFields, type SiteVisibility, normalizeSiteVisibility, normalizeSiteEditionFields, type SiteUpdateEditionFields } from '../editionSiteVisibility';
import { apiRequest, isHybridDual, type JsonObject, registerLocalChat, registerLocalProject, unwrapData } from './core';

// ── Sites (site hosting) ────────────────────────────────────────────────

interface LocalSiteSource {
  site_id: string;
  project_id: string;
  project_name: string;
  chat_id: string;
  source_dir: string;
  publish_dir: string;
}

export async function prepareLocalSiteProject(projectId?: string): Promise<{
  project_id: string; project_name: string; source_dir: string;
}> {
  const wrapped = await apiRequest<unknown>('/v1/local/site-sources/prepare', {
    method: 'POST', body: JSON.stringify({ project_id: projectId || '' }),
  }, 'local');
  const data = unwrapData<{ project_id: string; project_name: string; source_dir: string }>(wrapped);
  registerLocalProject(data.project_id);
  return data;
}

export async function openLocalSiteEditor(siteId: string): Promise<LocalSiteSource> {
  const wrapped = await apiRequest<unknown>(`/v1/local/site-sources/${encodeURIComponent(siteId)}/edit`, {
    method: 'POST',
  }, 'local');
  const data = unwrapData<LocalSiteSource>(wrapped);
  registerLocalProject(data.project_id);
  registerLocalChat(data.chat_id);
  return data;
}

async function withLocalSiteSources(items: SiteItem[]): Promise<SiteItem[]> {
  if (!isHybridDual() || !items.length) return items;
  try {
    const wrapped = await apiRequest<unknown>('/v1/local/site-sources', undefined, 'local');
    const data = unwrapData<{items: LocalSiteSource[]}>(wrapped);
    const sources = new Map((data.items || []).map((entry) => [entry.site_id, entry]));
    return items.map((site) => {
      const source = sources.get(site.site_id);
      if (!source || !['admin', 'edit'].includes(site.permission || '')) return site;
      registerLocalProject(source.project_id);
      return { ...site, editable: true, local_source: source };
    });
  } catch {
    // Cloud sites stay available while the local execution plane starts or is offline.
    return items;
  }
}

export interface SiteItem extends SiteEditionFields {
  local_source?: LocalSiteSource;
  /** 混合模式的正式站点统一托管在云端。 */
  origin?: 'cloud' | 'local';
  site_id: string;
  slug: string;
  /** In-app relative access URL, of the form /site/<slug>/ */
  url: string;
  title: string;
  description: string | null;
  visibility: SiteVisibility;
  entry_file: string;
  current_version: number;
  file_count: number;
  total_size_bytes: number;
  view_count: number;
  chat_id: string | null;
  /** Site source project id; when set → the "Edit" action on the card can continue editing; null for legacy sites */
  project_id: string | null;
  /** 站点已设访问密码（密码本身不会下发）。 */
  has_password: boolean;
  /** Current actor can edit this source project. */
  editable: boolean;
  permission?: 'none' | 'view' | 'edit' | 'admin';
  can_manage?: boolean;
  created_at: string | null;
  updated_at: string | null;
}

function toSiteItem(raw: JsonObject): SiteItem {
  return {
    site_id: String(raw.site_id ?? ''),
    slug: String(raw.slug ?? ''),
    url: String(raw.url ?? `/site/${raw.slug ?? ''}/`),
    title: String(raw.title ?? ''),
    description: typeof raw.description === 'string' ? raw.description : null,
    visibility: normalizeSiteVisibility(raw.visibility),
    ...normalizeSiteEditionFields(raw),
    entry_file: String(raw.entry_file ?? 'index.html'),
    current_version: Number(raw.current_version ?? 1),
    file_count: Number(raw.file_count ?? 0),
    view_count: Number(raw.view_count ?? 0),
    total_size_bytes: Number(raw.total_size_bytes ?? 0),
    chat_id: typeof raw.chat_id === 'string' ? raw.chat_id : null,
    project_id: typeof raw.project_id === 'string' ? raw.project_id : null,
    has_password: raw.has_password === true,
    editable: Boolean(raw.editable),
    permission: raw.permission as SiteItem['permission'],
    can_manage: raw.can_manage === true,
    created_at: typeof raw.created_at === 'string' ? raw.created_at : null,
    updated_at: typeof raw.updated_at === 'string' ? raw.updated_at : null,
  };
}

/** 混合模式忽略旧站点卡片的本机来源标记。 */
function siteTarget(origin?: 'cloud' | 'local'): 'local' | undefined {
  return !isHybridDual() && origin === 'local' ? 'local' : undefined;
}

export async function listSites(page = 1, pageSize = 50): Promise<{ items: SiteItem[]; total: number }> {
  const wrapped = await apiRequest<unknown>(`/v1/sites?page=${page}&page_size=${pageSize}`);
  const data = unwrapData<JsonObject>(wrapped);
  const items: SiteItem[] = Array.isArray(data.items)
    ? (data.items as JsonObject[]).map((r) => ({ ...toSiteItem(r), origin: 'cloud' as const }))
    : [];
  const pagination = (data.pagination ?? {}) as JsonObject;
  const total = Number(pagination.total_items ?? items.length);
  return { items: await withLocalSiteSources(items), total };
}

export async function updateSite(
  siteId: string,
  data: {
    title?: string;
    visibility?: SiteVisibility;
    slug?: string;
    description?: string;
  } & SiteUpdateEditionFields,
  origin?: 'cloud' | 'local',
): Promise<SiteItem> {
  const wrapped = await apiRequest<unknown>(`/v1/sites/${encodeURIComponent(siteId)}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  }, siteTarget(origin));
  return { ...toSiteItem(unwrapData<JsonObject>(wrapped)), origin };
}

/** 设置 / 修改站点访问密码（仅站点管理者）。 */
export async function setSitePassword(
  siteId: string,
  password: string,
  origin?: 'cloud' | 'local',
): Promise<SiteItem> {
  const wrapped = await apiRequest<unknown>(`/v1/sites/${encodeURIComponent(siteId)}/password`, {
    method: 'PUT',
    body: JSON.stringify({ password }),
  }, siteTarget(origin));
  return { ...toSiteItem(unwrapData<JsonObject>(wrapped)), origin };
}

/** 清除站点访问密码，站点回到"凭链接直接访问"。 */
export async function clearSitePassword(
  siteId: string,
  origin?: 'cloud' | 'local',
): Promise<SiteItem> {
  const wrapped = await apiRequest<unknown>(`/v1/sites/${encodeURIComponent(siteId)}/password`, {
    method: 'DELETE',
  }, siteTarget(origin));
  return { ...toSiteItem(unwrapData<JsonObject>(wrapped)), origin };
}

export async function deleteSite(siteId: string, origin?: 'cloud' | 'local'): Promise<void> {
  await apiRequest(`/v1/sites/${encodeURIComponent(siteId)}`, { method: 'DELETE' }, siteTarget(origin));
}

export interface SiteVersionItem {
  version: number;
  file_count: number;
  total_size_bytes: number;
  created_at: string;
}

export async function getSiteDetail(
  siteId: string, origin?: 'cloud' | 'local',
): Promise<SiteItem & { versions: SiteVersionItem[] }> {
  const wrapped = await apiRequest<unknown>(
    `/v1/sites/${encodeURIComponent(siteId)}`, undefined, siteTarget(origin),
  );
  const data = unwrapData<JsonObject>(wrapped);
  return {
    ...toSiteItem(data),
    origin,
    versions: Array.isArray(data.versions) ? (data.versions as SiteVersionItem[]) : [],
  };
}

export async function rollbackSite(
  siteId: string, version: number, origin?: 'cloud' | 'local',
): Promise<SiteItem> {
  const wrapped = await apiRequest<unknown>(`/v1/sites/${encodeURIComponent(siteId)}/rollback`, {
    method: 'POST',
    body: JSON.stringify({ version }),
  }, siteTarget(origin));
  return { ...toSiteItem(unwrapData<JsonObject>(wrapped)), origin };
}
