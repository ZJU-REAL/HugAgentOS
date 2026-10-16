import { navigateTo } from '../routing/navigation';
export const APPLICATION_PATTERN = '/apps/:applicationKey/*';
export interface WorkspaceRoute { path: string; requestId: string }
const keyPattern = /^(builtin|external|plugin):[^/\\?#\s]{1,240}$/;
/** Decode pathname bytes before validation; encoded URL delimiters cannot become suffixes. */
export function ownApplicationPath(value: unknown): string {
  if (typeof value !== 'string' || value.length > 4096 || !value.startsWith('/') || value.startsWith('//')
      || /[\\]/.test(value) || [...value].some(char => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)) throw new Error('invalid_navigation');
  const index = value.search(/[?#]/);
  const pathname = index < 0 ? value : value.slice(0, index);
  const suffix = index < 0 ? '' : value.slice(index);
  const parts = pathname.slice(1).split('/').map(part => {
    let decoded: string;
    try { decoded = decodeURIComponent(part.replace(/%(?![0-9a-f]{2})/gi, '%25')); } catch { throw new Error('invalid_navigation'); }
    if (decoded === '.' || decoded === '..' || /[/\\?#]/.test(decoded) || [...decoded].some(char => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)
        || /%(?:25)*(?:2e|2f|5c|3f|23)/i.test(decoded)) throw new Error('invalid_navigation');
    return decoded;
  });
  if (parts[0] === 'apps') throw new Error('invalid_navigation');
  return '/' + parts.join('/') + suffix;
}
export function pathForApplication(key: string, path = '/'): string {
  if (!keyPattern.test(key)) throw new Error('invalid_application_key');
  const safe = ownApplicationPath(path);
  const index = safe.search(/[?#]/);
  const pathname = index < 0 ? safe : safe.slice(0, index);
  const suffix = index < 0 ? '' : safe.slice(index);
  return '/apps/' + encodeURIComponent(key) + pathname.split('/').map(encodeURIComponent).join('/') + suffix;
}
export function applicationRoute(pathname: string, search = '', hash = '') {
  const match = /^\/apps\/([^/]+)(\/.*)?$/.exec(pathname);
  if (!match) return null;
  try {
    const applicationKey = decodeURIComponent(match[1]);
    if (!keyPattern.test(applicationKey)) return null;
    return { applicationKey, path: ownApplicationPath((match[2] || '/') + search + hash) };
  } catch { return null; }
}
export function openWorkspaceApplication(key: string, path = '/', replace = false, requestId?: string) {
  const to = pathForApplication(key, path);
  const same = typeof window !== 'undefined' && to === window.location.pathname + window.location.search + window.location.hash;
  navigateTo(to, { replace: replace || same, state: requestId ? {workspaceRequestId:requestId} : undefined });
}
