import type {WorkspaceExtension} from './workspaceBridge';
/** Only trusted host modules supply handlers. Duplicate authority is a composition error. */
export function combineExtensions(...extensions:WorkspaceExtension[]):WorkspaceExtension {
 const handlers=Object.create(null) as NonNullable<WorkspaceExtension['handlers']>;
 const capabilities=new Set<string>();
 for(const extension of extensions){
  for(const [method,handler] of Object.entries(extension.handlers??{})){
   if(Object.hasOwn(handlers,method)||['__proto__','prototype','constructor'].includes(method))throw new Error('duplicate_handler');
   handlers[method]=handler;
  }
  extension.capabilities?.forEach(value=>capabilities.add(value));
 }
 return {handlers,capabilities:[...capabilities],reset:()=>extensions.forEach(extension=>extension.reset?.())};
}
