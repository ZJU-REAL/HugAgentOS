import {apiRequest,authFetch,getApiUrl, type RequestTarget} from '../api';
import {useDeploymentModeStore} from '../stores/deploymentModeStore';
import {originHeaders, requestOrigin, requireSource,type WorkbenchSource,type WorkbenchOrigin} from './sourceIdentity';
let hostedOrigin:WorkbenchOrigin | undefined;
/** Set only after validating the authenticated primary directory; no URL guessing. */
export function setHostedWorkbenchOrigin(origin?:WorkbenchOrigin) { hostedOrigin=origin; }
export class WorkbenchSourceError extends Error {
  readonly source:WorkbenchSource;
  readonly code:string;
  constructor(source:WorkbenchSource,code:string) { super(source + ':' + code); this.source=source; this.code=code; }
}

/** No retry to another origin. Callers keep each failure scoped to its source. */
function requestOptions(path:string,options?:RequestInit,target?:RequestTarget) {
  const origin = requestOrigin(path);
  const selected = origin?.kind ?? target;
  if (origin && target && origin.kind !== target) throw new WorkbenchSourceError(origin.kind,'origin_mismatch');
  if (selected) {
    const mode=useDeploymentModeStore.getState();
    const hostedLocal=!mode.isDesktop && selected==='local' && hostedOrigin?.kind==='local'
      && (!origin || origin.id===hostedOrigin.id);
    try { if(!hostedLocal)requireSource(selected, mode); }
    catch(error) { throw new WorkbenchSourceError(selected,error instanceof Error && error.message.includes('execution_plane_not_ready')
      ? 'execution_plane_not_ready' : 'source_not_supported'); }
  }
  const normalized = new Headers(options?.headers);
  const headers = Object.fromEntries(normalized.entries());
  // The legacy JSON client spreads a Record with its canonical Content-Type
  // default. Use that exact key so any caller value replaces it once.
  const contentType = normalized.get('content-type');
  delete headers['content-type'];
  if (contentType !== null) headers['Content-Type'] = contentType;
  // Caller headers cannot override an application's validated source.
  return {selected, options:{...options,headers:{...headers,...(origin?originHeaders(origin):selected?{'x-hugagent-target':selected}:{})}}};
}
export async function workbenchFetch(path:string,options?:RequestInit,target?:RequestTarget) {
 const selected=requestOptions(path,options,target);
 return authFetch(getApiUrl()+path,selected.options);
}
export async function workbenchRequest<T>(path:string,options?:RequestInit,target?:RequestTarget):Promise<T> {
 const {selected,options:validated}=requestOptions(path,options,target);
 try { return await apiRequest<T>(path,validated,selected); } catch(error) {
    if(error instanceof TypeError && selected)throw new WorkbenchSourceError(selected,'origin_unavailable');
    throw error;
  }
}
export function preferenceSource(): RequestTarget {
  const mode = useDeploymentModeStore.getState();
  const source = mode.isDesktop && ['local_only','dual'].includes(mode.provisionMode) ? 'local' :
    !mode.isDesktop && hostedOrigin?.kind==='local' ? 'local' : 'cloud';
  if(mode.isDesktop || source!=='local')requireSource(source, mode);
  return source;
}
