import type {ApplicationDeclaration} from '@hugagent/workbench-protocol';
export type WorkspaceView = NonNullable<ApplicationDeclaration['views']>[number];
/** One URL match drives both module selection and the host's secondary navigation. */
export function selectedWorkspaceView<T extends Pick<WorkspaceView,'path'>>(views:readonly T[],path:string):T|undefined {
 const pathname=path.split(/[?#]/)[0];
 return [...views].sort((a,b)=>b.path.length-a.path.length).find(view=>{
  const prefix='/'+view.path.replace(/^\/+|\/+$/g,'');
  return pathname===prefix || (prefix!=='/' && pathname.startsWith(prefix+'/'));
 });
}
