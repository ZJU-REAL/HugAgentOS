/** Opaque origin IDs and exact install keys contain no URL or private path. */
export type WorkbenchSource = 'local' | 'cloud';
export interface WorkbenchOrigin { kind: WorkbenchSource; id: string }
export interface SourceMode { isDesktop: boolean; provisionMode: string; localReady: boolean }
export interface SourceFailure { source: WorkbenchSource; code: string }
export function sourceTargets(mode: SourceMode): WorkbenchSource[] {
  if (!mode.isDesktop) return ['cloud'];
  if (mode.provisionMode === 'local_only') return ['local'];
  return mode.provisionMode === 'dual' ? ['cloud', 'local'] : ['cloud'];
}
export function requireSource(source: WorkbenchSource, mode: SourceMode) {
  if (!sourceTargets(mode).includes(source)) throw new Error(source + ':source_not_supported');
  if (source === 'local' && mode.provisionMode === 'dual' && !mode.localReady)
    throw new Error('local:execution_plane_not_ready');
}
export function validOrigin(value: unknown): value is WorkbenchOrigin {
  const candidate = value as WorkbenchOrigin | null;
  return !!candidate && ['local','cloud'].includes(candidate.kind)
    && typeof candidate.id === 'string' && /^[0-9a-f]{32}$/.test(candidate.id);
}
/** WB07 SHA256 and UTC microseconds stay exact inside the source namespace. */
export function applicationOrigin(key: string): WorkbenchOrigin | undefined {
  const match = /^plugin:(local|cloud):([0-9a-f]{32}):[0-9a-f]{64}:[0-9]{8}T[0-9]{6}\.[0-9]{6}Z:[a-zA-Z0-9_.-]{1,64}$/.exec(key);
  return match ? {kind: match[1] as WorkbenchSource, id: match[2]} : undefined;
}
export function requestOrigin(path: string): WorkbenchOrigin | undefined {
  if (!path.startsWith('/v1/workbench/')) return undefined;
  // Upgrade routes address installation IDs, not application keys. Their caller
  // selects the source; the backend validates installation ownership.
  if (/^\/v1\/workbench\/(?:admin\/)?plugin-upgrades\/[^/?#]+(?:\/decision)?(?:[?#]|$)/.test(path))
    return undefined;
  for (const component of path.split(/[/?#]/)) {
    let key: string;
    try { key = decodeURIComponent(component); } catch { throw new Error('invalid_application_key'); }
    const origin = applicationOrigin(key);
    if (origin) return origin;
    if(key.startsWith('plugin:')) throw new Error('invalid_application_key');
  }
  return undefined;
}
export function originHeaders(origin: WorkbenchOrigin) {
  return {'x-hugagent-target': origin.kind, 'x-workbench-origin': origin.id};
}
export function packageAssetPath(path: string, key: string): string {
  const origin = applicationOrigin(key);
  if (!origin) throw new Error('invalid_application_key');
  if (!path.startsWith('/v1/workbench/applications/' + encodeURIComponent(key) + '/packages/'))
    throw new Error('package_binding_mismatch');
  return path + (path.includes('?') ? '&' : '?')
    + 'hg_target=' + origin.kind + '&hg_origin=' + origin.id;
}
export function sourceLabel(origin?: WorkbenchOrigin): string {
  return origin?.kind === 'local' ? '本机' : origin?.kind === 'cloud' ? '云端' : '';
}
