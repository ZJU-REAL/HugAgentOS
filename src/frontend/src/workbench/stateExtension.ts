import {workbenchRequest as apiRequest} from './sourceRequest';
import type {InstanceIdentity} from './lifecycle/instancePool';
export interface ApplicationSnapshot {
 revision:number; schemaVersion:string|null; payload:Record<string,unknown>|null; resetRequired:boolean;
}
/** Structural WB09 extension. Context comes only from the authenticated launch. */
export interface StateExtension {
 capabilities:string[];
 handlers:Record<string,(params:unknown,signal:AbortSignal)=>unknown|Promise<unknown>>;
}
function object(value:unknown):Record<string,unknown>{
 if(!value || typeof value!=='object' || Array.isArray(value))throw new Error('invalid_state');
 return value as Record<string,unknown>;
}
function fields(value:Record<string,unknown>,allowed:string[]){
 if(Object.keys(value).some(key=>!allowed.includes(key)))throw new Error('invalid_state');
}
export function createStateExtension(context:InstanceIdentity,stateSchemaVersion:string|undefined,
 setDirty:(dirty:boolean)=>void,isOpen:()=>boolean,instanceId:string):StateExtension {
 const query=new URLSearchParams({workspace:context.workspace,packageRevision:context.packageRevision,installId:context.installId,origin:context.origin,executionScope:context.executionScope,instanceId});
 if(stateSchemaVersion)query.set('schemaVersion',stateSchemaVersion);
 const path='/v1/workbench/me/application-state/'+encodeURIComponent(context.applicationKey)+'?'+query;
 async function request(method:string,body:unknown,signal:AbortSignal){
  if(signal.aborted)throw new Error('cancelled');
  if(!isOpen())throw new Error('session_closed');
  if(!stateSchemaVersion)throw new Error('state_unsupported');
  const response=await apiRequest<{data:ApplicationSnapshot}>(path,{method,signal,headers:{'x-workbench-origin':context.origin},...(body?{body:JSON.stringify(body)}:{})},context.executionScope as 'local'|'cloud');
  if(signal.aborted || !isOpen())throw new Error('session_closed');
  return response.data;
 }
 return {capabilities:['lifecycle.setDirty',...(stateSchemaVersion?['state.load','state.save']:[])],handlers:{
  'lifecycle.setDirty':(params,signal)=>{
   const value=object(params);fields(value,['dirty']);
   if(signal.aborted)throw new Error('cancelled');
   if(!isOpen())throw new Error('session_closed');
   if(typeof value.dirty!=='boolean')throw new Error('invalid_state');
   setDirty(value.dirty);return {dirty:value.dirty};
  },
  'state.load':(params,signal)=>{fields(object(params),[]);return request('GET',undefined,signal);},
  'state.save':(params,signal)=>{
   const value=object(params);fields(value,['revision','schemaVersion','payload','reset']);
   if(!Number.isSafeInteger(value.revision) || (value.revision as number)<0 ||
    value.schemaVersion!==stateSchemaVersion || (value.reset!==undefined && typeof value.reset!=='boolean'))throw new Error('invalid_state');
   object(value.payload);
   return request('PUT',value,signal);
  },
 }};
}
