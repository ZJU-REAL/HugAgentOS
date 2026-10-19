import { authFetch, unwrapData, getApiUrl, isLocalChat, isLocalProject, maybeLocalizeUrl } from '../api';
import { useDeploymentModeStore } from '../stores/deploymentModeStore';
import { t } from '../i18n';

export type ArtifactOrigin = 'local' | 'cloud';
export interface ArtifactLocation {
  file_id?: string;
  url?: string;
  download_url?: string;
  origin?: ArtifactOrigin;
  chat_id?: string;
  project_id?: string;
}

function sourceUrl(file: ArtifactLocation): string {
  return file.url || file.download_url || (file.file_id ? `/files/${encodeURIComponent(file.file_id)}` : '');
}

export function isArchiveMember(file: ArtifactLocation): boolean {
  return /\/archive\/member(?:[/?]|$)/.test(sourceUrl(file));
}

function externalUrl(url: string): boolean {
  return /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(url);
}

/** Explicit file ownership wins over its containing conversation (e.g. a cloud MCP result). */
export function artifactOrigin(file: ArtifactLocation): ArtifactOrigin {
  const mode = useDeploymentModeStore.getState();
  if (!mode.isDesktop) return 'cloud';
  if (file.origin) return file.origin;
  const url = sourceUrl(file);
  if (externalUrl(url)) return 'cloud';
  const parsed = new URL(url || '/', 'http://artifact.invalid');
  if (parsed.searchParams.get('hg_target') === 'local') return 'local';
  if (mode.provisionMode === 'local_only' || mode.activeLocal) return 'local';
  if (mode.provisionMode !== 'dual') return 'cloud';
  return isLocalChat(file.chat_id) || isLocalProject(file.project_id)
    || maybeLocalizeUrl(url) !== url ? 'local' : 'cloud';
}

/** Build path before query/fragment, and keep routing on every embedded media request. */
export function artifactUrl(
  file: ArtifactLocation,
  options: { preview?: boolean; params?: Record<string, string> } = {},
): string {
  const source = sourceUrl(file);
  if (!source) return '';
  let parsedSource: URL;
  try { parsedSource = new URL(source, 'http://artifact.invalid'); }
  catch { return ''; }
  if (parsedSource.pathname.startsWith('/__desktop/')) return '';
  const base = getApiUrl().replace(/\/$/, '');
  if (externalUrl(source)) {
    // Our API may be configured as an absolute URL; its file links still need
    // inline/preview and desktop routing options. Other hosts remain external.
    const api = new URL(base || '/', 'http://artifact.invalid');
    const apiPrefix = api.pathname.replace(/\/$/, '') + '/';
    const ownApi = externalUrl(base) && parsedSource.origin === api.origin
      && parsedSource.pathname.startsWith(apiPrefix);
    if (!ownApi) return /^(https?:)?\/\//i.test(source) ? source : '';
  }
  if (!/^(?:\/api)?\/(?:files\/|v1\/projects\/|v1\/myspace\/content)/.test(parsedSource.pathname)) return '';
  const raw = source.startsWith(`${base}/`) ? source : `${base}${source.startsWith('/') ? '' : '/'}${source}`;
  const url = new URL(raw, 'http://artifact.invalid');
  if (options.preview) url.pathname += '/preview';
  if (useDeploymentModeStore.getState().provisionMode === 'dual' && artifactOrigin(file) === 'local') {
    url.searchParams.set('hg_target', 'local');
  } else {
    url.searchParams.delete('hg_target');
  }
  for (const [key, value] of Object.entries(options.params || {})) url.searchParams.set(key, value);
  return /^https?:\/\//i.test(raw) ? url.toString() : url.pathname + url.search + url.hash;
}

export function withUrlParams(source: string, params: Record<string, string>): string {
  const url = new URL(source, 'http://artifact.invalid');
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
  return externalUrl(source) ? url.toString() : url.pathname + url.search + url.hash;
}

export function localArtifactLocationUrl(file: ArtifactLocation): string {
  const source = new URL(sourceUrl(file), 'http://artifact.invalid');
  const projectFile = source.pathname.match(/^(?:\/api)?(\/v1\/projects\/[^/]+\/local-files)\/raw$/);
  return projectFile
    ? artifactUrl({ ...file, url: `${projectFile[1]}/location?path=${encodeURIComponent(source.searchParams.get('path') || '')}` })
    : file.file_id
      ? artifactUrl({ ...file, url: `/files/${encodeURIComponent(file.file_id)}/local-path` })
      : '';
}

/** Only the authenticated local backend may resolve an artifact to an OS path. */
export async function openLocalArtifact(
  file: ArtifactLocation,
  folder = false,
  navigate: (url: string) => void = (url) => { window.location.href = url; },
): Promise<void> {
  if (isArchiveMember(file)) throw new Error(t('包内文件请下载后打开'));
  if (artifactOrigin(file) !== 'local') throw new Error(t('此文件不在本机'));
  const locationUrl = localArtifactLocationUrl(file);
  if (!locationUrl) throw new Error(t('缺少文件标识'));
  const response = await authFetch(locationUrl);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || payload.message || t('无法打开文件'));
  const data = unwrapData<{ path?: string; folder_path?: string }>(payload);
  const path = folder ? data.folder_path : data.path;
  if (typeof path !== 'string' || !path) throw new Error(t('无法获取本机文件路径'));
  navigate('/__desktop/open-path?path=' + encodeURIComponent(path));
}
