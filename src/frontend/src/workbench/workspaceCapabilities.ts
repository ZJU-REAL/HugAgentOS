import type {WorkspaceExtension} from './workspaceBridge';
/** Effective workspace methods retain the module's privileged capability boundary. */
export function workspaceCapabilities(merged:WorkspaceExtension,grants:readonly string[],
 hostExtension?:WorkspaceExtension):WorkspaceExtension {
 const declared=new Set(grants);
 // This method only changes the current app's URL. The trusted runtime owns
 // its namespace/binding checks; cross-app navigation remains a granted method.
 const ownNavigation=hostExtension?.capabilities?.includes('workspace.navigate')
  && typeof hostExtension.handlers?.['workspace.navigate']==='function';
 const capabilities=(merged.capabilities??[]).filter(capability=>capability==='workspace.navigate'
  ?ownNavigation
  :declared.has(capability)
   || (['resource.open','resource.lease','resource.close'].includes(capability)&&declared.has('resource.attach')));
 const handlers=Object.fromEntries(Object.entries(merged.handlers??{}).filter(([method])=>capabilities.includes(method)));
 return {...merged,capabilities,handlers};
}
